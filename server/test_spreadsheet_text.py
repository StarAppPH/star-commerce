"""spreadsheetText.py: a CSV or XLSX attachment as the text the chat model
reads, bounded in characters, cells, sheets, memory and time, and refused
(a ValueError the user can act on) when it cannot be read or would unpack
too large.

Pure: no database, no Storage. The workbooks are built here, byte by byte
(sheetXlsx) or with openpyxl, never read from disk. The hostile ones
(merges, styles, wide rows, repeated cells, long CSV lines, shared
strings) are read under tracemalloc, with a bound on their peak Python
memory and their CPU time."""

import datetime
import io
import os
import time
import tracemalloc
import zipfile

import openpyxl
import pytest

import spreadsheetText
from spreadsheetText import (CSV_CONTENT_TYPE, CUT_PREFIX, NOT_TEXT,
                             TOO_LARGE, UNREADABLE, XLSX_CONTENT_TYPE)

LABEL = 'attachments[0]'
# The chat's per-file cap (objects.CHAT_SPREADSHEET_TEXT_MAX_CHARS).
MAX_CHARS = 100_000


def toText(data: bytes, contentType: str, label: str,
           maxChars: int = MAX_CHARS, **kwargs):
    return spreadsheetText.spreadsheetText(data, contentType, label,
                                           maxChars, **kwargs)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/'
    'vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '{sheets}</Types>')
_SHEET_TYPE = ('<Override PartName="/xl/worksheets/sheet{n}.xml" '
               'ContentType="application/vnd.openxmlformats-officedocument.'
               'spreadsheetml.worksheet+xml"/>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.'
    'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>')
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
    'main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships"><sheets>{sheets}</sheets></workbook>')
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">{rels}</Relationships>')
_SHEET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
    '2006/main"><sheetData>{rows}</sheetData>{tail}</worksheet>')
_MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def sheetXlsx(sheets: dict[str, str], extra: dict[str, bytes] | None = None,
              compression=zipfile.ZIP_DEFLATED, tail: str = '',
              related: dict[str, bytes] | None = None,
              workbookPr: str = '') -> bytes:
    """An XLSX of `sheets` (name: its <sheetData> rows XML), as Excel saves
    one (inline strings, formulas with their cached values), plus `extra`
    zip entries, `tail` after each sheet's <sheetData>, and `related`
    workbook parts (styles, sharedStrings: their XML) with their
    relationships."""
    related = related or {}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression) as archive:
        names = list(sheets)
        archive.writestr('[Content_Types].xml', _CONTENT_TYPES.format(
            sheets=''.join(_SHEET_TYPE.format(n=n + 1)
                           for n in range(len(names)))))
        archive.writestr('_rels/.rels', _RELS)
        archive.writestr('xl/workbook.xml', _WORKBOOK.format(
            sheets=''.join(
                f'<sheet name="{name}" sheetId="{n + 1}" r:id="rId{n + 1}"/>'
                for n, name in enumerate(names))).replace(
                    '<sheets>', workbookPr + '<sheets>'))
        archive.writestr('xl/_rels/workbook.xml.rels', _WORKBOOK_RELS.format(
            rels=''.join(
                f'<Relationship Id="rId{n + 1}" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/sheet{n + 1}.xml"/>'
                for n in range(len(names))) + ''.join(
                f'<Relationship Id="rel{kind}" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/'
                f'{kind}" Target="{kind}.xml"/>' for kind in related)))
        for n, name in enumerate(names):
            archive.writestr(f'xl/worksheets/sheet{n + 1}.xml',
                             _SHEET.format(rows=sheets[name], tail=tail))
        for kind, xml in related.items():
            archive.writestr(f'xl/{kind}.xml', xml)
        for path, data in (extra or {}).items():
            archive.writestr(path, data)
    return out.getvalue()


def inline(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def number(ref: str, value) -> str:
    return f'<c r="{ref}"><v>{value}</v></c>'


def openpyxlBook(rows: list[list], title='Products') -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


# =============================== CSV =============================== #


def test_aUtf8Csv_isItsRows_trailingEmptiesDropped_quotedAsCsv():
    data = ('﻿name,price,notes\r\n'
            'Mug,"1,50","say ""hi"""\r\n'
            ',,\r\n'
            'Cup,2,\r\n'
            '\r\n,,\r\n').encode('utf-8')

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL)

    assert sheet == spreadsheetText.SheetText(
        'name,price,notes\nMug,"1,50","say ""hi"""\n\nCup,2\n', None)


def test_aCp1252Csv_isReadAsWindowsText():
    data = 'name;price\ncafé;1,5\n€ sign;2\n'.encode('cp1252')

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL)

    # Semicolons (a European Excel's CSV) are found and written as commas.
    assert sheet.text == 'name,price\ncafé,"1,5"\n€ sign,2\n'
    assert sheet.cutBy is None


def test_everyLineEnd_andAQuotedLineBreak_areRead():
    sheet = toText(b'a,b\rc,"two\r\nlines"\r\nd,e\nf', CSV_CONTENT_TYPE,
                   LABEL)

    assert sheet.text == 'a,b\nc,"two\r\nlines"\nd,e\nf\n'


@pytest.mark.parametrize('data', [
    b'a,b\n\x00\x01\x02',
    'name,price\n'.encode('utf-16'),
    # Bytes that are neither UTF-8 nor cp1252 (0x81 is undefined there).
    b'a,b\n\x81\x8d\x8f\x90\x9d\xff\xfe',
])
def test_aBinaryOrUndecodableCsv_isRefused_naming_itsPlaceOnly(data):
    with pytest.raises(ValueError) as error:
        toText(data, CSV_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {NOT_TEXT}'


def test_aCsvLinePastMaxChars_cutsTheText_beforeTheCsvModuleReadsIt():
    """A line longer than the text may hold could never be shown: it cuts
    the text (the rows before it stay), and is never read whole."""
    data = b'a,b\n' + b'c,"' + b'x' * 200_000 + b'"\nd,e\n'

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'chars'
    assert sheet.text.startswith('a,b\n' + CUT_PREFIX)
    assert 'xxx' not in sheet.text


def test_aSemicolonCsv_asAEuropeanExcelSavesIt_isReadAsItsColumns():
    data = 'name;price;note\nMug;1,50;"a;b"\nCup;2;\n'.encode('utf-8')

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL)

    assert sheet == ('name,price,note\nMug,"1,50",a;b\nCup,2\n', None)


@pytest.mark.parametrize('data', [
    b'',
    'name,price\nMug,1\n'.encode('utf-16'),
    'name,price\nMug,1\n'.encode('utf-16-le'),
], ids=['empty', 'utf16', 'utf16NoBom'])
def test_anEmptyOrUtf16Csv_isNotText_withWhatToDo(data):
    with pytest.raises(ValueError) as error:
        toText(data, CSV_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {NOT_TEXT}'


def test_aLongCsv_isCutAtMaxChars_atAWholeRow_withALineSayingSo():
    data = ''.join(f'row{n},{n}\n' for n in range(1000)).encode()

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL, maxChars=50)

    rows, cut = sheet.text.rsplit('\n', 1)
    assert rows.split('\n') == [f'row{n},{n}' for n in range(7)]
    assert len(rows) + 1 <= 50
    assert cut == (f'{CUT_PREFIX}the file is longer than what can be read '
                   'at once: only the first 7 rows are shown]')
    assert sheet.cutBy == 'chars'


def test_aMaxCharsOf0_showsNoRow_butSaysItWasCut():
    sheet = toText(b'a,b\n', CSV_CONTENT_TYPE, LABEL, maxChars=0)

    assert sheet.text.startswith(CUT_PREFIX)
    assert 'only the first 0 rows' in sheet.text
    assert sheet.cutBy == 'chars'


def test_emptyRowsCountAsScannedCells_soAnEndlessBlankFileStops(monkeypatch):
    monkeypatch.setattr(spreadsheetText, 'SPREADSHEET_CELLS_MAX', 100)
    data = b'a\n' + b',,,,\n' * 1000 + b'b\n'

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'cells'
    assert sheet.text.startswith('a\n' + CUT_PREFIX)


# =============================== XLSX =============================== #


def test_anXlsx_isEachSheetByName_formulasAsTheirLastValues():
    data = sheetXlsx({
        'Products': (
            '<row r="1">' + inline('A1', 'name') + inline('B1', 'price') +
            inline('C1', 'qty') + inline('D1', 'total') + '</row>'
            '<row r="2">' + inline('A2', 'Mug') + number('B2', 2.5) +
            number('C2', 4) +
            '<c r="D2"><f>B2*C2</f><v>10</v></c><c r="E2"/></row>'
            '<row r="4">' + inline('A4', 'far') + '</row>'
            '<row r="5"><c r="A5"/></row>'),
        'Notes': '<row r="1">' + inline('B1', 'x, "y"') + '</row>',
    })

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet == spreadsheetText.SheetText(
        'Sheet: Products\n'
        'name,price,qty,total\n'
        'Mug,2.5,4,10\n'
        '\n'
        'far\n'
        '\n'
        'Sheet: Notes\n'
        ',"x, ""y"""\n', None)


def test_anOpenpyxlBook_writesDatesAsIso_andWholeFloatsWithoutPoint():
    data = openpyxlBook([
        ['when', 'at', 'n', 'ok'],
        [datetime.datetime(2026, 11, 2), datetime.datetime(2026, 11, 2, 9, 30),
         3.0, True],
    ])

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.text == ('Sheet: Products\nwhen,at,n,ok\n'
                          '2026-11-02,2026-11-02T09:30:00,3,TRUE\n')


def test_aLongSheet_isCutAtMaxChars_namingTheSheet():
    data = openpyxlBook([[f'p{n}', n] for n in range(5000)], title='Big')

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL, maxChars=1000)

    assert sheet.cutBy == 'chars'
    body, cut = sheet.text.rsplit('\n', 1)
    assert len(body) + 1 <= 1000
    shown = body.count('\n')  # the rows after "Sheet: Big"
    assert cut == (f'{CUT_PREFIX}the file is longer than what can be read '
                   f'at once: only the first {shown} rows of sheet "Big" '
                   'are shown]')


def test_onlyTheFirstSheetsAreRead():
    limit = spreadsheetText.SPREADSHEET_SHEETS_MAX
    data = sheetXlsx({f'S{n}': '<row r="1">' + number('A1', n) + '</row>'
                      for n in range(limit + 2)})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'sheets'
    assert sheet.text.count('Sheet: ') == limit
    assert sheet.text.endswith(f'{CUT_PREFIX}only the first {limit} of '
                               f'{limit + 2} sheets are shown]')


def test_aCellAtTheLastRowAndColumn_costsCellsNotMinutes(monkeypatch):
    """One cell at XFD1048576: openpyxl would pad 16384 columns and fill a
    million rows; the cells cap stops it."""
    data = sheetXlsx({'S': '<row r="1">' + inline('A1', 'a') + '</row>'
                      '<row r="1048576">' + inline('XFD1048576', 'z') +
                      '</row>'})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'cells'
    assert sheet.text.startswith('Sheet: S\na\n' + CUT_PREFIX)


def zipBomb() -> bytes:
    """A workbook with an entry that inflates 8 MB of zeros from about
    8 KB: a ratio no real sheet has."""
    return sheetXlsx({'S': '<row r="1">' + inline('A1', 'a') + '</row>'},
                     extra={'xl/worksheets/sheet2.xml':
                            b'0' * (8 * 1024 * 1024)})


@pytest.mark.parametrize('build', [
    zipBomb,
    lambda: sheetXlsx({'S': '<row r="1">' + number('A1', 1) + '</row>' +
                       ' ' * (2 * 1024 * 1024)}),
    lambda: sheetXlsx({'S': ''}, extra={
        f'xl/media/{n}.bin': b'' for n in range(
            spreadsheetText.SPREADSHEET_ZIP_ENTRIES_MAX)}),
], ids=['bomb', 'ratioInASheet', 'entries'])
def test_aZipBomb_isRefusedBeforeAnyPartIsRead(build, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError('no part may be read')
    monkeypatch.setattr(spreadsheetText, '_records', never)
    data = build()

    with pytest.raises(ValueError) as error:
        toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'


def test_aPartPastItsLimitInflated_isTooLarge():
    """The shared strings are bounded on the bytes inflated, whatever the
    zip says (stored, so no ratio refuses it first)."""
    data = sheetXlsx({'S': ''}, related={'sharedStrings': (
        f'<sst xmlns="{_MAIN_NS}">'.encode() + b' ' * (
            spreadsheetText._PART_MAX_BYTES['sharedStrings']) +
        b'</sst>')}, compression=zipfile.ZIP_STORED)

    with pytest.raises(ValueError) as error:
        toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'


def test_aSheetPastItsStreamLimit_isCut(monkeypatch):
    # The XML parser reads 64 KB at a time: the rows of the reads within
    # the limit are written, then the sheet is cut.
    monkeypatch.setattr(spreadsheetText,
                        'SPREADSHEET_SHEETS_STREAM_MAX_BYTES', 100_000)
    data = sheetXlsx({'S': ''.join(
        f'<row r="{n}">' + number(f'A{n}', n) + '</row>'
        for n in range(1, 8000))})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'size'
    assert sheet.text.startswith('Sheet: S\n1\n2\n')
    assert CUT_PREFIX in sheet.text


def test_aSheetWithoutDimension_isNeverParsedWhole():
    """Read row by row: here the sheet's end is broken XML, and the text is
    cut long before it."""
    rows = ''.join(f'<row r="{n}">' + number(f'A{n}', n) + '</row>'
                   for n in range(1, 5000))
    data = sheetXlsx({'S': rows + '<row r="9999"><<<broken'})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL, maxChars=200)

    assert sheet.cutBy == 'chars'
    assert sheet.text.startswith('Sheet: S\n1\n2\n')


def test_theReader_importsNoOpenpyxl_andMatchesItsDates():
    """The worker stays small (openpyxl adds about 10 MB and 0.2 s to each
    child): spreadsheetText converts Excel serials as openpyxl 3.1.5 does,
    checked here against it (the tests' own dependency)."""
    import subprocess
    import sys
    from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
    from openpyxl.utils.datetime import MAC_EPOCH, WINDOWS_EPOCH, from_excel
    here = os.path.dirname(os.path.abspath(__file__))
    check = ('import sys; sys.path.insert(0, sys.argv[1]); '
             'import spreadsheetText; '
             'print(any(m.startswith("openpyxl") for m in sys.modules))')
    out = subprocess.run([sys.executable, '-I', '-c', check, here],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == 'False', out.stderr

    for value in (0, 0.25, 1, 59, 60, 61, 45000.5, 46328.999, 2_900_000):
        for epoch, ours in ((WINDOWS_EPOCH, spreadsheetText._WINDOWS_EPOCH),
                            (MAC_EPOCH, spreadsheetText._MAC_EPOCH)):
            assert spreadsheetText._fromExcel(value, ours) == from_excel(
                value, epoch)
            assert spreadsheetText._fromExcel(
                value, ours, timedelta=True) == from_excel(
                    value, epoch, timedelta=True)
    dates = {formatId for formatId, code in BUILTIN_FORMATS.items()
             if is_date_format(code)}
    assert dates == set(spreadsheetText._BUILTIN_DATE_FORMATS)


@pytest.mark.parametrize('data', [
    b'PK\x03\x04 not really a zip',
    # A zip, but no workbook in it.
    (lambda out: (zipfile.ZipFile(out, 'w').writestr('a.txt', 'x'),
                  out.getvalue())[1])(io.BytesIO()),
    # A workbook whose XML is broken.
    sheetXlsx({'S': '<row r="1"><c r="A1"'}),
], ids=['notAZip', 'noWorkbook', 'brokenXml'])
def test_aBrokenXlsx_isUnreadable_neverA500(data):
    with pytest.raises(ValueError) as error:
        toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {UNREADABLE}'


def test_anXmlEntityBomb_isRefused_byDefusedXml():
    laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
              '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>')
    data = sheetXlsx({'S': ''})
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, \
            zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                content = (laughs.encode() + content.split(b'?>', 1)[1]
                           .replace(b'<sheetData>',
                                    b'<sheetData><row r="1"><c r="A1" '
                                    b't="inlineStr"><is><t>&lol2;</t></is>'
                                    b'</c></row>'))
            target.writestr(entry.filename, content)

    with pytest.raises(ValueError) as error:
        toText(out.getvalue(), XLSX_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {UNREADABLE}'


def test_anotherType_isUnreadable():
    with pytest.raises(ValueError, match='couldn'):
        toText(b'%PDF-', 'application/pdf', LABEL)


def test_theModuleHoldsNoMutableState():
    """Any request thread may call it at once: no module-level list, dict
    or set."""
    for name, value in vars(spreadsheetText).items():
        if not name.startswith('__'):
            assert not isinstance(value, (list, dict, set, bytearray)), name


# ============================ Dates and times ============================ #

# Cell formats: 0 general, 1 a date (built-in 14), 2 a time (built-in 20,
# h:mm), 3 a duration ([h]:mm:ss).
_DATE_STYLES = (
    f'<styleSheet xmlns="{_MAIN_NS}"><numFmts count="1">'
    '<numFmt numFmtId="164" formatCode="[h]:mm:ss"/></numFmts>'
    '<cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs>'
    '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/>'
    '<xf numFmtId="20"/><xf numFmtId="164"/></cellXfs></styleSheet>')


def styled(ref: str, value, style: int) -> str:
    return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'


@pytest.mark.parametrize('date1904', [False, True], ids=['1900', '1904'])
def test_aDate_readsBackAsTheSameIsoDate_inEitherEpoch(date1904):
    epoch = (datetime.date(1904, 1, 1) if date1904
             else datetime.date(1899, 12, 30))
    serial = (datetime.date(2026, 11, 2) - epoch).days
    data = sheetXlsx(
        {'S': '<row r="1">' + styled('A1', serial, 1) + '</row>'},
        related={'styles': _DATE_STYLES.encode()},
        workbookPr='<workbookPr date1904="1"/>' if date1904 else '')

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\n2026-11-02\n')


def test_aTimeOnlyCell_isItsTime_andADurationIsHoursMinutesSeconds():
    data = sheetXlsx({'S': (
        '<row r="1">' + styled('A1', 9.5 / 24, 2) +
        styled('B1', (26 * 60 + 5) / 1440, 3) +
        styled('C1', -5 / 1440, 3) + styled('D1', 1234.5, 0) + '</row>')},
        related={'styles': _DATE_STYLES.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\n09:30:00,26:05:00,-0:05:00,1234.5\n')


def test_sharedStrings_areReadByIndex_withoutTheirPhoneticRuns():
    strings = (f'<sst xmlns="{_MAIN_NS}"><si><t>Mug</t></si>'
               '<si><r><t>Big </t></r><r><t>cup</t></r>'
               '<rPh sb="0" eb="1"><t>ignored</t></rPh></si></sst>')
    data = sheetXlsx({'S': '<row r="1"><c r="A1" t="s"><v>1</v></c>'
                      '<c r="B1" t="s"><v>0</v></c>'
                      '<c r="C1" t="s"><v>9</v></c></row>'},
                     related={'sharedStrings': strings.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\nBig cup,Mug\n')


# ============================ Hostile files ============================ #
#
# Each is a file a user could upload (at most 10 MB, past the zip checks),
# read under tracemalloc: its peak is the Python memory the conversion
# allocates (the file's own bytes are held before it starts), its seconds
# the CPU it takes. bench numbers: the PR's report.

PEAK_MAX_BYTES = 40 * 1024 * 1024
CPU_MAX_SECONDS = spreadsheetText.SPREADSHEET_SECONDS_MAX + 4


def _noisy(n: int) -> str:
    """A value that does not compress away (so the zip ratio check lets
    the part through)."""
    return format((n * 2654435761) % 4294967291, 'x')


def mergeHeavyXlsx(merges: int = 2_000_000) -> bytes:
    """One cell, then 2 million merged ranges after <sheetData>."""
    return sheetXlsx(
        {'S': '<row r="1">' + inline('A1', 'hi') + '</row>'},
        tail=f'<mergeCells count="{merges}">' + ''.join(
            f'<mergeCell ref="A{2 * n + 2}:B{2 * n + 3}"/>'
            for n in range(merges)) + '</mergeCells>')


def junkInSheetDataXlsx(elements: int = 1_500_000) -> bytes:
    """Millions of unknown elements inside <sheetData>, between rows."""
    return sheetXlsx({'S': '<row r="1">' + inline('A1', 'hi') + '</row>' +
                      ''.join(f'<x a="{_noisy(n)}"/>'
                              for n in range(elements))})


def stylesXfXlsx(cellFormats: int = 300_000) -> bytes:
    """Hundreds of thousands of cell formats (Excel keeps under 65000)."""
    return sheetXlsx(
        {'S': '<row r="1">' + number('A1', 1) + '</row>'},
        related={'styles': (
            f'<styleSheet xmlns="{_MAIN_NS}"><cellXfs>' + ''.join(
                f'<xf numFmtId="{n % 50}" fontId="{_noisy(n)}"/>'
                for n in range(cellFormats)) +
            '</cellXfs></styleSheet>').encode()})


def wideRowXlsx(cells: int = 1_500_000) -> bytes:
    """One row of 1.5 million cells (Excel's last column is 16384)."""
    return sheetXlsx({'S': '<row r="1">' + ''.join(
        f'<c s="{_noisy(n)}"/>' for n in range(cells)) + '</row>'})


def duplicateCellsXlsx(rows: int = 20, cells: int = 30_000) -> bytes:
    """Rows of the same cell (A<row>) repeated 30000 times each."""
    return sheetXlsx({'S': ''.join(
        f'<row r="{r}">' + ''.join(
            f'<c r="A{r}"><v>{_noisy(r * cells + n)}</v></c>'
            for n in range(cells)) + '</row>'
        for r in range(1, rows + 1))})


def bigSharedStringsXlsx(strings: int = 330_000) -> bytes:
    """Close to 16 MB of unique shared strings, each in a cell."""
    return sheetXlsx(
        {'S': ''.join(f'<row r="{n + 1}"><c r="A{n + 1}" t="s"><v>{n}</v>'
                      '</c></row>' for n in range(strings))},
        related={'sharedStrings': (
            f'<sst xmlns="{_MAIN_NS}">' + ''.join(
                f'<si><t>unique {_noisy(n)} string {n:08d}</t></si>'
                for n in range(strings)) + '</sst>').encode()})


def longLineCsv(size: int = 9_900_000) -> bytes:
    """One 9.9 MB CSV line."""
    return b'a,' + b'x' * size + b'\n'


def measured(data: bytes, contentType: str):
    """(its SheetText or the ValueError it raised, peak bytes, CPU
    seconds)."""
    tracemalloc.start()
    started = time.process_time()
    try:
        try:
            result = toText(data, contentType, LABEL)
        except ValueError as error:
            result = error
        return (result, tracemalloc.get_traced_memory()[1],
                time.process_time() - started)
    finally:
        tracemalloc.stop()


HOSTILE = {
    'mergeHeavy': (mergeHeavyXlsx, XLSX_CONTENT_TYPE),
    'junkInSheetData': (junkInSheetDataXlsx, XLSX_CONTENT_TYPE),
    'stylesXf': (stylesXfXlsx, XLSX_CONTENT_TYPE),
    'wideRow': (wideRowXlsx, XLSX_CONTENT_TYPE),
    'duplicateCells': (duplicateCellsXlsx, XLSX_CONTENT_TYPE),
    'bigSharedStrings': (bigSharedStringsXlsx, XLSX_CONTENT_TYPE),
    'longLineCsv': (longLineCsv, CSV_CONTENT_TYPE),
}

# What each hostile file may end as: its cuts (a busy machine, or
# tracemalloc's own cost, may reach the clock first), or TOO_LARGE (a part
# past its cap, or the clock while the parts are read).
_HOSTILE_ENDS = {
    'mergeHeavy': (None,),
    'junkInSheetData': ('time', 'size'),
    # Past the styles' caps: read as no date styles (tracemalloc on a
    # loaded machine may spend the CPU budget first: cut, never refused).
    'stylesXf': (None, 'time'),
    'wideRow': ('cells', 'time'),
    'duplicateCells': ('cells', 'time'),
    # Read whole in about 2 s untraced; tracemalloc may take it past 5 s.
    'bigSharedStrings': ('chars', 'time', TOO_LARGE),
    'longLineCsv': ('chars',),
}


@pytest.mark.parametrize('name', list(HOSTILE))
def test_aHostileFile_isReadWithinItsMemoryAndTime(name):
    build, contentType = HOSTILE[name]
    data = build()
    assert len(data) <= 10 * 1024 * 1024  # an upload's limit

    result, peak, seconds = measured(data, contentType)

    ends = _HOSTILE_ENDS[name]
    if isinstance(result, Exception):
        assert TOO_LARGE in ends and str(result) == f'{LABEL} {TOO_LARGE}'
    else:
        assert result.cutBy in ends
        if result.cutBy is not None:
            assert CUT_PREFIX in result.text
    assert peak < PEAK_MAX_BYTES, f'{peak / 2 ** 20:.1f} MiB'
    assert seconds < CPU_MAX_SECONDS, f'{seconds:.2f} s'


def test_sharedStrings_areHeldPacked(monkeypatch):
    """Read whole (the clock lifted), 60000 unique strings (2.8 MB of XML)
    hold about 2 MiB: their characters and 4 bytes each, never a str
    object each (about 60 bytes more)."""
    monkeypatch.setattr(spreadsheetText, 'SPREADSHEET_SECONDS_MAX', 600)
    data = bigSharedStringsXlsx(60_000)
    tracemalloc.start()
    try:
        sheet = toText(data, XLSX_CONTENT_TYPE, LABEL, seconds=600)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert sheet.cutBy == 'chars'
    assert peak < 6 * 1024 * 1024, f'{peak / 2 ** 20:.1f} MiB'


def test_aSheetIsReadOnlyToTheEndOfSheetData():
    """What follows <sheetData> (merges, validations, formatting) is never
    parsed: here it is broken XML, and the text is whole."""
    data = sheetXlsx({'S': '<row r="1">' + inline('A1', 'hi') + '</row>'},
                     tail='<mergeCells><<<broken')

    assert toText(data, XLSX_CONTENT_TYPE, LABEL) == ('Sheet: S\nhi\n', None)


def test_repeatedCellsInARow_countAsScannedCells(monkeypatch):
    """A row of the same cell written 100 times scans 100 cells, not one."""
    monkeypatch.setattr(spreadsheetText, 'SPREADSHEET_CELLS_MAX', 150)
    data = sheetXlsx({'S': ''.join(
        f'<row r="{r}">' + number(f'A{r}', r) * 100 + '</row>'
        for r in range(1, 4))})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'cells'
    assert sheet.text.startswith('Sheet: S\n1\n' + CUT_PREFIX)


def test_aCellPastTheLastColumn_cutsTheSheet():
    data = sheetXlsx({'S': '<row r="1">' + number('A1', 1) + '</row>'
                      '<row r="2">' + number('XFE2', 2) + '</row>'})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'cells'
    assert sheet.text.startswith('Sheet: S\n1\n' + CUT_PREFIX)


def test_theCpuBudget_cutsTheText_withALineSayingSo(monkeypatch):
    monkeypatch.setattr(spreadsheetText, 'SPREADSHEET_SECONDS_MAX', -1)
    data = sheetXlsx({'S': ''.join(
        f'<row r="{n}">' + number(f'A{n}', n) + '</row>'
        for n in range(1, 2000))})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'time'
    assert sheet.text.startswith('Sheet: S\n')
    assert CUT_PREFIX in sheet.text


def test_theSheetXmlBudget_isOnePerFile_acrossItsSheets(monkeypatch):
    monkeypatch.setattr(spreadsheetText,
                        'SPREADSHEET_SHEETS_STREAM_MAX_BYTES', 150_000)
    rows = ''.join(f'<row r="{n}">' + number(f'A{n}', n) + '</row>'
                   for n in range(1, 1700))  # under the budget alone
    assert toText(sheetXlsx({'One': rows}), XLSX_CONTENT_TYPE,
                  LABEL).cutBy is None

    sheet = toText(sheetXlsx({'One': rows, 'Two': rows, 'Three': rows}),
                   XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy == 'size'
    assert sheet.text.rsplit('\n', 1)[1].endswith('of sheet "Two" are '
                                                  'shown]')


# ============================ Round 2 ============================ #

class _BusyWallClock:
    """time for spreadsheetText on a busy machine: every wall clock read is
    an hour later; the thread's CPU time is the real one."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 3600
        return self.now

    def thread_time(self) -> float:
        return time.thread_time()


def rowsXlsx(rows: int) -> bytes:
    return sheetXlsx({'S': ''.join(
        f'<row r="{n}">' + number(f'A{n}', n) + '</row>'
        for n in range(1, rows + 1))})


def test_aBusyMachine_neverCutsAFile_onlyItsOwnCpuDoes(monkeypatch):
    """The budget is this thread's CPU: wall time lost to other threads
    cuts nothing, so a file reads the same on every try."""
    monkeypatch.setattr(spreadsheetText, 'time', _BusyWallClock())

    sheet = toText(rowsXlsx(3000), XLSX_CONTENT_TYPE, LABEL)

    assert sheet.cutBy is None
    assert sheet.text.endswith('\n3000\n')


@pytest.mark.parametrize('contentType', [XLSX_CONTENT_TYPE, CSV_CONTENT_TYPE])
def test_pastTheCallersTime_isOutOfTime_neverARefusal(monkeypatch,
                                                      contentType):
    data = (rowsXlsx(3000) if contentType == XLSX_CONTENT_TYPE
            else b'a,b\n' * 5000)
    monkeypatch.setattr(spreadsheetText, 'time', _BusyWallClock())

    with pytest.raises(spreadsheetText.OutOfTime):
        toText(data, contentType, LABEL, seconds=60)
    assert not issubclass(spreadsheetText.OutOfTime, ValueError)


def test_noTimeLeft_isOutOfTime_beforeAnyRead():
    with pytest.raises(spreadsheetText.OutOfTime):
        toText(b'a,b\n', CSV_CONTENT_TYPE, LABEL, seconds=0)


def withWorkbook(data: bytes, change) -> bytes:
    """`data` with its xl/workbook.xml changed by `change` (bytes to
    bytes)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, \
            zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == 'xl/workbook.xml':
                content = change(content)
            target.writestr(entry.filename, content)
    return out.getvalue()


def test_aStrictOpenXmlWorkbook_isRead():
    data = withWorkbook(
        sheetXlsx({'S': '<row r="1">' + inline('A1', 'hi') + '</row>'}),
        lambda xml: xml.replace(
            b'http://schemas.openxmlformats.org/officeDocument/2006/'
            b'relationships',
            b'http://purl.oclc.org/ooxml/officeDocument/relationships'))

    assert toText(data, XLSX_CONTENT_TYPE, LABEL) == ('Sheet: S\nhi\n', None)


@pytest.mark.parametrize('change', [
    lambda xml: xml.replace(b'r:id="rId1"', b'r:id="rId9"'),
    lambda xml: xml.replace(b' r:id="rId1"', b''),
], ids=['unknownRelationship', 'noRelationshipId'])
def test_aWorkbookWithNoReadableSheet_isUnreadable_neverEmpty(change):
    data = withWorkbook(
        sheetXlsx({'S': '<row r="1">' + inline('A1', 'hi') + '</row>'}),
        change)

    with pytest.raises(ValueError) as error:
        toText(data, XLSX_CONTENT_TYPE, LABEL)

    assert str(error.value) == f'{LABEL} {UNREADABLE}'


def test_aDifferentialFormat_neverTurnsANumberIntoADate():
    """<dxfs> (conditional formatting) has number formats of its own, with
    ids that may be a cell format's: only <numFmts> count."""
    styles = (
        f'<styleSheet xmlns="{_MAIN_NS}"><numFmts count="1">'
        '<numFmt numFmtId="164" formatCode="0.00"/></numFmts>'
        '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>'
        '<dxfs count="1"><dxf><numFmt numFmtId="164" '
        'formatCode="yyyy-mm-dd"/></dxf></dxfs></styleSheet>')
    data = sheetXlsx({'S': '<row r="1">' + styled('A1', 45000, 1) + '</row>'},
                     related={'styles': styles.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == 'Sheet: S\n45000\n'


def test_stylesPastTheirCaps_showNumbersAsTheyAre_neverARefusal():
    """Styles bloated past their caps are no part the user can save apart:
    the numbers are shown as saved (no date styles)."""
    styles = (f'<styleSheet xmlns="{_MAIN_NS}">'.encode() +
              b' ' * spreadsheetText._PART_MAX_BYTES['styles'] +
              _DATE_STYLES.encode().split(b'>', 1)[1])
    data = sheetXlsx({'S': '<row r="1">' + styled('A1', 46328, 1) + '</row>'},
                     related={'styles': styles},
                     compression=zipfile.ZIP_STORED)

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == 'Sheet: S\n46328\n'


def test_stylesAreReadOnlyToTheEndOfCellXfs():
    """What follows <cellXfs> (cell styles, dxfs, colors) is never parsed:
    here it is broken XML, and the date is still a date."""
    styles = _DATE_STYLES.replace('</styleSheet>', '<dxfs><<<broken')
    data = sheetXlsx({'S': '<row r="1">' + styled('A1', 46328, 1) + '</row>'},
                     related={'styles': styles.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\n2026-11-02\n')


def test_aDurationTooLargeForATimedelta_isItsSavedValue():
    data = sheetXlsx({'S': '<row r="1">' + styled('A1', '1e20', 3) +
                      styled('B1', (26 * 60 + 5) / 1440, 3) + '</row>'},
                     related={'styles': _DATE_STYLES.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\n1e20,26:05:00\n')


# ======================= Security round 2 (r2.py) ======================= #

def deepNestingXlsx(depth: int = 1_000_000) -> bytes:
    """A million nested elements before <sheetData> (7 MB)."""
    return _withSheetXml(
        b'<?xml version="1.0"?><worksheet xmlns="' + _MAIN_NS.encode() +
        b'">' + b'<a>' * depth + b'</a>' * depth +
        b'<sheetData/></worksheet>')


def _withSheetXml(xml: bytes) -> bytes:
    """A one-sheet workbook whose sheet is `xml`, stored."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sheetXlsx({'S': ''}))) as source, \
            zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                content = xml
            target.writestr(entry.filename, content)
    return out.getvalue()


def deepSharedStringsXlsx(depth: int = 1_000_000) -> bytes:
    """A million nested elements in the shared strings (3 MB)."""
    return sheetXlsx({'S': ''}, compression=zipfile.ZIP_STORED, related={
        'sharedStrings': f'<sst xmlns="{_MAIN_NS}">'.encode() +
        b'<a>' * depth})


def manyAttributesXlsx(attributes: int = 800_000) -> bytes:
    """One element of 800,000 attributes (8 MB, no '<' or '>' in it)."""
    return _withSheetXml(
        b'<?xml version="1.0"?><worksheet xmlns="' + _MAIN_NS.encode() +
        b'"><x' + b''.join(b' a%d=""' % n for n in range(attributes)) +
        b'/><sheetData/></worksheet>')


def longInlineTextXlsx(size: int = 9_000_000) -> bytes:
    """One inline string of 9 MB, an emoji first."""
    return _withSheetXml(
        b'<?xml version="1.0"?><worksheet xmlns="' + _MAIN_NS.encode() +
        b'"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>' +
        '\U0001F600'.encode() + b'a' * size +
        b'</t></is></c></row></sheetData></worksheet>')


def longSharedStringXlsx(size: int = 9_000_000) -> bytes:
    """One shared string of 9 MB, an emoji first."""
    return sheetXlsx(
        {'S': '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'},
        compression=zipfile.ZIP_STORED, related={'sharedStrings': (
            f'<sst xmlns="{_MAIN_NS}"><si><t>'.encode() +
            '\U0001F600'.encode() + b'a' * size + b'</t></si></sst>')})


def emojiSharedStringsXlsx(strings: int = 300_000) -> bytes:
    """Shared strings with an emoji in every block of 4096: a str block
    would hold 4 bytes a character."""
    return sheetXlsx(
        {'S': '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'},
        related={'sharedStrings': (
            f'<sst xmlns="{_MAIN_NS}">' + ''.join(
                f'<si><t>{"" if n % 4096 else chr(0x1F600)}'
                f'{_noisy(n)} item</t></si>' for n in range(strings)) +
            '</sst>').encode()})


def multiLineCsvRow(size: int = 9_900_000) -> bytes:
    """One CSV row of quoted fields each spanning two lines."""
    return b'"a\n",' * (size // 5)


HOSTILE.update({
    'deepNesting': (deepNestingXlsx, XLSX_CONTENT_TYPE),
    'deepSharedStrings': (deepSharedStringsXlsx, XLSX_CONTENT_TYPE),
    'manyAttributes': (manyAttributesXlsx, XLSX_CONTENT_TYPE),
    'longInlineText': (longInlineTextXlsx, XLSX_CONTENT_TYPE),
    'longSharedString': (longSharedStringXlsx, XLSX_CONTENT_TYPE),
    'emojiSharedStrings': (emojiSharedStringsXlsx, XLSX_CONTENT_TYPE),
    'multiLineCsvRow': (multiLineCsvRow, CSV_CONTENT_TYPE),
})
_HOSTILE_ENDS.update({
    'deepNesting': ('cells',),
    'deepSharedStrings': (TOO_LARGE,),
    'manyAttributes': ('size',),
    'longInlineText': ('size',),
    'longSharedString': (TOO_LARGE,),
    'emojiSharedStrings': (None, 'time', TOO_LARGE),
    'multiLineCsvRow': ('chars',),
})


def test_aNumberFormatLongerThanExcels_isANumber_andIsCheckedFast():
    """openpyxl's date check is quadratic in '[': 300,000 of them took
    36 s. Ours is one pass, and a format past 255 characters is a
    number."""
    started = time.process_time()

    assert spreadsheetText._formatKind('[' * 300_000 + 'yyyy') is None
    # One pass, however many brackets or quotes (openpyxl's regex is
    # quadratic: 36 s on 300,000).
    assert spreadsheetText._isDate('[' * 30_000 + 'yyyy') is True
    assert spreadsheetText._isDate('"' * 30_000) is False
    assert time.process_time() - started < 1


def test_aWorkbookWithALongNumberFormat_showsItsNumbers():
    styles = (f'<styleSheet xmlns="{_MAIN_NS}"><numFmts count="1">'
              f'<numFmt numFmtId="164" formatCode="{"[" * 200_000}yy"/>'
              '</numFmts><cellXfs count="2"><xf numFmtId="0"/>'
              '<xf numFmtId="164"/></cellXfs></styleSheet>')
    data = sheetXlsx({'S': '<row r="1">' + styled('A1', 45000, 1) + '</row>'},
                     related={'styles': styles.encode()})
    started = time.process_time()

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == 'Sheet: S\n45000\n'
    assert time.process_time() - started < 2


@pytest.mark.parametrize('code', [
    'yyyy-mm-dd', 'h:mm AM/PM', '[$-409]mmm d', '0.00', '"day" 0', '[Red]0',
    '\\d0', '_d0', '[h]:mm:ss', '[mm]:ss', '0;yyyy', '"unclosed d',
    '[unclosed d', 'General', '#,##0', '[ss].00', 'mmss.0', '"a\nd"0'])
def test_ourDateCheck_agreesWithOpenpyxls(code):
    from openpyxl.styles.numbers import is_date_format, is_timedelta_format
    assert spreadsheetText._isDate(code) == is_date_format(code)
    assert spreadsheetText._isDuration(code) == is_timedelta_format(code)


def test_aCsvRow_isCutPastMaxChars_beforeTheCsvModuleJoinsIt():
    data = b'a,b\n' + b'"x\n",' * 1000 + b'\nc,d\n'

    sheet = toText(data, CSV_CONTENT_TYPE, LABEL, maxChars=500)

    assert sheet.cutBy == 'chars'
    assert sheet.text.startswith('a,b\n' + CUT_PREFIX)


def test_aRunWithNoTagMark_pastItsLimit_cutsASheet_andRefusesAPart():
    run = spreadsheetText.SPREADSHEET_XML_RUN_MAX_BYTES
    sheet = toText(_withSheetXml(
        b'<?xml version="1.0"?><worksheet xmlns="' + _MAIN_NS.encode() +
        b'"><sheetData><row r="1">' + number('A1', 1).encode() +
        b'</row><row r="2"><c r="A2" t="inlineStr"><is><t>' +
        b'a' * (run + 1) + b'</t></is></c></row></sheetData></worksheet>'),
        XLSX_CONTENT_TYPE, LABEL)
    assert sheet.cutBy == 'size'

    # Excel's longest cell (32767 characters, 4 bytes each) still passes.
    longest = '\U0001F600' * 32767
    assert toText(sheetXlsx({'S': '<row r="1">' + inline('A1', longest) +
                             '</row>'}), XLSX_CONTENT_TYPE, LABEL,
                  maxChars=200_000).text == f'Sheet: S\n{longest}\n'


def test_sharedStrings_withEmoji_readBackWhole():
    strings = (f'<sst xmlns="{_MAIN_NS}"><si><t>a\U0001F600b</t></si>'
               '<si><t>été</t></si><si><t></t></si></sst>')
    data = sheetXlsx({'S': '<row r="1"><c r="A1" t="s"><v>1</v></c>'
                      '<c r="B1" t="s"><v>0</v></c><c r="C1" t="s"><v>2</v>'
                      '</c><c r="D1" t="s"><v>1</v></c></row>'},
                     related={'sharedStrings': strings.encode()})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        'Sheet: S\nété,a\U0001F600b,,été\n')


# ======================= Security round 3 (r3.py) ======================= #

def _sheetXml(body: bytes) -> bytes:
    return _withSheetXml(b'<?xml version="1.0"?><worksheet xmlns="' +
                         _MAIN_NS.encode() + b'">' + body + b'</worksheet>')


def greaterThanAttributesXlsx(attributes: int = 800_000) -> bytes:
    """One element of 800,000 attributes a="&gt;" written raw (9 MB): a '>'
    is legal in an attribute value."""
    return _sheetXml(b'<x' + b''.join(b' a%d=">"' % n
                                      for n in range(attributes)) +
                     b'/><sheetData/>')


def greaterThanTextXlsx(pairs: int = 4_000_000) -> bytes:
    """One inline string of "a>" (8 MB), an emoji first."""
    return _sheetXml(b'<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
                     + '\U0001F600'.encode() + b'a>' * pairs +
                     b'</t></is></c></row></sheetData>')


def cdataXlsx(pairs: int = 4_000_000) -> bytes:
    """One inline string in CDATA of "a<" (8 MB): CDATA may hold any '<'."""
    return _sheetXml(b'<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
                     b'<![CDATA[' + b'a<' * pairs +
                     b']]></t></is></c></row></sheetData>')


def greaterThanSharedStringXlsx(pairs: int = 4_000_000) -> bytes:
    """One shared string of "a>" (8 MB), an emoji first."""
    return sheetXlsx(
        {'S': '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'},
        compression=zipfile.ZIP_STORED, related={'sharedStrings': (
            f'<sst xmlns="{_MAIN_NS}"><si><t>'.encode() +
            '\U0001F600'.encode() + b'a>' * pairs + b'</t></si></sst>')})


def commentXlsx(pairs: int = 4_000_000) -> bytes:
    """A comment of "a<" (8 MB) before <sheetData>: a comment may hold a
    '<'."""
    return _sheetXml(b'<!--' + b'a<' * pairs + b'--><sheetData/>')


HOSTILE.update({
    'greaterThanAttributes': (greaterThanAttributesXlsx, XLSX_CONTENT_TYPE),
    'greaterThanText': (greaterThanTextXlsx, XLSX_CONTENT_TYPE),
    'cdata': (cdataXlsx, XLSX_CONTENT_TYPE),
    'greaterThanSharedString': (greaterThanSharedStringXlsx,
                                XLSX_CONTENT_TYPE),
    'comment': (commentXlsx, XLSX_CONTENT_TYPE),
})
_HOSTILE_ENDS.update({
    'greaterThanAttributes': ('size',),
    'greaterThanText': ('size',),
    'cdata': ('size',),
    'greaterThanSharedString': (TOO_LARGE,),
    'comment': ('size',),
})


@pytest.mark.parametrize('cell', [
    '\U0001F600' * 16383,  # 32766 UTF-16 characters, 4 bytes each
    '&' * 32767,           # written &amp;: 5 bytes each
    'a>' * 16383,          # '>' is legal text
], ids=['emoji', 'ampersands', 'greaterThan'])
def test_excelsLongestCell_stillPasses(cell):
    data = sheetXlsx({'S': '<row r="1">' +
                      inline('A1', cell.replace('&', '&amp;')) + '</row>'})

    assert toText(data, XLSX_CONTENT_TYPE, LABEL,
                  maxChars=200_000).text == f'Sheet: S\n{cell}\n'


class _Past(Exception):
    pass


def _readIn(data: bytes, first: int) -> None:
    """Reads `data` through a _Bounded stream, the first read `first`
    bytes, then the rest."""
    stream = spreadsheetText._Bounded(io.BytesIO(data),
                                      spreadsheetText._Budget(len(data) + 1),
                                      _Past)
    stream.read(first)
    while stream.read(spreadsheetText._XML_READ_MAX):
        pass


@pytest.mark.parametrize('mark', [b'<![CDATA[', b'<!--', b'<?'])
def test_aMark_splitByTwoReads_isStillFound(mark):
    """The last bytes of a read are carried into the next: CDATA is
    refused, and a comment's or PI's '<' never ends its run."""
    run = spreadsheetText.SPREADSHEET_XML_RUN_MAX_BYTES
    body = b'a' if mark == b'<![CDATA[' else b'a<' * run
    data = b'<x>' + mark + body
    for split in range(1, len(mark)):
        with pytest.raises(_Past):
            _readIn(data, 3 + split)


def test_aRun_isCountedAcrossReads_fromItsLastTokenStart():
    run = spreadsheetText.SPREADSHEET_XML_RUN_MAX_BYTES
    _readIn(b'<x>' + b'a>' * (run // 2 - 1) + b'<y/>', 1000)
    with pytest.raises(_Past):
        _readIn(b'<x>' + b'a>' * (run // 2 + 1) + b'<y/>', 1000)
    # A comment's end starts a new run.
    _readIn(b'<!-- < -->' + b'a' * (run - 10) + b'<y/>', 7)


def test_commentsAndTheDeclaration_withAngleBrackets_readAsUsual():
    xml = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           b'<!-- a > b < c --><worksheet xmlns="' + _MAIN_NS.encode() +
           b'"><?mso-application progid="Excel.Sheet"?><sheetData>'
           b'<!-- <row> --><row r="1">' + number('A1', 7).encode() +
           b'</row></sheetData></worksheet>')

    assert toText(_withSheetXml(xml), XLSX_CONTENT_TYPE,
                  LABEL).text == 'Sheet: S\n7\n'


def test_aSheetNameLongerThanExcels_isShortened_soTheCutLineStaysShort():
    """The cut line repeats the sheet's name outside maxChars: a name past
    Excel's 31 characters (only a hand-made file has one) is cut to 31."""
    name = 'N' * 100_000
    data = sheetXlsx({name: '<row r="1">' + number('A1', 1) + '</row>'})

    sheet = toText(data, XLSX_CONTENT_TYPE, LABEL, maxChars=5)

    assert sheet.cutBy == 'chars'
    assert len(sheet.text) < 200
    assert toText(data, XLSX_CONTENT_TYPE, LABEL).text == (
        f'Sheet: {"N" * 31}\n1\n')
