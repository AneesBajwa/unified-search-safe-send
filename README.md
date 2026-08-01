# Unified Search & Safe Send

Unified multi-provider search (Gmail, Slack, web) behind a pluggable adapter
layer, plus a safe-send gate where every outbound message is drafted,
reviewed, explicitly confirmed, and idempotent under retry.

> **Status.** The API, the console, the adapters, the send gate, OAuth
> connections, history and the seed dataset all work end to end against **real
> Gmail and real Slack**. The whole loop is drivable with `curl` and no UI
> process — see [Driving it with curl](#driving-it-with-curl).
> **Deployment to Cloud Run is written and blocked on GCP billing**; see
> [What is not here yet](#what-is-not-here-yet). Design and task breakdown live
> in `openspec/changes/unified-search-safe-send/` in the parent workspace.

## Architecture

Two things are the product; the unified inbox is one thing built on top of them.

### The adapter layer

A source is one class behind one interface. Nothing outside it knows the source
exists.

```python
class SearchAdapter(Protocol):
    source: str
    async def search(self, query: str, ctx: AdapterContext) -> list[Result]: ...
```

`AdapterContext` carries the connection id, a **lazy** token getter, a deadline,
a correlation id and a mode hint — and deliberately **no database session**, so
an adapter cannot reach past its own job.

One query becomes **one durable job per connection**, not per source: a user with
two Gmail grants gets two independent runs that succeed, fail or need
reconnecting separately. Each writes its own results and its own terminal status
in its own transaction, so partial results are readable the entire time a slow
source is still working.

Every adapter returns the same closed `Result` — `source, id, title, snippet,
url` required, `author` and `timestamp` optional, **nothing else**. Ranking
inputs ride the response envelope rather than the result, so a consumer can
render the list without knowing where a row came from.

🔴 **The merge layer may not name a source, and that is a test rather than a
convention.** `tests/test_adapters.py` greps `orchestrator.py` and `merge.py`
for the literals `gmail`, `slack` and `web` — comments included — and fails on
any hit. Adding a fourth source is one adapter file plus one registry line.

### The send gate

There is **no route that takes a recipient and a body and delivers**. The
absence is the feature.

```
POST /drafts            → a draft. Inert: no provider call, no job, nothing observable
                          from outside. Returns {draft, confirmation}
POST /drafts/{id}/send  → the gate. Requires confirmed_sha256 over channel ‖
                          recipient ‖ subject ‖ body. Idempotent on a key the
                          draft carries
```

Editing a draft changes the digest by construction, so a confirmation rendered
before the edit is refused — expressed as arithmetic, not as bookkeeping that
could get out of step.

Delivery is **draft-then-send**, not `messages.send`: Gmail's draft id is a
server-side idempotency token whose *existence* is the state machine. Crash
between dispatch and record and we ask the provider whether the draft still
exists instead of guessing. When the answer cannot be obtained the send becomes
**`uncertain`** — never `failed` — and is offered two explicit resolutions
rather than a retry, because re-sending a message that may already have arrived
is the misfire this whole thing exists to prevent.

### The module boundary

```
packages/core/     the reusable module: adapters, sending, jobs, connections
apps/api/          FastAPI — REST surface, OAuth callbacks, SSE. Stateless.
apps/worker/       same image, different entrypoint. HTTP-driven, not a poll loop.
apps/web/          Vite + React 19 + TypeScript SPA. Talks HTTP only.
```

`packages/core` imports nothing from `apps/` and no web framework — the
dependency arrow only ever points inward. Enforced, not asserted:

```bash
uv run lint-imports
```

The console is a **pure consumer**: every route is reachable with an API key and
there is no browser-session path, so "the UI has no privileged access" is
structurally true rather than an assertion to audit. That has a test on both
sides — `tests/test_route_surface.py` enumerates the API surface, and
`tests/test_web_boundary.py` greps `apps/web/src` for locally-derived decisions
(`canSend`, `isRetryable`, ranking inputs, `new EventSource(`, client-side
digests) and for the API fields the console must still be *reading*.

## Local development

Requires Docker (or Colima) and nothing else.

```bash
cp .env.example .env
docker compose up --build
make seed                        # the demo dataset — see below
```

- API — http://localhost:8080/health
- Worker — http://localhost:8081/health
- SPA — http://localhost:5173
- Postgres — `localhost:5433` (5433, not 5432, so a natively-installed
  Postgres on the default port is left alone)

Migrations run as their own one-shot `migrate` service before `api` and
`worker` start, so two replicas cannot race on the same migration.

### Without Docker

```bash
uv sync
docker compose up -d db          # or point DATABASE_URL at any Postgres 17
uv run alembic upgrade head
uv run uvicorn api.main:app --reload --port 8080
cd apps/web && npm install && npm run dev
```

⚠️ **Only the host process reads `.env`.** The compose `api` container does not,
so `configured_sources()` reports every provider unconfigured there and the
adapters fall back to fixtures badged `mock`. If you are working against real
Gmail or Slack credentials, run uvicorn on the host and keep Docker's `api`
stopped — otherwise whichever wins port 8080 decides whether you are testing
live providers or fixtures, silently.

### GitHub Codespaces

`.devcontainer/devcontainer.json` provisions Python 3.13, Node 22 and
**docker-in-docker** — the last is not optional: the test suite starts its own
Postgres with testcontainers and needs a daemon it controls.

```bash
# in the Codespace terminal
cp .env.example .env
docker compose up -d --wait db migrate
docker compose up -d api worker web
make seed
```

Ports 8080 (api), 8081 (worker), 5173 (SPA) and 5433 (Postgres) forward
automatically. The SPA derives its API host from the page it was served from
(see `apps/web/src/api/client.ts`), so the forwarded SPA URL reaches the
forwarded API with no rebuild — set `VITE_API_BASE_URL` only if you move the API
somewhere else.

⚠️ **OAuth in a Codespace needs one extra step.** Google and Slack match
redirect URIs byte-exactly, so a forwarded Codespace hostname has to be
registered in both consoles before a connect will complete — and the hostname
changes per Codespace. Everything except connecting a live provider works
without it: seed data, web search, the full send gate against the fixture
provider, and the whole test suite.

## OAuth setup

Neither provider is required to explore the app — see
[Seed data](#seed-data). This is for connecting real accounts.

Both providers need a **public HTTPS redirect URI**. Slack rejects
`http://localhost` outright, so a tunnel is a prerequisite rather than an
optimisation:

```bash
cloudflared tunnel --url http://localhost:8080
# → https://<random>.trycloudflare.com ; put it in .env as OAUTH_TUNNEL_URL
```

⚠️ **The quick-tunnel hostname is random per restart** and both consoles match
exactly. If OAuth suddenly fails with `redirect_uri_mismatch`, that is why:
update `OAUTH_TUNNEL_URL` **and** the redirect URI in both consoles.

### Google

1. Google Cloud console → **APIs & Services → OAuth consent screen**. External.
2. Enable the **Gmail API**.
3. **Credentials → Create OAuth client ID → Web application.** Authorized
   redirect URI, exactly:
   `https://<tunnel>/v1/connections/callback/gmail`
4. Scopes — request exactly these four and no more:

   | Scope | Why |
   |---|---|
   | `openid`, `email` | Identify the account so a re-grant can be matched to the row it repairs |
   | `gmail.readonly` | Restricted, and unavoidable: `gmail.metadata` cannot use the `q` parameter, so it cannot search |
   | `gmail.compose` | Draft-then-send. **Never `gmail.send`** — a draft gives the *user* an independent record, so an in-doubt send is something they resolve in their own mailbox instead of trusting our status page |

   Not requested: `gmail.modify`, `gmail.insert`, or the blanket
   `https://mail.google.com/`.
5. **Publish the app to "In production"** (it stays unverified). This is what
   removes the 7-day refresh-token expiry that testing-mode grants carry. An
   unverified app shows an interstitial — click **Advanced → Go to … (unsafe)**.
6. `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` into `.env`.

### Slack

The repo ships [`docs/slack-app-manifest.yaml`](docs/slack-app-manifest.yaml) so
you can stand up your own internal app in about thirty seconds rather than us
distributing one. Slack → **Your Apps → Create New App → From an app manifest**,
paste it, then replace the placeholder redirect URI with
`https://<tunnel>/v1/connections/callback/slack` in the manifest **and** in
OAuth & Permissions.

🔴 **`search:read` belongs in `user_scope`, not `scope`.** A bot token sent to
`search.messages` is rejected with `not_allowed_token_type`, which names nothing
and reads like our bug. Search runs on the **user** token so it sees the channels
the user sees; posting runs on the **bot** token so a message from this app is
attributable to the app rather than to a person. That two-token split is
deliberate and verified by a spike.

Bot scopes: `chat:write`, `chat:write.public` (post without joining, which kills
most `not_in_channel`), `channels:read`, `channels:history`, `channels:join`,
`metadata.message:read`. User scope: `search:read`.

⚠️ **Slack grants are additive and cannot be narrowed.** Re-authorizing with a
scope removed from both the request *and* the manifest still returns a token
carrying it; only uninstalling clears it. That is why the scope pre-flight
(`core/connections/scopes.py`) is a subset test and **reports rather than
blocks** — a literal `required ⊆ granted` equality check condemns a healthy
Google grant, because we ask for `email` and Google returns
`https://www.googleapis.com/auth/userinfo.email`.

⚠️ Prefer a **developer sandbox** over a free workspace: no 90-day window, no
10-app cap, and no bulk-send throttle while seeding demo messages.

#### The Slack ToS boundary

Slack's API terms restrict storing message content, and the design takes that
seriously rather than working around it:

- **Message bodies are cached with a TTL, not archived.** `search_results` gives
  provider-sourced body text an expiry column; identifiers, permalinks and
  timestamps persist so history stays navigable after the content ages out.
  A search result you can still click is not the same as a copy of the message.
- **Only identifiers are persisted long-term** — channel id, message `ts`,
  permalink. Everything a user needs to go and look at the original, nothing
  that stands in for it.
- **The app stays undistributed.** No public distribution, no directory listing,
  no shared installation. Each reviewer installs the manifest into their own
  workspace under their own tokens, which is also why the credentials never
  leave the machine they were granted on.

The same reasoning is why the reconciliation probe reads
`conversations.history` for one message id rather than pulling a channel: the
narrowest read that answers "did this specific message post".

### Test accounts

Use throwaway accounts. No reviewer is ever asked to connect a personal
account — the app is fully explorable with zero connections, and seeded sends go
to `TEST_RECIPIENT` (default `qa@example.test`; `.test` is reserved by RFC 6761
and can never resolve).

## Web search

`WEB_SEARCH_API_KEY` unset — the default — makes the web adapter serve a
deterministic fixture set and report `mode: "mock"`, which the API puts on every
source status and the console renders as a badge. **A mocked source is never
allowed to look live**; that is the same honesty rule the rest of the status
chip enforces, aimed at ourselves.

The brief explicitly permits a clearly-labelled mock, and taking it buys two
things worth more than live web results: the test suite and Codespaces stay
hermetic with no third-party key, and the demo is deterministic. Set the key and
the same adapter goes live with no other change — the mock is a fallback inside
one adapter, not a different code path.

## Seed data

```bash
make seed
```

Writes five searches and seven sends covering **every status the runtime can
produce** — delivered, transiently retrying mid-backoff, permanently failed with
a real provider payload, a revoked grant with a working reconnect link, and an
`uncertain` send with the evidence needed to settle it. The point is that the app
is fully explorable **with zero connections**: a reviewer who has connected
nothing can still browse history and see every state.

Re-running replaces the seed rather than duplicating it. Seed rows carry
`is_seed`, every listing reports it, and `?include_seed=false` excludes them —
so seeded data can never be mistaken for something you did.

🔴 **Seed rows never touch a provider.** They are rows. With live credentials
configured, a seeder that dispatched would send real email.

Seeded sends are addressed to the **designated test recipient**, `TEST_RECIPIENT`
(default `qa@example.test`). No reviewer is ever asked to supply a personal
address, and `.test` is reserved by RFC 6761 so it can never resolve.

## The API

Base `http://localhost:8080/v1` · Auth `X-API-Key: sk_live_…` on **every** route
except the two noted below.

There is **no browser-session path and no cookie**, so there is no endpoint the
console can reach that `curl` cannot. That is checked rather than promised:
`tests/test_route_surface.py` enumerates the router and fails on any route that
is neither API-key-guarded nor named in an allowlist with a written reason.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/dev-login` | Issue an API key (PoC sign-in) — **unauthenticated**, it is how you get a key |
| `GET` | `/api-keys` | List own keys, by prefix only |
| `POST` | `/api-keys` | Create — **plaintext returned once**, never recoverable |
| `DELETE` | `/api-keys/{key_id}` | Revoke |
| `GET` | `/connections` | List with status, plus which providers are connectable |
| `GET` | `/connections/{provider}/authorize` | Begin OAuth → `{authorize_url}`. `?reconnect={id}` re-grants in place |
| `GET` | `/connections/callback/{provider}` | OAuth callback — **unauthenticated**, a provider redirects a browser here; everything it trusts is in the signed `state` |
| `DELETE` | `/connections/{id}` | Disconnect. Tokens deleted, history retained |
| `POST` | `/searches` | Fan out — returns immediately, before any adapter runs |
| `GET` | `/searches` | History list |
| `GET` | `/searches/{id}` | Snapshot: merged results + per-source status. `?debug=1` adds ranking inputs |
| `GET` | `/searches/{id}/results` | Results only, partial-safe |
| `GET` | `/searches/{id}/events` | SSE progress — an optional accelerator, carries nothing the snapshot lacks |
| `POST` | `/searches/{id}/rerun` | Same query, **new** search |
| `POST` | `/drafts` | Create a draft. **No external effect of any kind** |
| `GET` | `/drafts/{id}` | Draft + confirmation payload |
| `PATCH` | `/drafts/{id}` | Edit. Changes `confirm_sha256`, invalidating any digest already held |
| `POST` | `/drafts/{id}/send` | **The gate.** Idempotent |
| `GET` | `/sends` | History list |
| `GET` | `/sends/{id}` | Detail: attempts, full error text, evidence |
| `POST` | `/sends/{id}/retry` | Operator retry, under the original key |
| `POST` | `/sends/{id}/resolve` | Settle an in-doubt send |

Machine-readable schema at `/openapi.json`. The console's TypeScript types are
**generated** from it with `make schema` and never hand-edited — a test
regenerates and compares.

**Listings** take `?limit` (≤100, default 25), `?cursor`, and
`?include_seed=false`, newest first. Paging is keyset rather than OFFSET, so a
row landing while you page cannot make you see another one twice.

**Scoping**: every query filters by the key's owner, and another user's resource
returns **404, never 403** — a 403 would confirm it exists.

### Errors

Every refusal carries a machine-readable `code` and a `classification`, so a
client can decide whether retrying is meaningful without parsing prose:

```jsonc
{ "error": { "code": "connection_needs_reconnect", "classification": "needs_reconnect",
             "message": "Google access was revoked.",
             "reconnect_url": "/v1/connections/gmail/authorize?reconnect=31" } }
```

| Code | HTTP | Class | What to do |
|---|---|---|---|
| `unauthorized` | 401 | permanent | Identical for wrong, revoked, expired and unknown keys — never disclose which |
| `not_found` | 404 | permanent | Also returned for another user's resource |
| `confirmation_required` | 422 | permanent | Create a draft and send its `confirm_sha256` |
| `body_changed_since_confirmation` | 422 | permanent | Re-read the draft and confirm what it holds now |
| `idempotency_key_body_mismatch` | 422 | permanent | Caller bug: this key was used for different content |
| `resolution_required` | 409 | permanent | An in-doubt send needs a decision — `POST /sends/{id}/resolve` |
| `connection_needs_reconnect` | 409 | needs_reconnect | A grant existed and was revoked — send the user to `action_url` |
| `connection_not_connected` | 409 | needs_reconnect | This provider was never connected — send the user to `action_url`. Distinct verb: offering to *re*connect an account nobody ever linked reads as though we lost something |
| `recipient_invalid` | 422 | permanent | Do not retry |
| `channel_not_found` | 422 | permanent | Do not retry |
| `provider_rate_limited` | 503 | transient | Auto-retried; `Retry-After` is honoured over our own backoff |
| `provider_unavailable` | 503 | transient | Auto-retried with backoff |
| `internal_config_error` | 500 | config | **Our** bug — alert the operator |
| `invalid_cursor` | 422 | permanent | Drop the cursor, re-read the first page |
| `provider_not_configured` | 503 | config | **Our** bug: no OAuth client for that provider |
| `authorization_denied` | 400 | permanent | The user declined at the consent screen |
| `authorization_incomplete` | 400 | permanent | The callback carried no code — restart the flow |
| `state_invalid` | 400 | permanent | Signed state missing, tampered with, or expired |
| `reconnect_account_mismatch` | 409 | permanent | The re-grant authorized a different account |

The `config` class exists so a rotated client secret never renders as "reconnect
your account", which sends a user round in circles repairing a grant that was
never broken.

Codes are declared once in `apps/api/catalog.py`, and `tests/test_error_catalog.py`
makes a **real request for every one of them** and reads the response — because a
documented error code is not an error code that has ever been returned.

## Driving it with curl

The whole product loop, no UI process running. `make smoke` runs this
continuously; here it is by hand.

```bash
API=http://localhost:8080

# 1. A key. The plaintext exists once, here, and is never recoverable.
KEY=$(curl -sX POST $API/v1/auth/dev-login \
        -H 'content-type: application/json' \
        -d '{"email":"you@example.test"}' | jq -r .key)
AUTH="X-API-Key: $KEY"

# 2. Fan out. Returns immediately — no adapter has run yet.
SEARCH=$(curl -sX POST $API/v1/searches -H "$AUTH" \
          -H 'content-type: application/json' \
          -d '{"query":"acme renewal"}' | jq -r .search_id)

# 3. Poll the snapshot. `finished` is false while ANY source is non-terminal.
#    Partial results are readable the whole time.
curl -s "$API/v1/searches/$SEARCH" -H "$AUTH" | jq '{finished, sources}'
curl -s "$API/v1/searches/$SEARCH/results" -H "$AUTH" | jq '.results[0]'

# 4. A draft. NO provider is contacted — a draft is inert.
DRAFT=$(curl -sX POST $API/v1/drafts -H "$AUTH" \
         -H 'content-type: application/json' \
         -d '{"channel":"gmail","to":"qa@example.test","body":"Confirming for Thursday."}')
ID=$(echo "$DRAFT"     | jq -r .draft.id)
SHA=$(echo "$DRAFT"    | jq -r .confirmation.confirm_sha256)

# 5. No digest, no send. The refusal IS the product.
curl -sX POST $API/v1/drafts/$ID/send -H "$AUTH" \
     -H 'content-type: application/json' -d '{}' | jq .error.code
#   → "confirmation_required"

# 6. The send. 201, and the worker delivers it in the background.
curl -sX POST $API/v1/drafts/$ID/send -H "$AUTH" \
     -H 'content-type: application/json' -d "{\"confirmed_sha256\":\"$SHA\"}" | jq

# 7. THE SAME CALL AGAIN. 200, the same send, the same provider_message_id.
#    One message. This is the guarantee the whole design is arranged around.
curl -isX POST $API/v1/drafts/$ID/send -H "$AUTH" \
     -H 'content-type: application/json' -d "{\"confirmed_sha256\":\"$SHA\"}" \
  | grep -i 'idempotent-replayed'
#   → Idempotent-Replayed: true

# 8. History, and the full untruncated error on anything that failed.
curl -s "$API/v1/sends?include_seed=false" -H "$AUTH" | jq '.sends[] | {state, attempts}'
curl -s "$API/v1/sends/<id>" -H "$AUTH" | jq '{state, error, retryable_by_operator}'
curl -sX POST "$API/v1/sends/<id>/retry" -H "$AUTH" | jq .state
```

Progress can also be streamed. Use `fetch` + `ReadableStream`, never
`EventSource` — it cannot set headers, and a key in a query string is written to
the request log:

```bash
curl -N "$API/v1/searches/$SEARCH/events" -H "$AUTH"
```

## Secret scanning

Enable the pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
brew install gitleaks
```

The hook **fails closed** — if gitleaks is missing it blocks the commit rather
than silently leaving changes unscanned. This repo handles OAuth refresh
tokens and a token-encryption key; a secret reaching git history is fixable
only by rotating the credential, never by a follow-up commit.

## Checks

```bash
make test        # the full suite against a throwaway Postgres 17
make headline    # just the behaviours this is graded on (11 tests, ~17s)
make lint        # ruff + import-linter module boundaries
make typecheck   # mypy --strict
make web         # the console: tsc, oxlint, and its unit tests
make smoke       # the loop above, end to end, with no UI running
make schema      # regenerate the console's types from OpenAPI
make image       # cross-build linux/amd64 and prove both entrypoints SERVE
```

CI runs all of these on every push (`.github/workflows/ci.yml`).

### The headline suite

`make headline` runs the six behaviours worth looking at first — marked with
`@pytest.mark.headline` rather than moved into one file, so each stays beside
the machinery it exercises where whoever changes that machinery will read it.

| What it proves | Where |
|---|---|
| **Exactly-once send** under a duplicate key — concurrently with real OS threads released by a barrier, and separately across a crash injected at **every seam** between dispatch and record | `test_send_gate.py`, `test_send_crash.py` |
| **A slow adapter does not block fast ones** — over a **real socket**, because `TestClient` and `ASGITransport` both buffer the whole response and so *cannot detect blocking at all* | `test_partial_results.py` |
| **Every result conforms to the closed shape**, asserted at the wire | `test_adapters.py` |
| **A revoked grant surfaces as reconnect** and survives one — the same connection id, and the search that failed then succeeds | `test_connections.py` |
| **Transient retries with backoff, permanent does not retry** — the 503 reschedules and keeps its attempt count; the invalid recipient is surfaced immediately | `test_job_runtime.py` |
| **History fidelity** — attempt count, untruncated error text, and an operator retry that *resumes* the record rather than starting a new one | `test_api_e2e.py` |

### Hermetic on purpose

No third-party key, no network. `tests/test_hermetic.py` asserts it rather than
claiming it: no credential is visible, every source reports itself
unconfigured, and **no default source is registered `live`**.

That last one is the real check. Adapters register at *module import*, so if the
test environment were applied any later than `conftest`'s own import, a machine
with a populated `.env` would silently register live adapters and start reaching
real providers — passing on CI and failing on a laptop, which is the worst shape
a defect can have. That is not hypothetical; it happened, and this is what would
have caught it.

`make smoke` is the important one: it is the reviewer's own "run it entirely
through the API" check, run continuously by us instead of once by them. Without
`--api-key` it runs as a fresh user, asserts the honest refusal a user with no
OAuth grant gets, and **skips the delivery leg loudly**. With
`--api-key <key of a connected user>` it runs the full loop against live
providers.

## Known limitations and security posture

Stated rather than discovered. The reasoning for each is in
`openspec/changes/unified-search-safe-send/risks.md`.

- **The API key lives in `sessionStorage`**, which is XSS-reachable. Accepted
  deliberately: a single credential path is what makes "the console is a pure
  consumer" structurally true, and that is the property being graded. The
  production alternative is an httpOnly refresh cookie plus a short-lived
  access token.
- **Refresh tokens are encrypted with an envelope key** bound to the connection
  row id as AAD, so ciphertext lifted into another row will not decrypt. The key
  is read from a mounted file, not an env var — env vars resolve at instance
  start, so rotating one forces a redeploy, and they leak through
  `/proc/self/environ`. Key management is a keyring string (`v1:…,v2:…`), which
  is honest key rotation but not an HSM.
- **`uncertain` is a real state, not a bug.** Exactly-once delivery to a
  provider with no idempotency key is impossible — it reduces to the Two
  Generals problem. We guarantee exactly-once *effect* and expose the residue
  honestly rather than guessing.
- **Ranking is deliberately simple**: within-source reciprocal rank blended with
  recency decay, plus a per-source cap. Relevance scores from Gmail, Slack and a
  web API are not comparable, so any unified score would be invented. The inputs
  are inspectable under `?debug=1`.
- **`gmail.compose` is a restricted scope** and we ask for it knowingly — see
  [OAuth setup](#oauth-setup) for why a draft beats `gmail.send`.
- **Slack message bodies are cached with a TTL, not archived.** Identifiers and
  permalinks persist; content expires. The app stays undistributed.
- **The web adapter is a labelled mock by default** — see
  [Web search](#web-search).

## What is not here yet

- **Deployment.** Cloud Run + Firebase Hosting is designed and scripted
  (`make deploy`), and blocked on GCP billing — the free trial ended, and
  staying off a paid account is the only mathematical guarantee of $0. Nothing
  here has been verified against a deployed URL.
- **Playwright.** The console has unit tests for its most dangerous component
  and a boundary test in Python; the confirm flow end to end is still held by
  hand verification.
- **A second real Gmail account.** The multi-account path is implemented and
  tested — two grants on one provider produce two independent adapter runs with
  per-account status and a per-account reconnect — but it has been demonstrated
  with one real account plus a locally created second grant, not two mailboxes.

Phase-by-phase reasoning, including every defect found by using the product
rather than by testing it, lives under
`openspec/changes/unified-search-safe-send/prompts/` and in `tasks.md`.
