#!/usr/bin/env python3
"""Idempotent seed data (task group 10).

A dev user, an API key, five searches whose adapter runs sit in every status the
runtime can produce — **with real result rows behind them** — and the seven sends
from ``contracts.md`` §4.

The point of all of it is one sentence from the plan: *the app is fully
explorable with zero connections.* A reviewer who has connected nothing should
still be able to open history and see a delivered send, a transient failure
mid-backoff, a permanent one, a revoked grant, and the state that matters most.

The **`uncertain` send is the single most valuable row in the dataset**. It is
the state a reviewer is least likely to be able to produce on demand, and the
one that best demonstrates the design's honesty: we do not know whether it
arrived, we say so, and we hand over the evidence to settle it.

🔴 **Seed rows never touch a provider.** They are rows, flagged ``is_seed``, and
nothing here enqueues work that could dispatch. With live credentials in ``.env``
that is not a stylistic point — a seeder that dispatched would send real email.

**Idempotent by deletion, not by upsert.** ``is_seed`` is a column, so "remove
every seeded row and write them again" is exact: no accumulating duplicates, no
``ON CONFLICT`` clause per table to keep in sync with the schema, and real rows
are untouched because they are not flagged.
"""

from __future__ import annotations

import asyncio
import sys

from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.enums import ProviderKind
from core.security import api_keys
from core.send.digest import confirmation_digest
from sqlalchemy import text

SEED_EMAIL = "seed@example.test"

#: (external_id, title, snippet, author, url)
SeedResult = tuple[str, str, str, str | None, str]

#: Results per source per query. Counted rather than declared, so
#: ``adapter_runs.result_count`` and the rows behind it can never disagree —
#: a source reporting 4 with 3 rows stored is the "status is untrue" failure the
#: snapshot exists to prevent, and hard-coding both numbers is how it happens.
SEED_RESULTS: dict[tuple[str, str], list[SeedResult]] = {
    ("acme renewal", "gmail"): [
        (
            "18f2a91c4b0e7d3a",
            "Re: Acme renewal — revised terms",
            "Attaching the revised terms for the renewal. The three-year option "
            "lands at 12% below list.",
            "dana@acme.example",
            "https://mail.google.com/mail/u/0/#inbox/18f2a91c4b0e7d3a",
        ),
        (
            "18f2a4d10c9b2e51",
            "Acme renewal — legal sign-off",
            "Legal has signed off on the amended indemnity clause. No further "
            "redlines expected.",
            "priya@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f2a4d10c9b2e51",
        ),
        (
            "18f29c7be3a10d44",
            "Renewal timeline for Acme",
            "Proposed timeline: quote Thursday, signature by the 14th, PO the "
            "following week.",
            "dana@acme.example",
            "https://mail.google.com/mail/u/0/#inbox/18f29c7be3a10d44",
        ),
        (
            "18f28b0d5c7e1a92",
            "Acme renewal — usage report attached",
            "Q2 usage came in 18% above the committed floor, which strengthens "
            "the case for the larger tier.",
            "ops@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f28b0d5c7e1a92",
        ),
        (
            "18f2770a1b3c9d05",
            "Re: Acme renewal — pricing question",
            "They asked whether the renewal price holds if seat count drops "
            "below 200. It does, for one term.",
            "sam@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f2770a1b3c9d05",
        ),
    ],
    ("acme renewal", "slack"): [
        (
            "C024BE91L:1785490021.418200",
            "#acme-renewal — Dana",
            "Thursday works for the renewal call. I'll bring the usage numbers.",
            "Dana",
            "https://slack.com/app_redirect?channel=C024BE91L",
        ),
        (
            "C024BE91L:1785488814.203100",
            "#acme-renewal — Priya",
            "Indemnity clause is agreed. Sending the amended draft over now.",
            "Priya",
            "https://slack.com/app_redirect?channel=C024BE91L",
        ),
        (
            "C7X2QF3AA:1785401177.911700",
            "#sales — Sam",
            "Acme renewal is the biggest one in the quarter. Worth the extra "
            "prep time.",
            "Sam",
            "https://slack.com/app_redirect?channel=C7X2QF3AA",
        ),
    ],
    ("acme renewal", "web"): [
        (
            "web:acme-renewal-guide",
            "Contract renewal checklist",
            "A checklist for enterprise contract renewals: usage review, "
            "pricing, legal, and signature routing.",
            None,
            "https://example.test/renewal-checklist",
        ),
        (
            "web:acme-press",
            "Acme Corp announces expansion",
            "Acme Corp announced an expansion of its platform team, citing "
            "growth in enterprise accounts.",
            None,
            "https://example.test/acme-expansion",
        ),
        (
            "web:renewal-benchmarks",
            "Renewal pricing benchmarks 2026",
            "Median enterprise renewal uplift held at 6% year over year across "
            "the surveyed cohort.",
            None,
            "https://example.test/renewal-benchmarks",
        ),
    ],
    ("q3 forecast", "gmail"): [
        (
            "18f1c02d7a4b8e11",
            "Q3 forecast — first pass",
            "First pass at the Q3 forecast. Commit is 4.1M, best case 4.7M.",
            "finance@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f1c02d7a4b8e11",
        ),
        (
            "18f1b7e90d2c5a83",
            "Re: Q3 forecast — pipeline coverage",
            "Coverage is 3.2x on commit, which is thinner than we'd like going "
            "into the last month.",
            "sam@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f1b7e90d2c5a83",
        ),
    ],
    ("q3 forecast", "web"): [
        (
            "web:forecast-method",
            "Bottom-up forecasting for SaaS",
            "Bottom-up forecasting tends to beat top-down at quarter granularity "
            "once pipeline data is reliable.",
            None,
            "https://example.test/forecasting",
        ),
    ],
    ("invoice 4417", "slack"): [
        (
            "C5N1MK8QQ:1785312900.774400",
            "#eng — Ops bot",
            "Invoice 4417 bounced at the payment provider; retry scheduled.",
            "Ops bot",
            "https://slack.com/app_redirect?channel=C5N1MK8QQ",
        ),
        (
            "C7X2QF3AA:1785309044.115900",
            "#sales — Priya",
            "Chased invoice 4417 with their AP team. They're re-issuing the PO.",
            "Priya",
            "https://slack.com/app_redirect?channel=C7X2QF3AA",
        ),
    ],
    ("invoice 4417", "web"): [
        (
            "web:ap-terms",
            "Accounts payable terms explained",
            "Net-30 remains the most common enterprise payment term, with net-45 "
            "increasingly requested.",
            None,
            "https://example.test/ap-terms",
        ),
    ],
    ("board deck", "web"): [
        (
            "web:board-deck-template",
            "The board deck template that works",
            "Five slides: metrics, pipeline, product, hiring, and the single ask.",
            None,
            "https://example.test/board-deck",
        ),
        (
            "web:board-reporting",
            "What boards actually read",
            "Directors read the metrics page and the ask. Everything else is "
            "appendix.",
            None,
            "https://example.test/board-reporting",
        ),
    ],
    ("pricing model", "gmail"): [
        (
            "18f0a5c31e9d7b26",
            "Pricing model — v3",
            "v3 of the pricing model, with the usage-based tier split out from "
            "the platform fee.",
            "finance@example.test",
            "https://mail.google.com/mail/u/0/#inbox/18f0a5c31e9d7b26",
        ),
    ],
    ("pricing model", "web"): [
        (
            "web:usage-pricing",
            "Usage-based pricing in practice",
            "Hybrid models — a platform fee plus metered usage — dominate the "
            "enterprise segment.",
            None,
            "https://example.test/usage-pricing",
        ),
    ],
}

# Every terminal status the runtime can produce, plus one still in flight — so
# history and detail views demo meaningfully from a cold start.
SEED_SEARCHES: list[tuple[str, list[tuple[str, str, str, str | None, str | None]]]] = [
    # (query, [(source, status, mode, error_class, error_detail)])
    # The healthy case: every source done, 11 results merged across three.
    (
        "acme renewal",
        [
            ("gmail", "done", "live", None, None),
            ("slack", "done", "live", None, None),
            ("web", "done", "mock", None, None),
        ],
    ),
    # Partial success. 🔴 The one that matters most to render correctly: slack
    # `failed` with 0 results and web `done` with 1 are **different facts**, and
    # a UI that draws them the same way tells the user "nothing matched" about a
    # source it never managed to ask (risks.md R16).
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
    # Drives the inline reconnect action — the fix offered at the point of
    # failure rather than as a global error the user has to translate.
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
    # Partial results without waiting — one source still running, so `finished`
    # is false and the snapshot is genuinely mid-flight.
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
        "gmail", "{recipient}", "{recipient}", "Re: renewal",
        "Confirming the renewal terms we discussed. — seeded",
        "delivered", 1, 0, None, None,
    ),
    (
        "slack", "C024BE91L", "#acme-renewal", None,
        "Posting the Thursday agenda. — seeded",
        "delivered", 1, 0, None, None,
    ),
    (
        "gmail", "{recipient}", "{recipient}", "Re: invoice 4417",
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
        "gmail", "{recipient}", "{recipient}", "Re: pricing",
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
    # Mid-backoff, so the console renders a live countdown rather than a
    # spinner. The job row below is what makes that real (task 11.5c): the
    # backoff has full jitter, so `run_at` is the only honest source for "when".
    (
        "slack", "C5N1MK8QQ", "#eng", None,
        "Retrying right now. — seeded",
        "in_flight", 2, 0, "transient",
        '{"ok": false, "error": "ratelimited"}',
    ),
]

#: The send that gets a live job row behind it, by index into ``SEED_SENDS``.
RETRYING_SEND_INDEX = 6
#: Far enough out that a reviewer sees a countdown rather than a value that has
#: already elapsed by the time the page renders.
RETRY_BACKOFF_SECONDS = 42.0

SEED_CONNECTIONS: list[tuple[str, str, str]] = [
    ("gmail", "seed:gmail", "seed@example.test (seeded Gmail)"),
    ("slack", "seed:slack", "Acme HQ (seeded Slack)"),
]


async def seed() -> None:
    # Task 10.6: a designated test recipient, configured rather than invented,
    # so no reviewer is ever asked to supply a personal address to see a send.
    recipient = get_settings().test_recipient

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

        # Clear the previous seed. Cascades take adapter_runs, search_results and
        # send_resolutions with their parents; real rows have is_seed = false and
        # are never touched.
        #
        # 🔴 Jobs first, and by reference. `jobs.ref_id` is deliberately not a
        # foreign key — one queue serves several kinds of work — so nothing
        # cascades to it, and a job left pointing at a deleted send is a row the
        # worker will claim, fail to resolve, and retry six times.
        await session.execute(
            text(
                "DELETE FROM jobs WHERE kind = 'send' AND ref_id IN "
                "(SELECT id FROM sends WHERE is_seed)"
            )
        )
        await session.execute(
            text(
                "DELETE FROM jobs WHERE kind = 'adapter_run' AND ref_id IN "
                "(SELECT r.id FROM adapter_runs r JOIN searches s ON s.id = r.search_id "
                "WHERE s.is_seed)"
            )
        )
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

        # Connections first: the searches below reference them.
        #
        # 🔴 They have to. `_source_view` only advertises a `reconnect_url` for a
        # run that carries a `connection_id` — it builds the link *from* that id —
        # so a seeded `needs_reconnect` source with a NULL connection renders the
        # chip and offers no way to act on it. That is the exact shape of the
        # phase-3 defect this project has already paid for once: the one action
        # that repairs a revoked grant, missing from the surface where a user
        # meets one. Found by requesting the advertised URL rather than reading it.
        #
        # Seeded connections are real rows with a distinguishable natural key, so
        # group 6 replacing how connections are *created* leaves this untouched.
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

        result_total = 0
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
                # Only a `done` source has results. A failed one has none *and
                # says why* — which is the distinction the whole per-source
                # status exists to carry.
                results = SEED_RESULTS.get((query, source), []) if status == "done" else []
                run_id = await session.scalar(
                    text(
                        """
                        INSERT INTO adapter_runs
                            (search_id, source, connection_id, status, mode, result_count,
                             error_class, error_detail, started_at, finished_at)
                        VALUES (:s, :src, :conn, CAST(:status AS run_status),
                                CAST(:mode AS source_mode), :count,
                                CAST(:ec AS error_class), :ed, now(),
                                CASE WHEN :terminal THEN now() ELSE NULL END)
                        RETURNING id
                        """
                    ),
                    {
                        "s": search_id,
                        "src": source,
                        # NULL for `web`, which needs no connection — matching what
                        # `plan_search` writes for a source whose registration does
                        # not require one.
                        "conn": connection_ids.get(source),
                        "status": status,
                        "mode": mode,
                        "count": len(results),
                        "ec": error_class,
                        "ed": error_detail,
                        "terminal": status not in {"pending", "running"},
                    },
                )
                for rank, (external_id, title, snippet, author, url) in enumerate(results):
                    await session.execute(
                        text(
                            """
                            INSERT INTO search_results
                                (search_id, adapter_run_id, source, external_id, title,
                                 snippet, author, occurred_at, url, source_rank,
                                 blended_score)
                            VALUES (:s, :r, :src, :ext, :title, :snippet, :author,
                                    now() - make_interval(hours => :age), :url,
                                    :rank, :score)
                            """
                        ),
                        {
                            "s": search_id,
                            "r": run_id,
                            "src": source,
                            "ext": external_id,
                            "title": title,
                            "snippet": snippet,
                            "author": author,
                            # Spread over the last few days so the merged order
                            # is a real interleaving of sources rather than three
                            # blocks — which is what makes the merge visible.
                            "age": rank * 7 + len(source),
                            "url": url,
                            "rank": rank,
                            "score": round(1.0 - rank * 0.07, 4),
                        },
                    )
                result_total += len(results)

        for index, row in enumerate(SEED_SENDS):
            (
                provider, raw_recipient, raw_display, subject, body, state,
                attempts, reconcile_attempts, error_class, error_detail,
            ) = row
            to = raw_recipient.format(recipient=recipient)
            display = raw_display.format(recipient=recipient)
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
                    "u": user_id, "c": connection_id, "p": provider, "to": to,
                    "display": display, "subject": subject, "body": body,
                    "key": idempotency_key,
                },
            )
            digest = confirmation_digest(
                channel=ProviderKind(provider),
                recipient=to,
                recipient_display=display,
                subject=subject,
                body=body,
            )
            delivered = state == "delivered"
            # `sends_delivered_has_evidence` and `sends_uncertain_was_dispatched`
            # are enforced by the database, so a seed row that got either of
            # these wrong would fail to insert rather than quietly demo a state
            # the system cannot actually reach.
            send_id = await session.scalar(
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
                    RETURNING id
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

            if index == RETRYING_SEND_INDEX:
                # The job row behind the countdown. `run_at` is when the next
                # attempt would fire and `backoff_seconds` is how long the wait
                # was — a client cannot compute either, because the backoff has
                # full jitter, so this row is the only honest source.
                #
                # 🔴 **Unclaimable by construction, and that is deliberate.** The
                # claim predicate is `state = 'ready' AND run_at <= now() AND
                # attempts < max_attempts`; with `attempts = max_attempts` the
                # third clause can never hold, so no worker will ever take this
                # row. It has to be that way twice over: a claimed seed job would
                # reach for a provider (rule one of this file), and it would also
                # destroy the state it exists to demonstrate — the retrying row
                # would resolve to a failure within a minute of every `make seed`
                # and the demo would only work if you looked quickly.
                #
                # `max_attempts` on the job therefore reads 2 rather than the
                # configured 6. Nothing surfaces it: `GET /sends/{id}` reports
                # `max_attempts` from settings, because the ceiling is a
                # deployment decision, and `attempts` from the send row — where
                # it is 2, exactly as contracts.md §4 specifies.
                await session.execute(
                    text(
                        """
                        INSERT INTO jobs (kind, ref_id, partition_key, state, attempts,
                                          max_attempts, run_at, backoff_seconds,
                                          last_error_class, last_error_detail,
                                          last_error_at, started_at)
                        VALUES ('send', :ref, :partition, 'ready', :attempts, :attempts,
                                now() + make_interval(secs => :backoff), :backoff,
                                CAST(:ec AS error_class), :ed, now(), now())
                        """
                    ),
                    {
                        "ref": send_id,
                        "partition": f"{provider}:{connection_id}",
                        "attempts": attempts,
                        "backoff": RETRY_BACKOFF_SECONDS,
                        "ec": error_class,
                        "ed": error_detail,
                    },
                )

        await session.commit()

    print(
        f"seeded {len(SEED_SEARCHES)} searches ({result_total} results) "
        f"and {len(SEED_SENDS)} sends for {SEED_EMAIL}"
    )
    print(f"  sends addressed to the designated test recipient: {recipient}")
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
