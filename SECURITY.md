# Security

> NAIW is a single-operator self-hosted task runner. The trust model
> assumes the operator is trusted; Pi inside each task container is NOT.
> See `README.md` Trust boundary section for the architectural view; this
> file is the "Looks Done But Isn't" verification checklist.

Each section below documents one promise the system makes, why that
promise is hard to keep, and a runnable verification command you can
paste after a fresh deploy or a maintenance window.

For the controller-side git contract (no automatic commit/push/merge),
see `docs/git-policy.md` (requirement `GIT-04`). For the GitHub-side
trust boundary (ruleset + App scopes), see `docs/github-bot.md`
(requirement `GIT-05`).

## 1. Hardening flags visible in `docker inspect` for every running task

**What we promise.** Every running task container has the locked
hardening flags applied: `CapDrop=[ALL]`, `SecurityOpt` contains
`no-new-privileges`, `ReadonlyRootfs=true`, `PidsLimit=512`,
`Memory=4 GiB`, `NanoCpus=2.0`, `NetworkMode=naiw-task-net`,
`RestartPolicy=no`, `Init=true`, `Tty=true`, `OpenStdin=true`, plus the
persistent `tasks/<id>/storage:/home/pi:rw` bind in `HostConfig.Binds`.

**Why this is hard.** A future PR could land a refactor that silently
relaxes a flag (e.g., `cap_drop=["NET_RAW"]` instead of `["ALL"]`). Or
a daemon reload (`systemctl daemon-reload`) could lose the pids-limit on
running containers. Or a `docker update` from outside the controller
could mutate live limits.

**How to verify.**

```sh
naiw-tasks doctor                              # exit 1 lists drift across all running tasks
naiw-tasks list                                # NOTES column carries (drift) marker per task
docker inspect naiw-task-<id> | jq '.[0].HostConfig'   # ad-hoc per-task inspect
```

Automated by `tests/smoke/run-hardened-smoke.sh` (create-time correctness)
and the doctor drift audit (post-create drift detection).

## 2. Symlink-escape resistance under `io/` and `storage/`

**What we promise.** Pi cannot use a symlink under `tasks/<id>/io/` or
`tasks/<id>/storage/` to read host files outside its own task folder.

**Why this is hard.** The controller reads `terminal.log`,
`events.jsonl`, and artefact files from these directories at finish
time. A naive implementation that uses `open(path)` without
`O_NOFOLLOW` would follow a Pi-planted symlink and exfiltrate host
bytes. Phase 5 artefact capture also walks `io/output/` recursively —
symlinks inside that tree must be skipped, not followed.

**How to verify.**

```sh
# Inside a task container as Pi:
ln -s /etc/passwd /io/terminal.log
# On the host, naiw-tasks output <id> MUST refuse, not print /etc/passwd:
naiw-tasks output <id>
```

Automated by `tests/smoke/run-hardened-smoke.sh` HARD-09 source-symlink
step and `tests/unit/test_path_validation.py` /
`tests/unit/test_output_cmd.py` / `tests/unit/test_capture_artifacts.py`.

## 3. Secrets absent from `docker inspect Config.Env`

**What we promise.** Secrets are projected as file mounts at
`/run/secrets/<name>:ro`. They never appear in `docker inspect`'s
`Config.Env` array.

**Why this is hard.** A future PR that switches from file-mounts to
`-e KEY=VALUE` would leak the secret via `docker inspect` to anyone
with read access to the Docker socket. The trust model assumes the
host's Docker daemon is reachable by the operator; leaking secrets via
inspect output would also leak them into shell history, screen
sessions, and any operator-side logging.

**How to verify.**

```sh
docker inspect naiw-task-<id> | jq '.[0].Config.Env'
# No `GITHUB_TOKEN=ghp_...`, no `OPENAI_API_KEY=sk-...`, no Bearer values.
# Only image-baked defaults (LANG, LC_ALL, PATH).
```

Automated by the hardened-smoke env-absence step in
`tests/smoke/run-hardened-smoke.sh` (Step 13) and the `git credential
fill` step that confirms the bot token resolves via file-mount, not env.

## 4. Secrets absent from `terminal.log` after `pipe-pane` redaction

**What we promise.** The `tmux pipe-pane` filter rewrites `ghp_*` /
`gho_*` / `sk-*` / `Bearer ...` patterns to `[REDACTED]` before bytes
hit `terminal.log` — across `start`, `recover`, and the lifetime of the
task.

**Why this is hard.** The redaction is a `sed -E` filter inside the
container. The image entrypoint must re-issue `pipe-pane` on every
container start, including after `naiw-tasks recover`. A regression
where the entrypoint forgets to re-issue would leave the post-recover
log unfiltered. Phase 5 added the recover boundary specifically to
catch this; the smoke harness step probes it.

**How to verify.**

```sh
# Inside the container, echo a fake token:
echo "ghp_TOKEN_SHAPE_BUT_FAKE_aaaabbbbccccdddd"
# On the host:
grep "ghp_" ~/naiw-data/tasks/<id>/io/terminal.log
# No matches — the redaction replaced the token shape with [REDACTED].
```

Automated by the post-recover redaction probe in
`tests/smoke/run-hardened-smoke.sh` (REC-IMG-06 step). The host-side
sentinel verifies the filter survives the container-replace boundary.

## 5. `schema_version` present in every `task.json`; unknown future versions refused

**What we promise.** Every `task.json` carries `schema_version: 1` (or
the current value). The controller refuses to act on `task.json` files
with unknown future versions — this is what lets us evolve the schema
later without silently corrupting old tasks. Requirement `DATA-07`.

**Why this is hard.** A bug that drops the field on a
`store.update_task` code path would let the next read fail open
("missing key") instead of refusing. The atomic-write recipe
(`tempfile` + `fcntl.flock` + `os.replace`) must preserve every
top-level key on every mutation; any mutator that returns a fresh
`dict` instead of a copy of the prior `dict` risks dropping the field.

**How to verify.**

```sh
jq -r .schema_version ~/naiw-data/tasks/*/meta/task.json | sort -u
# Output: only "1"
```

Automated by `tests/unit/test_store.py` and `tests/unit/test_model.py`
(UnsupportedSchemaError on read; schema_version preserved across every
mutator round-trip).

## 6. Daemon-restart handling (doc-only)

**What we promise.** When the Docker daemon is restarted (e.g.,
`systemctl restart docker`), running task containers are killed (we ship
`--restart=no`), and the controller surfaces this on the next
`naiw-tasks list` as `interrupted`. The operator runs
`naiw-tasks recover` to resume. `terminal.log` is never truncated; the
recovery banner is host-side append.

**Why this is hard.** Some Docker deployments enable `live-restore` to
keep containers running across daemon restarts. The controller's
startup_check detects this and surfaces a warning. Without that check
the reconciliation truth table (Phase 4 list_cmd) would still flip the
task to `interrupted`, but the container would still be running — an
operator-confusing split state.

**How to verify.** (Operator runbook — not automated in CI; CI cannot
safely `systemctl restart docker.service`.)

```sh
sudo systemctl restart docker
sleep 5
naiw-tasks list             # affected tasks appear as `interrupted`
naiw-tasks recover <id>     # operator-driven resume
```

The recovery banner appears in `terminal.log` as
`===== RECOVERY ATTEMPT #N AT <iso-ts-ms-Z> =====`.

## 7. Disk-fill safety — `start` refuses at >95% of `max_data_size`

**What we promise.** When `~/naiw-data/` usage exceeds 95% of
`max_data_size` (default 50 GiB), `naiw-tasks start` and
`naiw-tasks recover` refuse with a clear pointer to `clean`. `list` and
`finish` are NOT gated — the operator must be able to inspect and clean
even at high water marks.

**Why this is hard.** A disk-full corner case at create time can leave
the system in a half-initialised state if the controller tried to start
a container and ran out of space mid-flight. Phase 5 wired the refusal
into `startup_checks.check_disk_threshold` so the gate fires before any
mutation; the `(used, max, pct)` hook is shared with `naiw-tasks disk`
so the threshold is one source of truth.

**How to verify.**

```sh
naiw-tasks disk             # per-subdirectory + total + warning at >80%
naiw-tasks doctor           # [disk] section shows pct
```

Automated by `tests/unit/test_startup_checks.py` disk-threshold tests
and `tests/unit/test_disk.py` for the `_check_threshold` shared hook.
