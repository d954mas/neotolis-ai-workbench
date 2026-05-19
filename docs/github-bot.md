# GitHub bot setup (GIT-05)

This is the operator cookbook for the GitHub-side trust boundary that
complements the controller-side git policy (`docs/git-policy.md`,
requirement `GIT-04`).

Apply this page once per repository the bot will touch. There is no
automation: GitHub App private-key download is web-UI-only, so a script
that hides the web steps would lie. Follow the four sections in order.

Related requirements: `GIT-05`.

## Trust model

The bot is a GitHub App owned by the operator. It is installed on one or
more repositories and used by Pi (inside a task container) to upload
work-in-progress to `agent/<task-id>` branches.

What the bot can do:

- Read repository metadata
- Read and write repository contents on branches matching `refs/heads/agent/*`
- Open and update pull requests targeting `main` / `master`

What the bot cannot do (enforced by the GitHub-side ruleset, see Setup
walkthrough step 2):

- Write directly to `main` / `master` (push, force-push, branch deletion)
- Write to `.github/workflows/**` (Actions definitions are CODEOWNERS-gated
  to the operator)
- Administer the repository (settings, secrets, collaborators)

Host-side trust boundary (already enforced in earlier phases):

- The controller talks to Docker only through `naiw-docker-proxy` on the
  private `naiw-internal` network with a locked allowlist.
- Pi inside the task container talks to GitHub only through `git` resolving
  the bot token via the `!naiw-git-credential-helper` shell-exec helper
  installed in `/etc/gitconfig`. No SSH keys, no `~/.netrc`, no host
  credentials are reachable from inside the container.

The GitHub-side ruleset is the second half of the boundary: even if a
future bug or compromised Pi attempts a forbidden write, GitHub refuses.

## Setup walkthrough

Three concrete steps. Steps 1 and 2 are web-UI-only. Step 3 lands in the
repository as a commit on `main`.

### Step 1 — Create the GitHub App

1. Open https://github.com/settings/apps and click **New GitHub App**.
2. Name the App something operator-meaningful (e.g. `naiw-bot-<your-handle>`).
3. Homepage URL: anything you control (the bot does not receive
   callbacks; this field is required but unused for our purposes).
4. Permissions (repository-level):
   - **Contents: Read & Write** — required for `git push origin agent/<task-id>`
     and for the bot to open pull requests with a real `head` ref.
   - **Metadata: Read** — required implicitly by every App for repository
     enumeration.
5. Subscribe to events: none required.
6. Where can this GitHub App be installed: **Only on this account**
   (single-operator install).
7. Click **Create GitHub App**.
8. On the App settings page, scroll to **Private keys** and click
   **Generate a private key**. A `.pem` file downloads. Save it to the
   host as `~/naiw-data/secrets/github_app_key.pem` and lock the mode:

   ```sh
   chmod 0600 ~/naiw-data/secrets/github_app_key.pem
   ```

   The download is one-time. If you lose the file, generate a new key
   and retire the old one; GitHub keeps no copy.
9. Click **Install App** in the left sidebar, choose the target
   repository (or `All repositories` if you trust the operator).
10. After install, capture the **Installation ID** from the URL bar
    (`.../installations/<id>`) — the bot's token-resolver script needs
    it.

### Step 2 — Apply a branch ruleset

GitHub Rulesets are the modern replacement for branch protection rules.
They are imported as JSON via the web UI.

1. Open the target repository's settings: **Settings → Rules → Rulesets**.
2. Click **New ruleset → Import a ruleset**.
3. Paste the JSON below into the importer. Comments are stripped before
   the import; the file shape is `jsonc` for documentation only — strip
   `//` lines if your editor refuses jsonc.

```jsonc
{
  "name": "NAIW agent branch protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/agent/*", "refs/heads/main", "refs/heads/master"],
      "exclude": []
    }
  },
  "rules": [
    // Block branch creation on main/master (only agent/* may be created
    // by the bot). agent/* creation is implicitly allowed by absence of
    // a creation rule scoped to agent/*.
    {"type": "creation"},
    // Block branch deletion on every matched ref.
    {"type": "deletion"},
    // Block force-push on every matched ref. Combined with the
    // pull_request rule below, main/master is effectively append-only.
    {"type": "non_fast_forward"},
    // Pull-request gate on main/master. The bot can open PRs; merging
    // requires operator approval (require_code_owner_review wires this
    // to CODEOWNERS — see step 3).
    {"type": "pull_request",
     "parameters": {
       "required_approving_review_count": 1,
       "require_code_owner_review": true,
       "dismiss_stale_reviews_on_push": false,
       "require_last_push_approval": false,
       "required_review_thread_resolution": false
     }
    },
    // File-path restriction: nobody (including the operator via the bot)
    // can change Actions definitions without going through a PR that is
    // operator-reviewed under CODEOWNERS (step 3).
    {"type": "file_path_restriction",
     "parameters": {
       "restricted_file_paths": [".github/workflows/**"]
     }
    }
  ]
}
```

4. Click **Create**.
5. Confirm the ruleset is **Active** in the Rulesets list.

### Step 3 — Add CODEOWNERS

The `file_path_restriction` rule above blocks direct writes to
`.github/workflows/**`. The `pull_request` rule routes changes through
PR review. CODEOWNERS wires `require_code_owner_review` to the operator,
so any PR touching the workflows requires operator approval before merge.

Create `CODEOWNERS` at the repository root (or `.github/CODEOWNERS`)
with at least:

```
# Workflow definitions are operator-only.
/.github/workflows/  @<operator-handle>
```

Replace `<operator-handle>` with the operator's GitHub login. Commit on
`main` (this commit is operator-driven; the bot does not provision
itself).

## Operator runbook

Day-to-day operations after setup.

### How Pi resolves the bot token

The image's `/etc/gitconfig` registers a credential helper in
shell-exec form:

```
[credential]
    helper = !naiw-git-credential-helper
```

`naiw-git-credential-helper` (baked into the image, runs as `pi`) reads
the token from `/run/secrets/github_token` and emits the standard
`username=x-access-token` + `password=<token>` pair on stdout. Any `git`
inside the container (or `gh`, which resolves via the same helper)
acquires the bot token transparently. No environment variable is set —
`docker inspect Config.Env` stays clean (this is the SECURITY.md item 3
promise).

The host file must exist and be locked:

```sh
ls -l ~/naiw-data/secrets/github_token
# -rw-------  1 operator operator  ...  github_token
chmod 0600 ~/naiw-data/secrets/github_token
```

When `naiw-tasks start <project> --secret github_token` runs, the
controller projects this file at `/run/secrets/github_token:ro` inside
the container. The wrapper docs in `README.md` describe the secret-name
allowlist (no `..`, no `/`).

### Rotating the bot token

GitHub App installation tokens are short-lived; a long-lived classic PAT
or App-issued JWT is what the operator stores on disk. To rotate:

1. Generate a fresh App installation token (via the GitHub App settings
   page or via `gh api /app/installations/<id>/access_tokens` using the
   App's private-key JWT). For long-lived PAT-style use, regenerate the
   PAT from the operator's GitHub settings.
2. Replace the file on the host (note the leading space — this prevents
   the new token from landing in `~/.bash_history` on hosts where
   `HISTCONTROL` includes `ignorespace`):

   ```sh
    printf '%s\n' '<new-token>' > ~/naiw-data/secrets/github_token
   chmod 0600 ~/naiw-data/secrets/github_token
   ```

3. Affected tasks must restart to pick up the new secret (Docker bind
   mounts resolve at container-create time, not on file change). For
   each affected task:

   ```sh
   naiw-tasks finish <task-id>
   naiw-tasks start <project> --secret github_token
   ```

   The previous task's artefacts survive under `tasks/<id>/meta/artifacts/`.

## Verification

After setup, paste these one-liners. They are runnable from the operator
shell (with `gh` authenticated as the operator, not the bot).

```sh
# Confirm the ruleset is applied at the repository level. Expected to
# list at least one ruleset with name "NAIW agent branch protection".
gh api repos/:owner/:repo/rulesets

# Inspect the imported rules (replace <id> with the id from above).
gh api repos/:owner/:repo/rulesets/<id>

# Negative test from a clone the bot owns (run inside any test container
# whose /run/secrets/github_token resolves to the bot token):
git push origin main
# Expected: refused by ruleset, exit non-zero. The error message names
# the "NAIW agent branch protection" ruleset.

# Positive test on an agent/* branch:
git checkout -b agent/verify-12345
git commit --allow-empty -m "verify bot push"
git push origin agent/verify-12345
# Expected: succeeds. Clean up the verify branch when done.
```

The negative test on `main` is the load-bearing one. A `git push` to
`main` from anywhere — operator, bot, hostile Pi — must be refused. If
it succeeds, the ruleset import did not stick; recheck step 2.

## Cross-references

- Controller-side contract: `docs/git-policy.md`
- Live source-text guard: `tests/unit/test_git_ops_static_policy.py`
- Trust boundary architectural view: `README.md` Trust boundary section
- Requirement: `GIT-05` (see `.planning/REQUIREMENTS.md`)
