# Repo Settings (GitHub bootstrap checklist)

One-page checklist for spinning up the GitHub repo to match our security
baseline. Pair with `scripts/bootstrap_github_settings.sh` — that script
applies the settings reproducibly via `gh api`. This doc is the canonical
list; the script is the executable form.

## 1. Repository settings

Settings → General → "Features":

- [ ] Issues — **on**
- [ ] Discussions — off (v0)
- [ ] Projects — off (v0)
- [ ] Wikis — off (we use `docs/`)
- [ ] Allow forking — off (private repo)
- [ ] Allow GitHub Actions to create and approve pull requests — **off**

Settings → General → "Pull Requests":

- [ ] Allow merge commits — off
- [ ] Allow squash merging — **on** (default merge type)
- [ ] Allow rebase merging — off
- [ ] Always suggest updating pull request branches — on
- [ ] Automatically delete head branches — **on**

## 2. Code security

Settings → Code security:

- [ ] Dependency graph — **on**
- [ ] Dependabot alerts — **on**
- [ ] Dependabot security updates — **on**
- [ ] Dependabot version updates — **on** (driven by `.github/dependabot.yml`)
- [ ] Code scanning — **on**, CodeQL default setup (Phase 2 extends to
      custom queries)
- [ ] Secret scanning — **on**
- [ ] Push protection — **on**
- [ ] Private vulnerability reporting — **on**

## 3. Actions / workflows

Settings → Actions → General:

- [ ] Actions permissions — **Allow select actions and reusable workflows**
- [ ] Allow `actions/*` — **on** (GitHub-owned)
- [ ] Allow `astral-sh/setup-uv@v3` — **on** (pinned major)
- [ ] Allow `gitleaks/gitleaks-action@v2` — **on** (pinned major)
- [ ] Allow `dorny/paths-filter@v3` — **on** (pinned major)
- [ ] Workflow permissions — **Read repository contents and packages
      permissions** (each workflow widens explicitly in YAML when needed)
- [ ] Allow GitHub Actions to create and approve pull requests — **off**

## 4. Branch protection — `main`

Settings → Branches → Branch protection rules → `main`:

- [ ] Require pull request before merging — **on**
- [ ] Required approvals — **1**
- [ ] Dismiss stale reviews on new commits — **on**
- [ ] Require review from CODEOWNERS — **on**
- [ ] Require status checks to pass before merging — **on**
  - [ ] `ci / CI summary`
  - [ ] `security / gitleaks (secret scan)`
  - [ ] `security / pip-audit (Python advisories)`
  - [ ] `docker-test / prod-image integration suite`
- [ ] Require branches to be up to date before merging — **on**
- [ ] Require conversation resolution before merging — **on**
- [ ] Require signed commits — Phase 2 (when GPG/SSH-signing process
      stabilizes)
- [ ] Require linear history — **on**
- [ ] Do not allow bypassing — **on**
- [ ] Restrict who can push to matching branches — admins only
- [ ] Allow force pushes — **off**
- [ ] Allow deletions — **off**

## 5. Tags / releases

- [ ] Tag protection — `v*` requires admin
- [ ] Auto-generate release notes — on

## 6. Bootstrapping via script

```bash
# Requires `gh` CLI authenticated as a repo admin.
GH_REPO=org/repo ./scripts/bootstrap_github_settings.sh
```

The script applies the settings above via `gh api`; re-running is
idempotent.

## 7. Audit cadence

- Quarterly — re-run `scripts/bootstrap_github_settings.sh` to detect
  drift.
- After every GitHub Actions security advisory — re-confirm `Allow
  select actions` allowlist.
- On every CODEOWNERS change — re-verify branch-protection still cites
  the right teams.
