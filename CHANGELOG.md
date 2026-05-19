# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-05-19

First published release of the Neotolis AI Workbench (NAIW). Six phases
of work converge on a single-operator personal task/session manager that
runs Pi inside isolated, hardened Docker containers and gets out of the
way.

### Added

- **Foundation — image, proxy, host data layout, signal CLI.** Hardened
  `naiw-task-image` (Pi runtime + tmux + git/gh + node + python + ffmpeg
  + ripgrep + `naiw-signal` baked in); `naiw-docker-proxy` with a locked
  allowlist (CONTAINERS, POST, ALLOW_START, ALLOW_STOP, ALLOW_RESTARTS
  on; every other Tecnativa endpoint off); `~/naiw-data/` host layout
  with `projects.yaml`, `secrets/`, `pi-packages/`, `workspace/repos/`,
  and per-task `meta/`/`work/`/`io/` split; `naiw-signal done | fail |
  wait` Python CLI with POSIX-atomic JSONL writer.
- **Hardened smoke gate.** `tests/smoke/run-hardened-smoke.sh` 43-step
  bash gate and `tests/smoke/HARDENED-CHECKLIST.md` static contract; CI
  refuses to advance past the gate if any hardening flag drifts.
- **Controller skeleton.** `naiw-tasks start | attach | finish`
  lifecycle, atomic `task.json` writes under `fcntl.flock`, startup
  probes (Docker reachable, proxy allowlist, data-root symlink check,
  Windows-FS-on-Linux gate); deterministic exit-code matrix 0/1/2/3.
- **Containerised controller.** `naiw-controller` Docker image,
  `scripts/install-wrapper.sh` so Linux, macOS, and Windows operators
  get a `naiw-tasks` shim with no host Python dependency.
- **Visibility.** `naiw-tasks list | output` with lazy `events.jsonl`
  tailing; NOTES markers `(unknown status)`, `(leaked ctr)`,
  `(log shrunk)`; two-pass stable sort (id-asc then updated_at-desc).
- **Lifecycle closure.** `naiw-tasks recover | clean | disk`,
  finish-time artefact capture under `meta/artifacts/`, per-task
  `storage/` bind-mount at `/home/pi:rw` so Pi's home survives finish
  and recover, IEC-binary `max_data_size` parser.
- **Ops finalisation.** `naiw-tasks --version` (controller version +
  task-image digest + signal schema), `naiw-tasks doctor`
  (consolidated health-check + hardening drift audit), `(drift)` NOTES
  marker on `list`, `naiw-signal log <message>` event kind,
  integration test job in CI covering five live-Docker scenarios.

### Security

- **Trust model.** Pi never has Docker socket access, host filesystem
  access, or other tasks' folders. The controller talks to Docker only
  through `naiw-docker-proxy` with the locked allowlist.
- **Container hardening.** Every task container runs with
  `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`
  rootfs (with tmpfs writable surfaces only), `--pids-limit=512`,
  `--memory=4g`, `--cpus=2`, on the private `naiw-task-net` network,
  with no host networking and no host bind-mounts beyond
  `tasks/<id>/`.
- **Secrets via file mounts.** Projected from
  `~/naiw-data/secrets/<name>` (mode 0600) to
  `/run/secrets/<name>:ro`; never via env vars; never visible in
  `docker inspect`.
- **`terminal.log` redaction.** `tmux pipe-pane` filter strips
  `ghp_*`, `gho_*`, `sk-*`, and `Bearer <token>` patterns across
  `start` and `recover`; smoke harness REC-IMG-06 step proves
  redaction survives the recover boundary.
- **Branch policy.** Controller refuses to create a worktree on any
  branch not matching `agent/<task-id>`. Controller never
  auto-commits, pushes, or merges; a static source-text guard test in
  CI enforces the contract on `git_ops.py` and `lifecycle.py`.
- **GitHub-side trust boundary.** Operator cookbook in
  `docs/github-bot.md` walks through the path-restricted GitHub App
  scopes and the JSON ruleset that protects `main`/`master` and
  `.github/workflows/**`.

[1.0.0]: https://github.com/d954mas/neotolis-ai-workbench/releases/tag/v1.0.0
