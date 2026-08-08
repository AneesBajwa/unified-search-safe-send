# Demo run-through

Every screenshot below is a real capture from the deployed app, taken by walking
this exact path. Follow it top to bottom and you have the recording.

**Format:** each step gives you the screen, then **Do** (the click) and **Say**
(the point being made). Target 15–30 minutes: ~12 for part one, ~13 for part two.

- **App** — <https://unified-search-1785899621.web.app>
- **API** — `https://api-88631762875.us-east4.run.app`

---

## Before you hit record

**1. Reset the seed** so history reads cleanly:

```bash
DATABASE_URL='<neon-pooled-url>' uv run python scripts/seed.py
```

**2. Turn on the slow source** for the partial-results segment. The demo is much
weaker without it, and it is off in normal operation so reviewers are not slowed:

```bash
gcloud run services update worker --region=us-east4 \
  --update-env-vars=SLOW_ADAPTER_SOURCE=slack,SLOW_ADAPTER_DELAY_MS=8000
```

**Turn it off again when you finish recording:**

```bash
gcloud run services update worker --region=us-east4 \
  --remove-env-vars=SLOW_ADAPTER_SOURCE,SLOW_ADAPTER_DELAY_MS
```

**3. Sign out** (or use a fresh browser profile) so you start where a reviewer
starts. **4.** Have a terminal ready for the `curl` segment at the end.

---

# Part one — the working system

## 1 · What a stranger sees first

![Sign-in screen with three options](images/demo/01-sign-in.png)

**Do:** Land on the app signed out. Read the three options aloud, then click
**Sign in as a brand-new user**.

**Say:** *"Sign-in is an address, not a password — this is a proof of concept and
that is a deliberate trade I will come back to. Three states are worth seeing, so
each is one click. Let me start where a reviewer starts: an account with nothing
connected at all."*

---

## 2 · A brand-new user searches before connecting anything

![Brand-new user: Gmail and Slack offer Connect, web returns results](images/demo/02-brand-new-user-search.png)

**Do:** The query box is pre-filled. Click **Search**.

**Say:** *"Nothing is connected, and notice what that is not. It is not a red
failure, and it does not say 'reconnect' for an account nobody ever linked — both
providers offer an inline **Connect**. Meanwhile the web source needs no grant
and returns results, so the product is useful in the first five seconds. The
distinction between 'never connected', 'needs reconnecting' and 'genuinely
failed' runs through the whole system."*

---

## 3 · Connections

![Two live connections, both active](images/demo/03-connections.png)

**Do:** Sign out, sign in with the default `console@example.test`, then open
**Accounts**.

**Say:** *"This account has a throwaway Gmail and a Slack workspace. Each is a
separate OAuth grant, scoped narrowly — four Gmail scopes, and `gmail.compose`
rather than `gmail.send`, which I will explain when we get to sending. Note
'Connect another account': multiple accounts per provider is a first-class case,
not a limit I am working around."*

---

## 4 · One query, three sources, and a slow one that blocks nothing

![Partial results: 2 of 3 sources reported, Slack still searching](images/demo/04-partial-results-slack-working.png)

**Do:** Go to **Search**, type `acme renewal`, click **Search**. Stay on this
screen while Slack is still working — this is the moment worth pausing on.

**Say:** *"Slack is artificially delayed by eight seconds. Look at the header: **two
of three sources reported, showing partial results**. Gmail says 'no matches' —
that is a completed source with nothing in it, which is different from a failure.
Slack says **searching**. And the web results are already on screen and usable.
Nothing is waiting for the slowest source. Each source is an independent
background job writing its own status in its own transaction, which is what makes
this readable mid-flight rather than a spinner over the whole page."*

![All three sources reported and merged](images/demo/05-merged-all-three.png)

**Do:** Wait for Slack to land.

**Say:** *"Slack lands and merges into the one ranked list. Every result has the
same closed shape regardless of source — the merge layer literally cannot name a
source; there is a test that greps it for the words 'gmail', 'slack' and 'web'
and fails on a hit. Adding a fourth source is one adapter file and one registry
line."*

---

## 5 · Compose, and the gate

![Compose form](images/demo/06-compose.png)

**Do:** Click **Draft a message** on a result — or open **Compose** — and fill in
recipient, subject and body.

**Say:** *"Creating a draft contacts no provider at all. It is inert: no job, no
external effect, nothing observable from outside. The next screen is the only way
anything leaves this system."*

![The confirm gate naming recipient, sending account and exact body](images/demo/07-confirm-gate.png)

**Do:** Click **Review before sending**. Pause here.

**Say:** *"This is the gate. It names the recipient, it names **which account it
goes out from**, and it shows the exact body. The confirmation is a SHA-256 over
channel, recipient, subject and body — so if I go back and edit one character,
this confirmation stops being valid and the send is refused. That is arithmetic,
not bookkeeping that could drift. There is no route anywhere in the API that
takes a recipient and a body and delivers. The absence is the feature."*

---

## 6 · Double-tap the confirm button

![One send recorded after two rapid clicks](images/demo/08-double-tap-one-send.png)

**Do:** Click **Send it** twice, as fast as you can. Then click **Open the send**.

**Say:** *"I clicked that twice. One send."*

![Send detail: delivered, attempts 1 of 6, provider message id](images/demo/09-send-delivered-detail.png)

**Say:** *"Delivered, **attempt 1 of 6**, with the provider's own message id and
the exact text that was transmitted. Not two attempts that got deduplicated — one
attempt. The draft carries an idempotency key and the gate claims it before doing
anything."*

### Now prove it at the provider, not on our own status page

![Search showing exactly one Gmail copy of the sent message](images/demo/10-exactly-once-evidence.png)

**Do:** Search for the subject you just used.

**Say:** *"Our status page saying 'delivered' proves nothing about how many
messages the provider actually received. So here is the recipient's mailbox,
through the product: **Gmail, one**. Two clicks, one message. And all three
sources are reporting — Gmail now has a hit because the message I just sent is
genuinely there."*

---

## 7 · Break a grant, then repair it in one click

![Gmail chip says Reconnect with the real invalid_grant payload](images/demo/11-revoked-grant-needs-reconnect.png)

**Do:** Invalidate the stored token, then re-run the search:

```bash
DATABASE_URL='<neon-pooled-url>' TOKEN_KEYRING='<keyring>' \
  uv run python -c "
import asyncio
from core.connections.service import invalidate_stored_token
asyncio.run(invalidate_stored_token(5))"
```

**Say:** *"I have replaced the stored refresh token with a well-formed but dead
one — deliberately not a corrupt ciphertext, because a corrupt envelope is *our*
bug and classifies differently. Google returns `invalid_grant`, and the result is
a distinct **needs reconnecting** state carrying the real provider payload, not a
generic failure. Two things matter here: the fix is offered right where the
failure appeared, and **Slack and web are completely unaffected** — one broken
grant does not break the search."*

**Do:** Click **Reconnect Gmail · testuser.inbound@gmail.com** and complete the
Google consent (**Advanced → Go to … (unsafe)**, since the app is published but
unverified).

![The same search now succeeds](images/demo/12-after-reconnect-succeeds.png)

**Say:** *"Same connection id — **five**, before and after. That matters: drafts,
sends and historical runs all reference that row, so a reconnect that created a
new connection would orphan them. The re-grant is matched by the provider's
subject id, and authorizing a *different* account against a reconnect is refused
by name rather than silently overwriting. And the search that just failed now
succeeds."*

---

## 8 · History, and the failures worth keeping

![History with every status, seed-badged](images/demo/13-history-every-status.png)

**Do:** Open **History** on the seeded account.

**Say:** *"Every send and every search is recorded, including — especially — the
ones that failed. Everything here is badged **seed** so synthetic data can never
be mistaken for something you did, and there is a filter to exclude it. This
covers every status the runtime can actually produce."*

![Failed send detail: attempts, full provider error, operator retry](images/demo/14-failed-send-detail.png)

**Do:** Open the failed send.

**Say:** *"Attempt count, the **untruncated** provider error — the actual Gmail
503 JSON, not a summary of it — and an operator retry that resumes this record
rather than starting a fresh one. Transient failures like this one retry
themselves with full-jitter backoff, and a provider's `Retry-After` always wins
over our computed delay. A permanent failure — invalid recipient, revoked auth —
is surfaced immediately and never auto-retried, because the operator decides."*

---

## 9 · The state I am proudest of

![Uncertain send with two resolutions and no retry](images/demo/15-uncertain-two-resolutions.png)

**Do:** Open the `uncertain` send. Slow down here.

**Say:** *"We dispatched, and then we could not find out what happened. Exactly-once
delivery to a provider with no idempotency key is **impossible** — it reduces to
the Two Generals problem. So the guarantee is exactly-once *effect*, and where
the effect is genuinely unknown I expose the residue instead of guessing.*

*Notice there is no retry button. Re-sending a message that may already have
arrived is the exact misfire this product exists to prevent. Instead there are two
honest resolutions — 'I can see it, mark it delivered', which records an
attestation and invents no provider id, and 'it is not there, send it again',
which is the only path that clears the dispatch record. Plus the evidence needed
to choose: when we dispatched, how many times we probed, and a link into the
user's own mailbox.*

*A system that says 'failed' when it means 'I don't know' is lying to you."*

⚠️ Resolving this row destroys it. Rehearse on the seeded one; re-run `make seed`
to restore.

---

## 10 · Mobile

![History on a 390px viewport](images/demo/16-mobile-history.png)
![The uncertain state on mobile](images/demo/17-mobile-uncertain.png)

**Do:** Open the same screens at phone width (or on your phone).

**Say:** *"Every surface is mobile-first, not merely responsive — the navigation
becomes a bottom tab bar within thumb reach, and the confirm screen keeps its
actions pinned at the bottom. The dangerous button is never somewhere you hit by
accident."*

---

## 11 · The whole loop with no UI at all

**Do:** Switch to a terminal and run this live.

```bash
API=https://api-88631762875.us-east4.run.app
KEY=$(curl -sX POST $API/v1/auth/dev-login -H 'content-type: application/json' \
        -d '{"email":"console@example.test"}' | jq -r .key)
AUTH="X-API-Key: $KEY"

DRAFT=$(curl -sX POST $API/v1/drafts -H "$AUTH" -H 'content-type: application/json' \
  -d '{"channel":"slack","to":"#social","body":"Posted from curl with no UI running."}')
ID=$(jq -r .draft.id <<<"$DRAFT"); SHA=$(jq -r .confirmation.confirm_sha256 <<<"$DRAFT")

# 1 — no digest, no send
curl -sX POST $API/v1/drafts/$ID/send -H "$AUTH" -d '{}' | jq -r .error.code

# 2 — with the digest
curl -isX POST $API/v1/drafts/$ID/send -H "$AUTH" -H 'content-type: application/json' \
  -d "{\"confirmed_sha256\":\"$SHA\"}" | grep -iE 'HTTP|idempotent'

# 3 — the identical call again
curl -isX POST $API/v1/drafts/$ID/send -H "$AUTH" -H 'content-type: application/json' \
  -d "{\"confirmed_sha256\":\"$SHA\"}" | grep -iE 'HTTP|idempotent'
```

Real output from this run:

```
1 → confirmation_required

2 → HTTP/2 201
     idempotent-replayed: false

3 → HTTP/2 200
     idempotent-replayed: true
     same send_id, provider_message_id C0BM77XUEBX:1786162590.716339
```

**Say:** *"No UI process is running. The console is a pure consumer — every route
it uses is API-key authenticated, there is no cookie and no browser-session path,
so there is nothing the console can reach that a script cannot. The refusal
without a digest holds here too: the gate is in the module, not in the front
end."*

---

# Part two — the architecture

Lead with decisions, not the file tree. Full reasoning in
**[DESIGN.md](DESIGN.md)** — these are the beats worth saying out loud.

### The adapter layer (~2.5 min) → [DESIGN](DESIGN.md#the-adapter-layer)

One protocol; `AdapterContext` carries a **lazy** token getter and deliberately no
database session, so an adapter cannot reach past its own job.

> *"One durable job **per connection**, not per source. That was wrong until very
> late: the fan-out built a map from provider to connection id and kept the last
> one, so a user with two Gmail accounts had one silently never searched — behind
> a single healthy-looking chip. Every test used one account per provider, so
> nothing caught it. The spec said 'one run per connection' and the code did not."*

### Multi-account OAuth (~3 min) → [DESIGN](DESIGN.md#multi-account-oauth)

> *"Refresh tokens are encrypted with an envelope key **bound to the row id as
> AAD**, so ciphertext lifted into another row will not decrypt. The key is a
> mounted file, not an env var — env vars resolve at instance start, so rotating
> one forces a redeploy, and they leak through `/proc/self/environ`."*

> *"Refresh happens under a Postgres advisory lock, so twenty concurrent adapter
> runs produce one refresh, not twenty. And a raw 401 is **not** a dead grant —
> only `invalid_grant` from the refresh endpoint is. Escalating every 401 would
> disconnect healthy users constantly."*

### The send gate and the crash window (~4 min) — the strongest section → [DESIGN](DESIGN.md#the-send-gate)

> *"Delivery is draft-then-send rather than `messages.send`, because Gmail's draft
> id is a server-side idempotency token whose **existence** is the state machine.
> If we die between dispatch and record, we ask Gmail whether the draft still
> exists instead of guessing."*

> *"That also buys the user something: a draft is a visible record in their own
> mailbox, so an in-doubt send is something they can resolve by looking, rather
> than by trusting my status page. That is why the scope is `gmail.compose` and
> never `gmail.send`."*

> *"Exactly-once is proven two ways a single-threaded test cannot reach:
> concurrent duplicates with real OS threads released by a barrier, and a crash
> injected at **every commit seam** between dispatch and record. The crash is a
> `BaseException` subclass so production `except Exception` handlers cannot
> swallow it — otherwise the test would be exercising the error path while
> claiming to exercise the crash path."*

### Why the worker is push-driven (~1.5 min) → [DESIGN](DESIGN.md#why-the-worker-is-push-driven)

> *"A resident polling worker on Cloud Run is correct and costs **$44.71 a month**.
> There is no configuration of an always-on process on this platform that is
> free. So work is dispatched, not discovered: Cloud Tasks on the hot path, Cloud
> Scheduler for the sweep. It is also *lower* latency than the poll it replaced."*

> *"The trap avoided: Cloud Run **Jobs** bill a one-minute minimum per execution,
> so a three-second job every minute costs the same as running 24/7. A Cloud Run
> *service* rounds to 100 ms."*

### Tradeoffs and what I would change (~2 min) → [DESIGN](DESIGN.md#tradeoffs-and-what-i-would-change)

Be direct about the accepted risks: passwordless sign-in, the API key in
`sessionStorage`, the labelled web-search mock, envelope keys in a keyring string
rather than a KMS. Then the ordered list of what comes next — Playwright over the
confirm flow first, then real identity in front of key issuance.

---

## Closing (~30 s)

> *"The two things I would want judged are the adapter layer and the send gate.
> Both are a standalone module behind a documented API — the console is a pure
> consumer, and I ran the whole loop through `curl` with no UI running. And the
> part I would defend hardest is the amber one: a system that says 'failed' when
> it means 'I don't know' is lying to you, and this one does not."*

---

## Timing

| Segment | Target |
|---|---|
| First run, connections | 2:00 |
| Unified search + slow source | 2:30 |
| Compose → gate → double-tap → evidence | 3:30 |
| Revoke → reconnect | 2:00 |
| History, failures, `uncertain` | 2:30 |
| Mobile + curl | 1:30 |
| **Part one** | **~14:00** |
| Adapters, OAuth, send gate, worker, tradeoffs | ~13:00 |
| **Total** | **~27:00** |

If you are running long, cut the mobile segment and the web-search aside — never
the `uncertain` state or the double-tap.
