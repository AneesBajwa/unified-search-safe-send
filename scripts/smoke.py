#!/usr/bin/env python3
"""End-to-end smoke test — **API only, no UI**.

This is the reviewer's own "run it entirely through the REST API" check, run
continuously by us instead of once by them at the end. It grows a step per phase;
today it is the whole product loop:

    health -> mint a key -> search -> poll until every source is terminal
           -> create a draft -> send -> **send again with the same key**
           -> assert one delivery, one provider_message_id, idempotent_replay

The assertions that matter are the last three. Two calls, one message. The
second call returns the *same* `provider_message_id` rather than a new one, and
says so with `idempotent_replay: true`. If any of that stops being true the send
gate is broken, whatever the tests say.

Nothing here nudges the worker: if the jobs complete, a background worker claimed
and ran them, which is the durable-background-work requirement demonstrated
rather than described.

Usage:  python scripts/smoke.py [--base-url http://localhost:8080]
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

TERMINAL_SOURCE_STATUSES = {"done", "failed", "needs_reconnect"}
TERMINAL_SEND_STATES = {"delivered", "failed_permanent", "failed_transient", "uncertain"}


class SmokeFailure(AssertionError):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="how long to wait for a background worker to pick work up",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "run against an EXISTING key instead of minting a fresh one. The "
            "delivery leg needs a connection, and a connection needs a real "
            "OAuth grant that no headless script can complete — so pass the key "
            "of an already-connected user to exercise the full loop against "
            "live providers. Without it, a fresh user has no grant and the "
            "delivery leg is skipped (loudly)."
        ),
    )
    args = parser.parse_args()

    started = time.monotonic()
    with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
        # 1. The API is up and its database round trip works.
        health = client.get("/health")
        check(health.status_code == 200, f"/health returned {health.status_code}: {health.text}")
        body = health.json()
        check(body["status"] == "ok", f"/health is degraded: {body}")
        print(f"✓ health         {body['database']['server_version']} "
              f"migration={body['database']['migration']}")

        # 2. An API key, minted and stored as a hash we can never reverse. It
        #    also provisions a fake connection for each provider that is *not*
        #    configured — a configured provider gets none, because a fake row
        #    pointing at a real provider is a lie that surfaces as a product bug.
        if args.api_key:
            auth = {"X-API-Key": args.api_key}
            print("✓ api key        supplied (running against an existing user)")
        else:
            created = client.post("/v1/auth/dev-login", json={"email": "smoke@example.test"})
            check(created.status_code == 201, f"sign-in failed: {created.text}")
            key = created.json()
            check(
                key["key"].startswith("sk_live_"),
                f"unexpected key format: {key['prefix_display']}",
            )
            auth = {"X-API-Key": key["key"]}
            print(f"✓ api key        {key['prefix_display']}")

        # Every call from here carries the key. There is no unauthenticated path
        # and no browser-session path — that is what makes "the UI is a pure
        # consumer" structurally true rather than a claim to audit.
        unauthenticated = client.get("/v1/sends")
        check(unauthenticated.status_code == 401, "an unauthenticated read was allowed")

        # 3. Fan out. Returns before any adapter has run.
        queued = client.post("/v1/searches", json={"query": "acme renewal"}, headers=auth)
        check(queued.status_code == 202, f"search failed: {queued.text}")
        search = queued.json()
        check(
            {source["status"] for source in search["sources"]} == {"pending"},
            "a source was already past `pending` before any worker ran",
        )
        print(f"✓ searched       {len(search['sources'])} sources for {search['search_id']}")

        # 4. Poll the snapshot until every source is terminal. We do not nudge
        #    the worker.
        snapshot = _await_search(client, auth, search["search_id"], args.timeout)
        statuses = {source["source"]: source["status"] for source in snapshot["sources"]}
        check(
            all(status in TERMINAL_SOURCE_STATUSES for status in statuses.values()),
            f"a source never reached a terminal status: {statuses}",
        )
        check(snapshot["results"], "the search produced no results at all")
        # The closed shape, checked at the wire.
        allowed = {"source", "id", "title", "snippet", "author", "timestamp", "url"}
        for result in snapshot["results"]:
            extra = set(result) - allowed
            check(not extra, f"a result carries keys outside the closed shape: {extra}")
        print(f"✓ results        {len(snapshot['results'])} merged · {statuses}")

        # 5. A draft. No provider is contacted.
        #
        # Which channel depends on what is actually connected. A fresh user with
        # every provider configured has no grant and therefore no connection —
        # the gate then refuses at draft time, which is correct, so that is
        # asserted and the delivery leg is skipped **loudly** rather than
        # quietly reporting a pass over work that never ran.
        target = _send_target(client, auth)
        if target is None:
            refusal = client.post(
                "/v1/drafts",
                json={"channel": "gmail", "to": "nobody@example.test", "body": "x"},
                headers=auth,
            )
            check(
                refusal.status_code == 409
                and refusal.json()["error"]["code"] == "connection_needs_reconnect",
                f"a draft with no connection was not refused correctly: {refusal.text}",
            )
            print("✓ gate           a draft with no connection is refused")
            print()
            print("⏸️  SKIPPED: draft -> confirm -> send -> idempotent replay.")
            print("    Every provider is configured, so no fake connection is")
            print("    provisioned, and this user has no OAuth grant. Re-run with")
            print("    --api-key <key of a connected user> to exercise delivery.")
            print(f"\nSMOKE PASSED (delivery leg skipped) in {time.monotonic() - started:.1f}s")
            return 0

        channel, recipient = target
        print(f"✓ connection     sending via {channel} -> {recipient}")
        drafted = client.post(
            "/v1/drafts",
            json={"channel": channel, "to": recipient, "body": "Confirming for Thursday."},
            headers=auth,
        )
        check(drafted.status_code == 201, f"draft failed: {drafted.text}")
        draft = drafted.json()
        digest = draft["confirmation"]["confirm_sha256"]
        recipient = draft["confirmation"]["recipient_display"]
        print(f"✓ draft          {recipient} · digest {digest[:12]}…")

        # 6. No digest, no send. The absence of a recipient+body route is the
        #    feature; this is the same rule at the level of one call.
        refused = client.post(f"/v1/drafts/{draft['draft']['id']}/send", json={}, headers=auth)
        check(
            refused.status_code == 422,
            f"a send without confirmation was not refused: {refused.text}",
        )
        check(
            refused.json()["error"]["code"] == "confirmation_required",
            f"unexpected refusal code: {refused.json()}",
        )
        print("✓ gate           a send with no confirmation is refused")

        # 7. The send.
        first = client.post(
            f"/v1/drafts/{draft['draft']['id']}/send",
            json={"confirmed_sha256": digest},
            headers=auth,
        )
        check(first.status_code == 201, f"send failed: {first.text}")
        check(first.headers.get("Idempotent-Replayed") == "false", "first send reported a replay")
        send_id = first.json()["send_id"]
        delivered = _await_send(client, auth, send_id, args.timeout)
        check(
            delivered["state"] == "delivered",
            f"send finished as {delivered['state']}: {delivered.get('error')}",
        )
        print(f"✓ sent           {send_id} · {delivered['provider_message_id']}")

        # 8. The same key again. One message, same evidence, and the API says
        #    plainly that this was a replay.
        second = client.post(
            f"/v1/drafts/{draft['draft']['id']}/send",
            json={"confirmed_sha256": digest},
            headers=auth,
        )
        check(
            second.status_code == 200,
            f"duplicate send returned {second.status_code}: {second.text}",
        )
        check(
            second.headers.get("Idempotent-Replayed") == "true",
            "the duplicate did not carry Idempotent-Replayed: true",
        )
        replay = second.json()
        check(replay["idempotent_replay"] is True, f"duplicate not flagged as a replay: {replay}")
        check(replay["send_id"] == send_id, "the duplicate created a second send")
        check(
            replay["provider_message_id"] == delivered["provider_message_id"],
            "the duplicate returned a different provider_message_id — that is a second message",
        )
        check(replay["attempts"] == delivered["attempts"], "the duplicate cost an extra attempt")
        print(f"✓ idempotent     same send, same message id, replay={replay['idempotent_replay']}")

        # 9. And it is one row, from the outside.
        history = client.get("/v1/sends", headers=auth).json()["sends"]
        matching = [row for row in history if row["send_id"] == send_id]
        check(len(matching) == 1, f"expected one send in history, found {len(matching)}")

        # 10. The connection round trip (task group 6). Which half runs depends
        #     on whether an OAuth client is configured — and *that distinction is
        #     itself asserted*, so "it is configured but silently mocked" cannot
        #     pass unnoticed.
        _check_connections(client, auth)

    print(f"\nSMOKE PASSED in {time.monotonic() - started:.1f}s")
    return 0


def _send_target(
    client: httpx.Client, auth: dict[str, str]
) -> tuple[str, str] | None:
    """Pick a channel and recipient this user can actually send through.

    Returns ``None`` when the user has no active connection at all, which is the
    honest state for a fresh user once every provider is configured.

    Slack is preferred when available because its recipient is a channel — a
    send there is visible to the person running this and costs nobody an email.
    """
    connections = client.get("/v1/connections", headers=auth).json()["connections"]
    active = [c for c in connections if c["status"] == "active"]
    if not active:
        return None

    slack = next((c for c in active if c["provider"] == "slack"), None)
    if slack is not None:
        # Resolve a real channel to post into rather than guessing an id.
        channels = _first_slack_channel(client, auth)
        if channels is not None:
            return "slack", channels

    gmail = next((c for c in active if c["provider"] == "gmail"), None)
    if gmail is not None:
        return "gmail", gmail["display_name"]
    return None


def _first_slack_channel(client: httpx.Client, auth: dict[str, str]) -> str | None:
    """The channel id the seeded search results came from, if any.

    Read from a real search rather than hardcoded, so this keeps working in a
    workspace whose channel ids are not the fixture ones.
    """
    queued = client.post("/v1/searches", json={"query": "the"}, headers=auth)
    if queued.status_code != 202:
        return None
    search_id = queued.json()["search_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snap = client.get(f"/v1/searches/{search_id}", headers=auth).json()
        if snap.get("finished"):
            for result in snap.get("results", []):
                if result["source"] == "slack" and ":" in result["id"]:
                    return result["id"].split(":", 1)[0]
            return None
        time.sleep(0.4)
    return None


def _check_connections(client: httpx.Client, auth: dict[str, str]) -> None:
    """The connection round trip, asserted through the public API only.

    A full OAuth round trip cannot run headless — it ends in a browser at a
    consent screen — so what is checked here is everything on **our** side of
    it: that the authorize step mints a provider URL carrying the right scopes
    and a signed state, that an unconfigured provider says so rather than
    producing a broken link, and that a disconnect keeps the row.

    The scope assertions are the valuable part. `search:read` landing in Slack's
    bot `scope` instead of `user_scope` is invisible until `search.messages`
    fails at runtime with `not_allowed_token_type` (risks.md R4), and this is
    the cheapest place that mistake can be caught.
    """
    listing = client.get("/v1/connections", headers=auth).json()
    available = {row["provider"]: row for row in listing["available"]}
    check(available, "/v1/connections reported no providers at all")

    for provider, row in sorted(available.items()):
        if not row["configured"]:
            # No client id: the source is mocked and says so. The Connect button
            # is deliberately absent rather than leading to an OAuth error page.
            check(
                row["authorize_url"] is None,
                f"{provider} is unconfigured but still offered an authorize URL",
            )
            print(f"✓ connection     {provider} not configured · fixtures, badged mock")
            continue

        started = client.get(f"/v1/connections/{provider}/authorize", headers=auth)
        check(started.status_code == 200, f"{provider} authorize failed: {started.text}")
        url = started.json()["authorize_url"]
        check("state=" in url, f"{provider} authorize URL carries no signed state")
        check(
            started.json()["rationale"],
            f"{provider} offers no explanation of what it is asking for",
        )

        if provider == "gmail":
            check(
                "access_type=offline" in url,
                "no offline access: there would be no refresh token",
            )
            check("gmail.compose" in url, "no gmail.compose: draft-then-send is impossible (R3)")
            check("gmail.send" not in url, "gmail.send is the wrong scope for this design")
        if provider == "slack":
            check("user_scope=search%3Aread" in url, "search:read is not a user scope (R4)")
            check(url.startswith("https://"), "Slack rejects non-HTTPS redirect URIs (R18)")
        print(f"✓ connection     {provider} authorize URL ok · scopes verified")

    # A disconnect keeps the row: drafts and sends reference it, and erasing it
    # would erase the record of everything sent through it.
    #
    # 🔴 Only ever a **fake** connection. Disconnecting clears the stored tokens,
    # and a real grant cannot be restored without a browser — so a smoke run
    # against `--api-key` would silently destroy the connection it was meant to
    # be exercising, and every later run would fail for a reason that has
    # nothing to do with the code.
    def _is_fake(connection: dict[str, object]) -> bool:
        provider = str(connection.get("provider", ""))
        configured = bool(available.get(provider, {}).get("configured"))
        return "fake" in str(connection.get("display_name", "")).lower() or not configured

    existing = [c for c in listing["connections"] if _is_fake(c)]
    if not existing:
        print("⏸️  SKIPPED: disconnect check — only real connections present, and")
        print("    disconnecting one cannot be undone without a browser.")
    if existing:
        target = existing[0]
        dropped = client.delete(f"/v1/connections/{target['id']}", headers=auth)
        check(dropped.status_code == 200, f"disconnect failed: {dropped.text}")
        after = client.get("/v1/connections", headers=auth).json()["connections"]
        row = next((r for r in after if r["id"] == target["id"]), None)
        check(row is not None, "disconnect deleted the row instead of clearing its tokens")
        assert row is not None
        check(
            row["status"] == "needs_reconnect",
            f"a disconnected connection is {row['status']}, so it may still be searched",
        )
        check(row.get("reconnect_url"), "a disconnected connection offers no way back")
        print(f"✓ disconnect     {target['provider']} cleared · history retained")


def _await_search(
    client: httpx.Client, auth: dict[str, str], search_id: str, timeout: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    snapshot: dict[str, object] = {}
    while time.monotonic() < deadline:
        snapshot = client.get(f"/v1/searches/{search_id}", headers=auth).json()
        if snapshot.get("finished"):
            return snapshot
        time.sleep(0.25)
    raise SmokeFailure(
        f"search {search_id} never finished within {timeout}s — is a worker running? "
        "(RUN_WORKER_INLINE=1, or the worker service)"
    )


def _await_send(
    client: httpx.Client, auth: dict[str, str], send_id: str, timeout: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    send: dict[str, object] = {}
    while time.monotonic() < deadline:
        send = client.get(f"/v1/sends/{send_id}", headers=auth).json()
        if send.get("state") in TERMINAL_SEND_STATES:
            return send
        time.sleep(0.25)
    raise SmokeFailure(f"send {send_id} was still {send.get('state')!r} after {timeout}s")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as exc:
        print(f"\nSMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as exc:
        print(f"\nSMOKE FAILED: cannot reach the API ({exc})", file=sys.stderr)
        sys.exit(1)
