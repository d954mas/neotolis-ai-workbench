#!/usr/bin/env bash
# tests/smoke/_lib.sh — shared bash helpers for the smoke harness suite.
# Sourced by run-image-smoke.sh, run-proxy-smoke.sh, run-hardened-smoke.sh.
# Callers own their own error mode (we do not enable errexit here).

# Default prefix; callers override with `_lib_log_prefix="[hardened-smoke]"` before sourcing.
: "${_lib_log_prefix:=[smoke]}"
: "${_lib_step_fail_exits:=1}"

# ─── Named-step printers ────────────────────────────────────────────
# step_check ID msg / step_ok ID msg — informational stdout.
# step_fail ID msg — stderr; exits 1 when _lib_step_fail_exits=1 (default),
# otherwise returns 1 so the caller can aggregate.
# step_warn ID msg — stderr, never exits.
# Empty ID is allowed; it just suppresses the bracket prefix so harnesses
# that don't use requirement IDs still produce clean output.

step_check() {
    local id="$1" msg="$2"
    if [[ -z "$id" ]]; then
        echo "${_lib_log_prefix} checking: $msg"
    else
        echo "${_lib_log_prefix} [$id] checking: $msg"
    fi
}

step_ok() {
    local id="$1" msg="$2"
    if [[ -z "$id" ]]; then
        echo "${_lib_log_prefix} ok: $msg"
    else
        echo "${_lib_log_prefix} [$id] ok: $msg"
    fi
}

step_fail() {
    local id="$1" msg="$2"
    if [[ -z "$id" ]]; then
        echo "${_lib_log_prefix} FAIL: $msg" >&2
    else
        echo "${_lib_log_prefix} [$id] FAIL: $msg" >&2
    fi
    if [[ "${_lib_step_fail_exits}" == "1" ]]; then
        exit 1
    fi
    return 1
}

step_warn() {
    local id="$1" msg="$2"
    if [[ -z "$id" ]]; then
        echo "${_lib_log_prefix} WARN: $msg" >&2
    else
        echo "${_lib_log_prefix} [$id] WARN: $msg" >&2
    fi
}

# ─── Image build / readiness ────────────────────────────────────────

build_image_if_missing() {
    local image_tag="$1"
    if ! docker image inspect "$image_tag" >/dev/null 2>&1; then
        echo "${_lib_log_prefix} building $image_tag"
        bash scripts/build-image.sh
    fi
}

# Poll an arbitrary in-container probe at 0.5s cadence for up to N seconds.
# Default 15s = 30 iterations × 0.5s.
wait_for_container_ready() {
    local container="$1" probe_cmd="$2" timeout_seconds="${3:-15}"
    local i max=$(( timeout_seconds * 2 ))
    for i in $(seq 1 "$max"); do
        if docker exec "$container" sh -c "$probe_cmd" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

# Faster poll (100ms cadence) for the tmux-session-after-restart race; used
# by the hardened harness after docker stop+start to confirm the tmux server
# is back online before sending input.
wait_for_tmux_session() {
    local container="$1" session="$2" timeout_seconds="${3:-5}"
    local i max=$(( timeout_seconds * 10 ))
    for i in $(seq 1 "$max"); do
        if docker exec "$container" tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -qx "$session"; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# ─── Proxy probes ───────────────────────────────────────────────────
# Generic single-shot proxy probe via a sibling curl container on naiw-internal.
# Returns 0 on match, 1 on mismatch — caller decides whether to fail-fast
# (`|| fail "..."`) or aggregate.
# The -d '{}' / Content-Type pair is needed by some POST paths and harmless on
# GETs (we never pass -G so curl just sends a body curl ignores on GET).

probe_proxy_endpoint() {
    local method="$1" path="$2" expected_code="$3" label="${4:-${method} ${path}}"
    local code
    code="$(docker run --rm --network naiw-internal curlimages/curl:latest \
        -s -o /dev/null -w '%{http_code}' -X "$method" \
        --max-time 3 \
        -H 'Content-Type: application/json' \
        -d '{}' \
        "http://naiw-docker-proxy:2375/v1.43${path}" 2>/dev/null || echo "000")"
    if [[ "$code" == "$expected_code" ]]; then
        echo "${_lib_log_prefix} [PROXY-05] ${label} ok: ${code}"
        return 0
    else
        echo "${_lib_log_prefix} [PROXY-05] ${label} FAIL: returned ${code}, expected ${expected_code}" >&2
        return 1
    fi
}

# ─── Mountinfo assertions ───────────────────────────────────────────
# awk parses /proc/self/mountinfo column 5 (mountpoint) and column 6 (flags
# like rw,relatime,...). Returns 0 when <flag> is present in the comma list.
assert_mountinfo_flag() {
    local container="$1" mp="$2" flag="$3"
    docker exec "$container" awk -v mp="$mp" '$5 == mp {print $6}' /proc/self/mountinfo \
        | grep -qE "(^|,)${flag}(,|$)"
}

# Returns 0 (success) when NO /meta entry is found in mountinfo.
assert_no_meta_mount() {
    local container="$1"
    ! docker exec "$container" awk '$5 ~ /\/meta(\/|$)/ {found=1} END {exit !found}' /proc/self/mountinfo
}

# ─── Docker inspect helpers ─────────────────────────────────────────

inspect_json() {
    local container="$1" jq_path="$2"
    docker inspect --format="{{json ${jq_path}}}" "$container"
}

expect_inspect_eq() {
    local container="$1" id="$2" jq_path="$3" expected="$4"
    local actual
    actual="$(inspect_json "$container" "$jq_path")"
    if [[ "$actual" == "$expected" ]]; then
        step_ok "$id" "${jq_path}=${actual}"
        return 0
    else
        step_fail "$id" "${jq_path}=${actual}, expected ${expected}"
        return 1
    fi
}
