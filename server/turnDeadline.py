"""turnDeadline.py — the time a chat turn leaves the tool it runs
(hand-written, no database).

A tool with its own deadline (PhotoActions.PHOTO_DEADLINE_SECONDS) may
start well into a chat turn (ChatActions.TURN_DEADLINE_SECONDS), and the
turn's model must still reply after it. So ChatActions._runModel runs each
tool inside runningTool(the seconds the turn has left), and the tool takes
its times from here:

  - toolDeadline(seconds): its own deadline for its work, cut to the
    turn's deadline minus TOOL_REPLY_RESERVE_SECONDS and SAVE_MIN_SECONDS
    (the model's reply, and before it the tool's saves);
  - saveDeadline(seconds): the time its saves get once its work is done,
    cut to the turn's deadline minus TOOL_REPLY_RESERVE_SECONDS. Work done
    by its deadline leaves the saves at least SAVE_MIN_SECONDS.

A tool out of time raises its usual 503, which the turn's model reads as a
tool error (mcpRuntime.callToolFull) and answers. Outside a chat turn (the
MCP servers, a direct call) nothing is set and a tool keeps its own
times. A contextvar, so each request thread sees only its own turn's: call
toolDeadline and saveDeadline on the turn's own thread (a pool thread a
tool starts sees none, so take the deadline before, and pass it in).
"""

import contextlib
import contextvars
import time
from typing import Iterator

# What a tool's saves leave the turn for the model's reply after it: one
# model attempt, which took about 13 s with a photo in the history.
TOOL_REPLY_RESERVE_SECONDS = 15
# The least time a tool's work leaves its saves (uploads and records of
# what it made), before the reply's reserve.
SAVE_MIN_SECONDS = 10

_turnDeadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    'turnDeadline', default=None)


@contextlib.contextmanager
def runningTool(secondsLeft: float) -> Iterator[None]:
    """Run the block (one tool call) within the chat turn that has
    `secondsLeft` (seconds, not a deadline: the caller's clock is its own,
    a test's fake one)."""
    token = _turnDeadline.set(time.monotonic() + secondsLeft)
    try:
        yield
    finally:
        _turnDeadline.reset(token)


def _cut(seconds: float, reserve: float) -> float:
    own = time.monotonic() + seconds
    turn = _turnDeadline.get()
    return own if turn is None else min(own, turn - reserve)


def toolDeadline(seconds: float) -> float:
    """A tool's deadline for its work (time.monotonic()): `seconds` from
    now, within a chat turn no later than its deadline minus
    TOOL_REPLY_RESERVE_SECONDS and SAVE_MIN_SECONDS (which may already be
    past: the tool's next check is then its 503). Call it on the turn's own
    thread."""
    return _cut(seconds, TOOL_REPLY_RESERVE_SECONDS + SAVE_MIN_SECONDS)


def saveDeadline(seconds: float) -> float:
    """The deadline of a tool's saves once its work is done: `seconds`
    from now, within a chat turn no later than its deadline minus
    TOOL_REPLY_RESERVE_SECONDS. Call it on the turn's own thread."""
    return _cut(seconds, TOOL_REPLY_RESERVE_SECONDS)
