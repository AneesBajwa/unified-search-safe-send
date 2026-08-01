"""The error envelope (part of openspec task 9.3 — deliberately partial).

Group 9 owns the full catalog. What is here is what group 5 *fixes*: the send
gate's own status codes and machine-readable codes are part of its contract, not
presentation, so they land with the gate rather than waiting for the group that
formalises everything else.

Shape is `application/problem+json`-ish, always carrying a ``code`` and a
``classification`` so a client can decide whether retrying is meaningful without
parsing prose:

    {"error": {"code": "...", "classification": "...", "message": "..."}}

The ``config`` classification is why this is worth structuring at all: a rotated
client secret must never render as "reconnect your account", which sends the
user round in circles fixing a grant that was never broken (risks.md R24).
"""

from __future__ import annotations

from typing import Any

from core.send.service import SendGateError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def envelope(
    code: str, message: str, *, classification: str = "permanent", **extra: Any
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "classification": classification,
        "message": message,
    }
    error.update({key: value for key, value in extra.items() if value is not None})
    return {"error": error}


class ApiError(Exception):
    """Anything the API refuses, in the documented shape."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        classification: str = "permanent",
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.classification = classification
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return envelope(
            self.code, self.message, classification=self.classification, **self.extra
        )


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(SendGateError)
    async def _gate_error(_request: Request, exc: SendGateError) -> JSONResponse:
        # The gate raises in `core`, which cannot import a web framework (the
        # import-linter contract). Translating here is the price of that
        # boundary, and it is one function.
        return JSONResponse(
            envelope(exc.code, exc.message, classification=exc.classification),
            status_code=exc.status,
        )
