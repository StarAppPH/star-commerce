"""chatSpreadsheets.py — a chat attachment that is a spreadsheet (CSV or
XLSX), as the chat sends it to Meta (hand-written; ChatActions calls it).

Muse takes only images and PDFs as file parts, so a spreadsheet goes to it
as its text (spreadsheetText.py), in a text part between <attachment> tags
(sheetPart). One request's spreadsheets send at most
CHAT_SPREADSHEETS_TEXT_MAX_TOTAL_CHARS (SheetBudget: this message's files
first, then the history's, newest first), each at most
CHAT_SPREADSHEET_TEXT_MAX_CHARS.

A spreadsheet weighs its text in the request's Meta byte budget
(CHAT_ATTACHMENTS_MAX_TOTAL_BYTES: metaBytes), never its raw size: a 10 MB
workbook sends at most CHAT_SPREADSHEET_TEXT_MAX_CHARS characters. The
process's in-flight byte cap (ChatActions._BytesInFlight) still counts its
raw bytes, which are held while it is read.

At most SPREADSHEETS_AT_ONCE conversions run in one process (slots), each
in its own child process (spreadsheetSandbox: `python -I
spreadsheetWorker.py`, killed at the turn's deadline), so the app's
process never parses a file. The reader is bounded in memory and time
whatever the file (spreadsheetText's docstring); the child is the
backstop, capped on Linux at its address space after imports plus 64 MiB
(RLIMIT_AS) and 8 s of CPU (RLIMIT_CPU). Measured on Python 3.12 (Windows,
test_spreadsheet_text's hostile files through the sandbox), a child's peak
working set is 15 MiB for a tiny CSV and at most about 54 MiB (rows of
repeated cells; a row of 65,536 elements 51 MiB): at most 38 MiB of
private memory above an idle worker's, the file and the answer included.
Its start costs about 0.09 s of wall time. The 512 MiB service holds:
gunicorn's master (no --preload: gunicorn only, about 25 MiB), its one
worker with the app (about 95 MiB once imported), the 60 MB of
attachment bytes in flight (CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX: the files
being read, a spreadsheet's raw bytes among them), their base64 copies in
the model requests (about 80 MB) and two children: about 110 MiB
measured, at most about 2 x (30 + 64) MiB of address space by their
Linux caps. About 450 MiB at the caps, inside it. More would let more files hold a CPU
each for up to 5 s on a service of one or two CPUs. A turn that finds no
free slot in the time it has left, or whose child is still running at its
deadline, gives its own file a 503 and a history one a note.
"""

import re
import threading
import time
from typing import NamedTuple

import spreadsheetSandbox
import spreadsheetText
from objects import (CHAT_SPREADSHEET_TEXT_MAX_CHARS,
                     CHAT_SPREADSHEETS_TEXT_MAX_TOTAL_CHARS)

SPREADSHEET_TYPES = spreadsheetText.SPREADSHEET_CONTENT_TYPES
SPREADSHEETS_AT_ONCE = 2
slots = threading.BoundedSemaphore(SPREADSHEETS_AT_ONCE)
# What a spreadsheet's note and its <attachment> tag call it.
SHEET_KIND = 'spreadsheet'
# Inside a spreadsheet's text, any "<" that starts an attachment tag is
# written &lt;, so the file cannot close its block or open another.
ATTACHMENT_TAG = re.compile(r'<(?=\s*/?\s*attachment)', re.IGNORECASE)
# The text of this message's spreadsheet that the request has no text left
# for (its earlier spreadsheets took it all): never converted. A [Cut: ...]
# line, which the prompt has the model pass on.
NOT_READ = (f"{spreadsheetText.CUT_PREFIX}not read, this message's "
            'spreadsheets already fill the text limit: send it in its own '
            'message]')


class Sheet(NamedTuple):
    """A spreadsheet's text, held in a file's place of its bytes once read
    (never a str, so never taken for photoStorage's GONE or REFUSED)."""
    text: str


class SheetBudget:
    """The spreadsheet text one request may still send."""

    def __init__(self):
        self.left = CHAT_SPREADSHEETS_TEXT_MAX_TOTAL_CHARS

    def limit(self) -> int:
        """What the next file's text may hold."""
        return max(0, min(CHAT_SPREADSHEET_TEXT_MAX_CHARS, self.left))

    def take(self, text: str) -> Sheet:
        self.left = max(0, self.left - len(text))
        return Sheet(text)


class SlotsBusy(Exception):
    """No conversion slot freed up, or the conversion did not end, within
    the time the turn has left: the process was busy, not the file at
    fault."""


def isSheet(contentType: str) -> bool:
    return contentType in SPREADSHEET_TYPES


def metaBytes(contentType: str, size: int) -> int:
    """What a file weighs in the request's Meta byte budget: a
    spreadsheet at most its text's cap, any other file its size."""
    if isSheet(contentType):
        return min(size, CHAT_SPREADSHEET_TEXT_MAX_CHARS)
    return size


def _converted(data: bytes, contentType: str, label: str, limit: int,
               secondsLeft: float,
               sheet: str | None = None) -> spreadsheetText.SheetText:
    """spreadsheetText in one of the process's slots, waited for and run
    within `secondsLeft` together (else SlotsBusy): what the wait took is
    not given to the conversion. The caller computes `secondsLeft` from its
    own clock."""
    started = time.monotonic()
    if not slots.acquire(timeout=max(0.0, secondsLeft)):
        raise SlotsBusy()
    try:
        seconds = max(0.0, started + secondsLeft - time.monotonic())
        if sheet is None:  # an attachment: every sheet
            return spreadsheetSandbox.convert(data, contentType, label,
                                              limit, seconds)
        return spreadsheetSandbox.convert(data, contentType, label, limit,
                                          seconds, sheet=sheet)
    except spreadsheetText.OutOfTime:
        raise SlotsBusy() from None
    finally:
        slots.release()


# The text a spreadsheet import reads at most (productImport.py): many
# imports' rows (PRODUCT_IMPORT_ROWS_MAX each). The child's memory cap
# (spreadsheetWorker.WORKER_MEMORY_HEADROOM_BYTES) holds it: the text is
# held about twice (the writer, then the answer).
IMPORT_TEXT_MAX_CHARS = 500_000


def importText(data: bytes, contentType: str, label: str,
               secondsLeft: float,
               sheet: str | None = None) -> spreadsheetText.SheetText:
    """A spreadsheet's text for an import (productImport.py), in a slot
    and a child process as an attachment's (_converted): at most
    IMPORT_TEXT_MAX_CHARS, only `sheet`'s rows when it names one (an
    XLSX's other sheets give only their names), a refusal a ValueError
    naming `label`, no slot or no time left SlotsBusy."""
    return _converted(data, contentType, label, IMPORT_TEXT_MAX_CHARS,
                      secondsLeft, sheet)


def ownSheet(data: bytes, contentType: str, label: str,
             sheets: SheetBudget, secondsLeft: float) -> Sheet:
    """This message's spreadsheet as text, within what the request has
    left (cut past it, with a line saying so); NOT_READ, never converted,
    when its earlier spreadsheets left none. One that cannot be read is a
    ValueError naming `label`, no slot in time SlotsBusy."""
    if sheets.left == 0:
        return Sheet(NOT_READ)
    sheet = _converted(data, contentType, label, sheets.limit(),
                       secondsLeft)
    return sheets.take(sheet.text)


def historySheet(data: bytes, contentType: str, label: str,
                 sheets: SheetBudget, secondsLeft: float,
                 refused: str) -> Sheet | str | None:
    """A history spreadsheet as text; None (an Earlier note) when the
    request has no text left for it, or it does not fit in what is left
    (cut where a whole file budget would not have cut it); `refused` when
    it cannot be read or no slot freed up in time."""
    limit = sheets.limit()
    if limit == 0:
        return None
    try:
        sheet = _converted(data, contentType, label, limit, secondsLeft)
    except (ValueError, SlotsBusy):
        return refused
    if sheet.cutBy == 'chars' and limit < CHAT_SPREADSHEET_TEXT_MAX_CHARS:
        return None
    return sheets.take(sheet.text)


def sheetPart(attachment: dict, position: int, text: str) -> dict:
    """A spreadsheet's text as a text part, between <attachment> tags
    naming its place and its (sanitised) name; an attachment tag inside
    the text is neutralised (ATTACHMENT_TAG)."""
    body = ATTACHMENT_TAG.sub('&lt;', text).rstrip('\n')
    return {'type': 'text', 'text': (
        f'<attachment index="{position + 1}" name="{attachment["name"]}" '
        f'kind="{SHEET_KIND}">\n{body}\n</attachment>')}
