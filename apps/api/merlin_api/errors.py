"""Exception handlers — Sprint 1 fills with the domain error mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_exception_handlers(_app: FastAPI) -> None:
    """Wire FastAPI exception handlers.

    Sprint 1 maps `merlin_core.domain` errors → 4xx responses with a
    stable JSON shape (`{detail: str, code: str}`).
    """
