"""callSlots: a cap on the slow calls one process runs at once (chat turns,
photo calls), so the rest of gunicorn's threads stay free for the app."""

import contextlib
import threading
from typing import Callable, Iterator


@contextlib.contextmanager
def slot(slots: threading.BoundedSemaphore,
         failure: Callable[[], Exception]) -> Iterator[None]:
    """Hold one of `slots` for the block, never waiting for one: with none
    free, raise failure() (a 503) before the block runs."""
    if not slots.acquire(blocking=False):
        raise failure()
    try:
        yield
    finally:
        slots.release()
