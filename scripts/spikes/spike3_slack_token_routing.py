#!/usr/bin/env python
"""Spike 3 (risks.md R4) — Slack token routing.

Slack OAuth v2 returns TWO tokens from one install, and which scope list a
scope appears in decides which token can use it. Getting this wrong fails at
runtime, not at config time — and it is invisible in code review:

    search.messages with the BOT  token -> expect ok:false, not_allowed_token_type
    search.messages with the USER token -> expect ok:true

If that holds, `connections` must store both tokens and route search to the
user token and everything else to the bot token (design.md D8). Task 6.4b's
"search never uses the bot token" test then defends it.

Read-only. Posts nothing.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

API = "https://slack.com/api"


def search(http: httpx.Client, token: str, query: str) -> dict[str, Any]:
    r = http.get(
        f"{API}/search.messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"query": query, "count": 1},
    )
    body: dict[str, Any] = r.json()
    body["_http_status"] = r.status_code
    body["_scopes"] = r.headers.get("x-oauth-scopes", "")
    return body


def main() -> int:
    bot = os.getenv("SLACK_BOT_TOKEN")
    user = os.getenv("SLACK_USER_TOKEN")
    if not (bot and user):
        sys.exit("Set SLACK_BOT_TOKEN and SLACK_USER_TOKEN. See scripts/spikes/README.md.")
    if not bot.startswith("xoxb-"):
        print(f"WARNING: SLACK_BOT_TOKEN does not start with xoxb- (got {bot[:6]}…)")
    if not user.startswith("xoxp-"):
        print(f"WARNING: SLACK_USER_TOKEN does not start with xoxp- (got {user[:6]}…)")

    http = httpx.Client(timeout=30)
    query = os.getenv("SPIKE_QUERY", "the")

    # ---- 1. bot token: expected to be REJECTED --------------------------
    b = search(http, bot, query)
    print(f"1. search.messages [bot  xoxb] -> HTTP {b['_http_status']} ok={b.get('ok')}")
    print(f"   error = {b.get('error')!r}   (expect 'not_allowed_token_type')")
    print(f"   scopes = {b.get('_scopes') or '(header absent)'}")

    # ---- 2. user token: expected to WORK --------------------------------
    u = search(http, user, query)
    print(f"2. search.messages [user xoxp] -> HTTP {u['_http_status']} ok={u.get('ok')}")
    if u.get("ok"):
        total = (u.get("messages") or {}).get("total")
        print(f"   matches = {total}")
    else:
        print(f"   error = {u.get('error')!r}")
    print(f"   scopes = {u.get('_scopes') or '(header absent)'}")

    bot_rejected = b.get("ok") is False and b.get("error") == "not_allowed_token_type"
    user_works = u.get("ok") is True

    print()
    if bot_rejected and user_works:
        print("VERDICT: CONFIRMED — search.messages is user-token-only.")
        print("  Store BOTH tokens per connection; route search to xoxp and")
        print("  everything else to xoxb (design.md D8). Keep task 6.4b's test.")
        print("  classify(): not_allowed_token_type is `permanent` — it is our bug,")
        print("  never a reconnect prompt.")
        return 0

    print("VERDICT: CONTRADICTED — token routing does not behave as designed.")
    if not bot_rejected:
        print(f"  bot token was NOT rejected with not_allowed_token_type: {b.get('error')!r}")
        print(f"  full: {json.dumps({k: v for k, v in b.items() if not k.startswith('_')})[:300]}")
    if not user_works:
        print(f"  user token FAILED: {u.get('error')!r}")
        print("  if this is 'missing_scope', search:read is not in user_scope — fix the")
        print("  manifest and REINSTALL (a scope change needs a reinstall to take effect).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
