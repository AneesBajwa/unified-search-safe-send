#!/usr/bin/env python
"""Spike 1 (risks.md R3) — Gmail draft-then-send.

The exactly-once design depends on one documented behaviour:

    drafts.create(...)   -> note draft.id
    drafts.get(draft.id) -> expect 200
    drafts.send(draft.id)-> note message id
    drafts.get(draft.id) -> expect 404    <-- THIS IS THE CONTRACT

404 after send is what makes recovery a direct existence check rather than a
search: 200 means not yet sent and safe to dispatch, 404 means already sent.
If the final call is not 404, the reconciliation design in design.md D5 needs
rethinking, and we want to know now rather than in phase 3.

This sends a REAL email. Use a throwaway account and recipient.
"""

from __future__ import annotations

import base64
import os
import sys
import uuid
from email.message import EmailMessage

import httpx

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret


def access_token() -> str:
    if tok := os.getenv("GOOGLE_ACCESS_TOKEN"):
        return tok
    cid = os.getenv("GOOGLE_CLIENT_ID")
    secret = os.getenv("GOOGLE_CLIENT_SECRET")
    refresh = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (cid and secret and refresh):
        sys.exit(
            "Need GOOGLE_ACCESS_TOKEN, or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET "
            "+ GOOGLE_REFRESH_TOKEN. See scripts/spikes/README.md."
        )
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"token refresh failed {r.status_code}: {r.text}")
    return str(r.json()["access_token"])


def main() -> int:
    recipient = os.getenv("SPIKE_RECIPIENT")
    if not recipient:
        sys.exit("Set SPIKE_RECIPIENT to a throwaway address. This really sends mail.")

    token = access_token()
    http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    marker = uuid.uuid4().hex[:12]

    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = f"[spike1] draft-then-send {marker}"
    msg.set_content(
        "Phase 0 spike 1: proving drafts.get returns 404 after drafts.send.\n"
        f"marker={marker}\n"
    )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    # ---- 1. create -------------------------------------------------------
    r = http.post(f"{GMAIL}/drafts", json={"message": {"raw": raw}})
    print(f"1. drafts.create           -> {r.status_code}")
    if r.status_code != 200:
        sys.exit(f"   create failed: {r.text}")
    draft_id = r.json()["id"]
    print(f"   draft.id = {draft_id}  (stable across message replacement)")

    # ---- 2. get before send ---------------------------------------------
    r = http.get(f"{GMAIL}/drafts/{draft_id}")
    print(f"2. drafts.get (pre-send)   -> {r.status_code}  (expect 200)")
    before_ok = r.status_code == 200

    # ---- 3. send ---------------------------------------------------------
    r = http.post(f"{GMAIL}/drafts/send", json={"id": draft_id})
    print(f"3. drafts.send             -> {r.status_code}")
    if r.status_code != 200:
        sys.exit(f"   send failed: {r.text}")
    sent = r.json()
    print(f"   message id = {sent.get('id')}  threadId = {sent.get('threadId')}")

    # ---- 4. get after send  <-- THE CONTRACT -----------------------------
    r = http.get(f"{GMAIL}/drafts/{draft_id}")
    print(f"4. drafts.get (post-send)  -> {r.status_code}  (expect 404)")
    after_404 = r.status_code == 404
    if not after_404:
        print(f"   body: {r.text[:400]}")

    print()
    if before_ok and after_404:
        print("VERDICT: CONFIRMED — 200 before send, 404 after.")
        print("  drafts.get(draft_id) is a sound existence probe. design.md D5 stands:")
        print("  200 => not yet sent, safe to dispatch. 404 => already sent, reconcile.")
        return 0

    print("VERDICT: CONTRADICTED — the probe is not a reliable existence check.")
    print(f"  pre-send 200: {before_ok}   post-send 404: {after_404}")
    print("  design.md D5's Gmail reconciliation path needs rethinking BEFORE phase 3.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
