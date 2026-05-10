"""Well-known paths inside the task container."""

# Canonical event journal. Tests redirect by patching the binding in the
# importing module (e.g., writer_mod.EVENTS_PATH), not by env var.
EVENTS_PATH: str = "/io/.naiw/events.jsonl"

# Terminal log — written by tmux pipe-pane through the redaction filter.
TERMINAL_LOG: str = "/io/terminal.log"

# Secrets directory — read-only file mounts.
SECRETS_DIR: str = "/run/secrets"
