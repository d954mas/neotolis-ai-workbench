#!/usr/bin/env bash
# tests/smoke/run-hardened-smoke.sh — hardened lifecycle gate.
#
# Runs naiw-task-image under the full HARD-* hardening flag set with production-shape
# bind-mounts and exercises 11 requirement assertions (HARD-01..HARD-10 + PROXY-05)
# plus lifecycle cross-cutting validations (PID-1 wrapper, secret redaction,
# stop+start log survival, signal cycle, cgroup peak evidence).
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
        echo "${_lib_log_prefix} PASS — all 11 requirements verified"
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
echo "synthetic" > "$TMP/naiw-data/pi-packages/dummy-pkg/marker"
printf '%s' "$fake_token" > "$TMP/naiw-data/secrets/test_token"
chmod 0600 "$TMP/naiw-data/secrets/test_token"

# HARD-09 probe artifact — symlink ALONGSIDE naiw-data/, not inside it.
ln -s /etc "$TMP/evil-link"

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
host_uid="$(id -u)"
if [[ "$host_uid" != "1000" ]]; then
    docker run --rm --user 0 -v "$TMP/naiw-data/tasks/smoke-test:/t" alpine \
        chown -R 1000:1000 /t/work /t/io >/dev/null
fi

step_ok "" "Step 02: setup complete (TMP=$TMP)"

# ─── Step 08: HARD-03 pip install --user pyyaml two-pass probe ─────────
# Run BEFORE the main hardened container to establish the empirical contract.
# Pass 1 = no /home/pi tmpfs (must fail with EROFS); Pass 2 = with tmpfs (must succeed).
step_check "HARD-03" "Step 08: pip install --user pyyaml two-pass probe (writable-home contract)"

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
        pip_result_note="REQUIRES --tmpfs /home/pi:rw,size=128m"
    else
        step_warn "HARD-03" "pass1 pip failed for a DIFFERENT reason: $(head -1 <<< "$pass1_out")"
        pip_result_note="REQUIRES --tmpfs /home/pi (probable; non-fs error in pass1)"
    fi
fi

docker run -d --init --name "$pass2_container" \
    --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,size=512m,mode=1777 \
    --tmpfs /run:rw,size=64m,mode=755 \
    --tmpfs /home/pi:rw,size=128m \
    --pids-limit=512 --memory=4g --memory-swap=4g --cpus=2 \
    --network naiw-task-net --restart=no -t \
    -v "$TMP/naiw-data/tasks/smoke-test/work:/work" \
    -v "$TMP/naiw-data/tasks/smoke-test/io:/io" \
    -v "$TMP/naiw-data/pi-packages:/pi-packages:ro" \
    "$image" >/dev/null

wait_for_container_ready "$pass2_container" '[ -f /io/.naiw/events.jsonl ]' 30 \
    || fail "pass2 container not ready (pre-pip)"

docker exec -u pi "$pass2_container" sh -c 'pip install --user --quiet pyyaml && python -c "import yaml"' \
    || fail "pass2 pip install --user pyyaml failed even WITH /home/pi tmpfs — HARD-03 broken"
step_ok "HARD-03" "pass2 pip succeeded with /home/pi tmpfs; yaml importable"
echo "${_lib_log_prefix} [result] standard hardened run-flags MUST include --tmpfs /home/pi:rw,size=128m: $pip_result_note"
docker rm -f "$pass2_container" >/dev/null

# ─── Step 14 (early): HARD-09 threat baseline via alpine sidecar ───────
# Run BEFORE the main hardened container starts — this probe tests Docker
# bind-mount source-path resolution semantics, not the hardened-image runtime.
step_check "HARD-09" "Step 14: threat baseline — source-symlink bind-mount via alpine sidecar"
set +e
hard09_out="$(docker run --rm -v "$TMP/evil-link:/evil:ro" alpine cat /evil/passwd 2>&1)"
set -e
hard09_lines="$(wc -l <<< "$hard09_out")"
if grep -qE '^[a-z][a-z0-9_-]*:x:1000:' <<< "$hard09_out" || [[ "$hard09_lines" -ge 25 ]]; then
    step_ok "HARD-09" "threat baseline ok: raw docker bind-mount allows source-symlink escape (controller-level defense lives in the next phase) [PARTIAL]"
else
    step_fail "HARD-09" "source-symlink bind-mount did NOT expose host /etc/passwd — output lines=$hard09_lines (env may not support threat model)"
    fail "HARD-09 threat baseline failed (see above)"
fi

# ─── Main hardened container start ─────────────────────────────────────
step_check "" "Starting main hardened container"
docker run -d --init --name "$container" \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,size=512m,mode=1777 \
    --tmpfs /run:rw,size=64m,mode=755 \
    --tmpfs /home/pi:rw,size=128m \
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
    -v "$TMP/naiw-data/pi-packages:/pi-packages:ro" \
    -v "$TMP/naiw-data/secrets/test_token:/run/secrets/test_token:ro" \
    "$image" >/dev/null

wait_for_container_ready "$container" '[ -f /io/.naiw/events.jsonl ] && [ -f /io/terminal.log ]' 30 \
    || fail "main container not ready"
log_path="$TMP/naiw-data/tasks/smoke-test/io/terminal.log"
events_path="$TMP/naiw-data/tasks/smoke-test/io/.naiw/events.jsonl"
step_ok "" "Main hardened container ready"

# ─── Step 03: HARD-01 CapDrop == ["ALL"] ───────────────────────────────
step_check "HARD-01" "Step 03: CapDrop == [ALL]"
expect_inspect_eq "$container" "HARD-01" ".HostConfig.CapDrop" '["ALL"]' \
    || fail "HARD-01 CapDrop mismatch"

# ─── Step 04: HARD-02 SecurityOpt contains no-new-privileges ───────────
step_check "HARD-02" "Step 04: SecurityOpt contains no-new-privileges"
sec_opt="$(inspect_json "$container" ".HostConfig.SecurityOpt")"
if [[ "$sec_opt" == *"no-new-privileges"* ]]; then
    step_ok "HARD-02" "SecurityOpt=$sec_opt"
else
    step_fail "HARD-02" "SecurityOpt=$sec_opt does not contain no-new-privileges"
    fail "HARD-02 SecurityOpt missing"
fi

# ─── Step 05: HARD-03 ReadonlyRootfs == true ───────────────────────────
step_check "HARD-03" "Step 05: ReadonlyRootfs == true"
expect_inspect_eq "$container" "HARD-03" ".HostConfig.ReadonlyRootfs" 'true' \
    || fail "HARD-03 ReadonlyRootfs mismatch"

# ─── Step 06: HARD-03 /tmp writable ────────────────────────────────────
step_check "HARD-03" "Step 06: /tmp writable (tmpfs probe)"
docker exec "$container" sh -c 'touch /tmp/probe && rm /tmp/probe' \
    || fail "HARD-03 /tmp not writable"
step_ok "HARD-03" "/tmp writable"

# ─── Step 07: HARD-03 /run writable ────────────────────────────────────
step_check "HARD-03" "Step 07: /run writable (tmpfs probe)"
docker exec "$container" sh -c 'touch /run/probe && rm /run/probe' \
    || fail "HARD-03 /run not writable"
step_ok "HARD-03" "/run writable"

# Step 08 already ran above (pip two-pass).

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

# Step 14 already ran above (HARD-09).

# ─── Step 15: HARD-10 RestartPolicy == no ──────────────────────────────
step_check "HARD-10" "Step 15: RestartPolicy.Name == no"
expect_inspect_eq "$container" "HARD-10" ".HostConfig.RestartPolicy.Name" '"no"' \
    || fail "HARD-10 RestartPolicy mismatch"

# ─── Step 16: PID-1 wrapper ────────────────────────────────────────────
step_check "PID-1" "Step 16: /proc/1/comm in {tmux, tini, docker-init}"
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

# ─── Step 17: secret content + Config.Env scrub + redaction filter ─────
step_check "secret" "Step 17: /run/secrets/test_token content + Config.Env scrub + redaction"

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
sleep 1

if ! grep -q '\[REDACTED\]' "$log_path"; then
    step_fail "secret" "[REDACTED] marker missing from terminal.log after token-echo"
    fail "redaction filter not active"
fi
if grep -q "$fake_token" "$log_path"; then
    step_fail "secret" "raw token leaked into terminal.log"
    fail "redaction filter did not redact raw token"
fi
step_ok "secret" "content match + env scrub + terminal.log redaction confirmed"

# ─── Step 18: stop+start terminal.log survival + POST_RESTART_MARKER ───
step_check "HARD-restart" "Step 18: terminal.log survives stop+start; POST_RESTART_MARKER appears"
size_before="$(stat -c %s "$log_path")"
docker stop --time 10 "$container" >/dev/null
docker start "$container" >/dev/null
wait_for_container_ready "$container" '[ -f /io/terminal.log ]' 15 \
    || fail "terminal.log missing after restart"
wait_for_tmux_session "$container" main 5 \
    || fail "tmux session 'main' did not come back online within 5s"
docker exec "$container" tmux send-keys -t main 'echo POST_RESTART_MARKER' Enter
sleep 1
size_after="$(stat -c %s "$log_path")"
if (( size_after < size_before )); then
    step_fail "HARD-restart" "terminal.log shrunk: before=$size_before after=$size_after"
    fail "terminal.log shrunk across restart"
fi
if ! grep -q POST_RESTART_MARKER "$log_path"; then
    step_fail "HARD-restart" "POST_RESTART_MARKER missing from terminal.log after restart"
    fail "post-restart marker missing"
fi
step_ok "HARD-restart" "terminal.log appended through stop+start (before=$size_before after=$size_after)"

# ─── Step 19: signal cycle — naiw-signal done → events.jsonl ───────────
step_check "SIG-cycle" "Step 19: naiw-signal done appends valid event to events.jsonl"
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

# ─── Steps 20-39: PROXY-05 20 denied verb probes ───────────────────────
step_check "PROXY-05" "Steps 20-39: 20 denied verb probes (each must return 403)"
probe_proxy_endpoint POST /exec/fakeid/start         403 "EXEC"          || fail "PROXY-05 EXEC"
probe_proxy_endpoint GET  /images/json               403 "IMAGES"        || fail "PROXY-05 IMAGES"
probe_proxy_endpoint GET  /volumes                   403 "VOLUMES"       || fail "PROXY-05 VOLUMES"
probe_proxy_endpoint GET  /networks                  403 "NETWORKS"      || fail "PROXY-05 NETWORKS"
probe_proxy_endpoint POST /build                     403 "BUILD"         || fail "PROXY-05 BUILD"
probe_proxy_endpoint GET  /info                      403 "INFO"          || fail "PROXY-05 INFO"
probe_proxy_endpoint POST /auth                      403 "AUTH"          || fail "PROXY-05 AUTH"
probe_proxy_endpoint GET  /secrets                   403 "SECRETS"       || fail "PROXY-05 SECRETS"
probe_proxy_endpoint GET  /services                  403 "SERVICES"      || fail "PROXY-05 SERVICES"
probe_proxy_endpoint POST /session                   403 "SESSION"       || fail "PROXY-05 SESSION"
probe_proxy_endpoint GET  /swarm                     403 "SWARM"         || fail "PROXY-05 SWARM"
probe_proxy_endpoint GET  /system/df                 403 "SYSTEM"        || fail "PROXY-05 SYSTEM"
probe_proxy_endpoint GET  /tasks                     403 "TASKS"         || fail "PROXY-05 TASKS"
probe_proxy_endpoint GET  /plugins                   403 "PLUGINS"       || fail "PROXY-05 PLUGINS"
probe_proxy_endpoint GET  /nodes                     403 "NODES"         || fail "PROXY-05 NODES"
probe_proxy_endpoint GET  /configs                   403 "CONFIGS"       || fail "PROXY-05 CONFIGS"
probe_proxy_endpoint GET  /distribution/alpine/json  403 "DISTRIBUTION"  || fail "PROXY-05 DISTRIBUTION"
probe_proxy_endpoint GET  '/events?since=0&until=0'  403 "EVENTS"        || fail "PROXY-05 EVENTS"
probe_proxy_endpoint GET  /_ping                     403 "PING"          || fail "PROXY-05 PING"
probe_proxy_endpoint GET  /version                   403 "VERSION"       || fail "PROXY-05 VERSION"
step_ok "PROXY-05" "all 20 denied verb probes returned 403"

# ─── Step 40: PROXY-05 allowed GET /containers/json -> 200 ─────────────
step_check "PROXY-05" "Step 40: allowed GET /containers/json -> 200"
probe_proxy_endpoint GET /containers/json 200 "CONTAINERS-json" \
    || fail "PROXY-05 allowed GET /containers/json"

# ─── Step 41: PROXY-05 allowed POST /containers/<id>/start -> 204|304 ──
step_check "PROXY-05" "Step 41: allowed POST /containers/<target>/start -> 204 or 304 (throwaway sidecar)"
target_id="$(docker create --name "$probe_target" --network naiw-internal alpine sleep 1)"
probe_proxy_endpoint POST "/containers/${target_id}/start" 204 "POST start" \
    || probe_proxy_endpoint POST "/containers/${target_id}/start" 304 "POST start (already running)" \
    || fail "PROXY-05 POST start probe failed (expected 204 or 304)"
docker rm -f "$probe_target" >/dev/null 2>&1 || true

# ─── Step 42: cgroup peak evidence (WARN-only) ─────────────────────────
step_check "cgroup-peak" "Step 42: log cgroup pids.peak + memory.peak (WARN-only)"
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

# ─── Step 43: drift gate (checklist ↔ script ID set must align) ────────
step_check "" "Step 43: drift gate (HARDENED-CHECKLIST.md vs run-hardened-smoke.sh ID alignment)"
checklist_ids="$(grep -oE 'HARD-[0-9]+|PROXY-[0-9]+' tests/smoke/HARDENED-CHECKLIST.md | sort -u)"
script_ids="$(grep -oE 'HARD-[0-9]+|PROXY-[0-9]+' tests/smoke/run-hardened-smoke.sh | sort -u)"
set +e
drift="$(diff <(echo "$checklist_ids") <(echo "$script_ids"))"
set -e
if [[ -n "$drift" ]]; then
    echo "${_lib_log_prefix} FAIL: drift between HARDENED-CHECKLIST.md and run-hardened-smoke.sh:" >&2
    echo "$drift" >&2
    fail "drift gate"
fi
step_ok "" "Step 43: drift gate ok — IDs aligned"

# Final PASS line is printed by the cleanup trap's success branch on exit 0.
