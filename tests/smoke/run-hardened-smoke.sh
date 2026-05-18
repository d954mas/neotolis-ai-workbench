#!/usr/bin/env bash
# tests/smoke/run-hardened-smoke.sh — hardened lifecycle gate.
#
# Runs naiw-task-image under the full HARD-* hardening flag set with production-shape
# bind-mounts and exercises 10 requirement assertions (HARD-01..HARD-08, HARD-10 + PROXY-05)
# plus lifecycle cross-cutting validations (PID-1 wrapper, secret redaction,
# stop+start log survival, signal cycle, cgroup peak evidence).
#
# HARD-09 (controller-side bind-mount source-path validation) is owned by the
# controller phase and not probed here — its smoke check ships with that code.
#
# Contract: tests/smoke/HARDENED-CHECKLIST.md is the static contract; this script
# is the executor. Drift gate at the end of this script asserts the ID set in
# both files matches.
#
# Skip: on non-Linux hosts (Windows-native, WSL2 with /mnt/c-backed $HOME),
# prints "[hardened-smoke] SKIP: requires Linux host with Linux-FS" and exits 0.
set -euo pipefail

# Disable MSYS/Git-Bash path mangling — harmless no-op on Linux.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

_lib_log_prefix="[hardened-smoke]"
# step_fail returns 1 instead of exiting so this gate's own fail() can dump
# docker logs before bailing.
_lib_step_fail_exits=0
# shellcheck source=_lib.sh
source "$here/_lib.sh"

# ─── Globals ───────────────────────────────────────────────────────────
NAIW_VERSION="${NAIW_VERSION:-0.1.0}"
image="naiw-task-image:${NAIW_VERSION}"
container="naiw-smoke-hardened-$$"
pass1_container="naiw-smoke-pip-pass1-$$"
pass2_container="naiw-smoke-pip-pass2-$$"
probe_target="naiw-smoke-probe-target-$$"
compose_file="deploy/docker-compose.yml"
tmp_base="$HOME/.naiw-smoke"
TMP=""
fake_token="ghp_TESTFAKEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
gate_failed=0
log_path=""
events_path=""

# ─── fail() + cleanup() + EXIT trap ────────────────────────────────────
# fail dumps container logs then exits 1; cleanup is asymmetric per the
# checklist contract (success path tears everything down; fail path preserves
# container + tmpdir + compose for operator post-mortem).

fail() {
    gate_failed=1
    echo "${_lib_log_prefix} FAIL: $*" >&2
    docker logs "$container" 2>&1 | sed 's/^/[container] /' >&2 || true
    exit 1
}

_hardened_cleanup() {
    local rc=$?
    if [[ "$rc" -eq 0 && "$gate_failed" -eq 0 ]]; then
        docker rm -f "$container" >/dev/null 2>&1 || true
        docker rm -f "$pass1_container" >/dev/null 2>&1 || true
        docker rm -f "$pass2_container" >/dev/null 2>&1 || true
        docker rm -f "$probe_target" >/dev/null 2>&1 || true
        [[ -n "$TMP" ]] && rm -rf "$TMP" || true
        docker compose -f "$compose_file" down >/dev/null 2>&1 || true
        echo "${_lib_log_prefix} PASS — all 10 requirements verified"
    else
        echo "${_lib_log_prefix} FAIL — artifacts preserved for inspection:" >&2
        echo "  container: $container" >&2
        echo "  tmpdir:    $TMP" >&2
        echo "  compose:   $compose_file (still up)" >&2
        echo "Manual cleanup:" >&2
        echo "  docker rm -f $container $pass1_container $pass2_container $probe_target 2>/dev/null" >&2
        echo "  rm -rf \"$TMP\"" >&2
        echo "  docker compose -f $compose_file down" >&2
    fi
}
trap _hardened_cleanup EXIT INT TERM

# ─── Helper: start_smoke_container ──────────────────────────────────────
# Lifts the main hardened-container run+wait block into a function so the
# REC-IMG-06 redaction probe can re-run it after a host-side recover
# (docker rm + re-run with the same flags + same name + same mounts +
# same labels). Used by the main start AND by the recover-boundary probe.
start_smoke_container() {
    docker run -d --init --name "$container" \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --read-only \
        --tmpfs /tmp:rw,size=512m,mode=1777 \
        --tmpfs /run:rw,size=64m,mode=755 \
        --pids-limit=512 \
        --memory=4g \
        --memory-swap=4g \
        --cpus=2 \
        --network naiw-task-net \
        --restart=no \
        -t \
        --label naiw.managed=1 \
        --label naiw.task-id=smoke-test \
        --label naiw.role=task-container \
        -v "$TMP/naiw-data/tasks/smoke-test/work:/work" \
        -v "$TMP/naiw-data/tasks/smoke-test/io:/io" \
        -v "$TMP/naiw-data/tasks/smoke-test/storage:/home/pi:rw" \
        -v "$TMP/naiw-data/pi-packages:/pi-packages:ro" \
        -v "$TMP/naiw-data/secrets/test_token:/run/secrets/test_token:ro" \
        "$image" >/dev/null
    wait_for_container_ready "$container" '[ -f /io/.naiw/events.jsonl ] && [ -f /io/terminal.log ]' 30 \
        || fail "container not ready"
    wait_for_tmux_session "$container" main 10 \
        || fail "tmux 'main' session not online within 10s"
}

# ─── Step 01: platform check ───────────────────────────────────────────
step_check "" "Step 01: platform check (Linux + non-/mnt/c \$HOME)"
if ! [[ "$(uname -s)" == "Linux" && ! "$HOME" =~ ^/mnt/c ]]; then
    echo "${_lib_log_prefix} SKIP: requires Linux host with Linux-FS"
    gate_failed=0
    trap - EXIT INT TERM
    exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "${_lib_log_prefix} SKIP: docker not on PATH"
    trap - EXIT INT TERM
    exit 0
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "${_lib_log_prefix} SKIP: docker compose v2 not available"
    trap - EXIT INT TERM
    exit 0
fi
step_ok "" "Step 01: platform Linux + Linux-FS confirmed"

# ─── Step 02: setup (tmpdir, network, compose up, image build) ─────────
step_check "" "Step 02: setup (tmpdir, naiw-task-net, compose up)"

mkdir -p "$tmp_base"
TMP="$(mktemp -d --tmpdir="$tmp_base" smoke.XXXXXXXX)"

mkdir -p "$TMP/naiw-data/secrets"
mkdir -p "$TMP/naiw-data/pi-packages/dummy-pkg"
mkdir -p "$TMP/naiw-data/tasks/smoke-test/meta"
mkdir -p "$TMP/naiw-data/tasks/smoke-test/work"
mkdir -p "$TMP/naiw-data/tasks/smoke-test/io"
# Pre-create terminal.log mode 0666 so BOTH host (recovery banner append in
# step 17b) AND in-container pi (uid 1000, pipe-pane append) can write.
# Without this pre-create, pi creates the file 0644 owner-only and the
# host (CI runner uid != 1000) gets EACCES on `>> terminal.log`.
touch "$TMP/naiw-data/tasks/smoke-test/io/terminal.log"
chmod 0666 "$TMP/naiw-data/tasks/smoke-test/io/terminal.log"
echo "synthetic" > "$TMP/naiw-data/pi-packages/dummy-pkg/marker"
printf '%s' "$fake_token" > "$TMP/naiw-data/secrets/test_token"
chmod 0600 "$TMP/naiw-data/secrets/test_token"

# Idempotent network creation; race-safe against concurrent runs.
docker network inspect naiw-task-net >/dev/null 2>&1 \
    || docker network create naiw-task-net 2>&1 | grep -v 'already exists' || true

build_image_if_missing "$image"

docker compose -f "$compose_file" up -d

# Wait for proxy to accept allowed requests on naiw-internal.
ready=0
for i in $(seq 1 30); do
    code="$(docker run --rm --network naiw-internal curlimages/curl:latest \
        -s -o /dev/null -w '%{http_code}' --max-time 2 \
        http://naiw-docker-proxy:2375/v1.43/containers/json 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then ready=1; break; fi
    sleep 1
done
[[ "$ready" -eq 1 ]] || fail "proxy did not accept allowed requests within 30s"

# Permission wrinkle: image runs as uid 1000; chown if host uid differs.
# Covers /work, /io (read-write task dirs) and the secret file pi must read.
host_uid="$(id -u)"
if [[ "$host_uid" != "1000" ]]; then
    docker run --rm --user 0 \
        -v "$TMP/naiw-data/tasks/smoke-test:/t" \
        -v "$TMP/naiw-data/secrets:/s" \
        alpine sh -c 'chown -R 1000:1000 /t/work /t/io && chown 1000:1000 /s/test_token' >/dev/null
fi

step_ok "" "Step 02: setup complete (TMP=$TMP)"

# ─── Step 03: HARD-03 pip install --user pyyaml two-pass probe ─────────
# Run BEFORE the main hardened container to establish the empirical contract.
# Pass 1 = no /home/pi tmpfs (must fail because the home dir is unwritable
# on the read-only rootfs — EROFS or 'Permission denied' depending on which
# write op pip hits first); Pass 2 = with tmpfs (must succeed).
step_check "HARD-03" "Step 03: pip install --user pyyaml two-pass probe (writable-home contract)"

docker run -d --init --name "$pass1_container" \
    --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,size=512m,mode=1777 \
    --tmpfs /run:rw,size=64m,mode=755 \
    --pids-limit=512 --memory=4g --memory-swap=4g --cpus=2 \
    --network naiw-task-net --restart=no -t \
    -v "$TMP/naiw-data/tasks/smoke-test/work:/work" \
    -v "$TMP/naiw-data/tasks/smoke-test/io:/io" \
    -v "$TMP/naiw-data/pi-packages:/pi-packages:ro" \
    "$image" >/dev/null

wait_for_container_ready "$pass1_container" '[ -f /io/.naiw/events.jsonl ]' 30 \
    || fail "pass1 container not ready (pre-pip)"

set +e
pass1_out="$(docker exec -u pi "$pass1_container" sh -c 'pip install --user --quiet pyyaml' 2>&1)"
pass1_rc=$?
set -e
docker rm -f "$pass1_container" >/dev/null

pip_result_note=""
if [[ "$pass1_rc" -eq 0 ]]; then
    step_warn "HARD-03" "pip install --user pyyaml SUCCEEDED without /home/pi tmpfs — writable-home contract invalidated; check image layer"
    pip_result_note="DOES NOT REQUIRE --tmpfs /home/pi (pip succeeded without it)"
else
    if grep -qE 'Read-only file system|EROFS|Permission denied' <<< "$pass1_out"; then
        step_ok "HARD-03" "pass1 pip failed as expected (no /home/pi tmpfs): $(head -1 <<< "$pass1_out")"
        pip_result_note="REQUIRES --tmpfs /home/pi:rw,size=128m,mode=1777"
    else
        step_warn "HARD-03" "pass1 pip failed for a DIFFERENT reason: $(head -1 <<< "$pass1_out")"
        pip_result_note="REQUIRES --tmpfs /home/pi (probable; non-fs error in pass1)"
    fi
fi

# pass2 uses a bind-mount for /home/pi (matching the production shape:
# tasks/<id>/storage/ → /home/pi:rw). Re-create the source directory each
# run with sticky-writable mode so pi (uid 1000) can write.
mkdir -p "$TMP/naiw-data/tasks/smoke-test-pass2/storage"
chmod 1777 "$TMP/naiw-data/tasks/smoke-test-pass2/storage"

docker run -d --init --name "$pass2_container" \
    --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,size=512m,mode=1777 \
    --tmpfs /run:rw,size=64m,mode=755 \
    --pids-limit=512 --memory=4g --memory-swap=4g --cpus=2 \
    --network naiw-task-net --restart=no -t \
    -v "$TMP/naiw-data/tasks/smoke-test/work:/work" \
    -v "$TMP/naiw-data/tasks/smoke-test/io:/io" \
    -v "$TMP/naiw-data/tasks/smoke-test-pass2/storage:/home/pi:rw" \
    -v "$TMP/naiw-data/pi-packages:/pi-packages:ro" \
    "$image" >/dev/null

wait_for_container_ready "$pass2_container" '[ -f /io/.naiw/events.jsonl ]' 30 \
    || fail "pass2 container not ready (pre-pip)"

docker exec -u pi "$pass2_container" sh -c 'pip install --user --quiet pyyaml && python3 -c "import yaml"' \
    || fail "pass2 pip install --user pyyaml failed even WITH /home/pi bind-mount — HARD-03 broken"
step_ok "HARD-03" "pass2 pip succeeded with /home/pi bind-mount; yaml importable"
echo "${_lib_log_prefix} [result] standard hardened run-flags use bind-mount /home/pi from tasks/<id>/storage/: $pip_result_note"
docker rm -f "$pass2_container" >/dev/null

# ─── Main hardened container start ─────────────────────────────────────
step_check "" "Starting main hardened container"
# /home/pi is now provided as a per-task bind mount from tasks/<id>/storage/
# (matches the controller's lifecycle._build_volumes wiring; the bind source
# survives `docker rm` so Pi's home state persists across recover). Re-create
# the source dir each run with sticky-writable mode.
mkdir -p "$TMP/naiw-data/tasks/smoke-test/storage"
chmod 1777 "$TMP/naiw-data/tasks/smoke-test/storage"

start_smoke_container
log_path="$TMP/naiw-data/tasks/smoke-test/io/terminal.log"
events_path="$TMP/naiw-data/tasks/smoke-test/io/.naiw/events.jsonl"
step_ok "" "Main hardened container ready"

# ─── Step 04: HARD-01 CapDrop == ["ALL"] ───────────────────────────────
step_check "HARD-01" "Step 04: CapDrop == [ALL]"
expect_inspect_eq "$container" "HARD-01" ".HostConfig.CapDrop" '["ALL"]' \
    || fail "HARD-01 CapDrop mismatch"

# ─── Step 05: HARD-02 SecurityOpt contains no-new-privileges ───────────
step_check "HARD-02" "Step 05: SecurityOpt contains no-new-privileges"
sec_opt="$(inspect_json "$container" ".HostConfig.SecurityOpt")"
if [[ "$sec_opt" == *"no-new-privileges"* ]]; then
    step_ok "HARD-02" "SecurityOpt=$sec_opt"
else
    step_fail "HARD-02" "SecurityOpt=$sec_opt does not contain no-new-privileges"
    fail "HARD-02 SecurityOpt missing"
fi

# ─── Step 06: HARD-03 ReadonlyRootfs == true ───────────────────────────
step_check "HARD-03" "Step 06: ReadonlyRootfs == true"
expect_inspect_eq "$container" "HARD-03" ".HostConfig.ReadonlyRootfs" 'true' \
    || fail "HARD-03 ReadonlyRootfs mismatch"

# ─── Step 07: HARD-03 /tmp writable ────────────────────────────────────
step_check "HARD-03" "Step 07: /tmp writable (tmpfs probe)"
docker exec "$container" sh -c 'touch /tmp/probe && rm /tmp/probe' \
    || fail "HARD-03 /tmp not writable"
step_ok "HARD-03" "/tmp writable"

# ─── Step 08: HARD-03 /run writable by root ────────────────────────────
# /run is intentionally mode=755 owned by root (matches the requirement),
# so we probe writability from uid 0 rather than from user `pi`. Probing
# from pi would always fail and contradict the very mode bit the
# requirement asks for.
step_check "HARD-03" "Step 08: /run writable (tmpfs probe, from root)"
docker exec -u 0 "$container" sh -c 'touch /run/probe && rm /run/probe' \
    || fail "HARD-03 /run not writable by root"
step_ok "HARD-03" "/run writable by root"

# ─── Step 09: HARD-04 PidsLimit == 512 ─────────────────────────────────
step_check "HARD-04" "Step 09: PidsLimit == 512"
pids_limit="$(docker inspect --format='{{.HostConfig.PidsLimit}}' "$container")"
if [[ "$pids_limit" == "512" ]]; then
    step_ok "HARD-04" "PidsLimit=512"
else
    step_fail "HARD-04" "PidsLimit=$pids_limit, expected 512"
    fail "HARD-04 PidsLimit mismatch"
fi

# ─── Step 10: HARD-05 Memory + MemorySwap + NanoCpus ───────────────────
step_check "HARD-05" "Step 10: Memory=4Gi + MemorySwap=4Gi + NanoCpus=2.0"
mem="$(docker inspect --format='{{.HostConfig.Memory}}' "$container")"
mem_swap="$(docker inspect --format='{{.HostConfig.MemorySwap}}' "$container")"
cpus="$(docker inspect --format='{{.HostConfig.NanoCpus}}' "$container")"
if [[ "$mem" == "4294967296" && "$mem_swap" == "4294967296" && "$cpus" == "2000000000" ]]; then
    step_ok "HARD-05" "Memory=$mem MemorySwap=$mem_swap NanoCpus=$cpus"
else
    step_fail "HARD-05" "Memory=$mem MemorySwap=$mem_swap NanoCpus=$cpus (expected 4294967296/4294967296/2000000000)"
    fail "HARD-05 limits mismatch"
fi

# ─── Step 11: HARD-06 NetworkMode + single-key Networks ────────────────
step_check "HARD-06" "Step 11: NetworkMode == naiw-task-net and exactly one network attached"
net_mode="$(docker inspect --format='{{.HostConfig.NetworkMode}}' "$container")"
nets_json="$(inspect_json "$container" ".NetworkSettings.Networks")"
# Top-level keys of the Networks JSON object must equal exactly {naiw-task-net}.
net_keys="$(echo "$nets_json" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(",".join(sorted(d.keys())))')"
if [[ "$net_mode" == "naiw-task-net" && "$net_keys" == "naiw-task-net" ]]; then
    step_ok "HARD-06" "NetworkMode=$net_mode keys=$net_keys"
else
    step_fail "HARD-06" "NetworkMode=$net_mode keys=$net_keys (expected naiw-task-net / naiw-task-net)"
    fail "HARD-06 NetworkMode mismatch"
fi

# ─── Step 12: HARD-07 /meta NOT in container mountinfo ─────────────────
step_check "HARD-07" "Step 12: tasks/<id>/meta/ NOT visible in container mountinfo"
if assert_no_meta_mount "$container"; then
    step_ok "HARD-07" "no /meta entry in mountinfo"
else
    step_fail "HARD-07" "meta/ leaked into container mountinfo"
    fail "HARD-07 meta/ exposed"
fi

# ─── Step 13: HARD-08 mountinfo flags + write probes ───────────────────
step_check "HARD-08" "Step 13: /work rw, /io rw, /pi-packages ro, /run/secrets ro"

assert_mountinfo_flag "$container" "/work" "rw" \
    || { step_fail "HARD-08" "/work not rw in mountinfo"; fail "HARD-08 /work flag"; }
docker exec -u pi "$container" sh -c 'touch /work/probe && rm /work/probe' \
    || { step_fail "HARD-08" "/work write probe failed"; fail "HARD-08 /work write"; }

assert_mountinfo_flag "$container" "/io" "rw" \
    || { step_fail "HARD-08" "/io not rw in mountinfo"; fail "HARD-08 /io flag"; }
docker exec -u pi "$container" sh -c 'touch /io/probe && rm /io/probe' \
    || { step_fail "HARD-08" "/io write probe failed"; fail "HARD-08 /io write"; }

assert_mountinfo_flag "$container" "/pi-packages" "ro" \
    || { step_fail "HARD-08" "/pi-packages not ro in mountinfo"; fail "HARD-08 /pi-packages flag"; }
set +e
pp_write_out="$(docker exec -u pi "$container" sh -c 'touch /pi-packages/probe' 2>&1)"
pp_write_rc=$?
set -e
if [[ "$pp_write_rc" -eq 0 ]]; then
    step_fail "HARD-08" "/pi-packages write probe SUCCEEDED (expected EROFS)"
    fail "HARD-08 /pi-packages write should fail"
fi
if ! grep -q 'Read-only file system' <<< "$pp_write_out"; then
    step_warn "HARD-08" "/pi-packages write failed but not with EROFS: $pp_write_out"
fi

# /run/secrets/test_token ro
sec_flags="$(docker exec "$container" awk '$5 ~ /\/run\/secrets/ {print $6}' /proc/self/mountinfo | head -1)"
if [[ "$sec_flags" == *"ro"* ]]; then
    step_ok "HARD-08" "/work rw, /io rw, /pi-packages ro, /run/secrets ro (flags=$sec_flags)"
else
    step_fail "HARD-08" "/run/secrets flags=$sec_flags, missing ro"
    fail "HARD-08 /run/secrets not ro"
fi

# ─── Step 14: HARD-10 RestartPolicy == no ──────────────────────────────
step_check "HARD-10" "Step 14: RestartPolicy.Name == no"
expect_inspect_eq "$container" "HARD-10" ".HostConfig.RestartPolicy.Name" '"no"' \
    || fail "HARD-10 RestartPolicy mismatch"

# ─── Step 15: PID-1 wrapper ────────────────────────────────────────────
step_check "PID-1" "Step 15: /proc/1/comm in {tmux, tini, docker-init}"
pid1="$(docker exec "$container" cat /proc/1/comm | tr -d '\r\n')"
case "$pid1" in
    tmux|tini|docker-init)
        step_ok "PID-1" "/proc/1/comm=$pid1"
        ;;
    *)
        step_fail "PID-1" "/proc/1/comm=$pid1, expected tmux|tini|docker-init"
        fail "PID-1 wrapper mismatch"
        ;;
esac

# ─── Step 16: secret content + Config.Env scrub + redaction filter ─────
step_check "secret" "Step 16: /run/secrets/test_token content + Config.Env scrub + redaction"

in_container="$(docker exec "$container" cat /run/secrets/test_token | tr -d '\r\n')"
if [[ "$in_container" != "$fake_token" ]]; then
    step_fail "secret" "in-container secret content mismatch"
    fail "secret content mismatch"
fi

env_json="$(docker inspect --format='{{json .Config.Env}}' "$container")"
if [[ "$env_json" == *"ghp_"* || "$env_json" == *"test_token"* ]]; then
    step_fail "secret" "secret-shaped string leaked into Config.Env: $env_json"
    fail "secret leaked into env"
fi

docker exec "$container" tmux send-keys -t main "printf '%s\n' \"\$(cat /run/secrets/test_token)\"" Enter

if ! wait_for_log_marker "$log_path" '[REDACTED]' 3; then
    step_fail "secret" "[REDACTED] marker missing from terminal.log after token-echo (waited 3s)"
    fail "redaction filter not active"
fi
if grep -q "$fake_token" "$log_path"; then
    step_fail "secret" "raw token leaked into terminal.log"
    fail "redaction filter did not redact raw token"
fi
step_ok "secret" "content match + env scrub + terminal.log redaction confirmed"

# ─── Step 17: stop+start terminal.log survival + POST_RESTART_MARKER ───
step_check "HARD-restart" "Step 17: terminal.log survives stop+start; POST_RESTART_MARKER appears"
size_before="$(stat -c %s "$log_path")"
docker stop --time 10 "$container" >/dev/null
docker start "$container" >/dev/null
wait_for_container_ready "$container" '[ -f /io/terminal.log ]' 15 \
    || fail "terminal.log missing after restart"
wait_for_tmux_session "$container" main 5 \
    || fail "tmux session 'main' did not come back online within 5s"
docker exec "$container" tmux send-keys -t main 'echo POST_RESTART_MARKER' Enter
if ! wait_for_log_marker "$log_path" 'POST_RESTART_MARKER' 3; then
    step_fail "HARD-restart" "POST_RESTART_MARKER missing from terminal.log after restart (waited 3s)"
    fail "post-restart marker missing"
fi
size_after="$(stat -c %s "$log_path")"
if (( size_after < size_before )); then
    step_fail "HARD-restart" "terminal.log shrunk: before=$size_before after=$size_after"
    fail "terminal.log shrunk across restart"
fi
step_ok "HARD-restart" "terminal.log appended through stop+start (before=$size_before after=$size_after)"

# ─── Step 17b: REC-IMG-06 redaction filter across recover boundary ─────
# Simulates a host-side recover (docker rm + re-run with the same name,
# mounts, and labels) and asserts the pipe-pane redaction filter still
# catches a Pi-shaped token in the NEW container — the filter is re-issued
# by the entrypoint on every container start.
step_check "REC-IMG-06" "Step 17b: IMG-06 redaction filter active across recover boundary"

# Capture pre-recover size to assert monotonic growth across the boundary.
pre_size_rec=$(stat -c%s "${log_path}")

# Simulate operator interrupt: stop the container.
docker stop --time 10 "$container" >/dev/null

# Host-side recovery banner write (matches lifecycle._append_recovery_banner).
{
    printf '\n===== RECOVERED #1 AT %sZ =====\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%S.000')"
} >> "${log_path}"

# docker rm + re-run with the same flags via the extracted helper.
docker rm -f "$container" >/dev/null

start_smoke_container

# wait_for_tmux_session inside start_smoke_container confirms tmux is
# reachable, but pipe-pane setup happens later in the entrypoint —
# race window where send-keys would write raw before the sed filter is
# wired. Send a benign probe first, wait for it to land in terminal.log,
# proving pipe-pane is intercepting writes.
docker exec "$container" tmux send-keys -t main \
    "printf 'PIPE_PANE_PROBE_REC\n'" Enter
if ! wait_for_log_marker "${log_path}" 'PIPE_PANE_PROBE_REC' 5; then
    step_fail "REC-IMG-06" "pipe-pane filter never came online post-recover"
    fail "REC-IMG-06 pipe-pane not ready"
fi

# Send the token, then a sentinel that follows it through pipe-pane. When
# the sentinel lands in terminal.log, pipe-pane has definitely flushed
# the token line through sed. Avoids confusing stale [REDACTED] matches
# from Step 16, and avoids racing pipe-pane buffering.
docker exec "$container" tmux send-keys -t main \
    "printf 'ghp_TESTTOKEN1234567890abcdef\n'" Enter
docker exec "$container" tmux send-keys -t main \
    "printf 'POST_TOKEN_SENTINEL_REC\n'" Enter

if ! wait_for_log_marker "${log_path}" 'POST_TOKEN_SENTINEL_REC' 5; then
    step_fail "REC-IMG-06" "post-token sentinel never reached terminal.log (pipe-pane stuck?)"
    echo "--- terminal.log tail (debug) ---" >&2
    tail -n 40 "${log_path}" >&2 || true
    fail "REC-IMG-06 pipe-pane post-token flush missing"
fi
if grep -q 'ghp_TESTTOKEN' "${log_path}"; then
    step_fail "REC-IMG-06" "raw token ghp_TESTTOKEN leaked into terminal.log post-recover"
    echo "--- terminal.log tail (debug) ---" >&2
    tail -n 40 "${log_path}" >&2 || true
    fail "REC-IMG-06 raw token leaked"
fi
if ! grep -q 'RECOVERED #1' "${log_path}"; then
    step_fail "REC-IMG-06" "recovery banner missing from terminal.log"
    fail "REC-IMG-06 banner missing"
fi
post_size_rec=$(stat -c%s "${log_path}")
if (( post_size_rec < pre_size_rec )); then
    step_fail "REC-IMG-06" "terminal.log shrunk across recover (${pre_size_rec} -> ${post_size_rec})"
    fail "REC-IMG-06 terminal.log shrunk"
fi
step_ok "REC-IMG-06" "redaction filter green post-recover; banner present; terminal.log grew ${pre_size_rec} -> ${post_size_rec}"

# ─── Step 18: signal cycle — naiw-signal done → events.jsonl ───────────
step_check "SIG-cycle" "Step 18: naiw-signal done appends valid event to events.jsonl"
docker exec -u pi "$container" naiw-signal done --summary "hardened-smoke ok" \
    || fail "naiw-signal done failed"
tail -1 "$events_path" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert d["kind"] == "done", d
assert d["schema_version"] == 1, d
assert d["payload"] == {"summary": "hardened-smoke ok"}, d
' || fail "events.jsonl tail did not match expected schema"
step_ok "SIG-cycle" "events.jsonl tail kind=done schema_version=1 payload={summary:hardened-smoke ok}"

# ─── Steps 19-40: PROXY-05 22 denied verb probes ───────────────────────
# Note: pause/unpause are NOT probed. Tecnativa v0.4.2 doesn't honour
# ALLOW_PAUSE / ALLOW_UNPAUSE env-vars — they're dead config — so the
# proxy lets pause/unpause requests through. Documented limitation; the
# controller never pauses containers in normal operation (HARD-10:
# recovery is operator-driven, never automatic pause/unpause).
step_check "PROXY-05" "Steps 19-40: 22 denied verb probes (each must return 403)"
probe_proxy_endpoint POST /exec/fakeid/start             403 "EXEC"           || fail "PROXY-05 EXEC"
probe_proxy_endpoint GET  /images/json                   403 "IMAGES"         || fail "PROXY-05 IMAGES"
probe_proxy_endpoint GET  /volumes                       403 "VOLUMES"        || fail "PROXY-05 VOLUMES"
probe_proxy_endpoint GET  /networks                      403 "NETWORKS"       || fail "PROXY-05 NETWORKS"
probe_proxy_endpoint POST /build                         403 "BUILD"          || fail "PROXY-05 BUILD"
probe_proxy_endpoint POST /commit                        403 "COMMIT"         || fail "PROXY-05 COMMIT"
probe_proxy_endpoint POST /grpc                          403 "GRPC"           || fail "PROXY-05 GRPC"
probe_proxy_endpoint GET  /info                          403 "INFO"           || fail "PROXY-05 INFO"
probe_proxy_endpoint POST /auth                          403 "AUTH"           || fail "PROXY-05 AUTH"
probe_proxy_endpoint GET  /secrets                       403 "SECRETS"        || fail "PROXY-05 SECRETS"
probe_proxy_endpoint GET  /services                      403 "SERVICES"       || fail "PROXY-05 SERVICES"
probe_proxy_endpoint POST /session                       403 "SESSION"        || fail "PROXY-05 SESSION"
probe_proxy_endpoint GET  /swarm                         403 "SWARM"          || fail "PROXY-05 SWARM"
probe_proxy_endpoint GET  /system/df                     403 "SYSTEM"         || fail "PROXY-05 SYSTEM"
probe_proxy_endpoint GET  /tasks                         403 "TASKS"          || fail "PROXY-05 TASKS"
probe_proxy_endpoint GET  /plugins                       403 "PLUGINS"        || fail "PROXY-05 PLUGINS"
probe_proxy_endpoint GET  /nodes                         403 "NODES"          || fail "PROXY-05 NODES"
probe_proxy_endpoint GET  /configs                       403 "CONFIGS"        || fail "PROXY-05 CONFIGS"
probe_proxy_endpoint GET  /distribution/alpine/json      403 "DISTRIBUTION"   || fail "PROXY-05 DISTRIBUTION"
probe_proxy_endpoint GET  '/events?since=0&until=0'      403 "EVENTS"         || fail "PROXY-05 EVENTS"
probe_proxy_endpoint GET  /_ping                         403 "PING"           || fail "PROXY-05 PING"
probe_proxy_endpoint GET  /version                       403 "VERSION"        || fail "PROXY-05 VERSION"
step_ok "PROXY-05" "all 22 denied verb probes returned 403"

# ─── Step 41: PROXY-05 allowed GET /containers/json -> 200 ─────────────
step_check "PROXY-05" "Step 41: allowed GET /containers/json -> 200"
probe_proxy_endpoint GET /containers/json 200 "CONTAINERS-json" \
    || fail "PROXY-05 allowed GET /containers/json"

# ─── Step 42: PROXY-05 allowed POST /containers/<id>/start -> 204|304 ──
step_check "PROXY-05" "Step 42: allowed POST /containers/<target>/start -> 204 or 304 (throwaway sidecar)"
target_id="$(docker create --name "$probe_target" --network naiw-internal alpine sleep 1)"
probe_proxy_endpoint POST "/containers/${target_id}/start" 204 "POST start" \
    || probe_proxy_endpoint POST "/containers/${target_id}/start" 304 "POST start (already running)" \
    || fail "PROXY-05 POST start probe failed (expected 204 or 304)"
docker rm -f "$probe_target" >/dev/null 2>&1 || true

# ─── Step 43: cgroup peak evidence (WARN-only) ─────────────────────────
step_check "cgroup-peak" "Step 43: log cgroup pids.peak + memory.peak (WARN-only)"
container_id="$(docker inspect --format='{{.Id}}' "$container")"
cg_v2_path="/sys/fs/cgroup/system.slice/docker-${container_id}.scope"
if [[ -d "$cg_v2_path" ]]; then
    pids_peak="$(cat "${cg_v2_path}/pids.peak" 2>/dev/null || echo n/a)"
    mem_peak="$(cat "${cg_v2_path}/memory.peak" 2>/dev/null || echo n/a)"
else
    pids_peak="n/a"
    mem_peak="n/a"
fi
echo "${_lib_log_prefix} cgroup-peak: pids.peak=$pids_peak memory.peak=$mem_peak"
if [[ "$pids_peak" =~ ^[0-9]+$ ]] && (( pids_peak > 307 )); then
    step_warn "cgroup-peak" "consider raising --pids-limit: peak=$pids_peak, limit=512 (60% threshold breached)"
fi
if [[ "$mem_peak" =~ ^[0-9]+$ ]] && (( mem_peak > 2576980378 )); then
    step_warn "cgroup-peak" "consider raising --memory: peak=$mem_peak bytes, limit=4294967296 (60% threshold breached)"
fi
step_ok "cgroup-peak" "peak evidence recorded (pids=$pids_peak mem=$mem_peak)"

# ─── Step 44: STORAGE-BIND — /home/pi sourced from tasks/<id>/storage/ ─
# Confirms the bind-mount for /home/pi is actually in container mountinfo
# (line containing ' /home/pi '). The bind source survives `docker rm`,
# which is what makes recover able to resume Pi's home state.
step_check "STORAGE-BIND" "Step 44: tasks/<id>/storage/ bind-mounted at /home/pi"
if docker exec "$container" cat /proc/self/mountinfo | grep -qE ' /home/pi '; then
    step_ok "STORAGE-BIND" "/home/pi appears in container mountinfo"
else
    step_fail "STORAGE-BIND" "/home/pi NOT in container mountinfo (expected bind-mount from tasks/smoke-test/storage)"
    fail "STORAGE-BIND missing from mountinfo"
fi

# ─── Step 45: drift gate (checklist ↔ script ID set must align) ────────
# Only count IDs that are actually claimed (checklist table rows) or actually
# probed (step_check "ID" in script). Narrative mentions in headers/comments
# don't count — that's what makes this an honest coverage check.
step_check "" "Step 45: drift gate (HARDENED-CHECKLIST.md vs run-hardened-smoke.sh ID alignment)"
# Extract IDs from any cell in a markdown table row (leading-pipe line) so
# IDs in either the first column (HARD-XX/PROXY-XX requirements table) or
# the last column (REC-IMG-06/STORAGE-BIND lifecycle table) both surface.
checklist_ids="$(grep -E '^\|' tests/smoke/HARDENED-CHECKLIST.md \
    | grep -oE 'HARD-[0-9]+|PROXY-[0-9]+|REC-IMG-06|STORAGE-BIND' | sort -u)"
script_ids="$(grep -oE 'step_check[[:space:]]+"(HARD-[0-9]+|PROXY-[0-9]+|REC-IMG-06|STORAGE-BIND)"' tests/smoke/run-hardened-smoke.sh \
    | grep -oE 'HARD-[0-9]+|PROXY-[0-9]+|REC-IMG-06|STORAGE-BIND' | sort -u)"
set +e
drift="$(diff <(echo "$checklist_ids") <(echo "$script_ids"))"
set -e
if [[ -n "$drift" ]]; then
    echo "${_lib_log_prefix} FAIL: drift between HARDENED-CHECKLIST.md and run-hardened-smoke.sh:" >&2
    echo "$drift" >&2
    fail "drift gate"
fi
step_ok "" "Step 45: drift gate ok — IDs aligned"

# Final PASS line is printed by the cleanup trap's success branch on exit 0.
