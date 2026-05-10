#!/usr/bin/env bash
# Container entrypoint: prepare /io, install hot Pi packages, start tmux with
# the redaction-piped terminal log, then hand the terminal to tmux.
set -euo pipefail

# Step 1: verify required mounts exist. The image is useless without /work,
# /io, and /pi-packages — fail fast rather than silently writing to the
# container's ephemeral overlay fs.
for mnt in /work /io /pi-packages; do
    if ! mountpoint -q "$mnt"; then
        echo "naiw: required mount $mnt is missing" >&2
        exit 1
    fi
done

# Step 2: bootstrap io/.naiw and events.jsonl so naiw-signal can append immediately.
mkdir -p /io/.naiw
touch /io/.naiw/events.jsonl
chmod 0644 /io/.naiw/events.jsonl

# Step 3: hot-install Pi packages from the read-only mount, sequential and fail-fast.
shopt -s nullglob
pkg_count=0
for d in /pi-packages/*/; do
    pi install "$d" || { echo "naiw: package install failed: $d" >&2; exit 1; }
    pkg_count=$((pkg_count + 1))
done
shopt -u nullglob
if [[ "$pkg_count" -eq 0 ]]; then
    echo "naiw: no hot packages"
fi

# Step 4: cd into Pi's working dir.
cd /work

# Step 5: start tmux detached so we can attach pipe-pane before the user attaches.
tmux new-session -d -s main

# Step 6: install pipe-pane redaction chain. `stdbuf -oL` keeps sed line-buffered;
# without it terminal.log lags ~4 KB at a time and looks frozen to the operator.
tmux pipe-pane -o -t main "exec stdbuf -oL sed -E -f /etc/naiw/redact.sed >> /io/terminal.log"

# Step 7: hand the terminal to tmux. `exec` is non-negotiable — it ensures tmux
# replaces this shell as the container's PID 1 (or PID 2 under `docker run --init`).
exec tmux attach -t main
