"""The error envelope, and the one place a code is turned into a response.

Shape is `application/problem+json`-ish, always carrying a ``code`` and a
``classification`` so a client can decide whether retrying is meaningful without
parsing prose:

    {"error": {"code": "...", "classification": "...", "message": "..."}}

The ``config`` classification is why this is worth structuring at all: a rotated
client secret must never render as "reconnect your account", which sends the
user round in circles fixing a grant that was never broken (risks.md R24).

🔴 **Status and classification are looked up, never passed** (task 9.11).
``ApiError`` takes a code and a message; everything else comes from
``catalog.py``. Before this, each raise site restated its own status, and one of
them disagreed — ``confirmation_required`` was 422 at the send gate and 409 when
refusing to retry an in-doubt send, which is a code no client can branch on. A
code that is not in the catalog now raises at the raise site rather than
reaching a client.

``SendGateError`` comes out of ``core``, which cannot import a web framework (the
import-linter contract) and therefore cannot import the catalog either — it
carries its own status. ``test_error_catalog.py`` drives every gate refusal
through HTTP and asserts the two agree, so the duplication is checked rather
than trusted.
"""

from __future__ import annotations

from typing import Any

from core.security.crypto import KeyringUnavailable
from core.send.service import SendGateError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import catalog


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
    """Anything the API refuses, in the documented shape.

    ``**extra`` carries the fields only some codes have — ``reconnect_url`` on a
    revoked grant, ``retry_after`` on a throttle. Absent values are dropped, so
    a client never has to distinguish "null" from "not applicable".
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        spec = catalog.spec(code)
        self.code = code
        self.message = message
        self.status = spec.status
        self.classification = spec.classification
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return envelope(
            self.code, self.message, classification=self.classification, **self.extra
        )


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(KeyringUnavailable)
    async def _no_keyring(_request: Request, exc: KeyringUnavailable) -> JSONResponse:
        """🔴 Ours, and it must say so.

        With no key material every route that touches a credential fails —
        including `GET /connections/{provider}/authorize`, which signs its state
        with the same keyring. Unhandled it reaches the client as a bare 500 and
        a stack trace in the log, which is indistinguishable from the app being
        broken; a reviewer following the README hits it on the first thing they
        click after `cp .env.example .env`.

        `config`, never `needs_reconnect`: telling someone to reconnect an
        account sends them round in circles repairing a grant that was never
        broken, when the actual fix is one line of *our* configuration (R24).
        """
        return JSONResponse(
            envelope("internal_config_error", str(exc), classification="config"),
            status_code=catalog.spec("internal_config_error").status,
        )

    @app.exception_handler(SendGateError)
    async def _gate_error(_request: Request, exc: SendGateError) -> JSONResponse:
        # The gate raises in `core`, which cannot import a web framework (the
        # import-linter contract). Translating here is the price of that
        # boundary, and it is one function.
        return JSONResponse(
            envelope(
                exc.code,
                exc.message,
                classification=exc.classification,
                # `action_url` under both names, exactly as the search snapshot
                # does: `reconnect_url` is the older one and clients already
                # read it, so dropping it silently would break the one repair a
                # revoked grant has.
                action_url=exc.action_url,
                reconnect_url=exc.action_url,
            ),
            status_code=exc.status,
        )
