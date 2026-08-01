"""The public API, at its real paths (api-design.md).

Group 9 owns this surface in full — the complete error catalog, cursor
pagination, SSE. What lands here is what **groups 4 and 5 fix**, because their
own tasks specify an HTTP contract: 5.4b names ``201`` / ``200 +
idempotent_replay`` / ``422`` and the ``Idempotent-Replayed`` header, and the
claim being graded is user-scoped by construction. Building these against a dev
route with no authentication would have tested a weaker property than the one
that ships.

So: real paths, real ``X-API-Key`` auth, real status codes — and the ``/dev``
routes they replace are gone, so there is only ever one way in. Tasks 9.1 and
9.3 are **partial**, not done.

``POST /auth/dev-login`` is the one unauthenticated route, and it is the PoC
sign-in the brief allows. It also provisions the fake connections this phase
sends through; phase 3 replaces it with the OAuth flow in group 6.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from core.adapters import orchestrator, registry
from core.adapters.live import configured_sources
from core.connections import service as connections_service
from core.connections.oauth import OAuthConfigError
from core.connections.state import StateInvalid
from core.connections.store import ReconnectMismatch
from core.db import session_scope
from core.enums import ProviderKind
from core.security import api_keys
from core.send import service
from fastapi import APIRouter, Body, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from api.deps import CallerDep
from api.errors import ApiError

router = APIRouter(prefix="/v1", tags=["v1"])


# ---------------------------------------------------------------------------
# Sign-in and connections
# ---------------------------------------------------------------------------


class DevLoginRequest(BaseModel):
    email: str = "dev@example.test"
    name: str = "console"


#: The connections this phase pretends to have. Real rows in the real table with
#: a real natural key, so nothing downstream knows they are fake — which is the
#: point: group 6 replaces how these rows are *created* and touches nothing that
#: reads them.
FAKE_CONNECTIONS: tuple[tuple[ProviderKind, str, str], ...] = (
    (ProviderKind.GMAIL, "fake:gmail:dev", "dev@example.test (fake Gmail)"),
    (ProviderKind.SLACK, "fake:slack:dev", "Acme HQ (fake Slack)"),
)


@router.post("/auth/dev-login", status_code=201)
async def dev_login(body: DevLoginRequest) -> dict[str, Any]:
    """Issue an API key. The plaintext is returned once and never recoverable."""
    minted = api_keys.mint()
    async with session_scope() as session:
        user_id = await session.scalar(
            text(
                """
                INSERT INTO users (email) VALUES (:email)
                ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                RETURNING id
                """
            ),
            {"email": body.email},
        )
        await session.execute(
            text(
                """
                INSERT INTO api_keys (user_id, key_id, key_hash, prefix_display, name)
                VALUES (:user_id, :key_id, :key_hash, :prefix_display, :name)
                """
            ),
            {
                "user_id": user_id,
                "key_id": minted.key_id,
                "key_hash": minted.key_hash,
                "prefix_display": minted.prefix_display,
                "name": body.name,
            },
        )
        # 🔴 Only for providers that are NOT configured. In phase 2 these fake
        # rows were harmless scaffolding — every provider was a fake, so a fake
        # connection was an honest description of reality. Once a real OAuth
        # client exists they become lies: rows that claim to be a connection to
        # a provider they cannot possibly reach, which surface as a `failed`
        # source and a `failed_permanent` send that look like product bugs.
        #
        # A configured provider gets no fake row, so the only way to reach it is
        # a real grant — and a user with no grant gets the honest
        # `connection_needs_reconnect` refusal at draft time.
        configured = configured_sources()
        fakes_to_create = [
            row for row in FAKE_CONNECTIONS if not configured.get(row[0].value, False)
        ]
        for provider, external_id, display in fakes_to_create:
            await session.execute(
                text(
                    """
                    INSERT INTO connections (user_id, provider, external_account_id,
                                             display_name, status)
                    VALUES (:user_id, CAST(:provider AS provider_kind), :external_id,
                            :display, 'active')
                    ON CONFLICT ON CONSTRAINT connections_natural_key DO UPDATE
                        SET display_name = EXCLUDED.display_name
                    """
                ),
                {
                    "user_id": user_id,
                    "provider": provider.value,
                    "external_id": external_id,
                    "display": display,
                },
            )
        await session.commit()
    return {
        "key": minted.plaintext,
        "key_id": minted.key_id,
        "prefix_display": minted.prefix_display,
        "user_id": user_id,
    }


@router.get("/connections")
async def list_connections(caller: CallerDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, provider::text AS provider, display_name,
                           status::text AS status, last_error_detail,
                           granted_scopes, last_success_at
                      FROM connections WHERE user_id = :user_id ORDER BY id
                    """
                ),
                {"user_id": caller.user_id},
            )
        ).mappings().all()

    connections = []
    for row in rows:
        payload = dict(row)
        if payload["status"] == "needs_reconnect":
            # The fix, one click away, on the row that needs it — rather than a
            # generic error the customer has to translate into an action.
            payload["reconnect_url"] = connections_service.reconnect_url(
                int(payload["id"]), str(payload["provider"])
            )
        connections.append(payload)

    return {
        "connections": connections,
        # Which providers can be connected at all right now. The console renders
        # a "not configured" state from this rather than offering a Connect
        # button that leads to an OAuth error page.
        "available": [
            {
                "provider": provider,
                "configured": configured,
                "authorize_url": f"/v1/connections/{provider}/authorize" if configured else None,
                "rationale": connections_service.CONSENT_RATIONALE.get(provider, ""),
            }
            for provider, configured in sorted(configured_sources().items())
            if provider in {kind.value for kind in ProviderKind}
        ],
    }


@router.get("/connections/{provider}/authorize")
async def authorize_connection(
    caller: CallerDep,
    provider: str,
    reconnect: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    """Hand back the provider URL to send the browser to.

    JSON rather than a 302 because every route on this API is called with an
    ``X-API-Key`` header by a client that then navigates deliberately — and
    because a redirect would make the consent rationale unrenderable, which
    task 8.2d requires to appear in the connect flow rather than only in the
    README.
    """
    try:
        authorization = await connections_service.begin(
            user_id=caller.user_id,
            provider_name=provider,
            reconnect_connection_id=reconnect,
        )
    except OAuthConfigError as exc:
        # `config`, never `needs_reconnect`: a missing client id is our bug, and
        # telling the user to reconnect sends them round in circles fixing a
        # grant that was never broken (risks.md R24).
        raise ApiError(
            "provider_not_configured", str(exc), status=503, classification="config"
        ) from exc
    return {
        "authorize_url": authorization.url,
        "provider": authorization.provider,
        "rationale": authorization.rationale,
    }


@router.get("/connections/callback/{provider}")
async def connection_callback(
    provider: str,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The redirect target. **Unauthenticated by necessity** — a provider
    redirects a browser here — so everything it trusts comes out of the signed
    ``state`` rather than out of a query parameter.

    ⚠️ The registered redirect URI must match this path byte for byte: scheme,
    case and trailing slash all count, and there are no wildcards.
    """
    if error:
        raise ApiError(
            "authorization_denied",
            f"{provider} did not complete the authorization: {error}",
            status=400,
            classification="permanent",
        )
    if not code or not state:
        raise ApiError(
            "authorization_incomplete",
            "the callback carried no authorization code",
            status=400,
            classification="permanent",
        )

    try:
        row = await connections_service.complete(code=code, raw_state=state)
    except StateInvalid as exc:
        raise ApiError(
            "state_invalid", str(exc), status=400, classification="permanent"
        ) from exc
    except ReconnectMismatch as exc:
        # Never silently rebound: every draft and send references
        # `connection.id`, so repointing it at a different account would rewrite
        # the meaning of all of them.
        raise ApiError(
            "reconnect_account_mismatch", str(exc), status=409, classification="permanent"
        ) from exc
    except OAuthConfigError as exc:
        raise ApiError(
            "provider_not_configured", str(exc), status=503, classification="config"
        ) from exc

    return {
        "connection": {
            "id": row.id,
            "provider": row.provider,
            "display_name": row.display_name,
            "status": row.status,
            "granted_scopes": list(row.granted_scopes),
        }
    }


@router.delete("/connections/{connection_id}", status_code=200)
async def delete_connection(caller: CallerDep, connection_id: int) -> dict[str, Any]:
    """Drop the credentials, keep the history.

    A disconnect is not a delete: drafts and sends reference this row, and
    erasing it would erase the record of everything sent through it.
    """
    removed = await connections_service.disconnect(
        user_id=caller.user_id, connection_id=connection_id
    )
    if not removed:
        raise ApiError("not_found", "no such connection", status=404)
    return {"disconnected": connection_id}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class CreateSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    #: Omit for the ordinary fan-out. Naming sources is how the `fault:` adapters
    #: are reached, and how a demo pins the set being shown.
    sources: list[str] | None = None


@router.post("/searches", status_code=202)
async def create_search(caller: CallerDep, body: CreateSearchRequest) -> dict[str, Any]:
    """Fan out and return **immediately**. No adapter has run yet.

    That is not an optimisation: partial results are only meaningful if the
    response predates the work, and every source's status starts at `pending`
    because that is the truth at this instant.
    """
    async with session_scope() as session:
        try:
            plan = await orchestrator.plan_search(
                session, user_id=caller.user_id, query=body.query, sources=body.sources
            )
        except registry.UnknownSource as exc:
            raise ApiError("recipient_invalid", str(exc), status=422) from exc
        await session.commit()
    return {
        "search_id": str(plan.search_id),
        "query": plan.query,
        "sources": [
            {
                "source": run.source,
                "connection_id": run.connection_id,
                "status": "pending",
                "mode": run.mode,
                "result_count": 0,
            }
            for run in plan.runs
        ],
    }


@router.get("/searches")
async def list_searches(
    caller: CallerDep, limit: Annotated[int, Query(le=100, ge=1)] = 25
) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT s.id, s.query, s.is_seed, s.created_at, s.finished_at,
                           count(r.id) FILTER (WHERE r.status IN ('pending','running')) AS pending,
                           coalesce(sum(r.result_count), 0) AS result_count
                      FROM searches s LEFT JOIN adapter_runs r ON r.search_id = s.id
                     WHERE s.user_id = :user_id
                     GROUP BY s.id
                     ORDER BY s.created_at DESC
                     LIMIT :limit
                    """
                ),
                {"user_id": caller.user_id, "limit": limit},
            )
        ).mappings().all()
    return {
        "searches": [
            {
                "search_id": str(row["id"]),
                "query": row["query"],
                "is_seed": row["is_seed"],
                "created_at": row["created_at"],
                "finished": row["finished_at"] is not None,
                "result_count": int(row["result_count"]),
            }
            for row in rows
        ]
    }


@router.get("/searches/{search_id}")
async def get_search(
    caller: CallerDep, search_id: uuid.UUID, debug: Annotated[int, Query()] = 0
) -> dict[str, Any]:
    """The authoritative snapshot. Partial while sources are still running.

    Polling this is the primary progress path — the stream, when it exists, is
    an accelerator that carries nothing this does not (risks.md R8).
    """
    # Opens its own session at REPEATABLE READ — see `load_snapshot`.
    snapshot = await orchestrator.load_snapshot(
        search_id=search_id, user_id=caller.user_id
    )
    if snapshot is None:
        # 404 rather than 403 for another user's search: a 403 discloses that it
        # exists.
        raise ApiError("not_found", "no such search", status=404)

    payload: dict[str, Any] = {
        "search_id": str(snapshot.search_id),
        "query": snapshot.query,
        "is_seed": snapshot.is_seed,
        "finished": snapshot.finished,
        "sources": [_source_view(row) for row in snapshot.sources],
        "results": [result.as_public_dict() for result in snapshot.results],
    }
    if debug:
        # Outside `results`, always. A `score` key on a result would break the
        # closed shape the brief fixes (design D7).
        payload["ranking"] = snapshot.ranking
    return payload


def _source_view(row: dict[str, Any]) -> dict[str, Any]:
    """Per-source status, reported honestly.

    ``status: "failed"`` with ``result_count: 0`` and ``status: "done"`` with
    ``result_count: 0`` are different facts and clients must render them
    differently — a throttled source must never look like an empty one
    (risks.md R16). Everything needed to tell them apart is here.
    """
    view: dict[str, Any] = {
        "source": row["source"],
        "status": row["status"],
        "mode": row["mode"],
        "result_count": row["result_count"],
    }
    if row["error_class"] is not None:
        view["error"] = {
            "code": (
                "connection_needs_reconnect"
                if row["error_class"] == "needs_reconnect"
                else "provider_unavailable"
            ),
            "classification": row["error_class"],
            "message": row["error_detail"],
        }
        if row["error_class"] == "needs_reconnect" and row["connection_id"]:
            # Rendered as an action at the point of failure, never as a global
            # error — a dead end here is a dead end in the product.
            #
            # 🔴 Through the helper, not hand-rolled. This line used to build
            # `/v1/connections/{id}/reconnect` by hand, which is not a route and
            # answered 404 — so the one action that repairs a revoked grant, on
            # the surface where the user actually meets one, went nowhere. The
            # sentence above was true and the code below it was not.
            #
            # `source` is the provider name for any source that carries a
            # connection: `plan_search` attaches connections by matching
            # `connections.provider` to the source name, so a row with a
            # `connection_id` has them equal by construction.
            view["error"]["reconnect_url"] = connections_service.reconnect_url(
                int(row["connection_id"]), str(row["source"])
            )
    return view


# ---------------------------------------------------------------------------
# Drafts and the gate
# ---------------------------------------------------------------------------


class CreateDraftRequest(BaseModel):
    channel: ProviderKind
    to: str = Field(min_length=1)
    body: str = Field(min_length=1)
    subject: str | None = None
    connection_id: int | None = None
    in_reply_to_result_id: int | None = None


class PatchDraftRequest(BaseModel):
    to: str | None = None
    subject: str | None = None
    body: str | None = None


@router.post("/drafts", status_code=201)
async def create_draft(caller: CallerDep, body: CreateDraftRequest) -> dict[str, Any]:
    """Create a draft. **No provider call is made** — a draft is inert."""
    async with session_scope() as session:
        view = await service.create_draft(
            session,
            user_id=caller.user_id,
            channel=body.channel,
            recipient=body.to,
            body=body.body,
            subject=body.subject,
            connection_id=body.connection_id,
            in_reply_to_result_id=body.in_reply_to_result_id,
        )
        await session.commit()
    return view.as_dict()


@router.get("/drafts/{draft_id}")
async def get_draft(caller: CallerDep, draft_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        view = await service.get_draft(session, draft_id, user_id=caller.user_id)
    return view.as_dict()


@router.patch("/drafts/{draft_id}")
async def patch_draft(
    caller: CallerDep, draft_id: uuid.UUID, body: PatchDraftRequest
) -> dict[str, Any]:
    """Editing changes ``confirm_sha256``, invalidating any digest already held."""
    async with session_scope() as session:
        view = await service.update_draft(
            session,
            draft_id,
            user_id=caller.user_id,
            recipient=body.to,
            subject=body.subject,
            body=body.body,
        )
        await session.commit()
    return view.as_dict()


class SendRequestBody(BaseModel):
    #: REQUIRED. Absent is a 422 `confirmation_required`, which is the whole
    #: point: there is no path to a provider that does not pass through content
    #: the caller has demonstrably seen.
    confirmed_sha256: str | None = None


@router.post("/drafts/{draft_id}/send")
async def send_draft(
    caller: CallerDep,
    draft_id: uuid.UUID,
    response: Response,
    # Optional at the transport layer so that "no body at all" reaches the gate
    # as a `confirmation_required` refusal rather than as FastAPI's own generic
    # validation error. The refusal *is* the product behaviour being demonstrated
    # and it has to be legible as such.
    body: Annotated[SendRequestBody | None, Body()] = None,
) -> dict[str, Any]:
    """**The gate.** Idempotent by the draft-carried key.

    A duplicate returns ``200`` with the current state and
    ``idempotent_replay: true`` — never ``409``. See ``core.send.service`` for
    why that deviation from Stripe is deliberate.
    """
    async with session_scope() as session:
        outcome = await service.send_draft(
            session,
            draft_id,
            user_id=caller.user_id,
            confirmed_sha256=body.confirmed_sha256 if body else None,
        )
        await session.commit()
    response.status_code = outcome.status
    response.headers["Idempotent-Replayed"] = "true" if outcome.idempotent_replay else "false"
    return outcome.payload


# ---------------------------------------------------------------------------
# Send history
# ---------------------------------------------------------------------------


@router.get("/sends")
async def list_sends(
    caller: CallerDep,
    limit: Annotated[int, Query(le=100, ge=1)] = 25,
    include_seed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    async with session_scope() as session:
        sends = await service.list_sends(
            session, user_id=caller.user_id, limit=limit, include_seed=include_seed
        )
    return {"sends": sends}


@router.get("/sends/{send_id}")
async def get_send(caller: CallerDep, send_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        return await service.send_detail(session, send_id, user_id=caller.user_id)


@router.post("/sends/{send_id}/retry")
async def retry_send(caller: CallerDep, send_id: uuid.UUID) -> dict[str, Any]:
    """Operator retry, under the original key.

    Retrying a ``delivered`` send is a no-op that returns the original result.
    Retrying an ``uncertain`` one is refused — that state needs a decision, not
    a repeat.
    """
    async with session_scope() as session:
        payload = await service.operator_retry(session, send_id, user_id=caller.user_id)
        await session.commit()
    return payload
