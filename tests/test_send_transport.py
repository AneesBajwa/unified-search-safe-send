"""Transport-layer exactly-once (openspec task 5.12c, risks.md R23).

A fake provider counts the calls **it** was asked to make. It cannot see a
client library retrying underneath it — and that is precisely the failure this
test exists to catch. The Slack SDK defaults to ten retries over thirty minutes
and Stripe's to two; sensible for idempotent APIs, catastrophic for a send with
no dedup token, because the duplicate never passes through the gate at all. No
amount of correct claim logic helps.

So the assertion here is at the socket: **exactly one HTTP request leaves the
process** across repeated send calls.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import respx
from conftest import make_api_key
from core.db import session_scope
from core.enums import ProviderKind
from core.jobs import runtime
from core.send import providers, service

pytestmark = pytest.mark.usefixtures("clean_db")

ENDPOINT = "https://fake.provider.test/chat.postMessage"
CHANNEL = "C024BE91L"


async def _draft_and_send(user_id: int) -> tuple[uuid.UUID, str, dict[str, Any]]:
    async with session_scope() as session:
        view = await service.create_draft(
            session,
            user_id=user_id,
            channel=ProviderKind.SLACK,
            recipient=CHANNEL,
            body="Confirming for Thursday.",
        )
        await session.commit()
    return (
        uuid.UUID(view.draft["id"]),
        view.confirmation["confirm_sha256"],
        view.draft,
    )


async def test_exactly_one_http_request_leaves_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(providers.ENDPOINT_ENV, ENDPOINT)
    key = await make_api_key()
    user_id = int(str(key["user_id"]))
    draft_id, digest, _ = await _draft_and_send(user_id)

    with respx.mock(assert_all_called=True) as router:
        route = router.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"ok": True, "ts": "1720000000.0001"})
        )

        # Three send calls with the same key — the double-tap, then a retry from
        # a client that did not hear the first answer.
        for _ in range(3):
            async with session_scope() as session:
                await service.send_draft(
                    session, draft_id, user_id=user_id, confirmed_sha256=digest
                )
                await session.commit()

        while (await runtime.run_once(limit=10)).claimed:
            pass

        assert route.call_count == 1, (
            f"{route.call_count} HTTP requests reached the provider; the gate "
            "only sees the calls it makes, so a client-library retry would show "
            "up here and nowhere else"
        )


def test_the_provider_client_is_pinned_to_zero_retries() -> None:
    """The one-line mitigation, asserted rather than trusted.

    Stated on the protocol so a future real provider cannot ship with its SDK
    default, and checked at resolution time so a mis-set client fails loudly on
    first use rather than quietly duplicating a message under load.
    """
    impl = providers.get_provider(ProviderKind.SLACK)
    assert impl.max_retries == 0

    class ChattyClient:
        provider = "slack"
        max_retries = 10

        async def send(self, request: object) -> object:  # pragma: no cover
            raise AssertionError("must never be reached")

        async def probe_by_fingerprint(self, request: object) -> object:  # pragma: no cover
            raise AssertionError("must never be reached")

    providers.set_provider(ProviderKind.SLACK, ChattyClient())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="pinned to zero"):
        providers.get_provider(ProviderKind.SLACK)
