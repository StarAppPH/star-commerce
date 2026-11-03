"""productImport.py — new products read from a spreadsheet the admin
attached (hand-written; chatForms' showNewProductsFromSpreadsheet calls it).

The model never writes the rows: it names the file, the sheet and which
column holds what, and this module reads the rows itself. The file is read
again from Storage (the caller's own, under its chat folder) and converted
in a child process (chatSpreadsheets.importText: the same slot and sandbox
as an attachment's text, a larger character limit, only the named sheet's
rows), then:

- the text is split into its sheets (sheetsOf) and each read with the csv
  module, its rows numbered as the spreadsheet shows them (Excel's row
  numbers: the headers and empty rows counted);
- the header row is the sheet's first row holding both the name and the
  price headers (case and spaces ignored), so title rows above it are
  passed over; the rows under it with a value are its product rows;
- each value is read as an admin writes it (_price, _stock): a price with a
  currency symbol or code before or after it and thousands separators, a
  stock count blank or "unlimited" (no stock count: hasInventory false),
  names and descriptions stripped, "" as None;
- each row is checked as addStoreProduct checks one
  (ProductActions._validNewProduct), and left out, with its reason, when it
  fails, repeats an earlier row's name, or has a name the store already
  has (ProductActions.splitNewNames).

One import shows at most PRODUCT_IMPORT_ROWS_MAX product rows (a range:
from firstRow, up to lastRow), PRODUCT_IMPORT_PHOTO_ROWS_MAX with a photo
column (each row's photo link is read when the form opens:
PhotoLinkActions); nextFirstRow says where the next one starts.
An import with no row to show is refused, saying why. Nothing is saved
here: the form's submit saves the rows (ProductActions.importStoreProducts).

A sheet name line in the converted text (spreadsheetText's "Sheet: <name>",
first or after an empty line) is taken as the start of a sheet: a sheet
whose own data has an empty row then a row starting "Sheet: " is split
there.
"""

import csv
import io
import re
import unicodedata
from typing import NamedTuple

from actions.ProductActions import _validNewProduct, splitNewNames
from objects import (PRODUCT_IMPORT_ROWS_MAX, PRODUCT_NAME_MAX_LENGTH,
                     productNameKey)
from sharedConstants import XLSX_CONTENT_TYPE
from spreadsheetText import CUT_PREFIX, sheetKey

# The words a stock cell says "no stock count" with (case ignored), besides
# an empty cell.
UNLIMITED_STOCK = frozenset({'unlimited', 'no limit', '∞'})
# The most headers and sheet names one result lists, and each one's length.
LISTED_MAX = 30
LISTED_LENGTH_MAX = 60
# The most left-out rows the model reads of an import, or of its submit
# (the form shows them all), and the most rows with a photo link that gave
# no photo.
AI_SKIPPED_MAX = 20
# The most product rows one import with a photo column shows: each row's
# link is read when the form opens, within the chat turn's time.
PRODUCT_IMPORT_PHOTO_ROWS_MAX = 50

NO_NAME = 'No name'
BAD_PRICE = 'The price is not a number'
BAD_STOCK = 'The stock is not a whole number (leave it empty for no stock count)'

# A price: one number, with at most a currency symbol (Unicode Sc) or a
# 3-letter code before and after it, and a sign.
_PRICE = re.compile(r'(?P<sign>-)?\s*(?P<before>[A-Za-z]{3}|[^\s\d.,\-])?'
                    r'\s*(?P<number>-?[\d.,]+)\s*'
                    r'(?P<after>[A-Za-z]{3}|[^\s\d.,\-])?')
_GROUPED = re.compile(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?')
_DOT_GROUPED = re.compile(r'-?\d{1,3}(?:\.\d{3}){2,}')


class Sheet(NamedTuple):
    name: str | None
    rows: list[list[str]]


class Mapping(NamedTuple):
    """Which header holds what (inventory, description and photo
    optional)."""
    name: str
    price: str
    inventory: str | None
    description: str | None
    photo: str | None = None


class Imported(NamedTuple):
    """An import's rows for the form, and what the form and the model are
    told about it. `photoLinks`: each draft's row number, photo link (its
    cell, stripped; None when empty) and name, with a photo column; else
    empty."""
    drafts: list[dict]
    summary: dict
    headers: list[str]
    sheets: list[str]
    photoLinks: list[tuple[int, str | None, str]] = []


def _listed(values) -> list[str]:
    return [value[:LISTED_LENGTH_MAX] for value in values][:LISTED_MAX]


def _quoted(values) -> str:
    return ', '.join(f'"{value}"' for value in _listed(values) if value)


def sheetsOf(text: str, contentType: str) -> list[Sheet]:
    """The converted text's sheets: a CSV's one (no name), an XLSX's by
    their "Sheet: " lines. A trailing [Cut: ...] line is dropped."""
    end = text.rfind('\n' + CUT_PREFIX)
    if end >= 0:
        text = text[:end + 1]
    elif text.startswith(CUT_PREFIX):
        text = ''
    if contentType != XLSX_CONTENT_TYPE:
        return [Sheet(None, list(csv.reader(io.StringIO(text))))]
    parts = re.split(r'(?:\A|(?<=\n)\n)Sheet: ([^\n]*)\n', text)
    # [before the first sheet, name, body, name, body, ...]
    return [Sheet(parts[i], list(csv.reader(io.StringIO(parts[i + 1]))))
            for i in range(1, len(parts), 2)]


def _filled(row: list[str]) -> bool:
    return any(cell.strip() for cell in row)


def _headerRow(sheet: Sheet, mapping: Mapping) -> int | None:
    """The index of the sheet's first row holding both the name and the
    price headers."""
    wanted = {sheetKey(mapping.name), sheetKey(mapping.price)}
    for index, row in enumerate(sheet.rows):
        if wanted <= {sheetKey(cell) for cell in row}:
            return index
    return None


def _column(headers: list[str], wanted: str | None, sheet: Sheet,
            what: str) -> int | None:
    if wanted is None:
        return None
    keys = [sheetKey(header) for header in headers]
    if sheetKey(wanted) in keys:
        return keys.index(sheetKey(wanted))
    where = f' of sheet "{sheet.name}"' if sheet.name is not None else ''
    raise ValueError(f'No column{where} is headed '
                     f'"{wanted[:LISTED_LENGTH_MAX]}" (the {what}). Its '
                     f'columns: {_quoted(headers)}')


def _isCurrency(text: str | None) -> bool:
    return text is None or text.isalpha() and len(text) == 3 or (
        len(text) == 1 and unicodedata.category(text) == 'Sc')


def _price(text: str) -> float | None:
    """A price as an admin writes it: "₱1,250.50", "PHP 99", "99 USD",
    "12,50", "1.250,50"; None when empty. Anything else (a time, a date,
    words, a second number, an exponent, parentheses) is a ValueError
    (BAD_PRICE)."""
    if not text.strip():
        return None
    match = _PRICE.fullmatch(text.strip())
    if (match is None or not _isCurrency(match['before'])
            or not _isCurrency(match['after'])
            or (match['sign'] and match['number'].startswith('-'))):
        raise ValueError(BAD_PRICE)
    kept = match['number']
    if ',' in kept and '.' in kept:
        if kept.rfind(',') > kept.rfind('.'):  # 1.250,50
            kept = kept.replace('.', '').replace(',', '.')
        else:  # 1,250.50
            kept = kept.replace(',', '')
    elif ',' in kept:
        kept = (kept.replace(',', '') if _GROUPED.fullmatch(kept)
                else kept.replace(',', '.'))
    elif _DOT_GROUPED.fullmatch(kept):  # 1.250.000
        kept = kept.replace('.', '')
    try:
        value = float(kept)
    except ValueError:
        raise ValueError(BAD_PRICE) from None
    return -value if match['sign'] else value


def _stock(text: str) -> tuple[int | None, bool]:
    """(inventory, hasInventory) from a stock cell: empty or "unlimited"
    is no stock count (None, False), else a whole number ("1,200", "12.0"
    too). ValueError (BAD_STOCK) otherwise."""
    value = text.strip()
    if not value or value.casefold() in UNLIMITED_STOCK:
        return None, False
    value = value.replace(',', '').replace(' ', '')
    try:
        number = float(value)
    except ValueError:
        raise ValueError(BAD_STOCK) from None
    if not number.is_integer():
        raise ValueError(BAD_STOCK)
    return int(number), True


def _cell(row: list[str], column: int | None) -> str:
    if column is None or column >= len(row):
        return ''
    return row[column]


def _draft(row: list[str], columns: dict) -> dict:
    """A row as a new product's fields, checked (_validNewProduct): a
    ValueError says why it cannot be one."""
    name = _cell(row, columns['name']).strip()
    if not name:
        raise ValueError(NO_NAME)
    price = _price(_cell(row, columns['price']))
    if columns['inventory'] is None:
        inventory, hasInventory = None, False
    else:
        inventory, hasInventory = _stock(_cell(row, columns['inventory']))
    fields = _validNewProduct({
        'name': name, 'price': price, 'inventory': inventory,
        'hasInventory': hasInventory,
        'description': _cell(row, columns['description']).strip() or None})
    return {'name': fields['name'], 'price': fields['price'],
            'inventory': fields['inventory'] if hasInventory else None,
            'hasInventory': hasInventory,
            'description': fields['description']}


def _pickSheet(sheets: list[Sheet], contentType: str, sheetName: str | None,
               mapping: Mapping) -> Sheet:
    names = [sheet.name for sheet in sheets if sheet.name is not None]
    if sheetName is not None and contentType == XLSX_CONTENT_TYPE:
        found = [sheet for sheet in sheets
                 if sheetKey(sheet.name) == sheetKey(sheetName)]
        if not found:
            raise ValueError(f'No sheet is named '
                             f'"{sheetName[:LISTED_LENGTH_MAX]}". Its '
                             f'sheets: {_quoted(names)}')
        return found[0]
    if not any(_filled(row) for sheet in sheets for row in sheet.rows):
        raise ValueError('The spreadsheet has no rows to import')
    # The first sheet with the headers; else the first with rows, whose
    # missing headers the refusal names.
    return next((sheet for sheet in sheets
                 if _headerRow(sheet, mapping) is not None),
                next(sheet for sheet in sheets
                     if any(_filled(row) for row in sheet.rows)))


def importRows(text: str, contentType: str, notFullyRead: bool,
               fileName: str, storeId: str, mapping: Mapping,
               sheetName: str | None, firstRow: int | None,
               lastRow: int | None) -> Imported:
    """The rows of the import (see the module docstring). A ValueError
    (no such sheet, no such column, a range that is not one, no row to
    show) is the model's to fix or relay: its message names what the sheet
    has. `notFullyRead`: the converted text was cut."""
    sheets = sheetsOf(text, contentType)
    sheet = _pickSheet(sheets, contentType, sheetName, mapping)
    where = f' of sheet "{sheet.name}"' if sheet.name is not None else ''
    header = _headerRow(sheet, mapping)
    if header is None:
        first = next((row for row in sheet.rows if _filled(row)), [])
        raise ValueError(
            f'No row{where} holds both the "{mapping.name[:LISTED_LENGTH_MAX]}'
            f'" and "{mapping.price[:LISTED_LENGTH_MAX]}" headers. Its first '
            f'row: {_quoted(cell.strip() for cell in first)}')
    headers = [cell.strip() for cell in sheet.rows[header]]
    columns = {
        'name': _column(headers, mapping.name, sheet, 'name'),
        'price': _column(headers, mapping.price, sheet, 'price'),
        'inventory': _column(headers, mapping.inventory, sheet, 'stock'),
        'description': _column(headers, mapping.description, sheet,
                               'description'),
        'photo': _column(headers, mapping.photo, sheet, 'photo links'),
    }
    if firstRow is not None and firstRow < 1:
        raise ValueError('firstRow is a row number of the sheet, 1 or more')
    if lastRow is not None and lastRow < (firstRow or 1):
        raise ValueError('lastRow is firstRow or more')
    # Numbered as the spreadsheet shows them: the header row is header + 1.
    products = [(index + 1, row) for index, row in enumerate(sheet.rows)
                if index > header and _filled(row)]
    first = max(firstRow or 0, header + 2)
    inRange = [(number, row) for number, row in products if number >= first
               and (lastRow is None or number <= lastRow)]
    shown = inRange[:PRODUCT_IMPORT_PHOTO_ROWS_MAX if mapping.photo
                    else PRODUCT_IMPORT_ROWS_MAX]
    if not shown:
        span = (f' (its product rows: {products[0][0]} to {products[-1][0]})'
                if products else '')
        raise ValueError(f'The sheet{where} has no product rows from row '
                         f'{first}' + (f' to {lastRow}' if lastRow else '')
                         + span)
    last = shown[-1][0]
    drafts, skipped, rows = {}, [], []
    rowsByNumber = dict(shown)
    for number, row in shown:
        try:
            drafts[number] = _draft(row, columns)
        except ValueError as error:
            name = _cell(row, columns['name']).strip()
            skipped.append({'row': number,
                            'name': name[:PRODUCT_NAME_MAX_LENGTH] or None,
                            'reason': str(error)})
            continue
        rows.append({'position': number,
                     'nameKey': productNameKey(drafts[number]['name'])})
    split = splitNewNames(storeId, rows)
    skipped += [{'row': row['position'],
                 'name': drafts[row['position']]['name'],
                 'reason': row['reason']} for row in split.skipped]
    skipped.sort(key=lambda row: row['row'])
    kept = [drafts[row['position']] for row in split.kept]
    links = [(row['position'],
              _cell(rowsByNumber[row['position']], columns['photo']).strip()
              or None, drafts[row['position']]['name'])
             for row in split.kept] if mapping.photo else []
    if not kept:
        raise ValueError(
            f'None of rows {first} to {last}{where} can be added: '
            + '; '.join(f'row {row["row"]}: {row["reason"]}'
                        for row in skipped[:AI_SKIPPED_MAX]))
    later = next((number for number, _ in products if number > last), None)
    summary = {
        'fileName': fileName,
        'sheet': sheet.name,
        'firstRow': first,
        'lastRow': last,
        'totalRows': len(products),
        'nextFirstRow': later,
        # Only a cut in this sheet (the last the text holds) matters.
        'notFullyRead': notFullyRead and sheet is sheets[-1],
        'skippedRows': skipped,
    }
    names = [one.name for one in sheets if one.name is not None]
    return Imported(kept, summary, _listed(headers), _listed(names), links)


def aiResult(imported: Imported, note: str) -> dict:
    """What the model reads of an import: counts, reasons and the sheet's
    headers, never the rows."""
    summary = imported.summary
    skipped = summary['skippedRows']
    photos = summary.get('photoLinks')
    told = {} if photos is None else {
        'photosFetched': photos['fetched'],
        'photosNotFetched': len(photos['notFetched']),
        'photosNotFetchedRows': [
            {'row': row['row'], 'reason': row['reason']}
            for row in photos['notFetched'][:AI_SKIPPED_MAX]],
    }
    return {
        'result': note,
        'fileName': summary['fileName'],
        'sheet': summary['sheet'],
        'sheets': imported.sheets,
        'headers': imported.headers,
        'rowsOnForm': len(imported.drafts),
        'firstRow': summary['firstRow'],
        'lastRow': summary['lastRow'],
        'totalRows': summary['totalRows'],
        'nextFirstRow': summary['nextFirstRow'],
        'notFullyRead': summary['notFullyRead'],
        'skipped': len(skipped),
        'skippedRows': [{'row': row['row'], 'reason': row['reason']}
                        for row in skipped[:AI_SKIPPED_MAX]],
        **told,
    }

