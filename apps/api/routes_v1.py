"""The public API, at its real paths (api-design.md).

**Every route is reachable with an API key and there is no browser-session
path.** That is what makes "the UI is a pure consumer" structurally true rather
than an assertion to audit (design D10) — and ``test_route_surface.py``
enumerates this module and proves it, allowing exactly one exception by name:
the OAuth callback, which a *provider* redirects a browser to and which
therefore cannot carry a header. Everything it trusts comes out of the signed
``state``, never a query parameter.

Groups 4, 5 and 6 built the first two thirds of this surface, because their own
tasks specified an HTTP contract: 5.4b names ``201`` / ``200 +
idempotent_replay`` / ``422`` and the ``Idempotent-Replayed`` header, and the
claim being graded is user-scoped by construction. Group 9 completed it — key
management, the results and rerun routes, SSE, resolution of an in-doubt send,
cursor paging, and the error catalog every refusal now looks its status up in.

``POST /auth/dev-login`` is the PoC sign-in the brief allows. 🔴 It provisions a
fake connection **only for a provider that is not configured**: once a real
OAuth client exists, a fake row is a lie that surfaces as a `failed` source and
a `failed_permanent` send, and reads as a product bug.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from core.adapters import orchestrator, registry
from core.adapters.live import configured_sources
from core.connections import oauth, scopes, store
from core.connections import service as connections_service
from core.connections.oauth import OAuthConfigError
from core.connections.state import StateInvalid
from core.connections.store import ReconnectMismatch
from core.db import session_scope
from core.enums import ProviderKind
from core.security import api_keys
from core.send import service
from fastapi import APIRouter, Body, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from api import paging, schemas
from api.deps import CallerDep
from api.errors import ApiError

router = APIRouter(prefix="/v1", tags=["v1"])

#: How long the SSE generator waits between database reads, and how often it
#: writes a comment line when nothing has changed. The heartbeat exists so an
#: idle proxy does not reap a connection that is working correctly.
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
#: A hard ceiling on one stream. The snapshot is the primary path and the client
#: falls back to it on any error, so a stream that has run this long has stopped
#: being an accelerator and started being an idle connection we are paying for.
SSE_MAX_SECONDS = 300.0


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
        # 🔴 And retire the ones already there. The guard above only stops us
        # *creating* a lie; it does nothing about the rows written before the
        # OAuth client existed, which keep claiming `active` for a provider they
        # cannot reach. That was live on the **default sign-in identity**, so it
        # was the first thing a reviewer met: two connected-looking accounts, a
        # search that fails, a send that fails permanently, and a reconnect that
        # dead-ended on `reconnect_account_mismatch`.
        #
        # Retired rather than deleted where anything references them — drafts,
        # sends and adapter runs point at `connection.id`, and erasing the row
        # would erase the record of what was attempted through it. Marked
        # `needs_reconnect`, which is the state that renders as an *action*; the
        # re-grant now adopts the placeholder (`store._adopt_placeholder`), so
        # the action leads somewhere.
        retire = [
            provider.value
            for provider, _, _ in FAKE_CONNECTIONS
            if configured.get(provider.value, False)
        ]
        if retire:
            await session.execute(
                text(
                    """
                    DELETE FROM connections c
                     WHERE c.user_id = :user_id
                       AND c.provider::text = ANY(:providers)
                       AND c.external_account_id LIKE :placeholder
                       AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.connection_id = c.id)
                       AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.connection_id = c.id)
                       AND NOT EXISTS (
                             SELECT 1 FROM adapter_runs r WHERE r.connection_id = c.id)
                    """
                ),
                {
                    "user_id": user_id,
                    "providers": retire,
                    "placeholder": f"{store.PLACEHOLDER_PREFIX}%",
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE connections
                       SET status = 'needs_reconnect',
                           last_error_class = 'needs_reconnect',
                           last_error_detail = 'this account was a stand-in from '
                               'before a real OAuth client was configured, so it '
                               'holds no credentials — connecting for real '
                               'replaces it in place and keeps its history',
                           updated_at = now()
                     WHERE user_id = :user_id
                       AND provider::text = ANY(:providers)
                       AND external_account_id LIKE :placeholder
                       AND status <> 'needs_reconnect'
                    """
                ),
                {
                    "user_id": user_id,
                    "providers": retire,
                    "placeholder": f"{store.PLACEHOLDER_PREFIX}%",
                },
            )
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


# ---------------------------------------------------------------------------
# API keys (task 9.2)
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    name: str = Field(default="", max_length=100)


@router.get("/api-keys")
async def list_api_keys(caller: CallerDep) -> dict[str, Any]:
    """Own keys, by prefix only.

    ``prefix_display`` is enough to recognise a key in a list and never enough to
    use one. There is no route that returns a stored key, because there is no
    stored key to return — only an unsalted SHA-256 of it.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT key_id, prefix_display, name, created_at, last_used_at,
                           expires_at, revoked_at
                      FROM api_keys WHERE user_id = :user_id ORDER BY id DESC
                    """
                ),
                {"user_id": caller.user_id},
            )
        ).mappings().all()
    return {
        "api_keys": [
            # `current` so a console can warn before someone revokes the key they
            # are holding. Revoking it is allowed — it is their key — but doing it
            # by accident and losing the session is not a useful surprise.
            dict(row) | {"current": row["key_id"] == caller.key_id}
            for row in rows
        ]
    }


@router.post("/api-keys", status_code=201)
async def create_api_key(caller: CallerDep, body: CreateKeyRequest) -> dict[str, Any]:
    """Mint a key. **The plaintext is returned once and is never recoverable.**"""
    minted = api_keys.mint()
    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO api_keys (user_id, key_id, key_hash, prefix_display, name)
                VALUES (:user_id, :key_id, :key_hash, :prefix_display, :name)
                """
            ),
            {
                "user_id": caller.user_id,
                "key_id": minted.key_id,
                "key_hash": minted.key_hash,
                "prefix_display": minted.prefix_display,
                "name": body.name,
            },
        )
        await session.commit()
    return {
        "key": minted.plaintext,
        "key_id": minted.key_id,
        "prefix_display": minted.prefix_display,
        "name": body.name,
    }


@router.delete("/api-keys/{key_id}", status_code=200)
async def revoke_api_key(caller: CallerDep, key_id: str) -> dict[str, Any]:
    """Revoke, by stamping ``revoked_at``. The row stays.

    ``api.deps.require_api_key`` reads that column on every request, so a revoked
    key stops working immediately — and it fails with the *same* 401 as a key
    that never existed, because telling a prober which of the two they hold is a
    free oracle.

    Deleting the row instead would lose ``last_used_at``, which is the only
    evidence of what a leaked key was used for between the leak and the
    revocation.
    """
    async with session_scope() as session:
        revoked = await session.scalar(
            text(
                """
                UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now())
                 WHERE key_id = :key_id AND user_id = :user_id
                RETURNING key_id
                """
            ),
            {"key_id": key_id, "user_id": caller.user_id},
        )
        await session.commit()
    if revoked is None:
        # 404 for another user's key, exactly as for every other resource: a 403
        # would confirm that the key id exists.
        raise ApiError("not_found", "no such API key")
    return {"revoked": key_id}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


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
        # The scope pre-flight (task 6.3b). A connection missing a permission is
        # otherwise indistinguishable from a healthy one until a call fails with
        # `missing_scope` — or with `not_allowed_token_type`, which names nothing
        # and reads like our bug. Reported, never enforced: see
        # `core.connections.scopes` for why a wrong hint costs a banner and a
        # wrong gate costs the connection.
        provider = oauth.PROVIDERS.get(str(payload["provider"]))
        if provider is not None:
            gaps = scopes.missing_scopes(provider, tuple(payload["granted_scopes"] or ()))
            payload["missing_scopes"] = list(gaps)
            payload["scopes_ok"] = not gaps
            if gaps and payload["status"] != "needs_reconnect":
                # Re-granting is the fix, so offer the same action here as a
                # revoked grant gets — the difference is only the copy.
                payload["reconnect_url"] = connections_service.reconnect_url(
                    int(payload["id"]), str(payload["provider"])
                )
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
        raise ApiError("provider_not_configured", str(exc)) from exc
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
        )
    if not code or not state:
        raise ApiError(
            "authorization_incomplete",
            "the callback carried no authorization code",
        )

    try:
        row = await connections_service.complete(code=code, raw_state=state)
    except StateInvalid as exc:
        raise ApiError("state_invalid", str(exc)) from exc
    except ReconnectMismatch as exc:
        # Never silently rebound: every draft and send references
        # `connection.id`, so repointing it at a different account would rewrite
        # the meaning of all of them.
        raise ApiError("reconnect_account_mismatch", str(exc)) from exc
    except OAuthConfigError as exc:
        raise ApiError("provider_not_configured", str(exc)) from exc

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
        raise ApiError("not_found", "no such connection")
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
            raise ApiError("recipient_invalid", str(exc)) from exc
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
    caller: CallerDep,
    limit: Annotated[int, Query(le=paging.MAX_LIMIT, ge=1)] = paging.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    include_seed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """History, newest first, keyset-paged (api-design.md §Conventions).

    ``include_seed=false`` is what makes "show me only what I actually did"
    answerable. Seed rows exist so the product demos from a cold start; a
    listing with no way to exclude them makes that a cost rather than a feature.
    """
    before = paging.decode(cursor) if cursor else None
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
                       AND (:include_seed OR NOT s.is_seed)
                       AND (CAST(:cursor_at AS timestamptz) IS NULL
                            OR (s.created_at, s.id)
                               < (CAST(:cursor_at AS timestamptz), CAST(:cursor_id AS uuid)))
                     GROUP BY s.id
                     ORDER BY s.created_at DESC, s.id DESC
                     LIMIT :limit
                    """
                ),
                {
                    "user_id": caller.user_id,
                    # One over, so "is there another page" is a fact rather than
                    # a guess. `paging.page` discards it.
                    "limit": limit + 1,
                    "include_seed": include_seed,
                    "cursor_at": before.created_at if before else None,
                    "cursor_id": before.row_id if before else None,
                },
            )
        ).mappings().all()

    kept, next_cursor = paging.page([dict(row) for row in rows], limit=limit)
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
            for row in kept
        ],
        "next_cursor": next_cursor,
    }


# `exclude_none` so an absent optional is *omitted*, never serialized as
# `null` — matching `author?: string` in the brief, and matching what
# `Result.as_public_dict` already did before these models existed.
@router.get("/searches/{search_id}", response_model_exclude_none=True)
async def get_search(
    caller: CallerDep, search_id: uuid.UUID, debug: Annotated[int, Query()] = 0
) -> schemas.SearchSnapshot:
    """The authoritative snapshot. Partial while sources are still running.

    Polling this every second while any source is non-terminal is the **primary**
    progress path; the stream below is an accelerator that carries nothing this
    does not (risks.md R8).
    """
    snapshot = await _snapshot_or_404(search_id, caller.user_id)
    return _snapshot_view(snapshot, debug=bool(debug))


@router.get("/searches/{search_id}/results", response_model_exclude_none=True)
async def get_search_results(
    caller: CallerDep, search_id: uuid.UUID
) -> schemas.SearchResults:
    """Results only, partial-safe.

    Returns whatever has landed without waiting for the sources still running —
    which is the same guarantee the snapshot makes, offered to a caller that
    only wants the rows. ``finished`` rides along so "these are all of them" and
    "these are the ones so far" stay distinguishable; a results list that cannot
    say which it is invites a client to render a partial answer as a complete one.
    """
    snapshot = await _snapshot_or_404(search_id, caller.user_id)
    return schemas.SearchResults(
        search_id=str(snapshot.search_id),
        finished=snapshot.finished,
        results=[schemas.Result(**result.as_public_dict()) for result in snapshot.results],
    )


@router.post("/searches/{search_id}/rerun", status_code=202)
async def rerun_search(caller: CallerDep, search_id: uuid.UUID) -> dict[str, Any]:
    """Run the same query again as a **new** search.

    Never in place. The old search is a record of what the sources said at a
    moment, and overwriting it would destroy the only evidence of a result set a
    user may have acted on — including the failures, which are the rows most
    worth keeping.
    """
    async with session_scope() as session:
        query = await session.scalar(
            text("SELECT query FROM searches WHERE id = :id AND user_id = :user_id"),
            {"id": search_id, "user_id": caller.user_id},
        )
        if query is None:
            raise ApiError("not_found", "no such search")
        plan = await orchestrator.plan_search(
            session, user_id=caller.user_id, query=str(query)
        )
        await session.commit()
    return {
        "search_id": str(plan.search_id),
        "query": plan.query,
        "rerun_of": str(search_id),
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


@router.get("/searches/{search_id}/events")
async def stream_search_events(
    caller: CallerDep, search_id: uuid.UUID
) -> StreamingResponse:
    """SSE progress. **An optional enhancement, and the first thing to cut** (R8).

    It carries no information the snapshot lacks. Our worker is a separate
    service, so this generator polls the database exactly as a client would —
    which makes SSE client polling with a held connection, and the honest way to
    describe it is "the same facts, sooner".

    Two headers are load-bearing. ``X-Accel-Buffering: no`` stops an nginx-shaped
    proxy from holding the response until the buffer fills, which turns a
    progress stream into one delivery at the end. ``Cache-Control: no-cache``
    stops anything in between from replaying a finished stream to the next
    caller.

    🔴 **Authenticated by header, like every other route.** A query-string key
    would be written to Cloud Logging with the request URL, which is how a
    credential ends up in a log retained longer than the credential is. Clients
    use ``fetch`` + ``ReadableStream``, never ``EventSource`` — which cannot set
    headers at all, and is the reason this is often built the insecure way.
    """
    # Proven to exist, and to be theirs, *before* the stream opens — a 404
    # delivered as an SSE event is a 200 as far as the client's error handling
    # is concerned.
    await _snapshot_or_404(search_id, caller.user_id)

    async def events() -> AsyncIterator[bytes]:
        started = datetime.now(UTC)
        last_beat = started
        seen: dict[str, tuple[str, int]] = {}
        while True:
            snapshot = await orchestrator.load_snapshot(
                search_id=search_id, user_id=caller.user_id
            )
            if snapshot is None:  # pragma: no cover - deleted mid-stream
                return

            for row in snapshot.sources:
                fingerprint = (str(row["status"]), int(row["result_count"]))
                if seen.get(row["source"]) != fingerprint:
                    seen[row["source"]] = fingerprint
                    yield _sse("source_update", _source_view(row))
                    last_beat = datetime.now(UTC)

            if snapshot.finished:
                yield _sse(
                    "search_complete",
                    {
                        "search_id": str(snapshot.search_id),
                        "result_count": len(snapshot.results),
                    },
                )
                return

            now = datetime.now(UTC)
            if (now - started).total_seconds() > SSE_MAX_SECONDS:
                # Said out loud rather than dropped: a client that is told the
                # stream ended falls back to polling, and one that is not sits
                # waiting on a socket nobody is writing to.
                yield _sse("stream_timeout", {"search_id": str(snapshot.search_id)})
                return
            if (now - last_beat).total_seconds() >= SSE_HEARTBEAT_SECONDS:
                # A comment line. Valid SSE, ignored by every parser, and enough
                # to keep an idle proxy from reaping a healthy connection.
                yield b": keep-alive\n\n"
                last_beat = now
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    """One SSE frame.

    ``ensure_ascii`` stays on (the default) deliberately: httpx's
    ``aiter_lines`` splits on ``str.splitlines()`` semantics, which includes
    U+2028 and U+2029, so an unescaped separator inside a message body would cut
    a frame in half at the client (contracts.md §5).
    """
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n".encode()


async def _snapshot_or_404(search_id: uuid.UUID, user_id: int) -> orchestrator.Snapshot:
    # Opens its own session at REPEATABLE READ — see `load_snapshot`.
    snapshot = await orchestrator.load_snapshot(search_id=search_id, user_id=user_id)
    if snapshot is None:
        # 404 rather than 403 for another user's search: a 403 discloses that it
        # exists.
        raise ApiError("not_found", "no such search")
    return snapshot


def _snapshot_view(
    snapshot: orchestrator.Snapshot, *, debug: bool
) -> schemas.SearchSnapshot:
    return schemas.SearchSnapshot(
        search_id=str(snapshot.search_id),
        query=snapshot.query,
        is_seed=snapshot.is_seed,
        finished=snapshot.finished,
        sources=[schemas.SourceStatus(**_source_view(row)) for row in snapshot.sources],
        results=[schemas.Result(**result.as_public_dict()) for result in snapshot.results],
        # Outside `results`, always. A `score` key on a result would break the
        # closed shape the brief fixes (design D7).
        ranking=snapshot.ranking if debug else None,
    )


def _source_view(row: dict[str, Any]) -> dict[str, Any]:
    """Per-source status, reported honestly.

    ``status: "failed"`` with ``result_count: 0`` and ``status: "done"`` with
    ``result_count: 0`` are different facts and clients must render them
    differently — a throttled source must never look like an empty one
    (risks.md R16). Everything needed to tell them apart is here.

    🔴 **There are three ways to have no results, not two**, and the third was
    missing until phase 4 looked at what a brand-new user actually sees:

    * ``done``, 0 results — we looked and found nothing.
    * ``failed`` — we could not look. Something is wrong.
    * ``needs_reconnect`` — we could not look, and **you can fix it in one
      click**. This is an *action*, not an error.

    The third splits again by whether a connection ever existed. A revoked grant
    is repaired by reconnecting; a provider that was never connected is
    connected. Same status, same "this is an action" rendering, different verb —
    and offering someone a "Reconnect Gmail" button for an account they have
    never linked reads as though we lost something of theirs.
    """
    view: dict[str, Any] = {
        "source": row["source"],
        "status": row["status"],
        "mode": row["mode"],
        "result_count": row["result_count"],
        # `(source, connection_id)` is the key, not `source` — a user with two
        # Gmail grants gets two `gmail` entries and they succeed or fail
        # independently. `display_name` is what tells them apart on screen.
        "connection_id": row.get("connection_id"),
        "display_name": row.get("display_name"),
    }
    if row["error_class"] is None:
        return view

    connected = bool(row["connection_id"])
    actionable = row["error_class"] == "needs_reconnect"
    view["error"] = {
        "code": (
            ("connection_needs_reconnect" if connected else "connection_not_connected")
            if actionable
            else "provider_unavailable"
        ),
        "classification": row["error_class"],
        "message": row["error_detail"],
    }

    if actionable:
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
        # `connection_id` has them equal by construction — and for a source
        # with no connection the name is all we need to start a first grant.
        view["error"]["action_url"] = (
            connections_service.reconnect_url(
                int(row["connection_id"]), str(row["source"])
            )
            if connected
            else f"/v1/connections/{row['source']}/authorize"
        )
        if connected:
            # Kept alongside `action_url` under its original name. `SourceStatusChip`
            # already reads it, and a rename that silently broke the one repair
            # for a revoked grant is a trade nothing here is worth.
            view["error"]["reconnect_url"] = view["error"]["action_url"]
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


@router.post("/drafts", status_code=201, response_model_exclude_none=True)
async def create_draft(
    caller: CallerDep, body: CreateDraftRequest
) -> schemas.DraftEnvelope:
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
    return schemas.DraftEnvelope(**view.as_dict())


@router.get("/drafts/{draft_id}", response_model_exclude_none=True)
async def get_draft(caller: CallerDep, draft_id: uuid.UUID) -> schemas.DraftEnvelope:
    async with session_scope() as session:
        view = await service.get_draft(session, draft_id, user_id=caller.user_id)
    return schemas.DraftEnvelope(**view.as_dict())


@router.patch("/drafts/{draft_id}", response_model_exclude_none=True)
async def patch_draft(
    caller: CallerDep, draft_id: uuid.UUID, body: PatchDraftRequest
) -> schemas.DraftEnvelope:
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
    return schemas.DraftEnvelope(**view.as_dict())


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
    limit: Annotated[int, Query(le=paging.MAX_LIMIT, ge=1)] = paging.DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    include_seed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """History, newest first, keyset-paged (api-design.md §Conventions)."""
    before = paging.decode(cursor) if cursor else None
    async with session_scope() as session:
        sends = await service.list_sends(
            session,
            user_id=caller.user_id,
            # One over the limit, so `next_cursor` can be null on the last page
            # rather than a cursor that resolves to nothing.
            limit=limit + 1,
            include_seed=include_seed,
            before=(before.created_at, before.row_id) if before else None,
        )
    kept, next_cursor = paging.page(sends, limit=limit, id_key="send_id")
    return {"sends": kept, "next_cursor": next_cursor}


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


class ResolveRequest(BaseModel):
    #: ``marked_delivered`` | ``forced_resend``. Validated in the service against
    #: the same tuple the database CHECK enforces, so an unknown value is a named
    #: refusal rather than a constraint violation surfacing as a 500.
    resolution: str
    note: str | None = None


@router.post("/sends/{send_id}/resolve")
async def resolve_send(
    caller: CallerDep, send_id: uuid.UUID, body: ResolveRequest
) -> dict[str, Any]:
    """Settle an in-doubt send. **The one action `uncertain` actually needs.**

    ``uncertain`` means we do not know, which is not a failure — so it is never
    offered a retry, because re-sending a message that may already have arrived
    is the misfire this product exists to prevent. The send detail hands a person
    the evidence (``dispatched_at``, ``reconcile_attempts``, a ``verify_url`` into
    their own mailbox or channel); this is where they hand back the answer.
    """
    async with session_scope() as session:
        payload = await service.resolve_send(
            session,
            send_id,
            user_id=caller.user_id,
            resolution=body.resolution,
            note=body.note,
        )
        await session.commit()
    return payload
