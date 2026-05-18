# task.md

## Project

**Neotolis AI Workbench** is a minimal personal self-hosted AI workbench.

It is not a SaaS, not a multi-user platform, and not an AI orchestration framework.

The project is a task/session manager for running Pi inside isolated Docker task containers.

## Naming

```text
Project: Neotolis AI Workbench
Short name: NAIW
Main CLI: naiw-tasks
Task signal CLI: naiw-signal
Future workflow layer: naiw-gsd
```

Containers:

```text
naiw-tasks              # controller / task manager container
naiw-docker-proxy       # limited Docker socket proxy
naiw-task-<task-id>     # isolated task container
```

Docker images:

```text
naiw-task-image         # ready-to-use Pi environment with required extensions/tools
```

Data directory:

```text
~/naiw-data/
```

Task marker directory:

```text
/task/.naiw/
```

## Goal

Build a small personal AI workflow where each task runs in its own isolated Docker container.

The user interacts with `naiw-tasks` through terminal commands.

Each task gets:

- its own task folder
- its own Docker container
- its own tmux session
- its own persistent logs/output
- its own git worktree if connected to a project/repository

Pi is the main AI worker inside task containers.

`naiw-tasks` does not decide how the work should be done. It only prepares a safe workspace and terminal session. The user decides the workflow inside the task: code, review, research, planning, writing, custom skills, custom prompts, or future GSD workflows.

## Core architecture

```text
Host / VPS
  Docker
  ~/naiw-data/

naiw-tasks container
  naiw-tasks CLI
  trusted task/session manager
  creates and manages task containers

naiw-docker-proxy container
  limited proxy to Docker socket
  naiw-tasks talks to Docker through this proxy

naiw-task-<task-id> container
  created from naiw-task-image
  Pi
  stable Pi tools/extensions from image
  hot NAIW Pi packages installed from /pi-packages on startup
  tmux
  git / gh / node / python / ffmpeg / ripgrep
  sees /task and read-only /pi-packages
```

## Main rule

AI workers never get Docker access.

Pi runs only inside `naiw-task-*` containers.

Only `naiw-tasks` can create, inspect, attach to, stop, and remove task containers through the limited Docker socket proxy.

The controller is deterministic code. It must not be controlled by an LLM.

Free-text AI interaction is allowed only inside isolated task containers.

## Task image model

`naiw-task-image` is the base Pi runtime image.

It contains:

- Pi runtime
- stable third-party Pi extensions/packages that rarely change
- tmux
- git / gh
- node / npm
- python
- ffmpeg
- ripgrep
- build tools
- `naiw-signal`

Hot-iterated NAIW Pi packages are not baked into the image during MVP.

They live on the host:

```text
~/naiw-data/pi-packages/
  naiw-base/
  naiw-gsd/          # future
  naiw-review/       # future
  naiw-research/     # future
```

Task containers mount this directory read-only:

```text
/agent/pi-packages:/pi-packages:ro
```

On task container startup, the entrypoint installs the mounted packages into the container's Pi home.

Example startup flow:

```text
1. Container starts from naiw-task-image.
2. /task is mounted writable.
3. /pi-packages is mounted read-only.
4. Entrypoint runs pi install for selected local packages.
5. Entrypoint starts tmux + Pi.
```

This allows fast iteration on custom Pi packages without rebuilding `naiw-task-image` after every change.

Package installation happens inside the disposable task container. It must not modify the host `pi-packages` directory.

Stable packages can later be baked into `naiw-task-image` if startup time becomes a problem.

## Data layout

On host:

```text
~/naiw-data/
  projects.yaml

  pi-packages/
    naiw-base/
    naiw-gsd/        # future

  workspace/
    repos/
      <project>/              # base repo only

  tasks/
    <task-id>/
      task.json
      worktree/               # only for project/repo tasks
      terminal.log
      summary.md
      diff.patch
      output/
```

Inside task container:

```text
/task       # writable current task folder only
```

`/task` is the working space for the current task.

All shared Pi resources are already baked into `naiw-task-image`.

## projects.yaml

Keep it minimal. It is only an alias map.

```yaml
projects:
  neotolis-engine:
    path: /agent/workspace/repos/neotolis-engine

  not-a-trolley-problem:
    path: /agent/workspace/repos/not-a-trolley-problem
```

Project-specific instructions live in the repository `AGENTS.md`, not in `projects.yaml`.

## Task model

MVP does not need task types.

`naiw-tasks` should not decide whether a task is code, review, research, writing, planning, or media.

There are only two task shapes:

```text
project task      task connected to a known project/repository
generic task      task without a project/repository
```

Project tasks get a git worktree.

Generic tasks do not get a git worktree. They only get an empty `/task` workspace.

Inside a task, the user may manually choose any workflow:

- simple Pi conversation
- code change
- code review
- research
- planning
- writing
- using skills/prompts/extensions already installed in `naiw-task-image`
- manually coordinating tools/agents inside the task container

This keeps `naiw-tasks` minimal. It manages lifecycle, not the creative or technical workflow inside the task.

## naiw-tasks MVP commands

```bash
naiw-tasks start <project>
naiw-tasks start <project> --auto-finish
naiw-tasks start
naiw-tasks list
naiw-tasks list --all
naiw-tasks reap
naiw-tasks attach <task-id>
naiw-tasks output <task-id>
naiw-tasks finish <task-id>
naiw-tasks recover <task-id>
naiw-tasks clean --older-than 30d
naiw-tasks disk
```

## naiw-tasks start: project task

Example:

```bash
naiw-tasks start neotolis-engine
```

Expected behavior:

1. Read project path from `projects.yaml`.
2. Create unique task id, for example `neotolis-engine-001`.
3. Create task folder:

```text
/agent/tasks/neotolis-engine-001/
```

4. Create git worktree inside task folder:

```text
/agent/tasks/neotolis-engine-001/worktree/
```

5. Create branch:

```text
agent/neotolis-engine-001
```

6. Start task container from `naiw-task-image`:

```text
naiw-task-neotolis-engine-001
```

7. Mount only:

```text
/agent/tasks/neotolis-engine-001:/task
/agent/pi-packages:/pi-packages:ro
```

8. Inside task container, start tmux + Pi in:

```text
/task/worktree
```

9. Start continuous terminal logging:

```bash
tmux pipe-pane -o -t main "cat >> /task/terminal.log"
```

10. Print attach command:

```bash
naiw-tasks attach neotolis-engine-001
```

If started with `--auto-finish`, terminal `done` or `fail` events remain pending in `list` until the operator runs `naiw-tasks reap`.

## naiw-tasks start: generic task

Example:

```bash
naiw-tasks start
```

Expected behavior:

1. Create unique generic task id, for example `task-001`.
2. Create task folder:

```text
/agent/tasks/task-001/
```

3. Do not create git worktree.
4. Start task container from `naiw-task-image`:

```text
naiw-task-task-001
```

5. Mount only:

```text
/agent/tasks/task-001:/task
/agent/pi-packages:/pi-packages:ro
```

6. Inside task container, start tmux + Pi in:

```text
/task
```

7. Start continuous terminal logging:

```bash
tmux pipe-pane -o -t main "cat >> /task/terminal.log"
```

8. Print attach command:

```bash
naiw-tasks attach task-001
```

Generic tasks are useful for research, planning, writing, media instructions, experiments, and any task that does not need a repository.

## naiw-tasks list

Default `naiw-tasks list` shows the latest 10 tasks with their statuses.

It should not try to be smart by default. It simply shows recent task history.

Example:

```text
Latest tasks:

neotolis-engine-004   running       10 min ago
research-002          completed     1h ago
neotolis-engine-003   interrupted   3h ago
task-001              failed        yesterday
```

The user can control count and statuses:

```bash
naiw-tasks list --limit 20
naiw-tasks list --status running
naiw-tasks list --status running,interrupted,waiting_for_user
naiw-tasks list --status completed --limit 50
naiw-tasks list --project neotolis-engine --limit 20
naiw-tasks list --all
```

Default behavior:

```bash
naiw-tasks list
# same as:
naiw-tasks list --limit 10
```

Supported statuses:

```text
created
running
interrupted
waiting_for_user
completed
failed
cancelled
```

`naiw-tasks list` reads `/agent/tasks/*/task.json` and compares saved container state with real Docker state.

If a task is marked `running`, but its container is missing or stopped, show it as `interrupted`.

Sort by `updated_at` descending. If `updated_at` is missing, fall back to `created_at`.

## naiw-tasks reap

Example:

```bash
naiw-tasks reap
naiw-tasks reap --dry-run
```

Expected behavior:

Apply pending `auto_finish=true` terminal events and close ready task containers.

`naiw-tasks list` must not close containers. It may show `auto_finish pending` so the operator can decide when to run `reap`.

`naiw-tasks reap --dry-run` must not close containers or advance event offsets; it only reports what would be reaped.

For tasks with `finish_policy=ask`, non-interactive `reap` uses the documented default: `delete_worktree`.

## naiw-tasks attach

Example:

```bash
naiw-tasks attach neotolis-engine-001
```

Expected behavior:

1. Find task by id.
2. Find task container.
3. Attach to tmux session inside the task container.

Conceptually:

```bash
docker exec -it naiw-task-neotolis-engine-001 tmux attach -t main
```

Detach without stopping the task:

```text
Ctrl+B, then D
```

## naiw-tasks output

Example:

```bash
naiw-tasks output neotolis-engine-001
```

Expected behavior:

Show recent terminal output from the task.

This can read from:

```text
/task/terminal.log
```

or capture the current tmux pane if the container is running.

## naiw-tasks finish

Example:

```bash
naiw-tasks finish neotolis-engine-001
```

Expected behavior:

1. Capture latest terminal output if possible.
2. Ensure `/task/terminal.log` is saved.
3. Save git status if worktree exists.
4. Save changed files list if worktree exists.
5. Save git diff as `/task/diff.patch` if worktree exists.
6. Stop task container.
7. Remove task container.
8. Apply worktree cleanup policy.
9. Mark task as finished in `task.json`.

`finish` must support non-interactive worktree cleanup modes.

Commands:

```bash
naiw-tasks finish <task-id>                    # interactive/default mode
naiw-tasks finish <task-id> --delete-worktree  # save task files, then delete worktree
naiw-tasks finish <task-id> --keep-worktree    # save task files, keep worktree
```

`finish` always saves task-level files before cleanup:

```text
terminal.log
git-status.txt
changed-files.txt
diff.patch
summary.md if it exists
output/ if it exists
```

The flag only controls what happens to:

```text
/task/worktree/
```

Recommended default for manual tasks:

```text
ask user if worktree exists
```

Recommended default for automated tasks:

```text
delete-worktree
```

Automated tasks must never block waiting for user confirmation.

For automated tasks, cleanup policy should be stored in `task.json` when the task is created:

```json
{
  "auto_finish": true,
  "finish_policy": "delete_worktree"
}
```

Supported finish policies:

```text
ask              ask user what to do with worktree
keep_worktree    keep /task/worktree/
delete_worktree  save task files, then delete /task/worktree/
```

Default for small VPS:

```text
delete_worktree
```

## naiw-signal

`naiw-signal` is a small CLI inside task containers.

It lets Pi signal task state to `naiw-tasks` without giving Pi Docker access.

Commands:

```bash
naiw-signal done --summary "PR review finished"
naiw-signal fail "GitHub token has no permission"
naiw-signal wait "Need clarification about review scope"
```

`naiw-signal` only writes marker files into:

```text
/task/.naiw/
```

Examples:

```text
/task/.naiw/done.json
/task/.naiw/fail.json
/task/.naiw/wait.json
```

`naiw-signal` must never:

- talk to Docker
- stop containers
- access host files
- manage task lifecycle directly

`naiw-tasks` watches or checks these marker files and performs the actual lifecycle action.

## Recovery after host restart

Task folders are persistent.

Task containers and tmux sessions are disposable.

If VPS or Docker restarts, running task containers may be gone. In this case, `naiw-tasks list` should scan `/agent/tasks/*/task.json` and compare saved container names with real Docker state.

If `task.json` says `running`, but the container is missing or stopped, show:

```text
interrupted
```

Do not auto-recover interrupted tasks by default.

The user decides what to recover:

```bash
naiw-tasks recover <task-id>
```

`recover` starts a new task container with the same task folder mounted as `/task`.

The old Pi process and tmux session are not restored. A new Pi session starts in the same task folder.

The previous terminal history is available in:

```text
/task/terminal.log
```

`terminal.log` must never be truncated.

All terminal logging must append to the file:

```bash
tmux pipe-pane -o -t main "cat >> /task/terminal.log"
```

On recover, append a recovery marker before starting a new tmux/Pi session:

```bash
printf '\n\n===== RECOVERED AT %s =====\n\n' "$(date -Is)" >> /task/terminal.log
```

Then continue appending new terminal output to the same log file.

## Task container security

Task containers should be started with hardening:

```text
no Docker socket
no privileged mode
no host mounts except current task
read-only root filesystem if possible
write access only to /task and /tmp
CPU limit
memory limit
PID limit
no-new-privileges
cap-drop=ALL if compatible
```

The task container may have internet access, but it must not contain secrets.

## Controller security

`naiw-tasks` is trusted code.

It must:

- never pass arbitrary Docker arguments from user/AI input
- only create containers from the approved task image
- only mount approved paths
- never mount `/`, `$HOME`, `.ssh`, or Docker socket into task containers
- never create privileged task containers
- never expose Docker API to Pi

`naiw-tasks` talks to Docker through `naiw-docker-proxy`, not directly to `/var/run/docker.sock`.

## Docker socket proxy

Use a socket proxy to reduce controller access to Docker.

Allowed operations should be limited to what `naiw-tasks` needs:

- create task container
- start task container
- stop task container
- remove task container
- inspect task container
- list task containers
- get logs
- exec/attach for terminal access

Do not allow broad Docker management features unless required.

## Pi usage

Pi is the main worker inside each task container.

Pi runtime and stable tools are installed in `naiw-task-image`.

Hot-iterated NAIW Pi packages are mounted read-only from `/pi-packages` and installed by the task container entrypoint before Pi starts.

For project tasks, Pi should start inside:

```text
/task/worktree
```

For generic tasks, Pi should start inside:

```text
/task
```

For project tasks, Pi should read the project `AGENTS.md` when relevant.

Pi should keep generated files inside:

```text
/task/output/
```

If a final written result is needed, it should go to:

```text
/task/summary.md
```

Pi must not assume access to other projects or tasks.

## Git and GitHub policy

Pi may work with Git branches, but must never push directly to `main`, `master`, or other protected production branches.

Allowed:

- read repository
- inspect branches
- inspect PRs
- create task branches
- push only to agent-owned branches
- create draft PRs if explicitly requested
- write PR reviews/comments if explicitly requested

Forbidden:

- push to `main`
- push to `master`
- force-push protected branches
- delete protected branches
- modify repository settings
- modify GitHub Actions/secrets/settings
- merge PRs automatically

Branch naming:

```text
agent/<task-id>
```

Default MVP behavior:

```text
no commit
no push
no merge
```

Future behavior:

```text
Pi may create branches and draft PRs, but never update protected branches directly.
```

Recommended GitHub setup:

- use a separate GitHub bot/user account
- grant only required repository access
- protect `main` / `master`
- allow the bot to push only to `agent/*` branches where possible
- do not give admin permissions
- do not give access to secrets/settings/workflows unless explicitly needed

## MVP non-goals

Do not implement now:

- microVM backend
- multiple backends
- Telegram integration
- web UI
- OpenClaw
- long-term memory
- automatic PR creation
- full job queue
- random Pi package marketplace installs
- support for untrusted external users
- AI-controlled controller
- free-text assistant with controller privileges
- task type system
- `naiw-gsd` runtime workflow
- external writable/read-only Pi home mount
- rebuilding task image for every NAIW package change

## Success criteria

MVP is successful when:

- user can start a project task by project name
- user can start a generic task without project
- task gets unique id
- task gets its own folder
- project task gets its own git worktree
- task runs in its own container from `naiw-task-image`
- task container installs mounted local Pi packages from `/pi-packages` on startup
- Pi starts inside tmux in that container
- terminal output is continuously logged to `/task/terminal.log`
- user can list recent tasks
- user can attach to a task
- user can see recent output
- user can finish a task
- finish saves terminal log and diff patch
- task container is removed after finish
- task cannot see other tasks
- task cannot access Docker
- task cannot access host personal files
- `naiw-signal` can mark done/fail/wait without Docker access

## Recommended next step

Build the smallest full cycle:

```bash
naiw-tasks start neotolis-engine
naiw-tasks list
naiw-tasks attach neotolis-engine-001
naiw-tasks output neotolis-engine-001
naiw-tasks finish neotolis-engine-001
```

Then add:

```bash
naiw-signal done
naiw-signal fail
naiw-signal wait
naiw-tasks recover <task-id>
naiw-tasks clean --older-than 30d
```
