#!/usr/bin/env bash
# scripts/docs_check.sh — rg-based grep gate for forbidden tokens.
#
# Wired into:
#   * `make docs-check`
#   * `make ci`
#   * `.github/workflows/ci.yml` (transitively via `make ci`)
#
# Per the bot-flow plan §"Sprint 1 acceptance criteria" #3
# ("Documentation parity"). Catches drift between the current
# architecture and stale references that survive a search-and-replace.
#
# A line is allowed if any of:
#   * the file is in the global allowlist (historical / decision logs);
#   * the line contains a ``Q-NNN`` marker (legitimate reference to the
#     decision that dropped the token, e.g. "no pg_cron (Q-029)");
#   * the line contains ``docs-check-ok`` (explicit per-line override).
#
# Otherwise the gate fails. Run locally with ``make docs-check``.

set -euo pipefail

# Forbidden tokens (regex). Anchor where meaningful.
declare -a TOKENS=(
  'COINGECKO_DEMO_API_KEY'
  'pg_cron'
  'seed_db'
  'sync_asset_catalog'
  'simulated size'
  'not scored at v0'
)

# Files exempted globally. Historical / decision-log files may
# legitimately reference any forbidden token.
declare -a ALLOWLIST=(
  ':!docs/DECISIONS.md'
  ':!CHANGELOG.md'
  ':!scripts/docs_check.sh'
  ':!.github/workflows/security.yml'
)

# Lines containing any of these markers are allowed even outside the
# global allowlist — they're explaining the decision, not using it.
LINE_ALLOW_RE='(Q-0[0-9]{2}|docs-check-ok)'

color_ok="\033[1;32m"
color_fail="\033[1;31m"
color_reset="\033[0m"

ok()   { printf "  ${color_ok}✓${color_reset} %s\n" "$1"; }
fail() { printf "  ${color_fail}✗${color_reset} %s\n" "$1"; }

failed=0

echo "docs-check: scanning for forbidden tokens…"

for token in "${TOKENS[@]}"; do
  raw=$(git grep -nIE "$token" -- . "${ALLOWLIST[@]}" 2>/dev/null || true)
  matches=$(echo "$raw" | grep -vE "$LINE_ALLOW_RE" || true)
  if [[ -n "$matches" ]]; then
    fail "forbidden token: $token"
    echo "$matches" | sed 's/^/      /'
    failed=1
  else
    ok "no unmarked \"$token\""
  fi
done

# Python-source-only scan for `/v1/`. Doc references are caught by the
# generic per-file allowlist; this catches a literal in the route layer.
raw_v1=$(git grep -nIE '/v1/' -- 'apps/**/*.py' 'packages/**/*.py' 2>/dev/null || true)
v1_matches=$(echo "$raw_v1" | grep -vE "$LINE_ALLOW_RE" || true)
if [[ -n "$v1_matches" ]]; then
  fail "forbidden \`/v1/\` literal in Python source"
  echo "$v1_matches" | sed 's/^/      /'
  failed=1
else
  ok "no unmarked \"/v1/\" in Python source"
fi

if (( failed != 0 )); then
  echo ""
  fail "docs-check FAILED — see lines above. Either:"
  echo "      * update the offending reference to current architecture,"
  echo "      * add a ``Q-NNN`` marker (e.g. ``(Q-029)``) on the same line"
  echo "        to flag it as a legitimate reference to the decision, or"
  echo "      * if it's purely historical, move it to docs/DECISIONS.md."
  exit 1
fi

echo ""
ok "docs-check clean"
