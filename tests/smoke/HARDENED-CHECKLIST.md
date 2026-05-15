# NAIW Hardened-Lifecycle Smoke Checklist

Contract for `tests/smoke/run-hardened-smoke.sh`. Every row below must
correspond to a `step_check "<ID>"` line in the script; the script's
drift gate (step 42) enforces this. Run:

```bash
bash tests/smoke/run-hardened-smoke.sh    # Linux host
pytest tests/smoke/test_hardened.py -v    # same gate via pytest
```

On non-Linux hosts the gate prints `[hardened-smoke] SKIP:` and exits 0.
On FAIL the container, tmpdir, and compose stack are preserved for
inspection; on PASS they are torn down. `naiw-task-net` is always kept.

## Requirements

| ID       | Requirement summary                                                                  | Gate step           |
|----------|--------------------------------------------------------------------------------------|---------------------|
| HARD-01  | `--cap-drop=ALL`                                                                     | 04                  |
| HARD-02  | `--security-opt=no-new-privileges`                                                   | 05                  |
| HARD-03  | `--read-only` rootfs + tmpfs `/tmp` (512m), `/run` (64m), `/home/pi` (128m, mode=1777) writable | 03, 06, 07, 08 |
| HARD-04  | `--pids-limit=512`                                                                   | 09                  |
| HARD-05  | `--memory=4g --memory-swap=4g --cpus=2`                                              | 10                  |
| HARD-06  | `--network naiw-task-net` (no host net, no docker socket)                            | 11                  |
| HARD-07  | `tasks/<id>/meta/` never bind-mounted                                                | 12                  |
| HARD-08  | writable bind-mounts only `/work` and `/io`; read-only `/pi-packages`, `/run/secrets/<name>` | 13          |
| HARD-10  | `--restart=no`                                                                       | 14                  |
| PROXY-05 | All non-allowlisted ops return 403 (22 denied verbs + 2 allowed)                     | 19-42               |

HARD-09 (controller-side bind-mount source-path validation) lives in the
controller and is verified when that phase ships.

## Lifecycle

| Validation              | Gate step |
|-------------------------|-----------|
| PID-1 wrapper (tmux/tini/docker-init) | 15 |
| Secret readable + Config.Env scrub + terminal.log redaction | 16 |
| `terminal.log` survives stop+start; post-restart marker appears | 17 |
| `naiw-signal done` appends valid event to `events.jsonl` | 18 |
| cgroup `pids.peak` + `memory.peak` logged (WARN-only) | 43 |

## Phase 3.5 — Containerized controller (manual gate)

These four checks cannot be automated and live alongside `tests/smoke/run-containerized-smoke.sh` + `tests/smoke/test_containerized.py` (those harnesses cover the automatable invariants — proxy port, image labels, wrapper exit code). Run before declaring Phase 3.5 done.

| ID    | Manual check                                                              | How to run                                                                                     | Expected                                                                                                  |
|-------|---------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| M-3.5-01 | Layered TTY end-to-end: `naiw-tasks start` + `naiw-tasks attach` + Ctrl-P Ctrl-Q detach + reattach | (1) `naiw-tasks start <project>`; (2) `naiw-tasks attach <id>`; (3) press Ctrl-P Ctrl-Q; (4) shell returns; (5) `naiw-tasks attach <id>` again — session resumable | Detach returns to host shell with the wrapper exit 0; reattach reconnects to the same tmux session inside the task container |
| M-3.5-02 | SIGWINCH non-propagation through `docker compose run -it` (known bug: `docker/compose-cli#1460`) | While attached (M-3.5-01), resize the terminal window; observe tmux's column count inside the task container | tmux columns do NOT update until detach + reattach. This is the documented limitation, NOT a failure. Detach + reattach fixes it. |
| M-3.5-03 | Terminal-close mid-attach cleanup                                          | Open attach session; close the terminal window (do NOT detach first)                            | `docker compose run --rm` controller container exits and is removed (`docker ps -a` shows no `naiw-controller` ephemeral). The TASK container keeps running (it was managed by the daemon, not the controller's process). |
| M-3.5-04 | Linux VPS deploy on a fresh box                                            | Provision a fresh Ubuntu/Debian VM; `apt install docker.io`; `sudo install -d /etc/naiw && sudo cp deploy/docker-compose.yml /etc/naiw/`; `bash scripts/install-wrapper.sh`; `docker compose -f /etc/naiw/docker-compose.yml up -d`; `naiw-tasks doctor` | All steps succeed without host Python install; wrapper verifies config, proxy, and allowlist via `docker compose run`; ghcr-published images pull on first start |

Failure of M-3.5-02 is acknowledged-known (upstream Compose bug). Failures of M-3.5-01, M-3.5-03, or M-3.5-04 are gates — fix before merge.

References:
- Pitfall 1 (SIGWINCH): `docker/compose-cli#1460`
- Pitfall 2 (Ctrl-P Ctrl-Q hardcoded): `docker/compose#8934`
- Pitfall 3 (`compose run --rm` and `depends_on`): operator-driven `docker compose up -d` is the documented workflow.
