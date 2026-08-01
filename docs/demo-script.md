# Demo script — 15–30 minutes, two parts

Part one is the working system, ~12 minutes. Part two is the architecture,
~13 minutes. The budget is tight, so the running order below front-loads the two
things the submission is actually graded on — the send gate and per-source
honesty — and treats everything else as supporting material.

**The single most important instruction: do not narrate the UI.** Say what the
decision was and why, then let the screen show the result. "Here is the
connections page, and here are the connections" is dead air.

---

## Before you start

```bash
# One terminal, left visible. The API must be the NATIVE process, not Docker's:
# only the host process reads .env and therefore only it has provider credentials.
pkill -f "uvicorn apps.api.main"
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8080 &
cloudflared tunnel --url http://localhost:8080 &   # if the tunnel is down
docker compose up -d web
make seed
```

⚠️ The tunnel hostname is random per restart and both OAuth consoles match it
byte-exactly. If it restarted, update `OAUTH_TUNNEL_URL` **and** the redirect URI
in both consoles before recording, or the reconnect segment dies on camera.

Have ready, in tabs: the console, the throwaway Gmail's inbox, the Slack sandbox,
and a terminal.

---

## Part one — the working system (~12 min)

### 1. What a stranger sees first (~1 min)

Sign in with an address nobody has used. Search **before connecting anything**.

> "Both providers offer a **Connect** — not a failure, and not 'Reconnect' for an
> account nobody ever linked. The web source still returns results, so the page
> is worth looking at. This state had a bug in it until late: a brand-new user's
> first search reported both providers as permanently broken. It survived every
> test because every test ran as a user who already had connections."

Open **History** — every send state is already there, seed-badged.

> "Seeded, and it says so on every row. `?include_seed=false` hides them. The
> point is that this is explorable with zero connections."

### 2. Connections (~1.5 min)

Connect Gmail. Show the consent rationale **before** the redirect.

> "The scopes are `gmail.readonly` and `gmail.compose`. Not `gmail.send` — I'll
> come back to why the draft matters. Not `mail.google.com`."

Then Slack, and name the split:

> "`search:read` is a **user** scope, so search sees what the user sees.
> Posting is a **bot** token, so a message from this app is attributable to the
> app. Put `search:read` in the bot scopes and `search.messages` fails at runtime
> with `not_allowed_token_type` — an error that names nothing."

### 3. Unified search with one source slow (~2 min)

```bash
SLOW_ADAPTER_SOURCE=slack SLOW_ADAPTER_DELAY_MS=8000   # restart the API with this
```

Run a search and **stop talking** for three seconds.

> "Gmail and web have reported. Slack is still working and says so. Those are
> three visually distinct states: results, no matches, still working. The chip
> exists because a source that failed quietly makes an incomplete answer look
> complete — the user concludes 'nobody emailed me about this' when we never
> looked."

Wait for Slack to land. Six results, three sources.

> "The knob is source-agnostic and it applies to the *real* adapters. It is not a
> fake slow source."

### 4. The gate (~3 min) — **the centrepiece, do not rush it**

Compose to the test recipient. Show that nothing on this page can deliver.

> "Creating a draft contacts no provider. There is no route in the API that
> takes a recipient and a body and sends. The absence is the feature."

On confirm, at 375px if you can:

> "The destination is the largest thing on the screen and it never scrolls away.
> The body is the only scrolling region and it is never truncated — you cannot
> confirm what you cannot read. Send and Cancel are 24 pixels apart with Cancel
> at the bottom, where the thumb rests, because the safe action should be the
> easy one."

**Double-tap Send.** Then open the recipient's mailbox.

> "One message. The confirm sends a digest over the channel, recipient, subject
> and body — edit the draft and that digest stops being valid. The second tap
> returned the same send, same provider message id, `Idempotent-Replayed: true`."

### 5. Reconnect (~2 min)

```bash
uv run python -c "
import asyncio; from core.connections.service import invalidate_stored_token
asyncio.run(invalidate_stored_token(<connection_id>))"
```

Search again.

> "Not a red failure — an **action**, at the point of failure, with the
> provider's own words above it."

Click it, complete consent, search again.

> "Same connection id. Every draft and send referencing it still means what it
> meant. That is why a reconnect is a re-grant in place and not a new row."

### 6. A failure, end to end (~2 min)

Open a `failed_transient` send from history, then a `failed_permanent` one.

> "Attempt count, the untruncated provider payload, and a retry — on the
> transient one only. A permanent failure retried arrives at the same answer more
> slowly, so offering the button would teach you the button does nothing."

Then the `uncertain` send.

> "Amber, not red. Failed means we know nothing was sent. Uncertain means we do
> not know. It offers two resolutions and no retry."

### 7. No UI at all (~0.5 min)

```bash
make smoke
```

> "Search, draft, the refusal without a digest, the send, the replay, history —
> through the API with no UI process running. The console is a consumer of the
> same key an external script would use."

---

## Part two — the architecture (~13 min)

Lead with the decisions, not the file tree.

### The adapter layer (~2.5 min)

One protocol, one registry line, `AdapterContext` carrying a **lazy** token
getter and no database session.

> "The merge layer may not name a source, and that is a test rather than a
> convention — it greps the module for `gmail`, `slack` and `web`, comments
> included, and fails on a hit. Adding a source is one file."

One durable job **per connection**, not per source.

> "That was wrong until very late. The fan-out built a map from provider to
> connection id and kept the last one, so a user with two Gmail accounts had one
> of them silently never searched. It looked fine — one healthy chip."

### Multi-account OAuth (~3 min)

Token storage, silent refresh, recovery.

> "Refresh tokens are encrypted with an envelope key **bound to the row id as
> AAD**, so ciphertext lifted into another row will not decrypt. The key is a
> mounted file rather than an env var: env vars resolve at instance start, so
> rotating one forces a redeploy, and they leak through `/proc/self/environ`."

> "Refresh happens under a Postgres advisory lock, so twenty concurrent adapter
> runs produce one refresh, not twenty. And a raw 401 is **not** treated as a
> dead grant — only `invalid_grant` from the refresh endpoint is. Escalating
> every 401 disconnects healthy users constantly."

> "Google mints a new refresh token on every consent and silently evicts the
> oldest past 100 per account, so consent is not forced on an ordinary login."

### Idempotent send and the crash window (~4 min) — **the strongest section**

> "Exactly-once delivery to a provider with no idempotency key is impossible. It
> reduces to the Two Generals problem. So we guarantee exactly-once *effect* and
> expose the residue honestly."

Draw the seam:

> "We commit `in_flight` **before** contacting the provider. Then draft-then-send
> rather than `messages.send`, because Gmail's draft id is a server-side
> idempotency token whose *existence* is the state machine: after a send the
> draft is gone. Crash between dispatch and record and we ask whether the draft
> still exists rather than guessing."

> "I tested this by injecting `os._exit(1)` at **every seam** — the tests are
> parameterised over the crash points — and separately by killing the worker
> mid-dispatch against real Gmail. One message each time."

> "When the probe cannot answer, the send becomes `uncertain`, never `failed`,
> and it is offered two resolutions rather than a retry. Re-sending a message
> that may already have arrived is the exact misfire this product exists to
> prevent."

Also worth 30 seconds: the duplicate returns **200**, not Stripe's 409.

> "Stripe returns 409 because at the moment of conflict it has nothing committed
> to show you. We do. And our caller includes a human double-tapping a button on
> a phone, for whom a 409 is an error toast for a send that is working perfectly."

### Tradeoffs (~2.5 min)

Pick three and say them plainly.

- **The API key is in `sessionStorage`**, which is XSS-reachable. Taken
  deliberately: one credential path is what makes "the console is a pure
  consumer" structurally true, and that is the property being graded. Production
  answer is an httpOnly refresh cookie plus a short-lived access token.
- **Polling beat SSE.** The worker is a separate service, so an SSE generator
  polls the database anyway — it is client polling with a held connection, an
  auth workaround and a buffering risk. SSE ships as an accelerator with **no
  fallback path**, because polling is never conditional on it.
- **The worker is push-driven.** An always-on Cloud Run worker is $44.71/month;
  Cloud Tasks push is free *and* lower-latency than the poll it replaced.

### What I would change with more time (~1 min)

> "Playwright for the confirm flow — the double-tap claim is currently held by
> hand verification, and hand verification does not survive the next change.
> Ranking is deliberately naive because cross-source relevance scores are not
> comparable, but the inputs are inspectable under `?debug=1` rather than
> mysterious. And the deploy: it is written and blocked on billing, which I would
> rather say than paper over."

---

## Closing (~30 s)

> "Six real defects were found in this project by *using* it or by reading the
> spec back against the code — not one by the test suite, which was green
> throughout. Every one was a test asserting the shape of a value instead of
> reading the value that crossed the boundary. That is the thing I would take to
> the next project ahead of any of the code."

---

## Timing checklist

| Segment | Budget | Cumulative |
|---|---|---|
| Stranger's first view | 1:00 | 1:00 |
| Connections | 1:30 | 2:30 |
| Slow source | 2:00 | 4:30 |
| **The gate** | 3:00 | 7:30 |
| Reconnect | 2:00 | 9:30 |
| A failure | 2:00 | 11:30 |
| API with no UI | 0:30 | 12:00 |
| Adapter layer | 2:30 | 14:30 |
| Multi-account OAuth | 3:00 | 17:30 |
| **Idempotent send** | 4:00 | 21:30 |
| Tradeoffs | 2:30 | 24:00 |
| More time + closing | 1:30 | 25:30 |

If you are running long, cut in this order: the failure segment to 1 minute, the
adapter layer to 1:30, tradeoffs to 1:30. **Never cut the gate or the idempotent
send** — they are half the evaluation between them.
