#!/usr/bin/env bash
# tests/smoke/run-phase-4-scenarios.sh — automated Phase 4 operator-gate scenarios.
#
# Runs the four live-Docker scenarios from
# .planning/phases/04-list-output-reconciliation-lazy-events/04-04-MANUAL-VERIFICATION.md
# non-interactively against a real Docker daemon. `naiw-tasks attach` (interactive
# tmux) is replaced with `docker exec <task-ctr> tmux send-keys -t main "..." Enter`
# so the script drives the in-container tmux without holding a TTY.
#
# Scenarios:
#   1. naiw-tasks list happy path + reconciliation (LIST-01..06)
#   2. naiw-tasks output end-to-end + symlink defense (CTRL-08)
#   3. DATA-08 monotonic-growth tamper warning (DATA-08, LIST-06)
#   4. auto_finish=true → real container teardown (SIG-07, LIST-06, CTRL-08)
#
# Pattern matches tests/smoke/run-containerized-smoke.sh: same compose-file
# resolution, same `compose run --rm naiw-controller <subcommand>` for CLI ops,
# same ephemeral $tmp_data layout. SKIP-on-non-Linux.

set -uo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

_lib_log_prefix="[phase-4-scenarios]"
_lib_step_fail_exits=0
# shellcheck source=_lib.sh
source "$here/_lib.sh"

# Compose-file resolution mirrors run-containerized-smoke.sh: respect $COMPOSE_FILE
# when set (CI stacking), otherwise default to deploy/docker-compose.yml.
if [[ -n "${COMPOSE_FILE:-}" ]]; then
    COMPOSE_ARGS=()
    echo "${_lib_log_prefix} using env-var COMPOSE_FILE=${COMPOSE_FILE}"
else
    COMPOSE_ARGS=(-f "${repo_root}/deploy/docker-compose.yml")
    echo "${_lib_log_prefix} using default compose file ${repo_root}/deploy/docker-compose.yml"
fi

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "${_lib_log_prefix} SKIP: requires Linux (got $(uname -s))"
    exit 0
fi
for cmd in docker jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "${_lib_log_prefix} FAIL: $cmd not on PATH" >&2
        exit 1
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    echo "${_lib_log_prefix} FAIL: docker compose plugin missing" >&2
    exit 1
fi

scenario_failures=()
overall_status=FAIL
tmp_data="$(mktemp -d)"

cleanup() {
    local rc=$?
    # Best-effort: any task containers we left lying around.
    for ctr in $(docker ps -a --filter "label=naiw.managed=1" --format '{{.Names}}' 2>/dev/null); do
        docker rm -f "$ctr" >/dev/null 2>&1 || true
    done
    if [[ "$overall_status" == "PASS" && "$rc" -eq 0 ]]; then
        rm -rf "$tmp_data"
        docker compose "${COMPOSE_ARGS[@]}" down >/dev/null 2>&1 || true
        echo "${_lib_log_prefix} PASS — all 4 scenarios verified"
    else
        echo "${_lib_log_prefix} FAIL — preserving artefacts for inspection:" >&2
        echo "${_lib_log_prefix}   data:    $tmp_data" >&2
        echo "${_lib_log_prefix}   compose: still up (use 'docker compose ${COMPOSE_ARGS[*]} down')" >&2
        if [[ ${#scenario_failures[@]} -gt 0 ]]; then
            echo "${_lib_log_prefix}   failed scenarios: ${scenario_failures[*]}" >&2
        fi
    fi
}
trap cleanup EXIT

# Restore the locally-built controller image under the ghcr.io tag the compose
# file pins to. The existing `test_containerized_smoke_harness_passes` runs
# `docker pull ghcr.io/d954mas/naiw-controller:latest`, which on CI replaces
# the freshly-built image (with Phase 4 commands) with the published one from
# master (no `list` / `output`). The pull cannot overwrite the local-only
# `naiw-controller:latest` tag, so re-tagging from there is safe.
if docker image inspect naiw-controller:latest >/dev/null 2>&1; then
    docker tag naiw-controller:latest ghcr.io/d954mas/naiw-controller:latest >/dev/null 2>&1 || true
fi

# Bring up the proxy. Controller is one-shot via `compose run`.
docker network inspect naiw-task-net >/dev/null 2>&1 \
    || docker network create naiw-task-net >/dev/null
docker compose "${COMPOSE_ARGS[@]}" up -d naiw-docker-proxy

# Resolve task image (CI-built local vs. published).
task_image="ghcr.io/d954mas/naiw-task-image:latest"
if docker image inspect naiw-task-image:latest >/dev/null 2>&1; then
    task_image="naiw-task-image:latest"
fi

# Bootstrap host data layout.
mkdir -p "$tmp_data/secrets" "$tmp_data/pi-packages" "$tmp_data/workspace/repos" "$tmp_data/tasks"
chmod 0700 "$tmp_data/secrets"
cat >"$tmp_data/config.yaml" <<EOF
schema_version: 1
task_image: ${task_image}
EOF

# ─── Helpers ───────────────────────────────────────────────────────

naiw_tasks() {
    # Run a controller subcommand through compose. Non-interactive (-T) is
    # required for headless CI; --user matches host uid:gid so the bind-mounted
    # $tmp_data inherits operator ownership.
    docker compose "${COMPOSE_ARGS[@]}" run --rm \
        -T \
        --user "$(id -u):$(id -g)" \
        -e "NAIW_DATA=/naiw-data" \
        -e "NAIW_DATA_HOST=$tmp_data" \
        -v "$tmp_data:/naiw-data" \
        naiw-controller "$@"
}

assert_contains() {
    local needle="$1" haystack="$2" label="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "${_lib_log_prefix}   OK: $label contains '$needle'"
        return 0
    fi
    echo "${_lib_log_prefix}   FAIL: $label did NOT contain '$needle'" >&2
    echo "${_lib_log_prefix}     got:" >&2
    printf '%s\n' "$haystack" | sed 's/^/        /' >&2
    return 1
}

start_and_capture_id() {
    # Run `naiw_tasks start`, echo the assigned task-id (e.g. task-001) to stdout.
    # All other output is suppressed; caller reads stdout into a variable.
    local out
    if ! out="$(naiw_tasks start 2>&1)"; then
        echo "${_lib_log_prefix}   FAIL: start failed" >&2
        printf '%s\n' "$out" | sed 's/^/        /' >&2
        return 1
    fi
    if [[ "$out" =~ naiw-task-(task-[0-9]+) ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi
    echo "${_lib_log_prefix}   FAIL: could not parse task-id from start output" >&2
    printf '%s\n' "$out" | sed 's/^/        /' >&2
    return 1
}

reset_state_between_scenarios() {
    # Forget every task and container so the next scenario starts from task-001
    # against a clean tmp_data. Files under tasks/ may be owned by Pi
    # (uid=1000) — direct host `rm` then fails silently — so do the cleanup
    # from a root container that has the bind mount.
    for ctr in $(docker ps -a --filter "label=naiw.managed=1" --format '{{.Names}}' 2>/dev/null); do
        docker rm -f "$ctr" >/dev/null 2>&1 || true
    done
    docker run --rm --entrypoint=/bin/sh --user 0:0 \
        -v "$tmp_data:/d" \
        "$task_image" -c "rm -rf /d/tasks/* /d/workspace/repos/*" >/dev/null 2>&1 || true
}

# ─── Scenario 1 — list happy path + reconciliation ─────────────────

scenario_1() {
    echo
    echo "${_lib_log_prefix} ─── Scenario 1: list happy path + reconciliation ──"
    reset_state_between_scenarios
    local out tid

    tid="$(start_and_capture_id)" || return 1
    echo "${_lib_log_prefix}   OK: started $tid"

    out="$(naiw_tasks list 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "$tid" "$out" "list shows task" || return 1
    assert_contains "running" "$out" "list shows running" || return 1

    docker stop "naiw-task-$tid" >/dev/null || return 1

    # `docker stop` sends SIGTERM with a grace period; PID 1's exit code is
    # image-dependent. The truth table is: `running + exited(0)` → interrupted,
    # `running + exited(N!=0)` → failed. Both prove reconciliation works. The
    # worksheet's "Expected: interrupted" assumes exit 0; in CI we may get 143
    # (SIGTERM-on-tmux-server). Accept either as a pass — the scenario's truth
    # claim is "status flips off `running` based on container state", not the
    # specific destination cell.
    out="$(naiw_tasks list --all 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    local terminal_seen=""
    if [[ "$out" == *"interrupted"* ]]; then
        terminal_seen="interrupted"
    elif [[ "$out" == *"failed"* ]]; then
        terminal_seen="failed"
    fi
    if [[ -z "$terminal_seen" ]]; then
        echo "${_lib_log_prefix}   FAIL: reconcile did not flip running → interrupted/failed" >&2
        printf '%s\n' "$out" | sed 's/^/        /' >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: reconcile flips running → $terminal_seen"

    local status
    status="$(jq -r .status "$tmp_data/tasks/$tid/meta/task.json")"
    if [[ "$status" != "$terminal_seen" ]]; then
        echo "${_lib_log_prefix}   FAIL: task.json status=$status, expected $terminal_seen" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: persistence atomic (task.json=$status)"

    # Stickiness: rerun --all, status should still be $terminal_seen (neither
    # interrupted nor failed auto-recovers per LIST-05 + reconcile.py terminal
    # short-circuit).
    out="$(naiw_tasks list --all 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "$terminal_seen" "$out" "status is sticky (LIST-05)" || return 1

    out="$(naiw_tasks list --status "$terminal_seen" 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "$tid" "$out" "--status $terminal_seen finds task" || return 1

    out="$(naiw_tasks list --all 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "$tid" "$out" "--all finds task" || return 1

    # 2>/dev/null on the --json call: startup_checks.warn_uid_mismatch_with_image
    # writes a multi-line uid-mismatch note to stderr which jq cannot parse.
    # The note is informational, not an error — the command's exit code is the
    # source of truth for whether the call succeeded.
    out="$(naiw_tasks list --all --json 2>/dev/null)" || { printf '%s\n' "$out" >&2; return 1; }
    # Schema spot-check: top-level `tasks` array with at least one entry whose
    # `id` matches our task. events_offset is only populated when list_cmd
    # actually writes to task.json (transition / events / size change) — too
    # brittle to require it here. The presence of the documented shape is what
    # downstream consumers actually rely on.
    local id_in_json
    id_in_json="$(printf '%s' "$out" | jq -r '.tasks[0].id // empty' 2>/dev/null || true)"
    if [[ "$id_in_json" != "$tid" ]]; then
        echo "${_lib_log_prefix}   FAIL: --json tasks[0].id=$id_in_json, expected $tid" >&2
        printf '%s\n' "$out" | head -c 400 | sed 's/^/        /' >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: --json payload well-formed (tasks[0].id=$id_in_json)"

    naiw_tasks finish "$tid" --delete-worktree >/dev/null 2>&1 || true
    echo "${_lib_log_prefix} Scenario 1 PASS"
}

# ─── Scenario 2 — output end-to-end + symlink defense ──────────────

scenario_2() {
    echo
    echo "${_lib_log_prefix} ─── Scenario 2: output end-to-end + symlink defense ──"
    reset_state_between_scenarios
    local out tid marker="phase4-scenario-2-marker"

    tid="$(start_and_capture_id)" || return 1
    echo "${_lib_log_prefix}   OK: started $tid"

    if ! wait_for_tmux_session "naiw-task-$tid" main 10; then
        echo "${_lib_log_prefix}   FAIL: tmux session 'main' did not come up in 10s" >&2
        return 1
    fi

    docker exec "naiw-task-$tid" tmux send-keys -t main "echo $marker-line-1" Enter || return 1
    docker exec "naiw-task-$tid" tmux send-keys -t main "echo $marker-non-ascii: alpha beta" Enter || return 1

    local log_path="$tmp_data/tasks/$tid/io/terminal.log"
    if ! wait_for_log_marker "$log_path" "$marker-line-1" 5; then
        echo "${_lib_log_prefix}   FAIL: marker did not appear in terminal.log within 5s" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: tmux pipe-pane wrote to terminal.log"

    out="$(naiw_tasks output "$tid" 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "$marker-line-1" "$out" "output contains marker" || return 1

    out="$(naiw_tasks output "$tid" --lines 1 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    if [[ -z "$out" ]]; then
        echo "${_lib_log_prefix}   FAIL: --lines 1 returned empty" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: --lines 1 returns content"

    if naiw_tasks output "$tid" --lines 0 >/dev/null 2>&1; then
        echo "${_lib_log_prefix}   FAIL: --lines 0 did not exit non-zero" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: --lines 0 exits non-zero"

    if naiw_tasks output never-existed-001 >/dev/null 2>&1; then
        echo "${_lib_log_prefix}   FAIL: missing-task did not exit non-zero" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: missing task exits non-zero"

    docker stop "naiw-task-$tid" >/dev/null || return 1
    mv "$log_path" "$log_path.bak"
    ln -s /etc/hostname "$log_path"
    out="$(naiw_tasks output "$tid" 2>&1)" && {
        echo "${_lib_log_prefix}   FAIL: output on symlink did not exit non-zero" >&2
        printf '%s\n' "$out" | sed 's/^/        /' >&2
        rm -f "$log_path"; mv "$log_path.bak" "$log_path"
        return 1
    }
    rm -f "$log_path"
    mv "$log_path.bak" "$log_path"
    local host_id
    host_id="$(cat /etc/hostname)"
    if [[ -n "$host_id" && "$out" == *"$host_id"* ]]; then
        echo "${_lib_log_prefix}   FAIL: symlink leak — stdout contains hostname '$host_id'" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: symlink refused (no host data leaked)"

    naiw_tasks finish "$tid" --delete-worktree >/dev/null 2>&1 || true
    echo "${_lib_log_prefix} Scenario 2 PASS"
}

# ─── Scenario 3 — DATA-08 monotonic-growth tamper warning ──────────

scenario_3() {
    echo
    echo "${_lib_log_prefix} ─── Scenario 3: DATA-08 monotonic-growth tamper warning ──"
    reset_state_between_scenarios
    local out tid

    tid="$(start_and_capture_id)" || return 1
    echo "${_lib_log_prefix}   OK: started $tid"

    if ! wait_for_tmux_session "naiw-task-$tid" main 10; then
        echo "${_lib_log_prefix}   FAIL: tmux not up" >&2
        return 1
    fi

    # ~1 KiB of output via a single tmux send-keys to avoid 100 round-trips.
    docker exec "naiw-task-$tid" tmux send-keys -t main \
        'for i in $(seq 1 100); do echo "phase4 line $i to fill terminal.log"; done' Enter || return 1

    local log_path="$tmp_data/tasks/$tid/io/terminal.log"
    if ! wait_for_log_marker "$log_path" "phase4 line 100" 8; then
        echo "${_lib_log_prefix}   FAIL: log did not reach line 100 within 8s" >&2
        return 1
    fi

    naiw_tasks list >/dev/null 2>&1 || return 1
    local max_before
    max_before="$(jq -r .terminal_log_max_size "$tmp_data/tasks/$tid/meta/task.json")"
    if [[ "$max_before" -lt 1000 ]]; then
        echo "${_lib_log_prefix}   FAIL: terminal_log_max_size=$max_before, expected ≥ 1000" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: high-water mark = $max_before bytes"

    # Truncate from INSIDE the still-running container — Pi (uid=1000) owns
    # the file and can always truncate her own log. Host-side truncation (even
    # via docker --user 0:0) hit Permission denied in CI for reasons we
    # couldn't fully chase down; doing it from inside sidesteps the whole
    # uid-mismatch class of issues entirely.
    docker exec "naiw-task-$tid" sh -c ": > /io/terminal.log" || return 1
    docker stop "naiw-task-$tid" >/dev/null || return 1

    # After docker stop the task may show interrupted OR failed (exit-code-
    # dependent). Both are non-terminal-default-hidden (interrupted) vs
    # terminal-hidden (failed). Use --all to see the row regardless.
    out="$(naiw_tasks list --all 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "log shrunk" "$out" "list shows '(log shrunk)' marker" || return 1

    local max_after
    max_after="$(jq -r .terminal_log_max_size "$tmp_data/tasks/$tid/meta/task.json")"
    if [[ "$max_after" != "$max_before" ]]; then
        echo "${_lib_log_prefix}   FAIL: max changed $max_before → $max_after (must NOT lower)" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: max preserved at $max_after bytes (D-14)"

    # Restart container to append from inside. The append needs to push the
    # file size above the prior high-water mark; do enough lines for a healthy
    # margin past $max_before bytes.
    docker start "naiw-task-$tid" >/dev/null || return 1
    if ! wait_for_tmux_session "naiw-task-$tid" main 10; then
        echo "${_lib_log_prefix}   FAIL: tmux did not return after restart" >&2
        return 1
    fi
    docker exec "naiw-task-$tid" sh -c \
        "for i in \$(seq 1 500); do echo \"recovery line \$i to grow log above prior max ($max_before bytes)\" >> /io/terminal.log; done" \
        || return 1

    out="$(naiw_tasks list --all 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    if [[ "$out" == *"log shrunk"* ]]; then
        echo "${_lib_log_prefix}   FAIL: '(log shrunk)' marker still present after regrowth" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: '(log shrunk)' cleared after regrowth"

    naiw_tasks finish "$tid" --delete-worktree >/dev/null 2>&1 || true
    echo "${_lib_log_prefix} Scenario 3 PASS"
}

# ─── Scenario 4 — auto_finish=true → real container teardown ───────

scenario_4() {
    echo
    echo "${_lib_log_prefix} ─── Scenario 4: auto_finish=true → real container teardown ──"
    reset_state_between_scenarios
    local out tid tid2

    # ── done branch ──
    tid="$(start_and_capture_id)" || return 1
    echo "${_lib_log_prefix}   OK: started $tid (done branch)"

    local tj="$tmp_data/tasks/$tid/meta/task.json"
    jq '.auto_finish = true' "$tj" > "$tj.tmp" && mv "$tj.tmp" "$tj"
    if [[ "$(jq -r .auto_finish "$tj")" != "true" ]]; then
        echo "${_lib_log_prefix}   FAIL: auto_finish flip failed" >&2; return 1
    fi
    echo "${_lib_log_prefix}   OK: auto_finish=true set in task.json"

    if ! wait_for_tmux_session "naiw-task-$tid" main 10; then
        echo "${_lib_log_prefix}   FAIL: tmux not up" >&2; return 1
    fi
    docker exec "naiw-task-$tid" tmux send-keys -t main \
        'naiw-signal done --summary "scenario 4 complete"' Enter || return 1

    local events_path="$tmp_data/tasks/$tid/io/.naiw/events.jsonl"
    # naiw-signal writes compact JSON: {"ts":...,"kind":"done",...}.
    # 15s ceiling: first event also pays the .naiw/ mkdir + write-amplification
    # cost on a cold bind-mount.
    if ! wait_for_log_marker "$events_path" '"kind":"done"' 15; then
        echo "${_lib_log_prefix}   FAIL: done event did not land in events.jsonl" >&2
        if [[ -f "$events_path" ]]; then
            echo "${_lib_log_prefix}     events.jsonl contents:" >&2
            cat "$events_path" 2>/dev/null | sed 's/^/        /' >&2
        else
            echo "${_lib_log_prefix}     events.jsonl does not exist" >&2
        fi
        return 1
    fi
    echo "${_lib_log_prefix}   OK: done event recorded"

    naiw_tasks list >/dev/null 2>&1 || return 1
    out="$(naiw_tasks list --completed 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "completed" "$out" "task shows completed" || return 1
    assert_contains "notfound" "$out" "container shows notfound (torn down)" || return 1

    # _teardown_and_mark removes the container; assert it's gone. Brief retry
    # window because container.remove(force=True) returns before the daemon
    # finishes the unlink on slow filesystems.
    local removed=no
    for _ in 1 2 3 4 5; do
        if ! docker inspect "naiw-task-$tid" >/dev/null 2>&1; then
            removed=yes
            break
        fi
        sleep 0.5
    done
    if [[ "$removed" != "yes" ]]; then
        echo "${_lib_log_prefix}   FAIL: container still exists after auto_finish" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: container removed (not just stopped)"

    local s ft
    s="$(jq -r .status "$tj")"
    ft="$(jq -r .finished_at "$tj")"
    if [[ "$s" != "completed" ]]; then
        echo "${_lib_log_prefix}   FAIL: task.json status=$s, expected completed" >&2; return 1
    fi
    if [[ "$ft" == "null" || -z "$ft" ]]; then
        echo "${_lib_log_prefix}   FAIL: task.json finished_at is null/empty" >&2; return 1
    fi
    echo "${_lib_log_prefix}   OK: task.json status=completed, finished_at=$ft"

    # ── fail branch ──
    tid2="$(start_and_capture_id)" || return 1
    echo "${_lib_log_prefix}   OK: started $tid2 (fail branch)"

    local tj2="$tmp_data/tasks/$tid2/meta/task.json"
    jq '.auto_finish = true' "$tj2" > "$tj2.tmp" && mv "$tj2.tmp" "$tj2"

    if ! wait_for_tmux_session "naiw-task-$tid2" main 10; then
        echo "${_lib_log_prefix}   FAIL: tmux not up for $tid2" >&2; return 1
    fi
    # naiw-signal fail takes `--reason TEXT`, not a positional arg.
    docker exec "naiw-task-$tid2" tmux send-keys -t main \
        'naiw-signal fail --reason "scenario 4b - simulated failure"' Enter || return 1

    local events_path2="$tmp_data/tasks/$tid2/io/.naiw/events.jsonl"
    if ! wait_for_log_marker "$events_path2" '"kind":"fail"' 15; then
        echo "${_lib_log_prefix}   FAIL: fail event did not land" >&2
        echo "${_lib_log_prefix}     path: $events_path2" >&2
        if [[ -f "$events_path2" ]]; then
            local sz
            sz="$(stat -c %s "$events_path2" 2>/dev/null || echo "?")"
            echo "${_lib_log_prefix}     events.jsonl size=${sz}; contents:" >&2
            cat "$events_path2" 2>/dev/null | sed 's/^/        /' >&2
        else
            echo "${_lib_log_prefix}     events.jsonl does not exist" >&2
            if [[ -d "$tmp_data/tasks/$tid2/io" ]]; then
                echo "${_lib_log_prefix}     io/ dir tree:" >&2
                ls -la "$tmp_data/tasks/$tid2/io/" 2>&1 | sed 's/^/        /' >&2
                ls -la "$tmp_data/tasks/$tid2/io/.naiw/" 2>&1 | sed 's/^/        /' >&2 || true
            fi
        fi
        # Dump tmux pane contents to see if naiw-signal printed an error.
        echo "${_lib_log_prefix}     tmux pane snapshot:" >&2
        docker exec "naiw-task-$tid2" tmux capture-pane -t main -p 2>&1 \
            | tail -20 | sed 's/^/        /' >&2 || true
        return 1
    fi
    naiw_tasks list >/dev/null 2>&1 || return 1
    out="$(naiw_tasks list --completed 2>&1)" || { printf '%s\n' "$out" >&2; return 1; }
    assert_contains "failed" "$out" "task shows failed" || return 1

    local removed2=no
    for _ in 1 2 3 4 5; do
        if ! docker inspect "naiw-task-$tid2" >/dev/null 2>&1; then
            removed2=yes
            break
        fi
        sleep 0.5
    done
    if [[ "$removed2" != "yes" ]]; then
        echo "${_lib_log_prefix}   FAIL: $tid2 container still exists after fail+auto_finish" >&2
        return 1
    fi
    echo "${_lib_log_prefix}   OK: fail-branch teardown works"

    naiw_tasks finish "$tid" --delete-worktree >/dev/null 2>&1 || true
    naiw_tasks finish "$tid2" --delete-worktree >/dev/null 2>&1 || true
    echo "${_lib_log_prefix} Scenario 4 PASS"
}

# ─── Run scenarios ─────────────────────────────────────────────────

for n in 1 2 3 4; do
    if ! "scenario_$n"; then
        scenario_failures+=("$n")
    fi
done

if [[ ${#scenario_failures[@]} -eq 0 ]]; then
    overall_status=PASS
    exit 0
fi

echo "${_lib_log_prefix} FAIL: scenarios ${scenario_failures[*]} did not pass" >&2
exit 1
