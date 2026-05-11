#!/usr/bin/env bash
# Image smoke harness. Validates the static image artifacts:
# CMD/labels/init compatibility, hot-package install fail-fast, tool presence,
# pi-user identity, UTF-8 round-trip, redaction filter, naiw-signal end-to-end,
# git credential helper resolution. Hardened-lifecycle flags
# (cap-drop, read-only, etc.) belong in a separate suite.
#
# Mount strategy: we use Docker NAMED VOLUMES (not host bind-mounts). Two reasons:
#   1. Bind-mounts of host tmpdirs do NOT register as proper kernel mountpoints on
#      Docker Desktop (Windows/macOS, virtiofs/9P-backed). The image entrypoint
#      checks `mountpoint -q /work` etc. and refuses to start otherwise.
#   2. Named volumes register correctly everywhere (Linux, Docker Desktop), so the
#      same harness works on a developer laptop and on a Linux VPS.
# We pre-populate volumes via tiny helper containers (alpine isn't required —
# we use the image-under-test for ergonomics; nothing in /pi-packages by default).
set -euo pipefail

# Disable MSYS/Git-Bash path mangling — on Windows, MSYS rewrites POSIX absolute
# paths in command args (e.g., /work -> C:/Program Files/Git/work). We pass
# in-container paths to docker run -v / -e PATH=...; those must NOT be mangled.
# On Linux this env var is a harmless no-op.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$here/../.." && pwd)"
cd "$repo_root"

# shellcheck source=_lib.sh
source "$here/_lib.sh"

NAIW_VERSION="${NAIW_VERSION:-0.1.0}"
image="naiw-task-image:${NAIW_VERSION}"
container="naiw-task-smoke-$$"
fail_container="naiw-task-smoke-fail-$$"
populate_container="naiw-task-smoke-pop-$$"

# Named volumes (PID-suffixed for isolation across parallel smoke runs).
vol_work="naiw-smoke-work-$$"
vol_io="naiw-smoke-io-$$"
vol_pkg="naiw-smoke-pkg-$$"
vol_pkg_bad="naiw-smoke-pkgbad-$$"
vol_secret="naiw-smoke-secret-$$"
vol_stub_bin="naiw-smoke-stubbin-$$"

fail() {
    echo "[smoke] FAIL: $*" >&2
    docker logs "$container" 2>&1 | sed 's/^/[container] /' >&2 || true
    exit 1
}

cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rm -f "$fail_container" >/dev/null 2>&1 || true
    docker rm -f "$populate_container" >/dev/null 2>&1 || true
    docker volume rm "$vol_work" "$vol_io" "$vol_pkg" "$vol_pkg_bad" "$vol_secret" "$vol_stub_bin" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
    echo "[smoke] SKIP: docker not on PATH"
    exit 0
fi

# Step 1: build (or reuse existing tag).
build_image_if_missing "$image"

# Step 2: docker inspect probes (no run needed).
cmd="$(docker inspect --format='{{json .Config.Cmd}}' "$image")"
[[ "$cmd" == '["tmux","new-session","-A","-s","main"]' ]] || fail "CMD: expected tmux new-session, got $cmd"

labels="$(docker inspect --format='{{json .Config.Labels}}' "$image")"
for label in 'naiw.managed":"1"' 'naiw.role":"task-image"' 'naiw.version' 'naiw.git-sha' 'naiw.pi-version' 'naiw.signal-schema":"1"' 'org.opencontainers.image.source'; do
    [[ "$labels" == *"$label"* ]] || fail "labels: missing fragment '$label' in $labels"
done

# Step 3: --init compatibility. PID 1 should be the init wrapper (tini or
# Docker's `docker-init` equivalent on Docker Desktop). The image deliberately
# does NOT bake procps (ps), so we read /proc/1/comm directly. We use
# --entrypoint sh to bypass naiw-entrypoint (which requires /work /io
# /pi-packages mounts); we only need to verify --init injects an init wrapper.
pid1="$(docker run --init --rm --entrypoint sh "$image" -c 'cat /proc/1/comm' 2>/dev/null | tr -d '\r\n')"
case "$pid1" in
    tini|docker-init)
        ;;
    *)
        fail "PID 1 under --init is '$pid1', expected 'tini' or 'docker-init'"
        ;;
esac

# Create empty named volumes for /work, /io, /pi-packages, and the broken /pi-packages.
# Volumes default to root-owned mode 0755; the image runs as USER pi (uid 1000), so
# the entrypoint cannot mkdir under /io. On a real Linux VPS, the controller creates
# host directories with `chown 1000:1000` — we replicate that here by chowning the
# volume mountpoints to uid 1000 via a --user 0 helper container.
docker volume create "$vol_work" >/dev/null
docker volume create "$vol_io" >/dev/null
docker volume create "$vol_pkg" >/dev/null
docker volume create "$vol_pkg_bad" >/dev/null

docker run --rm --user 0 \
    -v "$vol_work:/work" \
    -v "$vol_io:/io" \
    --entrypoint sh "$image" -c 'chown 1000:1000 /work /io' >/dev/null

# ──────────────────────────────────────────────────────────────────
# Step 4: hot-package install fail-fast probe.
# Run the image with (a) a populated /pi-packages mount AND (b) a forced-failing
# `pi` binary on PATH; assert:
#   (a) docker run exits non-zero (entrypoint fails fast)
#   (b) docker logs contain `naiw: package install failed:`
# We do this BEFORE the happy-path run so a regression here trips early.
#
# Why a stub `pi` and not a "broken package directory"?
# Empirically, Pi's `install <path>` command is lenient: it succeeds for ANY
# existing directory regardless of contents (no `setup.py`/`pyproject.toml`
# validation). We cannot trigger Pi's installer itself to fail via package
# contents. What this probe is contractually testing is the entrypoint's
# `|| { echo 'naiw: package install failed: $d' >&2; exit 1; }` wrapper —
# i.e., does the entrypoint correctly fail fast when `pi install` returns
# non-zero, for ANY reason. We assert that contract by overriding `pi` on
# PATH with a stub that exits 1.
# ──────────────────────────────────────────────────────────────────
echo "[smoke] hot-package fail-fast probe (stub pi forces install failure)"
# Pre-populate $vol_pkg_bad with one (any) broken-pkg directory so the entrypoint's
# `for d in /pi-packages/*/` loop has a directory to iterate.
# Pre-populate a stub-bin volume with a `pi` that exits 1 on `install`.
# (vol_pkg_bad was already created above with the rest of the volumes.)
docker volume create "$vol_stub_bin" >/dev/null
docker run --rm --name "$populate_container" \
    --user 0 \
    -v "$vol_pkg_bad:/pkg" \
    -v "$vol_stub_bin:/stubbin" \
    --entrypoint sh "$image" -c '
        mkdir -p /pkg/broken-pkg
        echo "broken" > /pkg/broken-pkg/marker
        cat > /stubbin/pi <<PYEOF
#!/bin/sh
# stub pi that fails on install but succeeds on anything else.
case "${1:-}" in
    install) echo "stub-pi: refusing to install $2" >&2; exit 1 ;;
    *) exec /usr/local/bin/pi "$@" ;;
esac
PYEOF
        chmod 0755 /stubbin/pi
    ' >/dev/null

set +e
docker run --name "$fail_container" \
    -e "PATH=/stubbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    -v "$vol_work:/work" \
    -v "$vol_io:/io" \
    -v "$vol_pkg_bad:/pi-packages:ro" \
    -v "$vol_stub_bin:/stubbin:ro" \
    "$image" >/dev/null 2>&1
fail_rc=$?
set -e

if [[ "$fail_rc" -eq 0 ]]; then
    docker logs "$fail_container" 2>&1 | sed 's/^/[fail-container] /' >&2 || true
    echo "[smoke] FAIL: docker run with broken /pi-packages exited 0; expected non-zero (fail-fast)" >&2
    exit 1
fi

if ! docker logs "$fail_container" 2>&1 | grep -q 'naiw: package install failed:'; then
    docker logs "$fail_container" 2>&1 | sed 's/^/[fail-container] /' >&2 || true
    echo "[smoke] FAIL: docker logs missing 'naiw: package install failed:' message" >&2
    exit 1
fi
docker rm -f "$fail_container" >/dev/null 2>&1 || true
echo "[smoke] fail-fast probe ok (rc=$fail_rc, log contains 'package install failed:')"

# Reset /work and /io for the happy-path run by removing and re-creating the volumes.
# (The fail-fast container left /io/.naiw/events.jsonl from entrypoint Step 2; clean state for happy path.)
docker volume rm "$vol_work" "$vol_io" >/dev/null 2>&1 || true
docker volume create "$vol_work" >/dev/null
docker volume create "$vol_io" >/dev/null
docker run --rm --user 0 \
    -v "$vol_work:/work" \
    -v "$vol_io:/io" \
    --entrypoint sh "$image" -c 'chown 1000:1000 /work /io' >/dev/null

# Step 6: launch the happy-path container detached (empty /pi-packages volume).
# We use `-t` to allocate a TTY because the entrypoint ends with `exec tmux attach
# -t main` which fails with "open terminal failed: not a terminal" otherwise. In
# production the controller runs `docker exec -it ... tmux attach`; here we let
# the container hold a TTY so the embedded tmux session stays attached.
docker run -d -t --name "$container" \
    -v "$vol_work:/work" \
    -v "$vol_io:/io" \
    -v "$vol_pkg:/pi-packages:ro" \
    "$image" >/dev/null

# Wait for entrypoint to finish (tmux session exists, terminal.log appears).
wait_for_container_ready "$container" '[ -f /io/.naiw/events.jsonl ] && [ -f /io/terminal.log ]' 15 \
    || fail "/io/.naiw/events.jsonl missing"

# Step 7: tool presence.
for tool in pi tmux git gh node npm python3 ffmpeg rg; do
    docker exec -u pi "$container" sh -c "command -v $tool >/dev/null" \
        || fail "tool not on PATH: $tool"
done

# Step 8: docker CLI MUST NOT be inside the image. Pi never gets Docker access.
if docker exec -u pi "$container" sh -c "command -v docker" 2>/dev/null; then
    fail "docker CLI is on PATH inside image (must NOT be)"
fi

# Step 9: pi user runs the workload; ~/.gitconfig is empty
# (system gitconfig is the only credential source).
user="$(docker exec -u pi "$container" id -un | tr -d '\r\n')"
[[ "$user" == "pi" ]] || fail "user is '$user', expected 'pi'"
size="$(docker exec -u pi "$container" sh -c 'wc -c < /home/pi/.gitconfig' | tr -d '\r\n ')"
[[ "$size" == "0" ]] || fail "/home/pi/.gitconfig size=$size, expected 0"

# Step 10: UTF-8 locale; non-ASCII echo survives pipe-pane.
docker exec "$container" tmux send-keys -t main 'printf "%s\n" "héllo-utf8"' Enter
sleep 1
docker exec "$container" sh -c 'grep -q "héllo-utf8" /io/terminal.log' \
    || fail "UTF-8 round-trip failed (héllo-utf8 not in terminal.log)"

# Step 11: pipe-pane redaction filter is active.
fake_token="ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 36 a's matches ghp_[A-Za-z0-9]{30,}
docker exec "$container" tmux send-keys -t main "printf '%s\n' '$fake_token'" Enter
sleep 1
docker exec "$container" sh -c 'grep -q "\[REDACTED\]" /io/terminal.log' \
    || fail "[REDACTED] not in /io/terminal.log after synthetic ghp_ token"
if docker exec "$container" sh -c "grep -q '$fake_token' /io/terminal.log"; then
    fail "raw token '$fake_token' leaked into /io/terminal.log (redaction filter not active)"
fi

# Step 12: naiw-signal works as pi and writes a valid event.
docker exec -u pi "$container" naiw-signal --help >/dev/null \
    || fail "naiw-signal --help failed"
docker exec -u pi "$container" naiw-signal done --summary "smoke ok" \
    || fail "naiw-signal done failed"
line_count="$(docker exec "$container" sh -c 'wc -l < /io/.naiw/events.jsonl' | tr -d '\r\n ')"
[[ "$line_count" -ge 1 ]] || fail "events.jsonl has $line_count lines, expected >= 1"
docker exec "$container" sh -c 'tail -1 /io/.naiw/events.jsonl' \
    | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); assert d["kind"]=="done", d; assert d["schema_version"]==1, d; assert d["payload"]=={"summary":"smoke ok"}, d' \
    || fail "last events.jsonl line malformed"

# Step 13: `git credential fill` resolves /run/secrets/github_token through
# git's standard credential.helper protocol via the in-image /etc/gitconfig.
# Pre-populate a fake token in a named volume, re-launch the container with the
# volume mounted at /run/secrets, then run `git credential fill` and assert the
# resolved username + password came from the helper.
fake_token="ghp_TESTTOKEN1234567890123456789012345"
docker volume create "$vol_secret" >/dev/null
docker run --rm \
    --user 0 \
    -v "$vol_secret:/secrets" \
    --entrypoint sh "$image" -c "printf '%s' '$fake_token' > /secrets/github_token && chmod 0600 /secrets/github_token && chown 1000:1000 /secrets/github_token" >/dev/null

docker rm -f "$container" >/dev/null
docker run -d -t --name "$container" \
    -v "$vol_work:/work" \
    -v "$vol_io:/io" \
    -v "$vol_pkg:/pi-packages:ro" \
    -v "$vol_secret:/run/secrets:ro" \
    "$image" >/dev/null

# Wait for entrypoint to finish (same pattern as Step 6). `sleep 2` was flaky on
# slower hosts (Docker Desktop, busy CI) — the container needs to complete
# mountpoint checks, hot-package install, and tmux session bring-up before
# `docker exec` can reach a usable shell.
wait_for_container_ready "$container" '[ -f /io/.naiw/events.jsonl ] && [ -f /io/terminal.log ]' 15 \
    || fail "container not ready after re-launch with secret volume"

# Invoking `git credential fill` for an https://github.com URL must resolve
# through /etc/gitconfig's [credential "https://github.com"] helper entry,
# which reads /run/secrets/github_token and emits username=x-access-token +
# password=<token>. This exercises git's STANDARD credential resolution path
# end-to-end — no direct helper-script invocation, no bypass.
cred_out="$(printf 'protocol=https\nhost=github.com\n\n' \
    | docker exec -i -u pi "$container" git credential fill 2>/dev/null)"
[[ "$cred_out" == *"username=x-access-token"* ]] || fail "'git credential fill' output missing 'username=x-access-token': $cred_out"
[[ "$cred_out" == *"password=$fake_token"* ]] || fail "'git credential fill' output missing fake token password: $cred_out"

# Belt-and-braces: assert the gitconfig wiring itself is in shell-exec form.
# A regression in the gitconfig would resurface as a failure here even if
# `git credential fill` somehow short-circuited the helper.
gitconfig_helper="$(docker exec "$container" sh -c 'grep -E "^\s*helper" /etc/gitconfig' | tr -d '\r\n')"
[[ "$gitconfig_helper" == *"helper = !naiw-git-credential-helper"* ]] \
    || fail "/etc/gitconfig helper line is '$gitconfig_helper'; expected 'helper = !naiw-git-credential-helper'"

echo "[smoke] ok"
