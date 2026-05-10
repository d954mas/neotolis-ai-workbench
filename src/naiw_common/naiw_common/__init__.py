"""naiw_common — shared schema and path constants for NAIW."""

from .events import Event, Kind, SCHEMA_VERSION
from .paths import EVENTS_PATH, TERMINAL_LOG, SECRETS_DIR
from .version import __version__

__all__ = [
    "Event",
    "Kind",
    "SCHEMA_VERSION",
    "EVENTS_PATH",
    "TERMINAL_LOG",
    "SECRETS_DIR",
    "__version__",
]
