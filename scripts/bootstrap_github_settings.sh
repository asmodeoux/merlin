#!/usr/bin/env bash
# Apply the GitHub repo-settings checklist from docs/REPO_SETTINGS.md.
# Idempotent — re-run any time to recover from drift.
#
# Usage:
#   GH_REPO=org/repo ./scripts/bootstrap_github_settings.sh
#
# Requires `gh` CLI authenticated as a repo admin.

set -euo pipefail

: "${GH_REPO:?Set GH_REPO=org/repo}"

echo "  Applying repo settings to ${GH_REPO}..."

# ---------------------------------------------------------------------------
# Repository — general
# ---------------------------------------------------------------------------
gh api -X PATCH "/repos/${GH_REPO}" \
  -F has_issues=true \
  -F has_wiki=false \
  -F has_projects=false \
  -F has_discussions=false \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F allow_auto_merge=false \
  -F delete_branch_on_merge=true \
  -F allow_forking=false \
  >/dev/null
echo "  ✓ General settings applied"

# ---------------------------------------------------------------------------
# Code security
# ---------------------------------------------------------------------------
# Vulnerability alerts (Dependabot security alerts).
gh api -X PUT "/repos/${GH_REPO}/vulnerability-alerts" >/dev/null
echo "  ✓ Dependabot alerts enabled"

# Automated security fixes (Dependabot security updates).
gh api -X PUT "/repos/${GH_REPO}/automated-security-fixes" >/dev/null
echo "  ✓ Dependabot security updates enabled"

# Secret scanning + push protection.
gh api -X PATCH "/repos/${GH_REPO}" --input - <<'JSON' >/dev/null
{
  "security_and_analysis": {
    "secret_scanning": {"status": "enabled"},
    "secret_scanning_push_protection": {"status": "enabled"},
    "dependabot_security_updates": {"status": "enabled"},
    "secret_scanning_validity_checks": {"status": "enabled"}
  }
}
JSON
echo "  ✓ Secret scanning + push protection enabled"

# Private vulnerability reporting.
gh api -X PUT "/repos/${GH_REPO}/private-vulnerability-reporting" >/dev/null
echo "  ✓ Private vulnerability reporting enabled"

# ---------------------------------------------------------------------------
# Actions — workflow permissions
# ---------------------------------------------------------------------------
gh api -X PUT "/repos/${GH_REPO}/actions/permissions/workflow" --input - <<'JSON' >/dev/null
{
  "default_workflow_permissions": "read",
  "can_approve_pull_request_reviews": false
}
JSON
echo "  ✓ Workflow permissions = read-only by default"

# Restrict to GitHub-owned + select third-party actions.
gh api -X PUT "/repos/${GH_REPO}/actions/permissions" --input - <<'JSON' >/dev/null
{
  "enabled": true,
  "allowed_actions": "selected"
}
JSON

gh api -X PUT "/repos/${GH_REPO}/actions/permissions/selected-actions" --input - <<'JSON' >/dev/null
{
  "github_owned_allowed": true,
  "verified_allowed": false,
  "patterns_allowed": [
    "astral-sh/setup-uv@*",
    "gitleaks/gitleaks-action@*",
    "dorny/paths-filter@*",
    "docker/setup-buildx-action@*"
  ]
}
JSON
echo "  ✓ Actions allowlist applied"

# ---------------------------------------------------------------------------
# Branch protection — main
# ---------------------------------------------------------------------------
gh api -X PUT "/repos/${GH_REPO}/branches/main/protection" --input - <<'JSON' >/dev/null
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      {"context": "CI summary"},
      {"context": "gitleaks (secret scan)"},
      {"context": "pip-audit (Python advisories)"},
      {"context": "prod-image integration suite"}
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "require_last_push_approval": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
echo "  ✓ main branch protection applied"

echo ""
echo "Done. Re-run any time to recover from drift."
