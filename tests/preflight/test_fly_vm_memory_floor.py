"""Fly.io ``[[vm]] memory`` floor for the worker.

Regression suite for the OOM-kills-mid-job class of defect.

The live failure (2026-06-08): the worker app shipped with
``apps/worker/fly.toml`` set to ``memory = "256mb"``. RQ fires the
scheduled ``resolve_outcome`` job, which forks a work-horse process
that lazy-imports SQLAlchemy + asyncpg + httpx + the Telegram
notifier on top of:

* the parent worker process (BLPOP'ing on Redis),
* the ``with_scheduler=True`` subprocess (RQScheduler heartbeating
  every second),
* the Firecracker init + fly-init overhead.

The aggregate RSS hits the 256mb VM ceiling and Linux OOM-kills the
``uv`` process group. Fly reboots the machine ~15s later but RQ's
``Retry(max=3, interval=[60, 300, 900])`` policy only fires on
*raised* exceptions — a SIGKILL leaves the job orphaned in Redis and
the user without an outcome notification. (See the prod log excerpt
in ``apps/worker/fly.toml``'s ``[[vm]]`` block.)

The bot is exempt because:

* It does **not** fork a work-horse (single PTB ``Application``
  long-poll loop).
* Its hot imports (``telegram.ext`` + ``httpx``) are well below the
  256mb floor in practice.

The API floor is pinned at the bot's level (``>= bot``) because the
API runs uvicorn + the same lazy-imported infrastructure stack, and
any future regression there would also surface as a hard 500 wave
before the OOM-kill drops the machine.

The pattern matches ``tests.preflight.test_fly_release_command``:
a one-line config drift on a single ``fly.toml`` is the kind of
defect that only manifests at deploy time, so the contract is pinned
in CI instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_API_FLY = _REPO_ROOT / "apps" / "api" / "fly.toml"
_BOT_FLY = _REPO_ROOT / "apps" / "bot" / "fly.toml"
_WORKER_FLY = _REPO_ROOT / "apps" / "worker" / "fly.toml"

# Floors below which Fly will OOM-kill the relevant service at first
# real load. See module docstring for the prod incident that motivated
# the worker floor; the api/bot floors are the conservative status quo
# pinned forward so a future "let's shave $3/mo" diff doesn't drop
# either app under its known-good memory budget.
_FLOOR_MB_BY_APP = {
    "worker": 512,
    "api": 512,
    "bot": 256,
}

# Accepts the Fly machine-spec syntax: "256mb", "1gb", "1024", "2 gb".
# Case-insensitive; whitespace tolerated. Returns the value in MiB so
# the floor comparison is unit-agnostic.
_MEMORY_RE = re.compile(r"^\s*(?P<value>\d+)\s*(?P<unit>mb|gb)?\s*$", re.IGNORECASE)


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _parse_memory_mb(raw: str | int) -> int:
    """Return the memory value in MiB.

    Fly accepts ``"256mb"`` / ``"1gb"`` strings and bare integers
    (interpreted as MiB by ``fly``). The preflight is permissive in
    what it accepts so a future re-spelling doesn't need to update
    the test — only the floor does.
    """
    if isinstance(raw, int):
        return raw
    match = _MEMORY_RE.match(raw)
    if not match:
        raise AssertionError(
            f"could not parse fly.toml [[vm]] memory value {raw!r}; "
            "expected '256mb', '1gb', or a bare integer (MiB)"
        )
    value = int(match.group("value"))
    unit = (match.group("unit") or "mb").lower()
    return value * 1024 if unit == "gb" else value


def _vm_memory_mb(fly_toml: Path) -> int:
    cfg = _load(fly_toml)
    vms = cfg.get("vm")
    assert vms, (
        f"{fly_toml.relative_to(_REPO_ROOT)} is missing the [[vm]] table. "
        "Every Fly app must pin its machine spec so memory floors are "
        "explicit + reviewable in PR."
    )
    # Fly accepts multiple [[vm]] blocks (process-group scoped); the
    # preflight cares about the lowest one — that's what the smallest
    # process group runs under, and OOM is a per-process-group property.
    raw_values = [vm.get("memory") for vm in vms if vm.get("memory") is not None]
    assert raw_values, (
        f"{fly_toml.relative_to(_REPO_ROOT)} [[vm]] blocks declare no "
        "`memory =` keys. Set an explicit floor — Fly's default is too "
        "small for any of our services under real load."
    )
    return min(_parse_memory_mb(raw) for raw in raw_values)


@pytest.mark.parametrize(
    ("fly_toml", "app_label"),
    [
        pytest.param(_WORKER_FLY, "worker", id="worker"),
        pytest.param(_API_FLY, "api", id="api"),
        pytest.param(_BOT_FLY, "bot", id="bot"),
    ],
)
def test_fly_vm_memory_above_floor(fly_toml: Path, app_label: str) -> None:
    """Each Fly app's [[vm]] memory must meet the documented floor.

    Failure here means a regression on the OOM defect — read the
    module docstring for the prod incident, and bump the [[vm]] block
    in the offending fly.toml back to ``>= _FLOOR_MB_BY_APP[app]``
    before merging. Lowering the floor itself is allowed only with a
    matching commit message that cites measured RSS under prod load.
    """
    floor = _FLOOR_MB_BY_APP[app_label]
    actual = _vm_memory_mb(fly_toml)
    assert actual >= floor, (
        f"{fly_toml.relative_to(_REPO_ROOT)} [[vm]] memory is "
        f"{actual} MiB, below the documented floor of {floor} MiB for "
        f"the {app_label} service. The worker floor was set after a "
        "live OOM kill mid-`resolve_outcome` — see the module docstring "
        "and the fly.toml comment block for the postmortem. Either raise "
        "the memory back to the floor or, if you have measured RSS data "
        "justifying a lower number, update _FLOOR_MB_BY_APP in this test "
        "in the same commit."
    )


def test_worker_memory_at_or_above_api() -> None:
    """The worker should never carry less memory than the API.

    The worker forks a work-horse per job AND runs an extra scheduler
    subprocess; the API serves stateless HTTP from a single uvicorn
    worker. Whatever footprint the API needs, the worker needs at
    least as much under load. Pinning the relationship (instead of
    only the absolute floor) means a future "the API now needs 1gb"
    bump automatically protects the worker too.
    """
    api_mb = _vm_memory_mb(_API_FLY)
    worker_mb = _vm_memory_mb(_WORKER_FLY)
    assert worker_mb >= api_mb, (
        f"apps/worker/fly.toml [[vm]] memory ({worker_mb} MiB) is below "
        f"apps/api/fly.toml [[vm]] memory ({api_mb} MiB). The worker "
        "forks per job + runs the RQ scheduler subprocess, so it MUST "
        "carry at least the API's memory budget. Bump apps/worker/fly.toml "
        "[[vm]] memory back to at least the API's value."
    )
