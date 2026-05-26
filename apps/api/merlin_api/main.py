"""Entry point — boots uvicorn against the FastAPI app.

Kept tiny on purpose: `app.py:create_app` is the testable factory. This
module exists so the `prod` Dockerfile target can `CMD ["python", "-m",
"merlin_api.main"]` with a signal-handling shim.
"""

from __future__ import annotations

import uvicorn

from merlin_api.deps import get_settings


def run() -> None:
    """Start uvicorn with the merlin_api.app:app factory."""
    settings = get_settings()
    uvicorn.run(
        "merlin_api.app:app",
        host="0.0.0.0",  # noqa: S104 — container; Fly handles ingress
        port=8000,
        log_level=settings.log_level.lower(),
        # `--reload` only in dev; the prod Dockerfile target passes
        # `python -m merlin_api.main` with no reload.
        reload=False,
    )


if __name__ == "__main__":
    run()
