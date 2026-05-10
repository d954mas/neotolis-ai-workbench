"""naiw_signal — append done/fail/wait events to /io/.naiw/events.jsonl."""

from .writer import append_event

__all__ = ["append_event"]
