"""spreadsheetSandbox.py — spreadsheetText in a child process
(hand-written; chatSpreadsheets calls it).

spreadsheetText is bounded in memory and time by its own checks; this is
the backstop for a hole none of them knows of: whatever happens inside
the child, the app's process neither runs out of memory nor freezes. Each
conversion is one `python -I spreadsheetWorker.py` (subprocess, never a
fork of the threaded gunicorn worker, no preexec_fn), given the file on
stdin and a minimal environment (no secret reaches it), which caps itself
on Linux (spreadsheetWorker's docstring) and is killed past the caller's
wall time. Its answer is read bounded (OUTPUT_MAX_BYTES).

Outcomes, as spreadsheetText's own:
- the text: a SheetText;
- a refusal: a Refused naming `label`, in spreadsheetText's words (the
  child returns only a reason key, never words of its own);
- a child that crashed, was killed by its memory or CPU limit, wrote too
  much or wrote anything that is not its answer: TOO_LARGE (the file is
  what made it fail), logged with the exit code only: as
  `spreadsheetWorkerExited` when it ended without an answer (a non-zero
  exit, or nothing written), else `spreadsheetWorkerFailed`. Every file
  refused alike with `spreadsheetWorkerExited` is how a broken sandbox
  shows;
- the caller's time out (the child still running): OutOfTime, the child
  killed;
- no child could be started (an OSError from Popen: memory or process
  pressure): OutOfTime, logged as `spreadsheetWorkerFailed` with the errno
  only.
"""

import json
import logging
import os
import subprocess
import sys
import threading

import spreadsheetText
from spreadsheetText import OutOfTime, Refused, SheetText

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'spreadsheetWorker.py')
# The only environment variables the child gets (Windows needs SYSTEMROOT
# to start Python at all).
_ENVIRONMENT = ('PATH', 'SYSTEMROOT', 'LANG', 'LC_ALL', 'TZ')
# Its answer's bytes at most for a text of maxChars characters: each at
# most 6 in JSON (a control character as \\u00XX), plus the cut line.
_OUTPUT_BYTES_PER_CHAR = 6
_OUTPUT_MARGIN_BYTES = 64 * 1024
# What the parent waits past the time it gave the child, for the child's
# own OutOfTime answer.
_GRACE_SECONDS = 0.5
_READ_CHUNK = 64 * 1024
# The child's refusals, by the key it answers (spreadsheetWorker.REASONS).
_REASONS = {'unreadable': spreadsheetText.UNREADABLE,
            'notText': spreadsheetText.NOT_TEXT,
            'tooLarge': spreadsheetText.TOO_LARGE}
_CUTS = (None, 'chars', 'cells', 'size', 'time', 'sheets')

logger = logging.getLogger(__name__)


def outputMaxBytes(maxChars: int) -> int:
    return max(0, maxChars) * _OUTPUT_BYTES_PER_CHAR + _OUTPUT_MARGIN_BYTES


def _environment() -> dict:
    return {name: os.environ[name] for name in _ENVIRONMENT
            if name in os.environ}


def _failed(label: str, returncode, event: str = 'spreadsheetWorkerFailed'
            ) -> Refused:
    logger.warning({'event': event, 'returncode': returncode})
    return Refused(f'{label} {spreadsheetText.TOO_LARGE}')


def convert(data: bytes, contentType: str, label: str, maxChars: int,
            seconds: float, sheet: str | None = None) -> SheetText:
    """spreadsheetText.spreadsheetText(data, contentType, label, maxChars,
    seconds, sheet) in a child process (see the module docstring)."""
    if seconds <= 0:
        raise OutOfTime()
    header = json.dumps({'contentType': contentType, 'maxChars': maxChars,
                         'seconds': seconds, 'sheet': sheet}).encode() + b'\n'
    limit = outputMaxBytes(maxChars)
    try:
        child = subprocess.Popen(
            [sys.executable, '-I', WORKER], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_environment(), close_fds=True)
    except OSError as error:
        # No child could start (memory or process pressure, a missing
        # interpreter): the instance, not the file, so a 503 or a note.
        logger.warning({'event': 'spreadsheetWorkerFailed',
                        'errno': error.errno})
        raise OutOfTime() from None
    out, tooLong = bytearray(), threading.Event()

    def feed() -> None:
        try:
            child.stdin.write(header)
            child.stdin.write(data)
        except OSError:
            pass  # It ended (or was killed) before reading it all.
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    def drain() -> None:
        while True:
            chunk = child.stdout.read1(_READ_CHUNK)
            if not chunk:
                return
            if len(out) + len(chunk) > limit:
                tooLong.set()
                child.kill()
                return
            out.extend(chunk)

    threads = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=drain, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        child.wait(timeout=seconds + _GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        raise OutOfTime() from None
    finally:
        for thread in threads:
            thread.join(timeout=5)
        child.stdout.close()
    if tooLong.is_set():
        raise _failed(label, child.returncode)
    if child.returncode != 0 or not out:
        raise _failed(label, child.returncode, 'spreadsheetWorkerExited')
    try:
        answer = json.loads(bytes(out).decode('utf-8', 'surrogatepass'))
        if answer.get('outOfTime') is True:
            raise OutOfTime()
        if 'refused' in answer:
            reason = _REASONS[answer['refused']]
            raise Refused(f'{label} {reason}')
        text, cutBy = answer['text'], answer['cutBy']
        if not isinstance(text, str) or cutBy not in _CUTS:
            raise ValueError('not an answer')
    except (OutOfTime, Refused):
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _failed(label, child.returncode) from None
    return SheetText(text, cutBy)
