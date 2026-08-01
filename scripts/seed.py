#!/usr/bin/env python3
"""Idempotent seed data.

A dev user, an API key, searches whose adapter runs sit in every status the
runtime can produce, and the seven sends from ``contracts.md`` §4.

The formal home of the sends is group 10, but they are here now because **the
`uncertain` row is the single most valuable row in the demo**: it is the state a
reviewer is least likely to be able to produce on demand, and the one that best
demonstrates the design's honesty. Everything needed to write it existed by the
end of this phase, and adding it early is cheap.

**Idempotent by deletion, not by upsert.** `is_seed` is a column, so "remove
every seeded row and write them again" is exact: no accumulating duplicates, no
`ON CONFLICT` clause per table to keep in sync with the schema, and real rows
are untouched because they are not flagged.
"""

from __future__ import annotations

import asyncio
import sys

from core.db import dispose_engine, session_scope
from core.enums import ProviderKind
from core.security import api_keys
from core.send.digest import confirmation_digest
from sqlalchemy import text

SEED_EMAIL = "seed@example.test"

# Every terminal status the runtime can produce, plus one still in flight — so
# history and detail views demo meaningfully from a cold start.
SEED_SEARCHES: list[tuple[str, list[tuple[str, str, str, str | None, str | None]]]] = [
    # (query, [(source, status, mode, error_class, error_detail)])
    (
        "acme renewal",
        [
            ("gmail", "done", "live", None, None),
            ("slack", "done", "live", None, None),
            ("web", "done", "mock", None, None),
        ],
    ),
    (
        "q3 forecast",
        [
            ("gmail", "done", "live", None, None),
            (
                "slack",
                "failed",
                "live",
                "transient",
                '{"ok": false, "error": "request_timeout"}',
            ),
            ("web", "done", "mock", None, None),
        ],
    ),
    (
        "invoice 4417",
        [
            (
                "gmail",
                "needs_reconnect",
                "live",
                "needs_reconnect",
                '{"error": "invalid_grant", "error_description": '
                '"Token has been expired or revoked."}',
            ),
            ("slack", "done", "live", None, None),
            ("web", "done", "mock", None, None),
        ],
    ),
    # The zero-connections case: web only, and it still works.
    ("board deck", [("web", "done", "mock", None, None)]),
    # Partial results without waiting — one source still running.
    (
        "pricing model",
        [
            ("gmail", "done", "live", None, None),
            ("slack", "running", "live", None, None),
            ("web", "done", "mock", None, None),
        ],
    ),
]


#: contracts.md §4. Every send state, plus the two failure classes and the
#: needs-reconnect case — which is `failed_permanent` carrying a
#: `needs_reconnect` *class*, because the class is what decides whether the UI
#: offers a reconnect action and the state is only where the send stopped.
#: (provider, recipient, display, subject, body, state, attempts,
#:  reconcile_attempts, error_class, error_detail)
SEED_SENDS: list[tuple[str, str, str, str | None, str, str, int, int, str | None, str | None]] = [
    (
        "gmail", "qa@example.test", "qa@example.test", "Re: renewal",
        "Confirming the renewal terms we discussed. — seeded",
        "delivered", 1, 0, None, None,
    ),
    (
        "slack", "C024BE91L", "#acme-renewal", None,
        "Posting the Thursday agenda. — seeded",
        "delivered", 1, 0, None, None,
    ),
    (
        "gmail", "qa@example.test", "qa@example.test", "Re: invoice 4417",
        "Chasing invoice 4417. — seeded",
        "failed_transient", 6, 0, "transient",
        '{"error": {"code": 503, "message": "Backend Error", '
        '"errors": [{"reason": "backendError", "domain": "global"}]}}',
    ),
    (
        "gmail", "not-an-address", "not-an-address", "Re: kickoff",
        "This one never had a chance. — seeded",
        "failed_permanent", 1, 0, "permanent",
        '{"error": {"code": 400, "message": "Invalid to header", '
        '"errors": [{"reason": "invalidArgument"}]}}',
    ),
    (
        "gmail", "qa@example.test", "qa@example.test", "Re: pricing",
        "Sent after the grant was revoked. — seeded",
        "failed_permanent", 1, 0, "needs_reconnect",
        '{"error": "invalid_grant", "error_description": '
        '"Token has been expired or revoked."}',
    ),
    # 🟡 The important one. Dispatched, reconciled three times, still unknown.
    (
        "slack", "C7X2QF3AA", "#sales", None,
        "We think this went out. We are not certain. — seeded",
        "uncertain", 2, 3, "transient",
        "grant revoked before reconciliation could establish whether the "
        "message was posted; 3 probes returned no usable answer",
    ),
    (
        "slack", "C5N1MK8QQ", "#eng", None,
        "Retrying right now. — seeded",
        "in_flight", 2, 0, "transient",
        '{"ok": false, "error": "ratelimited"}',
    ),
]

SEED_CONNECTIONS: list[tuple[str, str, str]] = [
    ("gmail", "seed:gmail", "seed@example.test (seeded Gmail)"),
    ("slack", "seed:slack", "Acme HQ (seeded Slack)"),
]


async def seed() -> None:
    async with session_scope() as session:
        user_id = await session.scalar(
            text(
                """
                INSERT INTO users (email) VALUES (:email)
                ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                RETURNING id
                """
            ),
            {"email": SEED_EMAIL},
        )

        # Clear the previous seed. Cascades take adapter_runs and results with
        # the searches; real rows have is_seed = false and are never touched.
        await session.execute(text("DELETE FROM searches WHERE is_seed"))
        await session.execute(text("DELETE FROM sends WHERE is_seed"))
        await session.execute(
            text(
                "DELETE FROM drafts WHERE user_id = :u AND idempotency_key LIKE 'seed-%'"
            ),
            {"u": user_id},
        )
        await session.execute(
            text("DELETE FROM api_keys WHERE user_id = :u AND name = 'seed'"),
            {"u": user_id},
        )

        minted = api_keys.mint()
        await session.execute(
            text(
                """
                INSERT INTO api_keys (user_id, key_id, key_hash, prefix_display, name)
                VALUES (:u, :key_id, :key_hash, :prefix, 'seed')
                """
            ),
            {
                "u": user_id,
                "key_id": minted.key_id,
                "key_hash": minted.key_hash,
                "prefix": minted.prefix_display,
            },
        )

        for query, sources in SEED_SEARCHES:
            # A search is finished only when no source is still outstanding —
            # `pricing model` deliberately is not.
            in_progress = any(status in {"pending", "running"} for _, status, _, _, _ in sources)
            search_id = await session.scalar(
                text(
                    """
                    INSERT INTO searches (user_id, query, is_seed, finished_at)
                    VALUES (:u, :q, true, CASE WHEN :open THEN NULL ELSE now() END)
                    RETURNING id
                    """
                ),
                {"u": user_id, "q": query, "open": in_progress},
            )
            for source, status, mode, error_class, error_detail in sources:
                await session.execute(
                    text(
                        """
                        INSERT INTO adapter_runs
                            (search_id, source, status, mode, result_count,
                             error_class, error_detail, started_at, finished_at)
                        VALUES (:s, :src, CAST(:status AS run_status),
                                CAST(:mode AS source_mode), 0,
                                CAST(:ec AS error_class), :ed, now(),
                                CASE WHEN :terminal THEN now() ELSE NULL END)
                        """
                    ),
                    {
                        "s": search_id,
                        "src": source,
                        "status": status,
                        "mode": mode,
                        "ec": error_class,
                        "ed": error_detail,
                        "terminal": status not in {"pending", "running"},
                    },
                )

        # Sends need somewhere to have come from. Seeded connections are real
        # rows with a distinguishable natural key, so group 6 replacing how
        # connections are *created* leaves this untouched.
        connection_ids: dict[str, int] = {}
        for provider, external_id, display in SEED_CONNECTIONS:
            connection_ids[provider] = int(
                await session.scalar(
                    text(
                        """
                        INSERT INTO connections (user_id, provider, external_account_id,
                                                 display_name, status)
                        VALUES (:u, CAST(:p AS provider_kind), :ext, :display, 'active')
                        ON CONFLICT ON CONSTRAINT connections_natural_key DO UPDATE
                            SET display_name = EXCLUDED.display_name
                        RETURNING id
                        """
                    ),
                    {"u": user_id, "p": provider, "ext": external_id, "display": display},
                )
                or 0
            )

        for index, row in enumerate(SEED_SENDS):
            (
                provider, recipient, display, subject, body, state,
                attempts, reconcile_attempts, error_class, error_detail,
            ) = row
            connection_id = connection_ids[provider]
            idempotency_key = f"seed-{index:02d}-{SEED_EMAIL}"
            draft_id = await session.scalar(
                text(
                    """
                    INSERT INTO drafts (user_id, connection_id, channel, recipient,
                                        recipient_display, subject, body, idempotency_key)
                    VALUES (:u, :c, CAST(:p AS provider_kind), :to, :display, :subject,
                            :body, :key)
                    ON CONFLICT (user_id, idempotency_key) DO UPDATE SET body = EXCLUDED.body
                    RETURNING id
                    """
                ),
                {
                    "u": user_id, "c": connection_id, "p": provider, "to": recipient,
                    "display": display, "subject": subject, "body": body,
                    "key": idempotency_key,
                },
            )
            digest = confirmation_digest(
                channel=ProviderKind(provider),
                recipient=recipient,
                recipient_display=display,
                subject=subject,
                body=body,
            )
            delivered = state == "delivered"
            # `sends_delivered_has_evidence` and `sends_uncertain_was_dispatched`
            # are enforced by the database, so a seed row that got either of
            # these wrong would fail to insert rather than quietly demo a state
            # the system cannot actually reach.
            await session.execute(
                text(
                    """
                    INSERT INTO sends (user_id, draft_id, connection_id, provider,
                                       idempotency_key, state, confirmed_sha256,
                                       provider_message_id, attempts, reconcile_attempts,
                                       dispatched_at, delivered_at, last_error_class,
                                       last_error_detail, is_seed)
                    VALUES (:u, :d, :c, CAST(:p AS provider_kind), :key,
                            CAST(:state AS send_state), :digest,
                            CASE WHEN :delivered THEN :message_id ELSE NULL END,
                            :attempts, :reconciles,
                            CASE WHEN :dispatched THEN now() ELSE NULL END,
                            CASE WHEN :delivered THEN now() ELSE NULL END,
                            CAST(:ec AS error_class), :ed, true)
                    """
                ),
                {
                    "u": user_id, "d": draft_id, "c": connection_id, "p": provider,
                    "key": idempotency_key, "state": state, "digest": digest,
                    "message_id": f"seed-{provider}-{index:02d}",
                    "attempts": attempts, "reconciles": reconcile_attempts,
                    # Anything that reached the provider carries the evidence
                    # that it did. Only the never-dispatched permanent failure
                    # does not.
                    "dispatched": state != "failed_permanent" or error_class == "needs_reconnect",
                    "delivered": delivered, "ec": error_class, "ed": error_detail,
                },
            )

        await session.commit()

    print(f"seeded {len(SEED_SEARCHES)} searches and {len(SEED_SENDS)} sends for {SEED_EMAIL}")
    print("  including the `uncertain` row — the state a reviewer cannot produce on demand")
    print(f"api key: {minted.plaintext}")
    print("(re-running replaces the seed rather than duplicating it)")


async def main() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - a script, and the message is the point
        print(f"seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
