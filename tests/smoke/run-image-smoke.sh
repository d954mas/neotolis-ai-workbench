#!/usr/bin/env bash
# tests/smoke/run-image-smoke.sh — Phase 1 image smoke harness (D-33).
# Covers: IMG-01, IMG-02, IMG-03, IMG-04, IMG-05, IMG-06, IMG-07, IMG-08, IMG-09, IMG-10, SIG-01, SIG-03, SIG-04, GIT-02, GIT-03.
# Phase 2.5 runs the full hardened lifecycle (cap-drop, read-only, etc.); this only validates the static image artifacts.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

NAIW_VERSION="${NAIW_VERSION:-0.1.0}"
image="naiw-task-image:${NAIW_VERSION}"
container="naiw-task-smoke-$$"
fail_container="naiw-task-smoke-fail-$$"
tmpdir=""
fail_tmpdir=""

fail() {
    echo "[smoke] FAIL: $*" >&2
    docker logs "$container" 2>&1 | sed 's/^/[container] /' >&2 || true
    exit 1
}

cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rm -f "$fail_container" >/dev/null 2>&1 || true
    [[ -n "$tmpdir" ]] && rm -rf "$tmpdir" 2>/dev/null || true
    [[ -n "$fail_tmpdir" ]] && rm -rf "$fail_tmpdir" 2>/dev/null || true
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
    echo "[smoke] SKIP: docker not on PATH"
    exit 0
fi

# Step 1: build (or reuse existing tag).
if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[smoke] building $image"
    bash scripts/build-image.sh
fi

# Step 2: docker inspect probes (no run needed).
cmd="$(docker inspect --format='{{json .Config.Cmd}}' "$image")"
[[ "$cmd" == '["tmux","new-session","-A","-s","main"]' ]] || fail "IMG-04: CMD is $cmd"

labels="$(docker inspect --format='{{json .Config.Labels}}' "$image")"
for label in 'naiw.managed":"1"' 'naiw.role":"task-image"' 'naiw.version' 'naiw.git-sha' 'naiw.pi-version' 'naiw.signal-schema":"1"' 'org.opencontainers.image.source'; do
    [[ "$labels" == *"$label"* ]] || fail "IMG-10: missing label fragment '$label' in $labels"
done

# Step 3: --init compatibility (IMG-03). PID 1 should be tini.
pid1="$(docker run --init --rm "$image" sh -c 'ps -p 1 -o comm=' 2>/dev/null || true)"
[[ "$pid1" == "tini" ]] || fail "IMG-03: PID 1 with --init is '$pid1', expected 'tini'"

# ──────────────────────────────────────────────────────────────────
# Step 4 (NEW): IMG-05 fail-fast probe.
# Run the image with a deliberately-broken /pi-packages mount and assert:
#   (a) docker run exits non-zero (entrypoint fail-fast per D-11)
#   (b) docker logs contain `naiw: package install failed:`
# We do this BEFORE the happy-path run so a regression here trips early.
# ──────────────────────────────────────────────────────────────────
echo "[smoke] IMG-05 fail-fast probe (broken /pi-packages mount)"
fail_tmpdir="$(mktemp -d)"
mkdir -p "$fail_tmpdir/work" "$fail_tmpdir/io" "$fail_tmpdir/pi-packages/broken-pkg"
# Broken package: setup.py that always exits 1. Pi's package installer (npm/pip-based,
# depending on the resolved Pi tool) will see a non-zero install and propagate.
# We use BOTH a setup.py and a pyproject.toml-with-bad-syntax to maximize the chance
# that whatever installer Pi invokes will fail. The entrypoint's `pi install <dir> ||
# { echo 'naiw: package install failed: <dir>' >&2; exit 1; }` catches this regardless.
cat > "$fail_tmpdir/pi-packages/broken-pkg/setup.py" <<'PYEOF'
import sys
sys.exit(1)
PYEOF
cat > "$fail_tmpdir/pi-packages/broken-pkg/pyproject.toml" <<'TOMLEOF'
[project
name = broken
TOMLEOF

set +e
docker run --name "$fail_container" \
    -v "$fail_tmpdir/work:/work" \
    -v "$fail_tmpdir/io:/io" \
    -v "$fail_tmpdir/pi-packages:/pi-packages:ro" \
    "$image" >/dev/null 2>&1
fail_rc=$?
set -e

if [[ "$fail_rc" -eq 0 ]]; then
    docker logs "$fail_container" 2>&1 | sed 's/^/[fail-container] /' >&2 || true
    echo "[smoke] FAIL: IMG-05: docker run with broken /pi-packages exited 0; expected non-zero (fail-fast)" >&2
    exit 1
fi

if ! docker logs "$fail_container" 2>&1 | grep -q 'naiw: package install failed:'; then
    docker logs "$fail_container" 2>&1 | sed 's/^/[fail-container] /' >&2 || true
    echo "[smoke] FAIL: IMG-05: docker logs missing 'naiw: package install failed:' message" >&2
    exit 1
fi
docker rm -f "$fail_container" >/dev/null 2>&1 || true
rm -rf "$fail_tmpdir" || true
fail_tmpdir=""
echo "[smoke] IMG-05 fail-fast probe ok (rc=$fail_rc, log contains 'package install failed:')"

# Step 5: prepare host mocks for happy-path /work /io /pi-packages.
tmpdir="$(mktemp -d)"
mkdir -p "$tmpdir/work" "$tmpdir/io" "$tmpdir/pi-packages"

# Step 6: launch the happy-path container detached (no broken /pi-packages).
docker run -d --name "$container" \
    -v "$tmpdir/work:/work" \
    -v "$tmpdir/io:/io" \
    -v "$tmpdir/pi-packages:/pi-packages:ro" \
    "$image" >/dev/null

# Wait for entrypoint to finish (tmux session exists, terminal.log appears).
for i in $(seq 1 30); do
    if docker exec "$container" sh -c '[ -f /io/.naiw/events.jsonl ] && [ -f /io/terminal.log ]' 2>/dev/null; then
        break
    fi
    sleep 0.5
done
docker exec "$container" sh -c '[ -f /io/.naiw/events.jsonl ]' || fail "D-07: /io/.naiw/events.jsonl missing"

# Step 7: tool presence (IMG-02).
for tool in pi tmux git gh node npm python3 ffmpeg rg; do
    docker exec -u pi "$container" sh -c "command -v $tool >/dev/null" \
        || fail "IMG-02: $tool not on PATH"
done

# Step 8: SIG-04 — no docker CLI in image.
if docker exec -u pi "$container" sh -c "command -v docker" 2>/dev/null; then
    fail "SIG-04: docker CLI is on PATH inside image (must NOT be)"
fi

# Step 9: IMG-09 / GIT-02 — pi user runs the workload; ~/.gitconfig is empty.
user="$(docker exec -u pi "$container" id -un | tr -d '\r\n')"
[[ "$user" == "pi" ]] || fail "IMG-09: user is '$user', expected 'pi'"
size="$(docker exec -u pi "$container" sh -c 'wc -c < /home/pi/.gitconfig' | tr -d '\r\n ')"
[[ "$size" == "0" ]] || fail "GIT-02: /home/pi/.gitconfig size=$size, expected 0"

# Step 10: IMG-08 — UTF-8 locale; non-ASCII echo survives pipe-pane.
docker exec "$container" tmux send-keys -t main 'printf "%s\n" "héllo-utf8"' Enter
sleep 1
docker exec "$container" sh -c 'grep -q "héllo-utf8" /io/terminal.log' \
    || fail "IMG-08: UTF-8 round-trip failed (héllo-utf8 not in terminal.log)"

# Step 11: IMG-06 — pipe-pane redaction filter active.
fake_token="ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 36 a's = matches ghp_[A-Za-z0-9]{30,}
docker exec "$container" tmux send-keys -t main "printf '%s\n' '$fake_token'" Enter
sleep 1
docker exec "$container" sh -c 'grep -q "\[REDACTED\]" /io/terminal.log' \
    || fail "IMG-06: [REDACTED] not in /io/terminal.log after synthetic ghp_ token"
if docker exec "$container" sh -c "grep -q '$fake_token' /io/terminal.log"; then
    fail "IMG-06: raw token '$fake_token' leaked into /io/terminal.log (redaction filter not active)"
fi

# Step 12: SIG-01 + SIG-03 — naiw-signal works as pi and writes a valid event.
docker exec -u pi "$container" naiw-signal --help >/dev/null \
    || fail "SIG-01: naiw-signal --help failed"
docker exec -u pi "$container" naiw-signal done --summary "smoke ok" \
    || fail "SIG-02: naiw-signal done failed"
line_count="$(docker exec "$container" sh -c 'wc -l < /io/.naiw/events.jsonl' | tr -d '\r\n ')"
[[ "$line_count" -ge 1 ]] || fail "SIG-03: events.jsonl has $line_count lines, expected >= 1"
docker exec "$container" sh -c 'tail -1 /io/.naiw/events.jsonl' \
    | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); assert d["kind"]=="done", d; assert d["schema_version"]==1, d; assert d["payload"]=={"summary":"smoke ok"}, d' \
    || fail "SIG-03: last events.jsonl line malformed"

# Step 13: GIT-03 — credential helper reads /run/secrets/github_token. We mount a fake token, then
# invoke `git credential fill` and assert username + password come through.
fake_token="ghp_TESTTOKEN1234567890123456789012345"
fake_secret_dir="$(mktemp -d)"
printf '%s' "$fake_token" > "$fake_secret_dir/github_token"
chmod 0600 "$fake_secret_dir/github_token"
docker rm -f "$container" >/dev/null
docker run -d --name "$container" \
    -v "$tmpdir/work:/work" \
    -v "$tmpdir/io:/io" \
    -v "$tmpdir/pi-packages:/pi-packages:ro" \
    -v "$fake_secret_dir/github_token:/run/secrets/github_token:ro" \
    "$image" >/dev/null
sleep 2
cred_out="$(docker exec -u pi "$container" sh -c 'printf "protocol=https\nhost=github.com\n\n" | git credential fill 2>/dev/null')"
[[ "$cred_out" == *"username=x-access-token"* ]] || fail "GIT-03: credential output missing 'username=x-access-token': $cred_out"
[[ "$cred_out" == *"password=$fake_token"* ]] || fail "GIT-03: credential output missing fake token password: $cred_out"
rm -rf "$fake_secret_dir"

echo "[smoke] ok"
