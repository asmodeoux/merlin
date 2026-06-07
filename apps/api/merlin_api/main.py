"""Entry point — boots uvicorn against the FastAPI app.

Kept tiny on purpose: `app.py:create_app` is the testable factory. This
module exists so the `prod` Dockerfile target can `CMD ["python", "-m",
"merlin_api.main"]` with a signal-handling shim.
"""

from __future__ import annotations

import socket

import uvicorn

from merlin_api.deps import get_settings


def run() -> None:
    """Start uvicorn with the merlin_api.app:app factory.

    Pre-opens a dual-stack listening socket so the API is reachable on
    both:

    * IPv4 (`0.0.0.0:8000`) — Fly's public edge proxy connects this way,
      and every existing health/smoke check expects to find this in
      `ss -tlnp`. The `host="0.0.0.0"` shorthand we used previously
      only covered this case.
    * IPv6 (`[::]:8000`) — Fly's 6PN private network is IPv6-only. The
      bot's `http://merlin-api.internal:8000` calls hit a 6PN address,
      and an IPv4-only listener silently drops them with `All connection
      attempts failed` (which is exactly the `/me` failure mode that
      sent us hunting in 2026-06-07).

    Binding on the AF_INET6 socket with ``IPV6_V6ONLY`` explicitly off
    is the portable way to get both at once. Plain ``host="::"`` works
    on most Linux defaults (``net.ipv6.bindv6only=0``) but not on Fly's
    base image (some Firecracker kernels default to v6only=1) — so we
    set the sockopt ourselves rather than depending on sysctl drift.
    """
    settings = get_settings()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::", 8000))
    sock.listen(2048)
    uvicorn.run(
        "merlin_api.app:app",
        fd=sock.fileno(),
        log_level=settings.log_level.lower(),
        # `--reload` only in dev; the prod Dockerfile target passes
        # `python -m merlin_api.main` with no reload.
        reload=False,
    )


if __name__ == "__main__":
    run()
