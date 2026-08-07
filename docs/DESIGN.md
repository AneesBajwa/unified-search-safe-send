# Design notes

Why the system is shaped the way it is. The [README](../README.md) covers what
it does and how to run it; this covers the decisions and the tradeoffs behind
them.

## Contents

- [The adapter layer](#the-adapter-layer)
- [The send gate](#the-send-gate)
- [The failure model](#the-failure-model)
- [Multi-account OAuth](#multi-account-oauth)
- [The job runtime](#the-job-runtime)
- [The module boundary](#the-module-boundary)
- [Ranking](#ranking)
- [Provider notes](#provider-notes)
- [Tradeoffs, and what I would change](#tradeoffs-and-what-i-would-change)

---

## The adapter layer

A source is one class behind one interface, and nothing outside it knows the
source exists.

```python
class SearchAdapter(Protocol):
    source: str
    async def search(self, query: str, ctx: AdapterContext) -> list[Result]: ...
```

`AdapterContext` carries the connection id, a lazy token getter, a deadline, a
correlation id, and a mode hint. It deliberately carries **no database session**,
so an adapter cannot reach past its own job.

**One durable job per connection, not per source.** A user with two Gmail grants
gets two independent runs that succeed, fail, or need reconnecting separately.
Each writes its own results and its own terminal status in its own transaction,
which is what makes partial results readable the whole time a slow source is
still working.

This was originally keyed by provider — `{provider: connection_id}` — which kept
only the *last* grant, so a second Gmail account silently never ran behind a
single healthy-looking chip. Every test used one account per provider, so
nothing caught it; the spec said "one independent run per connection" and the
code did not.

**The `Result` shape is closed.** `source, id, title, snippet, url` required;
`author` and `timestamp` optional; nothing else. Ranking inputs ride the
response envelope rather than the result, so a consumer can render the list
without knowing where a row came from.

**The merge layer may not name a source, and that is a test.**
`tests/test_adapters.py` greps `orchestrator.py` and `merge.py` for the literals
`gmail`, `slack` and `web` — comments included — and fails on any hit. Adding a
fourth source is one adapter file plus one registry line.

## The send gate

**There is no route that takes a recipient and a body and delivers.** The
absence is the feature.

```
POST /drafts            → a draft. Inert: no provider call, no job, nothing
                          observable from outside. Returns {draft, confirmation}
POST /drafts/{id}/send  → the gate. Requires confirmed_sha256 over
                          channel ‖ recipient ‖ subject ‖ body
```

Editing a draft changes the digest by construction, so a confirmation rendered
before the edit is refused (`body_changed_since_confirmation`). That is
arithmetic rather than bookkeeping that could drift out of step.

**Delivery is draft-then-send, not `messages.send`.** Gmail's draft id is a
server-side idempotency token whose *existence* is the state machine. If the
process dies between dispatch and record, we ask the provider whether the draft
still exists instead of guessing.

That choice also buys something for the user: a draft gives them an independent,
visible record in their own mailbox, so an in-doubt send is something they can
resolve by looking rather than by trusting our status page. It is why we ask for
`gmail.compose` and never `gmail.send`.

### Why `uncertain` is a state and not a bug

Exactly-once delivery to a provider that offers no idempotency key is
impossible — it reduces to the Two Generals problem. So the guarantee is
exactly-once *effect*, and where the effect cannot be determined the residue is
exposed honestly instead of guessed at.

An `uncertain` send is never offered a retry. Re-sending a message that may
already have arrived is the exact misfire the product exists to prevent. It is
offered two explicit resolutions instead — *mark it delivered* (records an
attestation, invents no provider id) or *send it again* (the only path that
clears the dispatch record) — plus the evidence needed to choose: dispatch time,
probe count, and a link into the user's own mailbox or channel.

### Idempotency, concretely

The draft carries the key. `POST /send` with the same key returns the same send,
the same `provider_message_id`, and `Idempotent-Replayed: true` — 200 rather
than 409, deliberately, because a retried request that already succeeded is not
a client error.

Proven under two conditions that a single-threaded test cannot reach: concurrent
duplicate submission with real OS threads released by a barrier, and a crash
injected at **every commit seam** between dispatch and record. The crash is a
`BaseException` subclass so production `except Exception` handlers cannot
swallow it — otherwise the tests would exercise the error path while claiming to
exercise the crash path.

## The failure model

Every provider error is classified once, in one place, into one of three:

| Class | Meaning | Behaviour |
|---|---|---|
| `transient` | Rate limit, 503, timeout | Auto-retried with full-jitter backoff. A provider's `Retry-After` always wins over our computed delay |
| `permanent` | Invalid recipient, quota exhausted, malformed | Surfaced immediately with the untruncated provider payload. Never auto-retried; the operator decides |
| `needs_reconnect` | Grant revoked or expired | Routed to a one-click reconnect that preserves the connection id, so dependent state survives |

A fourth class, `config`, exists for *our* misconfiguration — a missing OAuth
client, an unreadable keyring. It is separated so that a rotated client secret
never renders as "reconnect your account", which would send a user in circles
repairing a grant that was never broken.

Backoff is full jitter (`uniform(0, min(cap, base·2ⁿ))`), base 2 s, cap 300 s.
Only `attempts` and `run_at` are stored, never a materialized schedule.

## Multi-account OAuth

**Storage.** Refresh tokens are encrypted with an envelope key bound to the
connection row id as AAD, so ciphertext lifted into another row will not
decrypt. The key is read from a **mounted file**, not an environment variable:
env vars resolve at instance start, so rotating one forces a redeploy, and they
leak through `/proc/self/environ`, debug endpoints and subprocess environments.
Key management is a keyring string (`v1:…,v2:…`) — honest rotation, not an HSM.

The app **fails closed** when the keyring is absent. There is no plaintext
fallback and no generate-if-missing path; a key that appeared by magic would
mean a later restart silently could not decrypt what an earlier one wrote.

**Silent refresh** happens under a transaction-scoped advisory lock, so
concurrent jobs on one connection cannot both spend the refresh token and race
each other into an invalid-grant state.

**Recovery.** A raw 401 is not a dead grant — it is usually an expired access
token, and treating it as revocation would push users through pointless
re-consent. Only a refresh that fails with `invalid_grant` marks the connection
`needs_reconnect`.

**Reconnect preserves identity.** The re-grant is matched to the row it repairs
by the provider's stable subject id, so drafts, sends and historical runs still
point at the right connection. Authorizing a *different* account against a
reconnect is refused by name (`reconnect_account_mismatch`) rather than silently
creating a second connection or overwriting the first.

`prompt=consent` is a repair tool, not a default: forcing it on every connect
mints a new refresh token each time and walks into Google's 100-per-account cap.

## The job runtime

Work is claimed with a materialized CTE using `FOR NO KEY UPDATE SKIP LOCKED` —
never `WHERE id IN (SELECT …)`, which does not hold the lock it appears to.

Leases are per *kind*, decided inside the one statement that sets them: a batch
claim mixes kinds, and a single lease value would quietly give sends the shorter
adapter lease, after which the sweeper would start reconciling sends that are
still in flight.

Recovery is a **policy registry** rather than a blanket retry: adapter runs
retry, sends reconcile, and any kind with no registered reconciler is *parked
for a human* rather than re-executed on the assumption it probably did not land.
A test asserts every job kind has an explicit entry, which turns "we thought
about side effects" into something the type system can check.

### Why the worker is push-driven

The original design was a resident Cloud Run worker with
`--no-cpu-throttling --min-instances=1` running `while True: poll()`. That is
correct, and it costs **$44.71/month**. Every always-on variant lands between
$25 and $45; there is no configuration of a continuously-running process on the
platform that is free.

So work is dispatched rather than discovered: the API enqueues a Cloud Tasks
push after each job-creating commit, and Cloud Scheduler hits a sweep endpoint
every 15 minutes. The trap avoided is that Cloud Run **Jobs** bill a one-minute
minimum per execution, so a three-second job on a one-minute schedule costs the
same as running around the clock; a Cloud Run *service* rounds to 100 ms.

Latency improved rather than regressed: a warm push lands in ~100–400 ms, which
beats the one-second poll it replaced.

The sweep endpoint recovers stale leases **and then drains due jobs**. The nudge
only fires on user-initiated commits, so a job the worker itself rescheduled — a
transient send waiting out its backoff — has nothing else to wake it. Without
the drain, backoff retries stall until an unrelated user action happens to nudge
the worker.

## The module boundary

```
packages/core/     the reusable module: adapters, sending, jobs, connections
apps/api/          FastAPI — REST surface, OAuth callbacks, SSE. Stateless.
apps/worker/       same image, different entrypoint. HTTP-driven.
apps/web/          Vite + React 19 + TypeScript SPA. Talks HTTP only.
```

`packages/core` imports nothing from `apps/` and no web framework — the
dependency arrow only ever points inward, enforced by import-linter
(`uv run lint-imports`).

The console is a **pure consumer**: every route is reachable with an API key and
there is no browser-session path, so "the UI has no privileged access" is
structurally true rather than an assertion to audit. Both sides have a test —
`tests/test_route_surface.py` enumerates the API surface and fails on any route
that is neither key-guarded nor allowlisted with a written reason, and
`tests/test_web_boundary.py` greps `apps/web/src` for locally-derived decisions
(`canSend`, `isRetryable`, ranking inputs, client-side digests).

## Ranking

Within-source reciprocal rank blended with recency decay, plus a per-source cap
so one chatty source cannot crowd out the others.

Deliberately simple: relevance scores from Gmail, Slack and a web API are not
comparable, so any unified score would be invented. The inputs are inspectable
under `?debug=1` rather than hidden behind a number that looks authoritative.

## Provider notes

### Slack: two tokens, on purpose

`search:read` belongs in `user_scope`, not `scope`. A bot token sent to
`search.messages` is rejected with `not_allowed_token_type`, which names nothing
and reads like a bug in our code.

Search runs on the **user** token so it sees the channels the user sees. Posting
runs on the **bot** token so a message from this app is attributable to the app
rather than to a person.

Slack grants are **additive and cannot be narrowed**: re-authorizing with a scope
removed from both the request and the manifest still returns a token carrying
it, and only uninstalling clears it. That is why the scope pre-flight is a
subset test that *reports* rather than blocks — a literal `required == granted`
check also condemns a healthy Google grant, because we ask for `email` and
Google returns `https://www.googleapis.com/auth/userinfo.email`.

### The Slack ToS boundary

Slack's API terms restrict storing message content, and the schema enforces the
boundary rather than a paragraph promising it:

- Message bodies are cached with a **TTL**, not archived —
  `search_results.body_expires_at` gives provider-sourced text an expiry with
  its own index.
- Identifiers, permalinks and timestamps **persist**, so history stays navigable
  after content ages out. A result you can still click is not a copy of the
  message.
- The app stays **undistributed**. Each reviewer installs the manifest into their
  own workspace under their own tokens.

The same reasoning is why the reconciliation probe reads `conversations.history`
for one message id rather than pulling a channel: the narrowest read that
answers "did this specific message post".

### Gmail scopes

`gmail.readonly` is restricted and unavoidable — `gmail.metadata` cannot use the
`q` parameter, so it cannot search. `gmail.compose` is requested instead of
`gmail.send` for the reason above. Not requested: `gmail.modify`,
`gmail.insert`, or the blanket `https://mail.google.com/`.

## Tradeoffs, and what I would change

**Accepted deliberately, for a proof of concept:**

- **Passwordless sign-in.** An address is an account. It makes "a brand-new user
  searches before connecting anything" reachable in one field, which is the
  state that hid a defect through three phases. Production puts an identity
  provider in front of key issuance.
- **The API key in `sessionStorage`**, which is XSS-reachable. One credential
  path is what makes the pure-consumer property structurally true. The
  production alternative is an httpOnly refresh cookie plus a short-lived access
  token.
- **A labelled mock for web search** by default, which the brief permits. It
  keeps the suite and Codespaces hermetic and the demo deterministic. The live
  path exists behind one environment variable.
- **Envelope keys in a keyring string** rather than a KMS or HSM.

**What I would do with more time, roughly in order:**

1. **Playwright over the confirm flow.** Two claims are currently held by hand
   verification: the end-to-end confirm, and that double-tapping confirm yields
   exactly one send. Both deserve to be pinned.
2. **Real identity** in front of key issuance, which removes the largest
   accepted risk above.
3. **Push-based freshness** — Gmail watch + Slack Events — so the inbox updates
   without a query, instead of fanning out on demand every time.
4. **A richer ranking signal**, probably learned from which results get acted
   on, since that is the only comparable signal across sources.
5. **Per-connection rate-limit budgets** shared across workers, rather than the
   current fixed per-provider concurrency caps.
