"""Exception handlers — map domain errors to stable HTTP responses.

Route handlers can raise the typed repository errors from
``merlin_core.application.ports`` without knowing the wire shape; the
handlers below translate each into a JSON body of the form::

    {"detail": "<human-readable>", "code": "<machine-readable>"}

Picking a stable ``code`` field gives the bot something to switch on
without parsing the prose. Status codes:

* ``InviteNotFoundError``          → 404 (``invite_not_found``)
* ``InviteAlreadyRedeemedError``   → 409 (``invite_already_redeemed``)
* ``SourceAlreadyExistsError``     → 409 (``source_already_exists``)
* ``RepositoryError`` (catchall)   → 500 (``repository_error``)

The catchall sits at the *base* class so any new
``RepositoryError`` subclass added later still maps to a useful 500
rather than the bare ``Internal Server Error`` an unhandled exception
would produce. New specific subclasses should land here with an
explicit handler in the same change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from merlin_core.application.ports import (
    InviteAlreadyRedeemedError,
    InviteNotFoundError,
    RepositoryError,
    SourceAlreadyExistsError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


def _body(detail: str, code: str) -> dict[str, str]:
    """Stable error body shape."""
    return {"detail": detail, "code": code}


def register_exception_handlers(app: FastAPI) -> None:
    """Wire FastAPI exception handlers for the repository error hierarchy.

    Order matters when subclasses share a handler: FastAPI matches the
    *most specific* registered type for a given raised exception, so
    registering ``RepositoryError`` last is safe — the specific 4xx
    handlers above still win for their own classes.
    """

    @app.exception_handler(InviteNotFoundError)
    async def _invite_not_found(_req: Request, exc: InviteNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_body(str(exc) or "invite not found", "invite_not_found"),
        )

    @app.exception_handler(InviteAlreadyRedeemedError)
    async def _invite_consumed(
        _req: Request,
        exc: InviteAlreadyRedeemedError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_body(
                str(exc) or "invite already redeemed",
                "invite_already_redeemed",
            ),
        )

    @app.exception_handler(SourceAlreadyExistsError)
    async def _source_exists(
        _req: Request,
        exc: SourceAlreadyExistsError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_body(
                str(exc) or "source already exists",
                "source_already_exists",
            ),
        )

    @app.exception_handler(RepositoryError)
    async def _repo_catchall(_req: Request, exc: RepositoryError) -> JSONResponse:
        # Catchall for any RepositoryError subclass not handled above.
        # The catchall is intentionally generic — the stable ``code`` is
        # what the bot keys off, and a 500 makes the bot's
        # "Something's off on our end" branch fire. New specific
        # subclasses should add their own handler above this one.
        return JSONResponse(
            status_code=500,
            content=_body(str(exc) or "repository error", "repository_error"),
        )
