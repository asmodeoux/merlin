"""Apps that talk to the same upstream must agree on its base URL.

Sprint-1 live outage: the API's ``COINGECKO_BASE_URL`` was
``https://api.coingecko.com/api/v3`` (correct, ``/api/v3`` suffix), but
the worker's was ``https://api.coingecko.com`` (no suffix). The API
captured ``price_usd_at_submit`` fine on claim submission; the worker
crashed on resolution because the URL fell through to a 301 redirect to
the CoinGecko marketing site, then a 404, then a ``JSONDecodeError``.
No notification, no obvious error, three retries to exhaustion.

The defect class: two ``.env.example`` files documenting different
values for what is **conceptually one knob** — the upstream service
endpoint. ``make env-check`` doesn't catch it because env-check defends
the *intra-app* contract (Settings ↔ .env.example for the same app),
not the *cross-app* one.

This preflight pins the cross-app contract: any env var pointing at the
same external service must have the same value in every app that uses
that service.

The bar for adding to this file: there must exist a live regression
where two apps drifted on the same upstream URL/key, costing real
debugging time. Today that's CoinGecko. Add new entries the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def _env_value(app: str, key: str) -> str | None:
    """Read ``KEY=VALUE`` from ``apps/<app>/.env.example`` or return None.

    Quoted values are stripped of surrounding quotes; comments and
    blank lines are skipped. Anything more elaborate (multiline values,
    interpolation) would belong in a real .env parser — the example
    files use the plain ``KEY=VALUE`` shape and we want a tiny dep-free
    check.
    """
    path = _REPO_ROOT / "apps" / app / ".env.example"
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if (len(v) >= 2) and v[0] == v[-1] and v[0] in {'"', "'"}:
            v = v[1:-1]
        return v
    return None


# ---------------------------------------------------------------------------
# CoinGecko — the bug we just lived through
# ---------------------------------------------------------------------------


def test_coingecko_base_url_agrees_between_api_and_worker() -> None:
    """Both apps must point at the same CoinGecko endpoint.

    If one of them silently drops the ``/api/v3`` suffix, requests fall
    through to the public marketing redirect chain instead of the JSON
    API — and only the affected app sees the failure (which is hard to
    spot because the *other* app is healthy).
    """
    api_url = _env_value("api", "COINGECKO_BASE_URL")
    worker_url = _env_value("worker", "COINGECKO_BASE_URL")

    assert api_url is not None, "apps/api/.env.example must document COINGECKO_BASE_URL"
    assert worker_url is not None, "apps/worker/.env.example must document COINGECKO_BASE_URL"
    assert api_url == worker_url, (
        f"COINGECKO_BASE_URL drift: api={api_url!r} worker={worker_url!r}. "
        f"Both apps talk to the same CoinGecko upstream — they must agree on the URL. "
        f"Missing the '/api/v3' suffix causes a 301 redirect to the marketing site "
        f"and breaks /coins/{{id}}/market_chart/range on resolution."
    )


def test_coingecko_base_url_includes_api_v3_suffix() -> None:
    """Public CoinGecko endpoint requires the ``/api/v3`` path prefix.

    Pin this explicitly so the diff against the bug is unambiguous: if
    the suffix ever disappears again, the assertion message points
    straight at the failure mode.
    """
    for app in ("api", "worker"):
        url = _env_value(app, "COINGECKO_BASE_URL")
        assert url, f"apps/{app}/.env.example must document COINGECKO_BASE_URL"
        # Demo + free tiers both live under ``api.coingecko.com/api/v3``;
        # paid tier lives under ``pro-api.coingecko.com/api/v3``. Either
        # way, ``/api/v3`` is required.
        assert "/api/v3" in url, (
            f"apps/{app}/.env.example COINGECKO_BASE_URL={url!r} is missing the '/api/v3' "
            f"suffix. Without it the public host issues a 301 to the marketing site and "
            f"the resolver crashes on a non-JSON body. This is exactly the Sprint-1 "
            f"outcome-notification outage."
        )
