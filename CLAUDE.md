<!-- GSD:project-start source:PROJECT.md -->
## Project

**Neotolis AI Workbench (NAIW)**

NAIW is a minimal personal self-hosted task/session manager that runs Pi (pi.dev) inside isolated Docker containers. Single user, one operator. It is not a SaaS, not a multi-user platform, and not an AI orchestration framework — it prepares a safe workspace and terminal session per task and gets out of the way.

**Core Value:** Each task runs in its own disposable, sandboxed Docker container with its own folder, tmux session, and (for project tasks) git worktree — so Pi never has access to the host, Docker, or other tasks, and the user can start, attach, observe, recover, and finish tasks from one CLI.

### Constraints

- **Tech stack:** Python (controller and signal CLI). Docker required on host. No databases — JSON files on disk.
- **Security:** task containers MUST NOT mount `/`, `$HOME`, `.ssh`, or the Docker socket. Pi MUST NOT have Docker access. Controller MUST talk to Docker via `naiw-docker-proxy` with limited operations only.
- **Compatibility:** runs on Linux (Ubuntu/Debian VPS) and WSL2/Linux locally. Host Windows path conventions are not supported inside containers.
- **Dependencies:** prefer stdlib + a thin set (e.g., `docker`, `click`, `pyyaml`). No premature abstraction frameworks.
- **Operations:** updates must not break running tasks or in-flight `task.json` state. `terminal.log` must never be truncated.
- **Performance:** disk-light by default (default `delete_worktree` for small VPS).
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## TL;DR — The Boring Stack
- **Language:** Python 3.12 (3.13 acceptable; pin in `pyproject.toml`)
- **CLI:** `click` 8.3.x (NOT typer, NOT argparse)
- **Docker:** `docker` SDK 7.1.x for everything except interactive `attach` (NOT `python-on-whales`, NOT shell-out for create/inspect/start/stop)
- **Interactive attach:** raw `os.execvp("docker", ["exec", "-it", ...])` (SDK cannot do real TTY)
- **tmux:** raw `subprocess` to `tmux pipe-pane` etc., invoked via `docker exec` (NOT `libtmux` — runs on host, tmux runs inside container)
- **Git:** raw `subprocess` to `git` (NOT GitPython, NOT pygit2)
- **YAML:** `PyYAML` 6.0.3 with `safe_load` only (NOT ruamel.yaml — we don't round-trip)
- **JSON state:** stdlib `json` + atomic write via `os.replace()` + advisory `fcntl.flock` (NOT `filelock` package; NOT a database)
- **Logging:** stdlib `logging` (NOT loguru, NOT structlog)
- **Socket proxy:** `tecnativa/docker-socket-proxy:latest` pinned to a digest (battle-tested; alternative `wollomatic/socket-proxy` only if regex-based ops allowlisting is needed)
- **Packaging:** `pyproject.toml` + `setuptools` build backend; install with `pipx` or `uv tool install`
## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | **3.12.x** (min `>=3.12`, `<3.14`) | Runtime for `naiw-tasks` and `naiw-signal` | 3.12 is the safest production choice in 2026 — full support window through ~2028, mature wheels for every dependency we use. 3.13 free-threading is irrelevant (we don't multi-thread). 3.14 (released Oct 2025) is fine but introduces churn we don't need for a personal tool. Pinning a floor of 3.12 lets us use modern type hints (`list[str]`, `X | None`, `match` statements) without compatibility shims. |
| `click` | **8.3.3** (use `>=8.2,<9`) | CLI framework for `naiw-tasks` and `naiw-signal` | Battle-tested, deterministic, decorator-based, no magic. Fits KISS: command groups are explicit (`@cli.group()` + `@group.command()`), error handling is predictable, argument parsing is the default `click.UsageError` path. Click is also the substrate `typer` builds on, so picking Click drops one layer of abstraction. Stable API with clear deprecation policy. |
| `docker` (docker-py) | **7.1.0** | Talk to Docker Engine API via the proxy for create/inspect/start/stop/remove/list/logs | Official Docker SDK. Speaks the Engine API directly — no dependency on a `docker` CLI binary inside the controller container. Plays cleanly with `naiw-docker-proxy` over TCP (`DOCKER_HOST=tcp://naiw-docker-proxy:2375`). Use it for everything *except* interactive TTY attach. |
| `tecnativa/docker-socket-proxy` | **latest** (pin by sha256 digest) | Limited-operation reverse proxy in front of `/var/run/docker.sock` | De-facto standard since 2018, used by Traefik, Nextcloud, Authelia, and the `linuxserver` ecosystem. Tiny HAProxy-based image. Endpoint allowlisting via env vars (e.g., `CONTAINERS=1`, `EXEC=1`, `POST=1`). Single config surface, no app code to maintain. |
| `PyYAML` | **6.0.3** | Parse `~/naiw-data/projects.yaml` (read-only) | Standard, ubiquitous, fast (with libyaml C bindings). We only need `yaml.safe_load()` once on startup — no round-tripping, no comment preservation, no schema. |
### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| stdlib `json` | (3.12) | Read/write `task.json`, `done.json`, `fail.json`, `wait.json` | Always. Task state is small, flat, human-readable. No ORM, no Pydantic. |
| stdlib `logging` | (3.12) | Controller logs (not Pi terminal logs — those go to `/task/terminal.log` via `tmux pipe-pane`) | Always. Configure once with a `RotatingFileHandler` to `~/naiw-data/logs/naiw-tasks.log` plus a stderr `StreamHandler` for the human at the terminal. |
| stdlib `subprocess` | (3.12) | Shell out to `git`, to `docker exec` for tmux commands, and to `docker exec -it` for attach | Always for git ops (`add`, `worktree add/remove`, `status`, `diff`) and for invoking `tmux pipe-pane` inside the running task container. KISS — these are one-shot calls with predictable args. |
| stdlib `os.execvp` | (3.12) | `naiw-tasks attach` — replace controller process with `docker exec -it … tmux attach` | Only for attach. Replacing the process is the *only* way to give the user a real TTY without us writing a PTY pump. `os.execvp` hands the terminal to `docker` directly. |
| stdlib `fcntl` | (3.12) | Advisory lock on `task.json` during read-modify-write | Always — single-process today, but the controller may spawn helper invocations or run alongside `naiw-tasks finish` while a list is in flight. `fcntl.flock(fd, LOCK_EX)` + `os.replace(tmp, final)` gives us crash-safe, lock-correct writes. |
| stdlib `pathlib`, `shutil`, `tempfile` | (3.12) | Path math, atomic write tmpfiles, `clean --older-than` | Always. |
| stdlib `dataclasses` | (3.12) | `Task`, `Project`, `FinishPolicy` value objects | Always — keeps state structured without pulling in Pydantic. `dataclasses.asdict()` round-trips cleanly to JSON. |
### Development Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| `pyproject.toml` (PEP 621) | Project metadata + entry points | Define `naiw-tasks` and `naiw-signal` as `[project.scripts]`. Use `setuptools` build backend — boring, universal, no surprises. |
| `pipx` *or* `uv tool install` | Install controller on host into an isolated venv | Either works. `pipx` if user already has it; `uv tool install` is faster but adds an `uv` dependency. The `naiw-signal` CLI inside containers ships in `naiw-task-image` via `pip install /pi-packages/...` from the entrypoint — pipx isn't needed there. |
| `ruff` (latest) | Lint + format | Drop-in replacement for flake8/black/isort. One config in `pyproject.toml`. |
| `pytest` (latest) | Tests | Unit-test the deterministic helpers (id generation, status reconciliation, finish policy resolver). Avoid full Docker integration tests in the harness — exercise those manually against a real local Docker. |
## Installation
# pyproject.toml (controller package, host-installed)
# pyproject.toml (signal package, baked into naiw-task-image)
# Host install (pick one)
# Inside naiw-task-image build (Dockerfile)
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `click` | `typer` | If you want type-hint-driven argument parsing and don't mind the extra abstraction layer over Click. For NAIW: not worth it — the CLI is small, the surface is decorator-trivial, and `typer` re-exports Click anyway. |
| `click` | stdlib `argparse` | If zero-dependency is a hard requirement. NAIW is OK with one CLI dependency for command groups + nested subcommands; argparse subparsers get awkward at the `naiw-tasks finish --keep-worktree` level. |
| `docker` SDK + `os.execvp` for attach | `python-on-whales` (1-to-1 mapping over `docker` CLI) | If you need features only the CLI exposes (`docker buildx`, `docker stack`, `docker compose`). NAIW only needs core container ops, so the SDK over a TCP proxy is the cleanest fit. Also `python-on-whales` requires the `docker` CLI binary in the controller image (~50 MB). |
| `docker` SDK + `os.execvp` for attach | Pure shell-out to `docker` CLI for everything | Acceptable as a fallback if the SDK gives you trouble through the proxy. The SDK is preferred because it gives typed errors (`docker.errors.NotFound`, `APIError`) that map cleanly to status reconciliation logic. |
| `tecnativa/docker-socket-proxy` | `wollomatic/socket-proxy` | If you need fine-grained per-endpoint regex allowlisting (e.g., "only POST `/containers/create` with names matching `^naiw-task-`"). For NAIW MVP, Tecnativa's coarse env-var allowlist (CONTAINERS, EXEC, POST) is sufficient — the controller is trusted code and is the only client. |
| `tecnativa/docker-socket-proxy` | `linuxserver/socket-proxy` | If you already use the linuxserver.io image stack. Drop-in replacement, same env vars, same ports. |
| stdlib `subprocess` for git | `pygit2` | If you need libgit2 performance for large repos or tree walking. NAIW only does `worktree add`, `worktree remove`, `status`, `diff` — subprocess is faster to write, easier to debug (you see the exact git command in logs), and has no libssh2 wheel headache. |
| stdlib `subprocess` for git | `GitPython` | GitPython also shells out to `git`, so it adds an abstraction for no isolation gain. Skip. |
| stdlib `subprocess` to `tmux` | `libtmux` | If you control tmux from a long-lived Python process *on the same host as tmux*. NAIW's controller runs in one container; tmux runs inside *each task container*. `libtmux` would need to be invoked *inside* the task container — that's not where the controller lives. Cleaner to `docker exec naiw-task-X tmux pipe-pane …`. |
| stdlib `json` + `fcntl.flock` + `os.replace` | `filelock` package | If we needed cross-platform locking (Windows). We don't — the controller only runs in a Linux container. stdlib gives us atomic-write + lock with no extra dep. |
| stdlib `json` + atomic write | SQLite | If task count grows to >10k or we need indexed queries. For ~100s of task folders on disk, `glob` + `json.load` per file is fine and fully grep-able. |
| `PyYAML.safe_load` | `ruamel.yaml` | If we needed to *write* `projects.yaml` programmatically while preserving comments/order. We only read it. |
| `PyYAML.safe_load` | `StrictYAML` | If we wanted schema validation up front. We can validate post-load with a 5-line dataclass check. |
| stdlib `logging` | `loguru` | If you want zero-config colored output and trivial file rotation. Loguru is great for prototypes; for a single-user tool it's an unnecessary dep when stdlib + a 10-line `logging.basicConfig` wrapper does the job. |
| stdlib `logging` | `structlog` | If we needed JSON logs piped to an observability backend. Single user, terminal-only — overkill. |
| `pipx` / `uv tool install` | `pip install --user` | If you're on a system where neither pipx nor uv is available. Works but pollutes the user-site Python; isolated venvs are preferable. |
| `pipx` / `uv tool install` | PyInstaller / shiv single-file | If you needed a single binary with no Python on the host. The user already runs Python on their VPS and locally; not worth the build complexity. |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `typer` | Adds an abstraction layer over Click that we don't benefit from. Type-hint magic is an extra failure mode for a CLI this small. Tiangolo himself notes Typer is "Click underneath" — pick the substrate. | `click>=8.2,<9` |
| `argparse` for nested subcommands | Subparsers + mutually-exclusive groups + `--no-foo` flags get messy fast. NAIW has 10+ commands with options like `--keep-worktree`, `--status running,interrupted`, `--older-than 30d`. | `click` command groups |
| `python-on-whales` | Requires the `docker` CLI binary on PATH inside the controller container. Adds ~50 MB to the image and one more failure surface. The Engine API over the proxy is sufficient. | `docker` SDK 7.1.x |
| Mounting the host Docker socket directly into the controller container | Defeats the entire isolation model — the controller would have full root-equivalent access on the host. | `naiw-docker-proxy` (Tecnativa) on a private Docker network with `CONTAINERS=1 EXEC=1 POST=1` and nothing else |
| `libtmux` from the host/controller | Runs against tmux on the *same machine* via the tmux socket. Our tmux lives inside each task container, behind `docker exec`. Wiring libtmux through `docker exec` is more complex than just calling `tmux …` directly. | `docker exec <task-container> tmux pipe-pane -o -t main "cat >> /task/terminal.log"` via `subprocess.run` |
| Docker SDK `container.attach(stream=True, stdin=True)` for the user's tmux session | docker-py's exec/attach is *not* a real TTY (long-standing issue, see docker/docker-py#247, #390, #983). tmux's "not a tty" message will hit you immediately. | `os.execvp("docker", ["exec", "-it", container_name, "tmux", "attach", "-t", "main"])` — replaces the controller process and hands the user's terminal directly to docker |
| `GitPython` | It also shells out to `git`. You get the same perf as subprocess but with one more dep, lazy-loaded git versions, and Windows path quirks. | stdlib `subprocess.run(["git", "worktree", "add", …], check=True, capture_output=True)` |
| `pygit2` for our use case | libgit2 is great for repo-walking and high-throughput indexing. We do `worktree add/remove`, `status`, `diff` — git CLI does these atomically and matches the user's local mental model. Also, libssh2 is disabled on recent Linux distros' wheels. | stdlib `subprocess` to `git` |
| `ruamel.yaml` | We never write `projects.yaml`. The controller reads it once at startup. Round-trip preservation is a feature we don't need. | `yaml.safe_load(open(path))` |
| `yaml.load(stream)` (without `Loader=`) | Pre-6.0 default could execute arbitrary Python. Even though `projects.yaml` is user-owned, default to safe APIs to keep the habit. | `yaml.safe_load(...)` always |
| `loguru` / `structlog` | Single-user, terminal-only tool. The marginal value over `logging.basicConfig` is zero. | stdlib `logging` |
| `filelock` package | We're Linux-only inside the controller container. `fcntl` is in stdlib, faster, and well-understood. | `fcntl.flock(fd, fcntl.LOCK_EX)` around the read-modify-write window, plus `os.replace(tmp, final)` for atomic publish |
| Pydantic / attrs for `task.json` | Adds a heavy dep for what is a flat dict with ~10 keys. | `@dataclass` + `dataclasses.asdict()` + a small `from_dict` constructor |
| SQLite or any DB | Each task is one folder with one JSON file — `ls`/`grep`/`cat` are first-class debug tools. A DB hides state. | One `task.json` per task folder, scanned with `Path.glob("tasks/*/task.json")` |
| `--privileged`, `--pid=host`, `--network=host` for task containers | Each one breaks the security model. `--privileged` alone gives the container full host root. | Defaults below + `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, `--tmpfs /tmp`, `--pids-limit`, `--memory`, `--cpus` |
| Mounting `$HOME`, `~/.ssh`, `~/.aws`, `/`, or any host secret path into task containers | Pi runs inside — assume hostile. The image is the trust boundary. | Mount only `~/naiw-data/tasks/<task-id>:/task` (rw) and `~/naiw-data/pi-packages:/pi-packages` (ro) |
| `--no-verify` git pushes from inside task containers | Bypasses pre-push hooks that protect `main`/`master` | Branch naming `agent/<task-id>` enforced by controller; protected-branch enforcement in GitHub |
## Container Hardening Reference (verified syntax for current Docker)
# (re-add only if Pi tooling provably needs one — it should not)
- `--security-opt=no-new-privileges` (preferred form) is equivalent to `--security-opt="no-new-privileges:true"`. Both work; the first is the current docs form.
- `--cap-drop=ALL` (singular `cap-drop`, value `ALL`) — Docker accepts `ALL` case-insensitively. Re-add capabilities with `--cap-add=…` only if proven necessary.
- `--read-only` requires you to provide writable areas for any path the image's tooling writes to. Pi/tmux/git all work fine with `/task` (rw), `/tmp` (tmpfs), `/run` (tmpfs).
- If the entrypoint installs Pi packages from `/pi-packages` into a Pi home dir, that home dir is inside the container's normal layered fs *only if not under read-only*. Since root is `--read-only`, ensure Pi's home (e.g., `/home/pi`) is on a tmpfs *or* explicitly excluded — simplest is a named tmpfs: `--tmpfs /home/pi:rw,size=256m`.
## Stack Patterns by Variant
- Tighten container limits: `--memory=1g --cpus=1 --pids-limit=256`
- Default `finish_policy=delete_worktree` (already the recommended MVP default)
- Run a single task at a time — no scheduler in MVP
- Loosen limits: `--memory=8g --cpus=4`
- Multiple concurrent tasks are fine; `naiw-tasks list` is your "scheduler"
- Bake the stable subset of NAIW Pi packages into the image build
- Keep the hot-iteration loop only for actively-developed packages
## Version Compatibility
| Package | Compatible With | Notes |
|---------|-----------------|-------|
| `click 8.3.x` | Python 3.10+ | We target 3.12; safe. |
| `docker 7.1.x` | Docker Engine API ≥1.43 (Docker 25+) | Verify host Docker is recent. The SDK negotiates API version automatically; pin `client.api.version` only if you hit incompatibilities. |
| `tecnativa/docker-socket-proxy` | Docker Engine 20.10+ | Pin by `sha256:...` digest, not `:latest`, in production. |
| `PyYAML 6.0.3` | Python 3.8+ (we use 3.12) | Wheels include libyaml C bindings on glibc Linux — confirm with `yaml.__with_libyaml__` if perf matters. |
| Python 3.12 ↔ docker SDK 7.1 | OK | docker-py 7.x dropped Python 3.7; 3.12 is fully supported. |
| Python 3.13 ↔ all listed deps | OK | All four deps publish 3.13 wheels as of researched date. We pin `<3.14` only to defer free-threading/JIT churn until next milestone. |
## Confidence Levels per Recommendation
| Recommendation | Confidence | Basis |
|----------------|------------|-------|
| Python 3.12 floor | HIGH | Verified release status (3.13 GA Oct 2024, 3.14 GA Oct 2025), documented support windows |
| `click` over typer/argparse | HIGH | Verified versions; rationale aligns with KISS principle in `AGENTS.md` |
| `docker` SDK 7.1 over python-on-whales | HIGH | Verified PyPI release; trade-off documented (SDK = no CLI binary, P-O-W = needs CLI binary) |
| `os.execvp` for attach (not SDK) | HIGH | Long-documented limitation in docker-py issues #247, #390, #983 — SDK exec is not a real TTY |
| Tecnativa proxy as default | HIGH | De-facto standard; mentioned in Traefik / Nextcloud / Authelia docs |
| `wollomatic/socket-proxy` only if regex needed | MEDIUM | Newer alternative; project is active but smaller ecosystem |
| subprocess over GitPython/pygit2 | HIGH | Use case is 4 git commands; GitPython itself shells out; pygit2 has libssh2 wheel issues |
| subprocess to tmux over libtmux | HIGH | tmux runs inside the task container, not on the host where the controller lives — libtmux's host-socket model doesn't fit |
| stdlib `json` + `fcntl` over filelock/SQLite | HIGH | Linux-only controller, small state, grep-able; `filelock` adds nothing on Linux |
| stdlib `logging` over loguru/structlog | HIGH | Single-user terminal tool; no observability backend |
| PyYAML over ruamel | HIGH | Read-only config, no round-tripping needed |
| Container hardening flags | HIGH | Verified against current `docker container run` docs |
| pyproject.toml + pipx / uv | HIGH | Modern boring path; both work, both isolate |
## Trade-offs Worth Calling Out
## Sources
- [click on PyPI](https://pypi.org/project/click/) — verified 8.3.3 latest, requires Python ≥3.10
- [Click 8.3.x changelog](https://click.palletsprojects.com/en/stable/changes/) — API stability confirmed
- [docker (docker-py) on PyPI](https://pypi.org/project/docker/) — verified 7.1.0 current
- [Docker SDK for Python docs (7.1.0)](https://docker-py.readthedocs.io/) — Engine API client
- [docker/docker-py issue #247](https://github.com/docker/docker-py/issues/247) — interactive run / TTY limitation
- [docker/docker-py issue #390](https://github.com/docker/docker-py/issues/390) — interactive container open issue
- [docker/docker-py issue #983](https://github.com/docker/docker-py/issues/983) — exec_create/exec_start stdin piping
- [Tecnativa/docker-socket-proxy GitHub](https://github.com/Tecnativa/docker-socket-proxy) — env-var allowlisting reference
- [tecnativa/docker-socket-proxy on Docker Hub](https://hub.docker.com/r/tecnativa/docker-socket-proxy/tags) — actively maintained
- [wollomatic/socket-proxy](https://github.com/wollomatic/socket-proxy) — alternative with regex allowlisting (Go, zero deps)
- [linuxserver/socket-proxy](https://docs.linuxserver.io/images/docker-socket-proxy/) — drop-in alt
- [PyYAML on PyPI](https://pypi.org/project/PyYAML/) — verified 6.0.3 latest
- [docker container run reference](https://docs.docker.com/reference/cli/docker/container/run/) — verified hardening flag syntax
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) — confirms cap-drop/no-new-privileges patterns
- [libtmux on PyPI](https://pypi.org/project/libtmux/) — verified pre-1.0 (0.55.x) status; not used here
- [python-on-whales on PyPI](https://pypi.org/project/python-on-whales/) — alternative (rejected for NAIW); requires `docker` CLI binary
- [GitPython vs pygit2 comparison](https://piptrends.com/compare/gitpython-vs-pygit2) — confirms GitPython shells out anyway
- [pygit2 worktree docs](https://www.pygit2.org/worktree.html) — has worktree API but libssh2 wheel issues on recent Linux
- [Python release status (devguide)](https://devguide.python.org/versions/) — Python 3.12/3.13/3.14 support windows
- [What's New in Python 3.13](https://docs.python.org/3/whatsnew/3.13.html) — confirms 3.13 baseline features
- [What's New in Python 3.14](https://docs.python.org/3/whatsnew/3.14.html) — confirms 3.14 GA in Oct 2025
- [py-filelock docs](https://py-filelock.readthedocs.io/) — alternative (rejected for Linux-only deployment)
- [uv tool docs (Astral)](https://docs.astral.sh/uv/) — modern alternative to pipx
- [pipx comparisons](https://pipx.pypa.io/latest/explanation/comparisons/) — pipx vs uv tool
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd:quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd:debug` for investigation and bug fixing
- `/gsd:execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
