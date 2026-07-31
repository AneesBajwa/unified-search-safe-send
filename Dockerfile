# One image, two entrypoints (design D2): `api` and `worker` differ only in the
# uvicorn target, so they share a build and Artifact Registry stores one layer
# set rather than two.
#
# Build for Cloud Run from an Apple Silicon machine with:
#   docker buildx build --platform linux/amd64 -t "$IMG" --push .
# The platform flag is mandatory. An arm64 image fails on Cloud Run with an
# opaque "container failed to start" and no useful log line.
#
# Deliberately NOT `gcloud run deploy --source`: that pulls in Cloud Build,
# auto-creates a repository, and produces buildpack images 2-3x larger than
# this, which blows the 0.5 GiB Artifact Registry free tier.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a source-only change does not re-resolve them.
COPY pyproject.toml README.md ./
RUN uv pip install --system --no-cache \
      fastapi 'uvicorn[standard]' sqlmodel 'sqlalchemy[asyncio]' asyncpg \
      alembic httpx cryptography pydantic-settings

COPY alembic.ini ./
COPY packages ./packages
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker

RUN uv pip install --system --no-cache --no-deps -e .

# Cloud Run injects PORT and ignores EXPOSE; this is for local readers.
EXPOSE 8080
ENV PORT=8080

# Overridden to `worker.main:app` for the worker service.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
