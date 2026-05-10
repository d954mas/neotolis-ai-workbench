"""Well-known paths inside the task container."""

import os

# Inside naiw-task-image, /io/.naiw/events.jsonl is the canonical event journal (D-04, DATA-06).
# NAIW_EVENTS_PATH override exists ONLY for unit tests; production code never sets it.
EVENTS_PATH: str = os.environ.get("NAIW_EVENTS_PATH", "/io/.naiw/events.jsonl")

# Terminal log — written by tmux pipe-pane via the redaction filter (IMG-06, DATA-06).
TERMINAL_LOG: str = "/io/terminal.log"

# Secrets directory — read-only file mounts (D1, DATA-02).
SECRETS_DIR: str = "/run/secrets"
