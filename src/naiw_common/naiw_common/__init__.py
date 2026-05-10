"""naiw_common — shared schema and path constants for NAIW."""

from .events import SCHEMA_VERSION, Event, Kind
from .paths import EVENTS_PATH, SECRETS_DIR, TERMINAL_LOG
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
