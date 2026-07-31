#!/usr/bin/env python
"""Spike 2 (risks.md R5) — Slack message metadata round-trip.

`chat.postMessage` has no idempotency parameter of any kind, so the Slack
reconciliation probe depends on stamping `metadata.event_payload` and reading
it back:

    chat.postMessage(channel, text, metadata={event_type, event_payload:{idempotency_key}})
    conversations.history(channel, include_all_metadata=True)
    -> confirm event_payload.idempotency_key comes back

Two things this actually verifies:

1. That the payload survives the round trip at all.
2. That `include_all_metadata=true` is load-bearing — WITHOUT it the payload
   is SILENTLY dropped from the response, leaving only `event_type`. Silent,
   not an error, which is why this is worth five minutes now.

It also records which scopes the token carried, because the docs are
inconsistent about whether `metadata.message:read` is actually required. We
request it and observe empirically rather than trusting the documentation.

This posts a REAL message. Use a throwaway workspace.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

API = "https://slack.com/api"


def call(http: httpx.Client, method: str, token: str, **params: Any) -> dict[str, Any]:
    """Slack signals failure with ok:false at HTTP 200. Never trust the status."""
    is_post = method in {"chat.postMessage"}
    if is_post:
        r = http.post(
            f"{API}/{method}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=params,
        )
    else:
        r = http.get(
            f"{API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    scopes = r.headers.get("x-oauth-scopes", "")
    body: dict[str, Any] = r.json()
    if scopes:
        body["_scopes"] = scopes
    return body


def main() -> int:
    bot = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_CHANNEL")
    if not (bot and channel):
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_CHANNEL. See scripts/spikes/README.md.")

    http = httpx.Client(timeout=30)
    key = f"spike2-{uuid.uuid4().hex}"
    posted_at = int(time.time())

    # ---- 1. post with metadata ------------------------------------------
    res = call(
        http,
        "chat.postMessage",
        bot,
        channel=channel,
        text=f"[spike2] metadata round-trip {key[:20]}",
        # snake_case keys, no nested objects — Slack rejects both.
        metadata={
            "event_type": "safe_send_dispatch",
            "event_payload": {"idempotency_key": key},
        },
    )
    print(f"1. chat.postMessage        -> ok={res.get('ok')}  error={res.get('error')}")
    if not res.get("ok"):
        sys.exit(f"   post failed: {json.dumps(res)[:500]}")
    ts = res["ts"]
    print(f"   ts = {ts}")
    print(f"   bot token scopes = {res.get('_scopes', '(header absent)')}")

    time.sleep(1)  # the history index is not instantaneous
    oldest = str(posted_at - 60)  # design D5: attempt_time - 60s

    # ---- 2. read back WITHOUT the flag  <-- the silent-failure case ------
    res_without = call(
        http, "conversations.history", bot, channel=channel, oldest=oldest, limit=20
    )
    got_without = _find(res_without, ts)
    print(
        f"2. conversations.history (no include_all_metadata) -> "
        f"ok={res_without.get('ok')} error={res_without.get('error')}"
    )
    print(f"   metadata present: {got_without!r}")

    # ---- 3. read back WITH the flag -------------------------------------
    res_with = call(
        http,
        "conversations.history",
        bot,
        channel=channel,
        oldest=oldest,
        limit=20,
        include_all_metadata="true",
    )
    got_with = _find(res_with, ts)
    print(
        f"3. conversations.history (include_all_metadata=true) -> "
        f"ok={res_with.get('ok')} error={res_with.get('error')}"
    )
    print(f"   metadata present: {got_with!r}")

    round_trips = got_with == key
    flag_matters = got_without != key

    print()
    if round_trips:
        print("VERDICT: CONFIRMED — event_payload.idempotency_key round-trips.")
        print("  The Slack reconciliation probe in design.md D5 is sound.")
        if flag_matters:
            print("  include_all_metadata=true is REQUIRED — without it the payload")
            print("  is silently dropped, exactly as the design assumes.")
        else:
            print("  NOTE: the payload came back even WITHOUT include_all_metadata.")
            print("  Keep sending the flag anyway; do not rely on this.")
        print(f"  Scopes on the token that worked: {res.get('_scopes', 'unknown')}")
        print("  -> whether metadata.message:read is required is answered by")
        print("     whether it appears in that list. Re-run without it to be sure.")
        return 0

    print("VERDICT: CONTRADICTED — the idempotency key did not come back.")
    print(f"  with flag: {got_with!r}   without flag: {got_without!r}")
    print(f"  history error: {res_with.get('error')}")
    print("  design.md D5's Slack probe cannot work as specified. Rethink BEFORE phase 3.")
    return 1


def _find(history: dict[str, Any], ts: str) -> str | None:
    for m in history.get("messages", []):
        if m.get("ts") == ts:
            md = m.get("metadata") or {}
            payload = md.get("event_payload") or {}
            return payload.get("idempotency_key")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
