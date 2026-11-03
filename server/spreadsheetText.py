"""spreadsheetText.py — a CSV or XLSX file as the text the chat model reads
(hand-written, pure: no database, no Storage, no module-level mutable state,
so any request thread may call it at once).

Meta's Muse takes only images and PDFs as file parts, so a spreadsheet a
user attaches goes to it as text: spreadsheetText() turns its bytes into
CSV rows, an XLSX one sheet after another (`Sheet: <name>`, then its rows),
formulas as their last calculated values (the value Excel saved), dates and
times ISO 8601, durations as Excel's [h]:mm:ss, trailing empty cells and
rows dropped.

Bounded in memory, CPU and time, whatever the file:
- The text stops at `maxChars`, at SPREADSHEET_CELLS_MAX cells scanned
  (empty, padded and repeated ones included; the rows between two far-apart
  rows counted without being made), after SPREADSHEET_SHEETS_MAX sheets,
  past SPREADSHEET_SHEETS_STREAM_MAX_BYTES of the file's sheet XML (one
  budget for all its sheets), past SPREADSHEET_ROW_ELEMENTS_MAX elements in
  one row, or past SPREADSHEET_SECONDS_MAX of this thread's CPU (so a
  busy process cuts a file where an idle one does);
  it then ends with a CUT_PREFIX line that tells the model (and so the user)
  what it holds. A CSV line longer than maxChars (it could never be shown)
  cuts it too, before the csv module reads it.
- An XLSX is read by this module, not openpyxl's loader (which builds every
  merge, validation, style and cell of a row before anything can stop it).
  It is a zip: at most SPREADSHEET_ZIP_ENTRIES_MAX entries, none past
  SPREADSHEET_ZIP_RATIO_MAX times its compressed size (from
  SPREADSHEET_ZIP_RATIO_FROM bytes). It reads only: the package and
  workbook relationships, the workbook's sheet list and 1904 flag (stopped
  at </sheets>), the styles' number formats and cell formats (for dates;
  to </cellXfs>, and styles past their caps read as none, numbers then
  shown as they are),
  the shared strings, and each sheet's <sheetData> (stopped at its end:
  merges, validations and formatting after it are never parsed). Every part
  has an inflated byte cap (_PART_MAX_BYTES) enforced on the bytes inflated,
  never on what the zip declares, and an element cap; XML is parsed by
  defusedxml (no DTD, no entity), streamed with iterparse, and each element
  is dropped once read (_records), so memory holds one record (a row, a
  shared string) at a time. Shared strings are kept packed (_SharedStrings:
  their characters plus 4 bytes each).
- No XML element may nest deeper than _XML_DEPTH_MAX, and no stretch of
  SPREADSHEET_XML_RUN_MAX_BYTES may pass without a '<' that starts a token
  (expat holds a whole start tag, text node or comment before it reports
  it; a '>' is legal in text and attribute values, a '<' only in a
  comment or PI, which count whole); CDATA (which may hold any '<') is
  refused: a part past either is too large, a sheet is cut. Shared
  strings are kept as UTF-8.
- Number formats are checked by this module, linearly (_isDate,
  _isDuration), each distinct one once, none longer than Excel's 255;
  Excel's serial dates are converted as openpyxl 3.1.5 does (_fromExcel).
  No openpyxl import: the child process (spreadsheetWorker.py) stays
  small.
- A CSV row (quoted fields may span lines) stops past maxChars characters
  read for it, before the csv module joins them.

A file that cannot be read is a Refused (a ValueError) naming `label`,
never quoting the file: what the user can do about it.
"""

import array
import codecs
import csv
import datetime
import io
import posixpath
import re
import time
import zipfile
from types import MappingProxyType
from typing import Iterator, NamedTuple

from sharedConstants import CSV_CONTENT_TYPE, XLSX_CONTENT_TYPE

SPREADSHEET_CONTENT_TYPES = (CSV_CONTENT_TYPE, XLSX_CONTENT_TYPE)
# Cells scanned per file, empty, padded and repeated ones included.
SPREADSHEET_CELLS_MAX = 200_000
SPREADSHEET_SHEETS_MAX = 10
# Excel's last column; a row's elements (cells and their parts) at most.
SPREADSHEET_COLUMNS_MAX = 16_384
SPREADSHEET_ROW_ELEMENTS_MAX = 4 * SPREADSHEET_COLUMNS_MAX
# The sheet XML of one file read at most, all its sheets together.
SPREADSHEET_SHEETS_STREAM_MAX_BYTES = 16 * 1024 * 1024
# One conversion's time at most (a turn may give it less).
SPREADSHEET_SECONDS_MAX = 5.0
# An XLSX's zip (see the module docstring).
SPREADSHEET_ZIP_ENTRIES_MAX = 1000
SPREADSHEET_ZIP_RATIO_MAX = 200
SPREADSHEET_ZIP_RATIO_FROM = 1024 * 1024
# What each part may inflate to, and its elements at most.
_PART_MAX_BYTES = MappingProxyType({
    'rels': 1024 * 1024, 'workbook': 1024 * 1024,
    'styles': 2 * 1024 * 1024, 'sharedStrings': 16 * 1024 * 1024})
_PART_MAX_ELEMENTS = MappingProxyType({
    'rels': 10_000, 'workbook': 20_000, 'styles': 200_000,
    'sharedStrings': 4_000_000})
# The deepest an XML element may nest (a sheet's cells are at 7).
_XML_DEPTH_MAX = 64
# The most XML bytes in a row with no '<' starting a token (Excel's
# longest cell text, 32767 characters, is at most 128 KB of UTF-8, 160 KB
# written as &amp;).
SPREADSHEET_XML_RUN_MAX_BYTES = 256 * 1024
# Excel's longest sheet name.
SHEET_NAME_MAX_LENGTH = 31
# Excel's longest number format.
NUMBER_FORMAT_MAX_LENGTH = 255
# Excel's own limit of cell formats.
SPREADSHEET_CELL_FORMATS_MAX = 65_000
SPREADSHEET_SHARED_STRINGS_MAX = 1_000_000
# How often (in XML elements or CSV rows) the clock is read.
_CLOCK_EVERY = 512
_CSV_CHUNK = 64 * 1024
CUT_PREFIX = '[Cut: '
UNREADABLE = "couldn't be read: save it again as .xlsx or .csv"
NOT_TEXT = "isn't a text CSV file: save it again as CSV UTF-8"
TOO_LARGE = ('is too large to read: save a smaller part of it as .xlsx or '
             '.csv')
_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_OFFICE_DOCUMENT = '/officeDocument'
_EOL = re.compile(r'\r\n|\r|\n')
_CELL_REF = re.compile(r'([A-Za-z]{1,3})([0-9]+)')


class SheetText(NamedTuple):
    """A spreadsheet's text, and what cut it: None (whole), 'chars' (at
    maxChars), 'cells', 'size' (the sheet XML budget), 'time' or
    'sheets'."""
    text: str
    cutBy: str | None


class Refused(ValueError):
    """A spreadsheet refused, in words the user can act on, naming its
    label: every refusal of this module."""


class _Cut(Exception):
    """Where the text stopped: cutBy (SheetText's), and the CUT_PREFIX
    line saying so."""

    def __init__(self, cutBy: str):
        super().__init__(cutBy)
        self.cutBy = cutBy
        self.line = ''


class OutOfTime(Exception):
    """The caller's time (`seconds`) ran out before the text was made: the
    machine was busy, not the file too large (never a ValueError, so never
    a refusal of the file)."""


class _PartTooLarge(Exception):
    """A part read whole passed its byte or element cap, or the clock."""


class _Clock:
    """Two bounds: SPREADSHEET_SECONDS_MAX of this thread's CPU (the same
    file is cut at the same place however busy the process is), and the
    caller's `seconds` of wall time (None: none), past which over() raises
    OutOfTime."""

    def __init__(self, seconds: float | None):
        if seconds is not None and seconds <= 0:
            raise OutOfTime()
        self.cpuStopAt = time.thread_time() + SPREADSHEET_SECONDS_MAX
        self.wallStopAt = (None if seconds is None
                           else time.monotonic() + seconds)

    def over(self) -> bool:
        """Whether the CPU budget is spent; OutOfTime past the caller's
        time."""
        if self.wallStopAt is not None and time.monotonic() > self.wallStopAt:
            raise OutOfTime()
        return time.thread_time() > self.cpuStopAt


# ── Values ──────────────────────────────────

def _duration(value) -> str:
    seconds = int(value.total_seconds())
    sign = '-' if seconds < 0 else ''
    seconds = abs(seconds)
    return (f'{sign}{seconds // 3600}:{seconds % 3600 // 60:02}:'
            f'{seconds % 60:02}')


def _cellText(value) -> str:
    """A cell's value as text: None is empty, a whole float has no .0, a
    date or time is ISO 8601 (a midnight date-time as its date), a duration
    [h]:mm:ss."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if hasattr(value, 'total_seconds'):
        return _duration(value)
    isoformat = getattr(value, 'isoformat', None)
    if isoformat is not None:
        if (hasattr(value, 'hour') and hasattr(value, 'date')
                and (value.hour, value.minute, value.second,
                     value.microsecond) == (0, 0, 0, 0)):
            return value.date().isoformat()
        return isoformat()
    return str(value)


def _cleanName(name) -> str:
    """A sheet's name on its own line: control characters as spaces, at
    most Excel's SHEET_NAME_MAX_LENGTH characters (a longer one, which only
    a hand-made file has, would make the cut line, which repeats it and
    falls outside maxChars, as long as the workbook part)."""
    return ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c
                   for c in str(name)[:SHEET_NAME_MAX_LENGTH]).strip()


# ── The text ────────────────────────────────

class _Writer:
    """The text, row by row, within maxChars, SPREADSHEET_CELLS_MAX and the
    clock (shared by all of a file's sheets): raises _Cut at the first row
    that would pass one."""

    def __init__(self, maxChars: int, clock: _Clock):
        self.out = io.StringIO()
        self.chars = 0
        self.cells = 0
        self.maxChars = maxChars
        self.clock = clock
        self.rows = 0  # of the current sheet (or the CSV), written

    def scan(self, cells: int) -> None:
        """Count `cells` more cells scanned."""
        self.cells += cells
        if self.cells > SPREADSHEET_CELLS_MAX:
            raise _Cut('cells')

    def line(self, text: str) -> None:
        if self.chars + len(text) + 1 > self.maxChars:
            raise _Cut('chars')
        self.out.write(text + '\n')
        self.chars += len(text) + 1

    def rows_(self, rows) -> None:
        """Write the rows, each (the empty rows before it, its values),
        trailing empty cells dropped and empty rows written only when a row
        with a value follows."""
        self.rows = 0
        blank = 0
        for gap, row in rows:
            blank += gap
            cells = [_cellText(value) for value in row]
            while cells and cells[-1] == '':
                cells.pop()
            if not cells:
                blank += 1
                continue
            # Never joined when even its cells alone would not fit.
            if self.chars + blank + sum(map(len, cells)) > self.maxChars:
                raise _Cut('chars')
            self._write(cells, blank)
            blank = 0

    def _write(self, cells: list[str], blank: int) -> None:
        one = io.StringIO()
        csv.writer(one, lineterminator='\n').writerow(cells)
        text = '\n' * blank + one.getvalue()
        if self.chars + len(text) > self.maxChars:
            raise _Cut('chars')
        self.out.write(text)
        self.chars += len(text)
        self.rows += 1 + blank


def _cutLine(cut: _Cut, rows: int, sheet: str | None, sheets: int) -> str:
    if cut.cutBy == 'sheets':
        return (f'{CUT_PREFIX}only the first {SPREADSHEET_SHEETS_MAX} of '
                f'{sheets} sheets are shown]')
    where = f' of sheet "{sheet}"' if sheet is not None else ''
    return (f'{CUT_PREFIX}the file is longer than what can be read at once: '
            f'only the first {rows} rows{where} are shown]')


# ── CSV ─────────────────────────────────────

def _csvEncoding(data: bytes, label: str) -> str:
    """UTF-8 (a BOM allowed), else cp1252, checked without decoding the
    whole file at once; NUL bytes, an empty file or neither is NOT_TEXT."""
    if not data or b'\x00' in data:
        raise Refused(f'{label} {NOT_TEXT}')
    for encoding in ('utf-8-sig', 'cp1252'):
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            for start in range(0, len(data), 1024 * 1024):
                decoder.decode(data[start:start + 1024 * 1024])
            decoder.decode(b'', final=True)
            return encoding
        except UnicodeDecodeError:
            continue
    raise Refused(f'{label} {NOT_TEXT}')


def _csvLines(data: bytes, encoding: str, maxLine: int) -> Iterator[str]:
    """The file's lines with their line ends (\\r\\n, \\n or a lone \\r),
    decoded a chunk at a time; a line past `maxLine` characters cuts the
    text (_Cut 'chars') before it is ever whole."""
    stream = io.TextIOWrapper(io.BytesIO(data), encoding=encoding,
                              newline='')
    buffer = ''
    while True:
        chunk = stream.read(_CSV_CHUNK)
        buffer += chunk
        start = 0
        while True:
            end = _EOL.search(buffer, start)
            # A \r at the end may be the first half of \r\n.
            if end is None or (chunk and end.end() == len(buffer)
                               and end.group() == '\r'):
                break
            if end.end() - start > maxLine:
                raise _Cut('chars')
            yield buffer[start:end.end()]
            start = end.end()
        buffer = buffer[start:]
        if len(buffer) > maxLine:
            raise _Cut('chars')
        if not chunk:
            if buffer:
                yield buffer
            return


class _RowLines:
    """The lines csv.reader joins into a row, counted: past `maxChars`
    characters since the last row (quoted fields may span any number of
    lines), the text is cut before the row is ever whole."""

    def __init__(self, lines: Iterator[str], maxChars: int):
        self.lines, self.maxChars, self.since = lines, maxChars, 0

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = next(self.lines)
        self.since += len(line)
        if self.since > self.maxChars:
            raise _Cut('chars')
        return line


def _csvRows(data: bytes, label: str, writer: _Writer):
    encoding = _csvEncoding(data, label)
    head = data[:4096].decode(encoding, errors='ignore')
    try:
        # A European Excel's CSV uses semicolons: written with commas.
        dialect = csv.Sniffer().sniff(head, delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    lines = _RowLines(_csvLines(data, encoding, writer.maxChars + 2),
                      writer.maxChars + 2)
    try:
        for n, row in enumerate(csv.reader(lines, dialect)):
            lines.since = 0
            writer.scan(max(1, len(row)))
            if n % _CLOCK_EVERY == 0 and writer.clock.over():
                raise _Cut('time')
            yield 0, row
    except csv.Error:
        raise Refused(f'{label} {UNREADABLE}') from None


def _csvText(data: bytes, label: str, writer: _Writer) -> _Cut | None:
    try:
        writer.rows_(_csvRows(data, label, writer))
    except _Cut as cut:
        cut.line = _cutLine(cut, writer.rows, None, 0)
        return cut
    return None


# ── XLSX: the zip and its XML ───────────────

class _Bounded:
    """A zip entry's stream that raises `error()` once more than `budget`
    allows was read from it (a _Budget, shared by several streams)."""

    def __init__(self, raw, budget: '_Budget', error):
        self.raw, self.budget, self.error = raw, budget, error
        self.run = 0  # bytes since the last token's start (_passed)
        self.tail = b''  # the last bytes read, for a mark split by a read
        self.until = None  # inside a comment or PI: the bytes that end it

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > self.budget.left + 1:
            size = self.budget.left + 1
        size = min(size, _XML_READ_MAX)
        data = self.raw.read(size)
        self.budget.left -= len(data)
        if self.budget.left < 0:
            raise self.error()
        self._passed(data)
        return data

    def _passed(self, data: bytes) -> None:
        """Counts the bytes since the last '<' that starts a token (a run),
        and raises past SPREADSHEET_XML_RUN_MAX_BYTES. Only '<' starts one:
        a '>' is legal in text and attribute values, a '<' is not. Inside
        a comment or processing instruction a '<' is legal, so the run
        goes on to its end; CDATA (which may hold any '<') is refused."""
        window = self.tail + data
        base = len(self.tail)
        self.tail = window[-_XML_MARK_CARRY:]
        mark = base - self.run  # where the run started, in `window`
        if b'<![CDATA[' in window:
            raise self.error()
        if self.until is None and b'<!' not in window and b'<?' not in window:
            first = data.find(b'<')
            if first >= 0:
                if base + first - mark > SPREADSHEET_XML_RUN_MAX_BYTES:
                    raise self.error()
                mark = base + data.rfind(b'<') + 1
        else:
            for found in _XML_MARKS.finditer(window):
                if found.end() <= base:
                    continue  # read and counted before
                token = found.group()
                if self.until is not None:
                    if token != self.until:
                        continue
                    at, self.until = found.end(), None
                elif token in (b'-->', b'?>'):
                    continue
                else:
                    at = found.start() + 1
                    self.until = _XML_TOKEN_ENDS.get(token)
                if at - 1 - mark > SPREADSHEET_XML_RUN_MAX_BYTES:
                    raise self.error()
                mark = at
        self.run = len(window) - mark
        if self.run > SPREADSHEET_XML_RUN_MAX_BYTES:
            raise self.error()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# A read's size at most, so a run is measured in small steps.
_XML_READ_MAX = 64 * 1024
# What _Bounded._passed looks for: the start of a token, a comment's or
# PI's start and end. The last bytes of a read are carried to the next, so
# a mark split by two reads is still found.
_XML_MARKS = re.compile(rb'<!--|<\?|-->|\?>|<')
_XML_TOKEN_ENDS = MappingProxyType({b'<!--': b'-->', b'<?': b'?>'})
_XML_MARK_CARRY = 8


class _Budget:
    def __init__(self, left: int):
        self.left = left


def _local(tag: str) -> str:
    return tag.rpartition('}')[2]


def _records(source, records: frozenset, maxElements: int, clock: _Clock,
             tooMany, recordMax: int | None = None, stopAt: str | None = None,
             late=None) -> Iterator[tuple[str, object, str | None]]:
    """(local tag, element, its parent's local tag) of each element of
    `records` once ended, with its children; every other element is dropped
    once ended, and a record once yielded, so memory holds the open path
    and one record. Stops at the end of `stopAt`. Past `maxElements`
    elements, `recordMax` inside one record, or nesting deeper than
    _XML_DEPTH_MAX: raises tooMany(); past the clock, late() (tooMany() by
    default)."""
    late = late or tooMany
    from defusedxml.ElementTree import iterparse
    stack, record, inRecord, count = [], None, 0, 0
    for event, element in iterparse(source, events=('start', 'end'),
                                    forbid_dtd=True):
        if event == 'start':
            count += 1
            if count > maxElements:
                raise tooMany()
            if count % _CLOCK_EVERY == 0 and clock.over():
                raise late()
            if record is not None:
                inRecord += 1
                if recordMax is not None and inRecord > recordMax:
                    raise tooMany()
            elif _local(element.tag) in records:
                record, inRecord = element, 0
            stack.append(element)
            if len(stack) > _XML_DEPTH_MAX:
                raise tooMany()
            continue
        stack.pop()
        tag = _local(element.tag)
        if element is record:
            record = None
            yield tag, element, _local(stack[-1].tag) if stack else None
        if record is None:
            element.clear()
            if stack:
                # Its earlier siblings are gone: it is the only child.
                stack[-1].remove(element)
            if tag == stopAt:
                return


class _Zip:
    """The XLSX's zip, refused when it has too many entries, one that
    inflates like a zip bomb, or is no zip."""

    def __init__(self, data: bytes, label: str):
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError,
                ValueError, EOFError):
            raise Refused(f'{label} {UNREADABLE}') from None
        entries = self.archive.infolist()
        if len(entries) > SPREADSHEET_ZIP_ENTRIES_MAX or any(
                entry.file_size > SPREADSHEET_ZIP_RATIO_FROM
                and entry.file_size > SPREADSHEET_ZIP_RATIO_MAX
                * max(1, entry.compress_size) for entry in entries):
            self.archive.close()
            raise Refused(f'{label} {TOO_LARGE}')
        self.names = set(self.archive.namelist())
        self.sheetBytes = _Budget(SPREADSHEET_SHEETS_STREAM_MAX_BYTES)

    def part(self, name: str, kind: str) -> _Bounded:
        return _Bounded(self.archive.open(name),
                        _Budget(_PART_MAX_BYTES[kind]), _PartTooLarge)

    def sheet(self, name: str) -> _Bounded:
        return _Bounded(self.archive.open(name), self.sheetBytes,
                        lambda: _Cut('size'))


def _partRecords(zipped: _Zip, name: str, kind: str, records: frozenset,
                 clock: _Clock, stopAt: str | None = None,
                 recordMax: int | None = 10_000):
    with zipped.part(name, kind) as source:
        yield from _records(source, records, _PART_MAX_ELEMENTS[kind], clock,
                            _PartTooLarge, recordMax, stopAt)


def _relationships(zipped: _Zip, name: str, clock: _Clock) -> dict:
    """Id: (Type, target path) of a .rels part (none when absent)."""
    if name not in zipped.names:
        return {}
    base = posixpath.dirname(posixpath.dirname(name))
    found = {}
    for _, element, _ in _partRecords(zipped, name, 'rels',
                                      frozenset({'Relationship'}), clock):
        target = element.get('Target') or ''
        path = (target.lstrip('/') if target.startswith('/')
                else posixpath.normpath(posixpath.join(base, target)))
        found[element.get('Id')] = (element.get('Type') or '', path)
    return found


class _SharedStrings:
    """The shared strings, packed: their UTF-8 bytes joined per block of
    _BLOCK and each one's end offset (4 bytes), never a str object each
    (and never 4 bytes a character: one emoji would widen a str block)."""
    _BLOCK = 4096

    def __init__(self):
        self.blocks: list[bytes] = []
        self.ends = array.array('I')
        self.pending: list[bytes] = []
        self.offset = 0

    def add(self, text: str) -> None:
        encoded = text.encode('utf-8', 'surrogatepass')
        self.pending.append(encoded)
        self.offset += len(encoded)
        self.ends.append(self.offset)
        if len(self.pending) == self._BLOCK:
            self._close()

    def _close(self) -> None:
        if self.pending:
            self.blocks.append(b''.join(self.pending))
            self.pending = []
        self.offset = 0

    def done(self) -> '_SharedStrings':
        self._close()
        return self

    def __getitem__(self, index: int) -> str:
        block, position = divmod(index, self._BLOCK)
        if index < 0 or block >= len(self.blocks):
            raise IndexError(index)
        start = self.ends[index - 1] if position else 0
        return self.blocks[block][start:self.ends[index]].decode(
            'utf-8', 'surrogatepass')


def _text(element) -> str:
    """A shared or inline string's text: its <t> runs, never the phonetic
    (<rPh>) ones."""
    if _local(element.tag) == 't':
        return element.text or ''
    return ''.join(_text(child) for child in element
                   if _local(child.tag) != 'rPh')


def _sharedStrings(zipped: _Zip, name: str | None,
                   clock: _Clock) -> _SharedStrings:
    strings = _SharedStrings()
    if name is None or name not in zipped.names:
        return strings.done()
    for _, element, _ in _partRecords(zipped, name, 'sharedStrings',
                                      frozenset({'si'}), clock):
        if len(strings.ends) >= SPREADSHEET_SHARED_STRINGS_MAX:
            raise _PartTooLarge()
        strings.add(_text(element))
    return strings.done()


def _dateStyles(zipped: _Zip, name: str | None,
                clock: _Clock) -> tuple[set, set]:
    """The cell formats (their index) whose number format is a date, and
    those a duration ([h]:mm). Read to the end of <cellXfs> only (the
    differential formats after it, <dxfs>, have number formats of their
    own); styles past their caps are read as none (every number shown as
    it is): the bloat is in the styles, which the user cannot cut out."""
    if name is None or name not in zipped.names:
        return set(), set()
    formats, cellFormats = dict(_BUILTIN_DATE_FORMATS), array.array('i')
    try:
        for tag, element, parent in _partRecords(
                zipped, name, 'styles', frozenset({'numFmt', 'xf'}), clock,
                stopAt='cellXfs'):
            if tag == 'numFmt' and parent == 'numFmts':
                formats[_int(element.get('numFmtId'), -1)] = (
                    element.get('formatCode') or '')
            elif tag == 'xf' and parent == 'cellXfs':
                cellFormats.append(_int(element.get('numFmtId'), 0))
                if len(cellFormats) > SPREADSHEET_CELL_FORMATS_MAX:
                    raise _PartTooLarge()
    except _PartTooLarge:
        return set(), set()
    kinds = {}  # each distinct format id checked once
    for n, formatId in enumerate(set(cellFormats)):
        if n % _CLOCK_EVERY == 0 and clock.over():
            return set(), set()
        kinds[formatId] = _formatKind(formats.get(formatId, ''))
    dates, durations = set(), set()
    for index, formatId in enumerate(cellFormats):
        kind = kinds[formatId]
        if kind == 'duration':
            durations.add(index)
        elif kind == 'date':
            dates.add(index)
    return dates, durations


# Excel's built-in number formats that are dates or durations (the others
# are numbers), as openpyxl 3.1.5 lists them.
_BUILTIN_DATE_FORMATS = MappingProxyType({
    14: 'mm-dd-yy', 15: 'd-mmm-yy', 16: 'd-mmm', 17: 'mmm-yy',
    18: 'h:mm AM/PM', 19: 'h:mm:ss AM/PM', 20: 'h:mm', 21: 'h:mm:ss',
    22: 'm/d/yy h:mm', 45: 'mm:ss', 46: '[h]:mm:ss', 47: 'mmss.0'})
_DURATION = re.compile(
    r'\[hh?\](:mm(:ss(\.0*)?)?)?|\[mm?\](:ss(\.0*)?)?|\[ss?\](\.0*)?',
    re.IGNORECASE)
_ELAPSED = ('h]', 'hh]', 'm]', 'mm]', 's]', 'ss]')


def _formatKind(code: str) -> str | None:
    """'duration', 'date' or None (a number) for a number format; one
    longer than Excel allows is a number."""
    if len(code) > NUMBER_FORMAT_MAX_LENGTH:
        return None
    if _isDuration(code):
        return 'duration'
    return 'date' if _isDate(code) else None


def _isDuration(code: str) -> bool:
    """openpyxl's is_timedelta_format: an elapsed [h], [m] or [s] in the
    first section."""
    return _DURATION.search(code.split(';', 1)[0]) is not None


def _isDate(code: str) -> bool:
    """openpyxl's is_date_format, in one pass: the first section, without
    its quoted text and its bracketed parts (but an elapsed [h], [m], [s]),
    has a d, m, h, y or s not escaped by \\ or _."""
    code = code.split(';', 1)[0]
    kept, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c == '"':
            end = code.find('"', i + 1)
            # openpyxl's ".*?" stops at a line end.
            if end > 0 and '\n' not in code[i:end]:
                i = end + 1
                continue
        elif c == '[' and not code.startswith(_ELAPSED, i + 1):
            end = code.find(']', i + 1)
            if end > 0:
                i = end + 1
                continue
        kept.append(c)
        i += 1
    return any(c in 'dmhysDMHYS' and (j == 0 or kept[j - 1] not in '_\\')
               for j, c in enumerate(kept))


def _relId(element) -> str | None:
    """A <sheet>'s r:id, whatever its namespace (transitional or Strict
    Open XML)."""
    for key, value in element.attrib.items():
        if key.startswith('{') and _local(key) == 'id':
            return value
    return None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _workbook(zipped: _Zip, clock: _Clock):
    """The workbook's sheets (name, path) that the file holds, its 1904
    flag, and its shared strings and date styles' parts. A workbook with no
    sheet the file holds is unreadable (a ValueError)."""
    package = _relationships(zipped, '_rels/.rels', clock)
    workbook = next((path for kind, path in package.values()
                     if kind.endswith(_OFFICE_DOCUMENT)), 'xl/workbook.xml')
    if workbook not in zipped.names:
        raise ValueError('no workbook')
    rels = _relationships(
        zipped, posixpath.join(posixpath.dirname(workbook), '_rels',
                               posixpath.basename(workbook) + '.rels'),
        clock)
    sheets, date1904 = [], False
    for tag, element, _ in _partRecords(
            zipped, workbook, 'workbook',
            frozenset({'sheet', 'workbookPr'}), clock, stopAt='sheets'):
        if tag == 'workbookPr':
            date1904 = element.get('date1904') in ('1', 'true')
        else:
            kind, path = rels.get(_relId(element), ('', None))
            if path in zipped.names and kind.endswith('/worksheet'):
                sheets.append((element.get('name') or '', path))
    if not sheets:
        raise ValueError('no sheet')
    byType = {kind.rpartition('/')[2]: path for kind, path in rels.values()}
    return sheets, date1904, byType.get('sharedStrings'), byType.get('styles')


# ── XLSX: a sheet's rows ────────────────────

def _column(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - 64
    return number


def _number(text: str):
    try:
        return int(text)
    except ValueError:
        return float(text)


_WINDOWS_EPOCH = datetime.datetime(1899, 12, 30)
_MAC_EPOCH = datetime.datetime(1904, 1, 1)


def _fromExcel(value: float, epoch: datetime.datetime,
               timedelta: bool = False):
    """An Excel serial as openpyxl 3.1.5's from_excel converts it: a
    timedelta (to the millisecond), a time (under a day), or a datetime
    (the 1900 system's phantom 29 February skipped)."""
    if timedelta:
        delta = datetime.timedelta(days=value)
        if delta.microseconds:
            delta = datetime.timedelta(
                seconds=delta.total_seconds() // 1,
                microseconds=round(delta.microseconds, -3))
        return delta
    day, fraction = divmod(value, 1)
    diff = datetime.timedelta(milliseconds=round(fraction * 86400 * 1000))
    if 0 <= value < 1 and diff.days == 0:
        minutes, seconds = divmod(diff.seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return datetime.time(hours, minutes, seconds, diff.microseconds)
    if 0 < value < 60 and epoch == _WINDOWS_EPOCH:
        day += 1
    return epoch + datetime.timedelta(days=day) + diff


class _Book(NamedTuple):
    strings: _SharedStrings
    dates: set
    durations: set
    epoch: object


def _cellValue(cell, book: _Book):
    """A <c>'s value: its saved value (data only), typed by `t` and, for a
    number, its style (a date or a duration)."""
    kind = cell.get('t', 'n')
    if kind == 'inlineStr':
        inline = next((c for c in cell if _local(c.tag) == 'is'), None)
        return None if inline is None else _text(inline)
    saved = next((c.text for c in cell if _local(c.tag) == 'v'), None)
    if saved is None:
        return None
    if kind == 's':
        try:
            return book.strings[int(saved)]
        except (ValueError, IndexError):
            return None
    if kind == 'b':
        return saved.strip() in ('1', 'true')
    if kind in ('str', 'e', 'd'):
        return saved
    try:
        value = _number(saved)
    except ValueError:
        return saved
    style = _int(cell.get('s'), 0)
    if style in book.durations:
        try:
            return _fromExcel(value, book.epoch, timedelta=True)
        except (OverflowError, ValueError):
            return saved
    if style in book.dates:
        try:
            return _fromExcel(value, book.epoch)
        except (OverflowError, ValueError):
            return saved
    return value


def _sheetRows(zipped: _Zip, path: str, book: _Book,
               writer: _Writer) -> Iterator[tuple[int, list]]:
    """A sheet's rows, each (the empty rows before it, its values), read
    from <sheetData> only, a row at a time; a row's cells are counted
    (writer.scan) before its values are made."""
    def rowTooWide():
        return _Cut('cells')
    last = 0
    with zipped.sheet(path) as source:
        for _, row, _ in _records(source, frozenset({'row'}), 1 << 62,
                                  writer.clock, rowTooWide,
                                  SPREADSHEET_ROW_ELEMENTS_MAX,
                                  stopAt='sheetData',
                                  late=lambda: _Cut('time')):
            index = _int(row.get('r'), last + 1)
            gap = max(0, index - last - 1)
            last = max(last, index)
            cells, column, width = [], 0, 0
            for cell in row:
                if _local(cell.tag) != 'c':
                    continue
                match = _CELL_REF.fullmatch(cell.get('r') or '')
                column = _column(match.group(1)) if match else column + 1
                if column > SPREADSHEET_COLUMNS_MAX:
                    raise rowTooWide()
                cells.append((column, cell))
                width = max(width, column)
            writer.scan(gap + max(1, width, len(cells)))
            if writer.clock.over():
                raise _Cut('time')
            values = [None] * width
            for column, cell in cells:
                values[column - 1] = _cellValue(cell, book)
            yield gap, values


def _xlsxText(data: bytes, label: str, writer: _Writer) -> _Cut | None:
    zipped = _Zip(data, label)
    try:
        try:
            sheets, date1904, stringsPart, stylesPart = _workbook(
                zipped, writer.clock)
            strings = _sharedStrings(zipped, stringsPart, writer.clock)
            dates, durations = _dateStyles(zipped, stylesPart, writer.clock)
        except _PartTooLarge:
            raise Refused(f'{label} {TOO_LARGE}') from None
        book = _Book(strings, dates, durations,
                     _MAC_EPOCH if date1904 else _WINDOWS_EPOCH)
        name = None
        try:
            for shown, (title, path) in enumerate(sheets):
                if shown == SPREADSHEET_SHEETS_MAX:
                    raise _Cut('sheets')
                name = _cleanName(title)
                writer.rows = 0
                writer.line(('\n' if shown else '') + f'Sheet: {name}')
                writer.rows_(_sheetRows(zipped, path, book, writer))
        except _Cut as cut:
            cut.line = _cutLine(cut, writer.rows, name, len(sheets))
            return cut
        return None
    except (Refused, OutOfTime, MemoryError):
        raise
    except Exception:
        # Broken XML, a missing part, a DTD defusedxml refused, ...: never
        # quoted (an error's text may quote the file).
        raise Refused(f'{label} {UNREADABLE}') from None
    finally:
        zipped.archive.close()


def spreadsheetText(data: bytes, contentType: str, label: str,
                    maxChars: int, seconds: float | None = None
                    ) -> SheetText:
    """The CSV (text/csv) or XLSX file `data` as text of at most `maxChars`
    characters (its rows) plus, when cut, a CUT_PREFIX line, within
    SPREADSHEET_SECONDS_MAX of CPU. A file that cannot be read is a Refused
    naming `label`; `seconds` (wall time) past, OutOfTime."""
    writer = _Writer(max(0, maxChars), _Clock(seconds))
    if contentType == CSV_CONTENT_TYPE:
        cut = _csvText(data, label, writer)
    elif contentType == XLSX_CONTENT_TYPE:
        cut = _xlsxText(data, label, writer)
    else:
        raise Refused(f'{label} {UNREADABLE}')
    text = writer.out.getvalue()
    if cut is None:
        return SheetText(text, None)
    return SheetText(text + cut.line, cut.cutBy)
