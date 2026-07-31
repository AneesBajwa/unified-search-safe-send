# Phase 0 spikes

Three ~15-line probes, each answering a question the design currently
*assumes*. Every one of them is cheap now and expensive in phase 3.

**A contradicted assumption here is a good outcome.** It is the cheapest
possible moment to find out.

Run them, then write the answers into
`openspec/changes/unified-search-safe-send/risks.md` as verified.

| Spike | Risk | Question | Design that depends on it |
|---|---|---|---|
| `spike1_gmail_draft_send.py` | R3 | After `drafts.send`, does `drafts.get` return **404**? | The whole Gmail reconciliation path (design D5). If it is not 404 the exactly-once probe needs rethinking. |
| `spike2_slack_metadata.py` | R5 | Does `metadata.event_payload` survive a round trip through `conversations.history`? | The Slack reconciliation probe. Without `include_all_metadata=true` the payload silently vanishes. |
| `spike3_slack_token_routing.py` | R4 | Does `search.messages` reject a **bot** token and accept a **user** token? | Token routing in `connections`. This failure is silent in code review and obvious in a five-minute spike. |

## Running them

```bash
uv run python scripts/spikes/spike1_gmail_draft_send.py
uv run python scripts/spikes/spike2_slack_metadata.py
uv run python scripts/spikes/spike3_slack_token_routing.py
```

Each prints every request it makes and finishes with an explicit
`VERDICT: CONFIRMED` / `CONTRADICTED` line.

## Credentials

Put these in `.env` (git-ignored) or export them. **Throwaway accounts
only** — spike 1 sends a real email and spike 2 posts a real message.

### Spike 1 — Gmail

Either a ready access token:

```bash
GOOGLE_ACCESS_TOKEN=ya29.…
```

…or a refresh token, which the script exchanges itself (more convenient,
since access tokens expire in an hour):

```bash
GOOGLE_CLIENT_ID=…apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=…
GOOGLE_REFRESH_TOKEN=1//…
```

Scopes required: `gmail.compose` (create/send drafts) and `gmail.readonly`.
The quickest way to a token before the app's own OAuth flow exists is
[OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) — use
the gear icon to supply your own client id/secret so the token belongs to
your app, otherwise the redirect URI will not match later.

```bash
SPIKE_RECIPIENT=throwaway@example.com   # spike 1 really sends to this address
```

### Spikes 2 and 3 — Slack

```bash
SLACK_BOT_TOKEN=xoxb-…      # scopes: chat:write, channels:history, metadata.message:read
SLACK_USER_TOKEN=xoxp-…     # scope:  search:read
SLACK_CHANNEL=C0123456789   # a channel id the bot can post to
```

Both tokens come from one install — Slack OAuth v2 returns two. Which scope
list a scope appears in decides which token can use it, which is exactly what
spike 3 verifies.
