# Unified Search & Safe Send

Unified multi-provider search (Gmail, Slack, web) behind a pluggable adapter
layer, plus a safe-send gate where every outbound message is drafted,
reviewed, explicitly confirmed, and idempotent under retry.

> **Status: phase 0 — walking skeleton.** The feature set today is one
> `/health` endpoint and a page that renders it. That is deliberate: phase 0
> exists to prove the deployment path and the risky provider assumptions
> before anything is built on top of them. Design and task breakdown live in
> `openspec/changes/unified-search-safe-send/` in the parent workspace.

## Layout

```
packages/core/     the reusable module: adapters, sending, jobs, connections
apps/api/          FastAPI — REST surface, OAuth callbacks, SSE. Stateless.
apps/worker/       same image, different entrypoint. HTTP-driven, not a poll loop.
apps/web/          Vite + React 19 + TypeScript SPA. Talks HTTP only.
```

`packages/core` imports nothing from `apps/` — the dependency arrow only ever
points inward. That is enforced by an import-linter contract in
`pyproject.toml`, not asserted in prose:

```bash
uv run lint-imports
```

## Local development

Requires Docker (or Colima) and nothing else.

```bash
cp .env.example .env
docker compose up --build
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
docker compose up -d db          # or point DATABASE_URL at any Postgres 16
uv run alembic upgrade head
uv run uvicorn api.main:app --reload --port 8080
cd apps/web && npm install && npm run dev
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

## Tests

```bash
uv run pytest
uv run ruff check
uv run mypy
uv run lint-imports
```

## What is not here yet

Everything else: the adapter layer, the job runtime, the send gate, OAuth,
the REST surface, seed data, and the console. Phase 0 is scaffolding plus
proof. See the phase prompts under
`openspec/changes/unified-search-safe-send/prompts/`.
