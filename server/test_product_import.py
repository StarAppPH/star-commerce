"""Products imported from a spreadsheet the admin attached in the chat
(showNewProductsFromSpreadsheet, productImport.py,
ProductActions.importStoreProducts).

The model names only the file and its columns: the server reads the rows
again from Storage (through the spreadsheet sandbox), opens the new products
form with them, and saves the submitted rows itself, so no model call ever
carries the rows. No real Meta or Storage call: the counting FakeBucket and
the FakeModel of the chat attachment suite play them. Rows are numbered as
the spreadsheet numbers them: the header row is row 1 here, unless a test
says otherwise."""

import json

import openai
import pytest
from pymongo.errors import BulkWriteError

import chatForms
import chatSpreadsheets
import productImport
import spreadsheetSandbox
from actions import ChatActions, ProductActions
from actions.ProductActions import (IMPORT_DUPLICATE, IMPORT_NAME_TAKEN,
                                    IMPORT_STORE_FULL, importStoreProducts)
from conftest import addProduct, createTestStore
from mongoDb import db
from objects import PRODUCT_BATCH_MAX, PRODUCT_IMPORT_ROWS_MAX
import sharedConstants
# Fixtures (the autouse ones apply here too) and helpers of the chat and
# chat attachment suites.
from test_chat import (ADMIN, OTHER_ADMIN, calls, clean, clock,  # noqa
                       freezeNow, frozenNow, model, offline, reply, sdkError)
from test_chat_attachments import (XLSX_TYPE, mine, sendAdmin,  # noqa
                                   storage, urlOf)
from test_chat_components import formAnswerDetails
from test_spreadsheet_text import inline, number, sheetXlsx

TOOL = 'showNewProductsFromSpreadsheet'


# ----------------------------- helpers ----------------------------- #


def csvOf(rows: list[list]) -> bytes:
    return ('\r\n'.join(','.join(str(cell) for cell in row) for row in rows)
            + '\r\n').encode('utf-8')


def mapping(fileName='catalog.csv', **columns) -> str:
    return json.dumps({'fileName': fileName, 'nameColumn': 'Name',
                       'priceColumn': 'Price', **columns})


def openImport(storage, model, data: bytes, arguments: str,
               name='catalog.csv', contentType=None, uid=ADMIN):
    """The admin attaches `data` and the model calls the import with
    `arguments`: (the turn's response, the FakeModel)."""
    storage.put(mine(uid, name), data, contentType)
    fake = model(calls(('c1', TOOL, arguments)), reply('Check the form.'))
    res = sendAdmin('Add the products in this file',
                    [urlOf(mine(uid, name))])
    return res, fake


def toolMessages(fake) -> list[dict]:
    """What the model read of each tool call of the turn."""
    return [json.loads(m['content']) for m in fake.requests[-1]['messages']
            if m['role'] == 'tool']


def toolMessage(fake) -> dict:
    [message] = toolMessages(fake)
    return message


def refusal(fake) -> str:
    return toolMessage(fake)['error']


def submit(res: dict, form: dict, products=None) -> dict:
    return sendAdmin(None, conversationId=res['conversationId'], form={
        'kind': 'newProducts', 'formId': form['formId'],
        'products': form['products'] if products is None else products})


def storedProducts(storeId: str) -> dict:
    return {doc['name']: doc for doc in db.read({'storeId': storeId},
                                                'Products')}


def everyToolArgument(*fakes) -> str:
    """Every tool call's arguments in every request the model got."""
    return '\n'.join(
        call['function']['arguments']
        for fake in fakes for request in fake.requests
        for message in request['messages']
        if message['role'] == 'assistant'
        for call in message.get('tool_calls') or [])


def answerContent(fake) -> str:
    return [m for m in fake.requests[0]['messages']
            if m['role'] == 'user'][-1]['content']


def formAnswer(fake) -> dict:
    return formAnswerDetails(answerContent(fake))


HUNDRED = csvOf([['Name', 'Price', 'Stock', 'Description']] + [
    [f'Item {n:03}', n * 10, n, f'Note {n:03}'] for n in range(1, 101)])


# ===================== the whole flow, through the route ===================== #


def test_a100RowCsv_isImported_withTheModelSendingOnlyTheMapping(
        storage, model, monkeypatch):
    store = createTestStore(ADMIN)
    batches = []
    realCreateMany = db.createMany

    def recordingCreateMany(documents, *args, **kwargs):
        batches.append(len(documents))
        return realCreateMany(documents, *args, **kwargs)
    monkeypatch.setattr(db, 'createMany', recordingCreateMany)

    res, opened = openImport(storage, model, HUNDRED, mapping(
        inventoryColumn='stock', descriptionColumn='DESCRIPTION'))

    [form] = res['components']
    assert form['kind'] == 'newProductsForm'
    assert len(form['products']) == 100
    assert form['products'][36] == {
        'name': 'Item 037', 'price': 370.0, 'inventory': 37,
        'hasInventory': True, 'description': 'Note 037'}
    # Rows as the spreadsheet numbers them: the header is row 1.
    assert form['spreadsheetImport'] == {
        'fileName': 'catalog.csv', 'sheet': None, 'firstRow': 2,
        'lastRow': 101, 'totalRows': 100, 'nextFirstRow': None,
        'notFullyRead': False, 'skippedRows': []}
    told = toolMessage(opened)
    assert told['rowsOnForm'] == 100
    assert told['skipped'] == 0
    assert told['headers'] == ['Name', 'Price', 'Stock', 'Description']
    assert 'Item 0' not in json.dumps(told)
    assert db.read({'storeId': store['_id']}, 'Products') == []  # not yet

    saved = model(reply('All added.'))
    submit(res, form)

    products = storedProducts(store['_id'])
    assert len(products) == 100
    assert products['Item 100']['price'] == 1000
    assert products['Item 100']['inventory'] == 100
    assert products['Item 100']['description'] == 'Note 100'
    assert products['Item 100']['importFormId'] == form['formId']
    # Two batches of at most PRODUCT_BATCH_MAX.
    assert batches == [PRODUCT_BATCH_MAX, 100 - PRODUCT_BATCH_MAX]
    assert answerContent(saved).startswith(
        'Added 100 products from catalog.csv.')
    details = formAnswer(saved)
    assert details == {'kind': 'newProductsSaved', 'added': 100,
                       'notAdded': 0, 'notAddedRows': [],
                       'nextFirstRow': None, 'photosOn': 0,
                       'photosNotOn': 0}
    # No model call ever carries the rows: not the tool calls, not the
    # answer the model reads.
    assert 'Item 0' not in everyToolArgument(opened, saved)
    assert 'Item 0' not in answerContent(saved)
    assert 'Note 0' not in answerContent(saved)


def test_theImportedProducts_neverSendTheirFormId(storage, model):
    store = createTestStore(ADMIN)
    res, _ = openImport(storage, model, csvOf([['Name', 'Price'],
                                               ['Mug', 2]]), mapping())
    [form] = res['components']
    model(reply('Done.'))
    submit(res, form)
    assert storedProducts(store['_id'])['Mug']['importFormId']

    listed = ProductActions.listMyStoreProducts(
        ProductActions.AdminProductListResponse, ADMIN)
    assert 'importFormId' not in json.dumps(listed)


def test_anXlsx_readsTheSheetTheModelNames(storage, model):
    store = createTestStore(ADMIN)
    book = sheetXlsx({
        'Notes': '<row r="1">' + inline('A1', 'Read me') + '</row>',
        'Products': (
            '<row r="1">' + inline('A1', 'Product') + inline('B1', 'Cost') +
            inline('C1', 'Qty') + '</row>'
            '<row r="2">' + inline('A2', 'Mug') + number('B2', 250) +
            number('C2', 3) + '</row>'
            '<row r="3">' + inline('A3', 'Plate') + number('B3', 90.5) +
            '</row>')})

    res, opened = openImport(storage, model, book, json.dumps({
        'fileName': 'Book.XLSX', 'sheet': 'products',
        'nameColumn': 'Product', 'priceColumn': 'Cost',
        'inventoryColumn': 'Qty'}), name='book.xlsx', contentType=XLSX_TYPE)

    [form] = res['components']
    assert form['products'] == [
        {'name': 'Mug', 'price': 250.0, 'inventory': 3, 'hasInventory': True,
         'description': None},
        {'name': 'Plate', 'price': 90.5, 'inventory': None,
         'hasInventory': False, 'description': None}]
    assert form['spreadsheetImport']['sheet'] == 'Products'
    assert toolMessage(opened)['sheets'] == ['Notes', 'Products']

    model(reply('Done.'))
    submit(res, form)
    products = storedProducts(store['_id'])
    assert products['Mug']['inventory'] == 3
    assert products['Plate']['hasInventory'] is False


def test_aNamedSheet_isTheOnlyOneConverted_soABigFirstSheetLeavesItWhole(
        storage, model, monkeypatch):
    """Only the named sheet's rows are read: a first sheet far longer than
    the import's text limit takes none of it."""
    createTestStore(ADMIN)
    monkeypatch.setattr(chatSpreadsheets, 'IMPORT_TEXT_MAX_CHARS', 2000)
    big = ''.join(f'<row r="{n}">' + inline(f'A{n}', 'x' * 50) + '</row>'
                  for n in range(1, 200))
    book = sheetXlsx({'Big': big, 'Products': (
        '<row r="1">' + inline('A1', 'Name') + inline('B1', 'Price') +
        '</row><row r="2">' + inline('A2', 'Mug') + number('B2', 2) +
        '</row>')})
    sheets = []
    real = spreadsheetSandbox.convert

    def recording(*args, **kwargs):
        sheets.append(args[5] if len(args) > 5 else kwargs.get('sheet'))
        return real(*args, **kwargs)
    monkeypatch.setattr(spreadsheetSandbox, 'convert', recording)

    res, opened = openImport(storage, model, book, mapping(
        fileName='book.xlsx', sheet='Products'), name='book.xlsx',
        contentType=XLSX_TYPE)

    [form] = res['components']
    assert [p['name'] for p in form['products']] == ['Mug']
    assert form['spreadsheetImport']['notFullyRead'] is False
    assert toolMessage(opened)['sheets'] == ['Big', 'Products']
    # The attachment's conversion (no sheet), then the import's.
    assert sheets == [None, 'Products']


def test_notFullyRead_isSetOnlyWhenTheImportedSheetWasCut(storage, model,
                                                          monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(chatSpreadsheets, 'IMPORT_TEXT_MAX_CHARS', 200)
    data = csvOf([['Name', 'Price']] + [[f'Product {n}', n]
                                        for n in range(1, 40)])

    res, opened = openImport(storage, model, data, mapping())

    [form] = res['components']
    summary = form['spreadsheetImport']
    assert summary['notFullyRead'] is True
    assert 0 < summary['totalRows'] < 39
    assert toolMessage(opened)['notFullyRead'] is True


def test_titleRowsAboveTheHeaders_arePassedOver(storage, model):
    createTestStore(ADMIN)
    data = csvOf([['Price list 2026', ''], ['', ''], ['Name', 'Price'],
                  ['Mug', 2], ['', ''], ['Plate', 'free']])

    res, _ = openImport(storage, model, data, mapping())

    [form] = res['components']
    assert [p['name'] for p in form['products']] == ['Mug']
    summary = form['spreadsheetImport']
    # The header is row 3; the empty row 5 counts but is no product row.
    assert (summary['firstRow'], summary['lastRow'],
            summary['totalRows']) == (4, 6, 2)
    assert summary['skippedRows'] == [
        {'row': 6, 'name': 'Plate', 'reason': productImport.BAD_PRICE}]


def test_messyPricesAndUnlimitedStock_areReadAsAnAdminWritesThem(storage,
                                                                 model):
    createTestStore(ADMIN)
    data = ('Name,Price,Stock\r\n'
            'Mug,"₱1,250.50",12\r\n'
            'Plate,PHP 99,Unlimited\r\n'
            'Bowl,"12,50",\r\n'
            'Cup,"1.250,75","1,200"\r\n'
            'Jar, $ 5 ,no limit\r\n'
            'Pot,09:30:00,1\r\n').encode('utf-8')

    res, _ = openImport(storage, model, data,
                        mapping(inventoryColumn='Stock'))

    [form] = res['components']
    assert [(p['name'], p['price'], p['inventory'], p['hasInventory'])
            for p in form['products']] == [
        ('Mug', 1250.5, 12, True),
        ('Plate', 99.0, None, False),
        ('Bowl', 12.5, None, False),
        ('Cup', 1250.75, 1200, True),
        ('Jar', 5.0, None, False)]
    assert form['spreadsheetImport']['skippedRows'] == [
        {'row': 7, 'name': 'Pot', 'reason': productImport.BAD_PRICE}]


def test_noStockColumn_makesEveryRowUnlimited(storage, model):
    store = createTestStore(ADMIN)
    res, _ = openImport(storage, model, csvOf([['Name', 'Price'],
                                               ['Mug', 2], ['Plate', 3]]),
                        mapping())

    [form] = res['components']
    assert {p['hasInventory'] for p in form['products']} == {False}
    model(reply('Done.'))
    submit(res, form)
    assert {p['hasInventory'] for p in
            storedProducts(store['_id']).values()} == {False}


def test_blankColumnsAndSheet_areLeftOut(storage, model):
    """"" for the sheet, the stock or the description column is one left
    out, never a column headed ""."""
    createTestStore(ADMIN)
    res, opened = openImport(storage, model, csvOf(
        [['Name', 'Price', 'Stock'], ['Mug', 2, 5]]),
        mapping(sheet='', inventoryColumn='', descriptionColumn='  '))

    [form] = res['components']
    assert form['products'] == [{'name': 'Mug', 'price': 2.0,
                                 'inventory': None, 'hasInventory': False,
                                 'description': None}]


def test_rowsThatCannotBeProducts_areLeftOut_withTheirReasons(storage,
                                                              model):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Old Mug', 100)
    longName = 'L' * 300
    data = csvOf([
        ['Name', 'Price', 'Stock'],
        ['Mug', 10, 1],  # row 2
        ['', 5, 1],  # row 3: no name
        ['Plate', 'free', 1],  # row 4: no number
        ['  mug ', 12, 1],  # row 5: a repeat of row 2
        ['old mug', 9, 1],  # row 6: already in the store
        ['Cup', 3, 'some'],  # row 7: stock not a number
        ['Bowl', -1, 1],  # row 8: a negative price
        ['Vase', 4, -2],  # row 9: a negative stock
        [longName, 4, 2],  # row 10: a name too long
        ['Jar', 4, 2],  # row 11
    ])

    res, opened = openImport(storage, model, data,
                             mapping(inventoryColumn='Stock'))

    [form] = res['components']
    assert [p['name'] for p in form['products']] == ['Mug', 'Jar']
    skipped = form['spreadsheetImport']['skippedRows']
    assert [(row['row'], row['name']) for row in skipped] == [
        (3, None), (4, 'Plate'), (5, 'mug'), (6, 'old mug'), (7, 'Cup'),
        (8, 'Bowl'), (9, 'Vase'), (10, 'L' * 80)]
    reasons = {row['row']: row['reason'] for row in skipped}
    assert reasons[3] == productImport.NO_NAME
    assert reasons[4] == productImport.BAD_PRICE
    assert reasons[5] == f'{IMPORT_DUPLICATE} (row 2)'
    assert reasons[6] == IMPORT_NAME_TAKEN
    assert reasons[7] == productImport.BAD_STOCK
    assert reasons[8] == 'The price is a number from 0 to 10000000'
    assert reasons[9] == ('The inventory is a whole number from 0 to '
                          '1000000')
    assert reasons[10].startswith('The name')
    told = toolMessage(opened)
    assert told['skipped'] == 8
    # The model reads the rows' numbers and reasons, never their names.
    assert told['skippedRows'][1] == {'row': 4,
                                      'reason': productImport.BAD_PRICE}
    assert 'Plate' not in json.dumps(told)


def test_aSubmittedNameTheStoreAlreadyHas_isSkipped_theOthersSaved(storage,
                                                                   model):
    """A product added between the form opening and its submit."""
    store = createTestStore(ADMIN)
    res, _ = openImport(storage, model, csvOf(
        [['Name', 'Price'], ['Mug', 2], ['Plate', 3], ['Cup', 4]]),
        mapping())
    [form] = res['components']
    addProduct(store['_id'], 'PLATE', 1)
    saved = model(reply('Done.'))

    submit(res, form)

    assert sorted(storedProducts(store['_id'])) == ['Cup', 'Mug', 'PLATE']
    assert answerContent(saved).startswith(
        'Added 2 products from catalog.csv; 1 not added.')
    assert formAnswer(saved)['notAddedRows'] == [{
        'formRow': 2, 'name': 'Plate', 'reason': IMPORT_NAME_TAKEN}]


def test_aResubmitAfterAFailedTurn_countsWhatTheFormAddedAsAdded(storage,
                                                                 model):
    """The products are saved before the model is called: when that call
    fails, the turn is not saved and the form can be sent again, which
    counts the rows it already added as added, never as taken."""
    store = createTestStore(ADMIN)
    res, _ = openImport(storage, model, csvOf(
        [['Name', 'Price'], ['Mug', 2], ['Plate', 3]]), mapping())
    [form] = res['components']
    model(*[sdkError(openai.BadRequestError, 400)] * 3)
    with pytest.raises(Exception):
        submit(res, form)
    assert sorted(storedProducts(store['_id'])) == ['Mug', 'Plate']
    addProduct(store['_id'], 'Cup', 1)  # not this form's
    saved = model(reply('Done.'))

    submit(res, form, form['products'] + [{'name': 'cup', 'price': 1,
                                           'hasInventory': False}])

    assert answerContent(saved).startswith(
        'Added 2 products from catalog.csv; 1 not added.')
    assert formAnswer(saved)['notAddedRows'] == [{
        'formRow': 3, 'name': 'cup', 'reason': IMPORT_NAME_TAKEN}]
    assert len(storedProducts(store['_id'])) == 3


def test_aSheetOverTheRowLimit_showsOneRange_andSaysWhereTheNextStarts(
        storage, model):
    createTestStore(ADMIN)
    total = PRODUCT_IMPORT_ROWS_MAX + 50
    data = csvOf([['Name', 'Price']] + [[f'P{n}', n]
                                        for n in range(1, total + 1)])

    res, opened = openImport(storage, model, data, mapping())

    [form] = res['components']
    assert len(form['products']) == PRODUCT_IMPORT_ROWS_MAX
    summary = form['spreadsheetImport']
    nextRow = PRODUCT_IMPORT_ROWS_MAX + 2
    assert (summary['firstRow'], summary['lastRow'], summary['totalRows'],
            summary['nextFirstRow']) == (
        2, PRODUCT_IMPORT_ROWS_MAX + 1, total, nextRow)
    assert toolMessage(opened)['nextFirstRow'] == nextRow

    # The next range.
    fake = model(calls(('c2', TOOL, mapping(firstRow=nextRow))),
                 reply('The rest.'))
    nextRes = sendAdmin('The rest please',
                        conversationId=res['conversationId'])
    [nextForm] = nextRes['components']
    assert [p['name'] for p in nextForm['products']][:2] == [
        f'P{PRODUCT_IMPORT_ROWS_MAX + 1}', f'P{PRODUCT_IMPORT_ROWS_MAX + 2}']
    assert len(nextForm['products']) == 50
    assert nextForm['spreadsheetImport']['nextFirstRow'] is None
    assert len(fake.requests) == 2


def test_lastRow_endsTheRange_andTheNextRowIsNamed(storage, model):
    createTestStore(ADMIN)
    data = csvOf([['Name', 'Price']] + [[f'P{n}', n] for n in range(1, 11)])

    res, _ = openImport(storage, model, data,
                        mapping(firstRow=4, lastRow=6))

    [form] = res['components']
    assert [p['name'] for p in form['products']] == ['P3', 'P4', 'P5']
    summary = form['spreadsheetImport']
    assert (summary['firstRow'], summary['lastRow'],
            summary['nextFirstRow']) == (4, 6, 7)


@pytest.mark.parametrize('rows, arguments, error', [
    ([['Mug', 2]], mapping(firstRow=10),
     'The sheet has no product rows from row 10 (its product rows: 2 to 2)'),
    ([['', 2], ['Plate', 'free']], mapping(),
     'None of rows 2 to 3 can be added: row 2: No name; row 3: The price is '
     'not a number'),
])
def test_anImportWithNoRowToShow_isRefused_sayingWhy(storage, model, rows,
                                                     arguments, error):
    createTestStore(ADMIN)
    res, opened = openImport(storage, model,
                             csvOf([['Name', 'Price']] + rows), arguments)
    assert res['components'] == []
    assert refusal(opened) == error


def test_theImport_runsOncePerTurn(storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'catalog.csv'), HUNDRED)
    fake = model(calls(('c1', TOOL, mapping()), ('c2', TOOL, mapping())),
                 reply('One form.'))

    res = sendAdmin('Import', [urlOf(mine(ADMIN, 'catalog.csv'))])

    assert len(res['components']) == 1
    first, second = toolMessages(fake)
    assert first['rowsOnForm'] == 100
    assert second == {'error': chatForms.ONCE_PER_TURN.format(name=TOOL)}


def test_theFilesBytes_areGivenBack_assoonAsItIsConverted(storage, model):
    """The import's own read is held in the process's bytes in flight only
    while it is converted: by the next model call, only the message's own
    attachment is."""
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'catalog.csv'), HUNDRED)
    fake = model(calls(('c1', TOOL, mapping())), reply('Check it.'))
    held = []
    fake.onCall = lambda index: held.append(ChatActions._bytesInFlight)

    sendAdmin('Import', [urlOf(mine(ADMIN, 'catalog.csv'))])

    assert held == [len(HUNDRED), len(HUNDRED)]


def test_aBusyProcess_isAToolError_notA503(storage, model, monkeypatch):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'catalog.csv'), HUNDRED)
    monkeypatch.setattr(ChatActions, 'CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX',
                        len(HUNDRED) + 10)
    fake = model(calls(('c1', TOOL, mapping())), reply('Try later.'))

    res = sendAdmin('Import', [urlOf(mine(ADMIN, 'catalog.csv'))])

    assert res['components'] == []
    assert refusal(fake) == chatForms.IMPORT_BUSY


def test_anotherUsersFile_isNeverRead(storage, model):
    """A file in another user's folder cannot be attached, and the import
    reads only the files of the caller's own conversation."""
    createTestStore(ADMIN)
    storage.put(mine(OTHER_ADMIN, 'catalog.csv'), HUNDRED)
    fake = model(calls(('c1', TOOL, mapping())), reply('No file.'))

    with pytest.raises(ValueError):
        sendAdmin('Import', [urlOf(mine(OTHER_ADMIN, 'catalog.csv'))])
    assert storage.downloads == []

    res = sendAdmin('Import catalog.csv')

    assert res['components'] == []
    assert refusal(fake) == chatForms.IMPORT_NO_SPREADSHEET
    assert storage.downloads == []


def test_aFileRefusedOnRead_isAToolError(storage, model, monkeypatch):
    """The file is read again as the caller's own: a path that is not
    theirs (FormContext.readSheet refuses it) reaches the model as an
    error, nothing opened."""
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'catalog.csv'), HUNDRED)
    real = chatForms._conversationSheets

    def swapped(context):
        return [{**sheet, 'path': mine(OTHER_ADMIN, 'catalog.csv')}
                for sheet in real(context)]
    monkeypatch.setattr(chatForms, '_conversationSheets', swapped)
    storage.put(mine(OTHER_ADMIN, 'catalog.csv'), HUNDRED)
    fake = model(calls(('c1', TOOL, mapping())), reply('Could not.'))

    res = sendAdmin('Import', [urlOf(mine(ADMIN, 'catalog.csv'))])

    assert res['components'] == []
    assert refusal(fake)
    assert mine(OTHER_ADMIN, 'catalog.csv') not in storage.downloads


def test_aFileFromAnEarlierTurn_isFound_byItsName(storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'catalog.csv'), HUNDRED)
    model(reply('Got it.'))
    first = sendAdmin('Here is my list', [urlOf(mine(ADMIN, 'catalog.csv'))])
    model(calls(('c1', TOOL, mapping(inventoryColumn='Stock'))),
          reply('Check it.'))

    res = sendAdmin('Add them', conversationId=first['conversationId'])

    [form] = res['components']
    assert len(form['products']) == 100


@pytest.mark.parametrize('arguments, error', [
    (mapping(fileName='nope.csv'),
     'No spreadsheet named "nope.csv" was attached in this chat. Its '
     'spreadsheets: "catalog.csv"'),
    (mapping(priceColumn='Cost'),
     'No row holds both the "Name" and "Cost" headers. Its first row: '
     '"Name", "Price", "Stock", "Description"'),
    (mapping(inventoryColumn='Qty'),
     'No column is headed "Qty" (the stock). Its columns: "Name", '
     '"Price", "Stock", "Description"'),
    (mapping(firstRow=0),
     'firstRow is a row number of the sheet, 1 or more'),
    (mapping(firstRow=5, lastRow=3), 'lastRow is firstRow or more'),
])
def test_aMappingThatDoesNotFit_isAToolErrorTheModelCanFix(storage, model,
                                                           arguments, error):
    createTestStore(ADMIN)
    res, opened = openImport(storage, model, HUNDRED, arguments)
    assert res['components'] == []
    assert refusal(opened) == error


def test_aPlainNewProductsForm_stillRefusesMoreThanABatch(storage, model):
    """Only a form from a spreadsheet takes more than PRODUCT_BATCH_MAX
    rows: the model saves a plain form's rows itself."""
    createTestStore(ADMIN)
    model(calls(('c1', 'showNewProductsForm', '{}')), reply('Fill it in.'))
    res = sendAdmin('Add products')
    [form] = res['components']
    fake = model()

    with pytest.raises(ValueError):
        submit(res, form, [{'name': f'P{n}', 'price': 1, 'inventory': 1}
                           for n in range(PRODUCT_BATCH_MAX + 1)])
    assert fake.requests == []


def test_theImportTool_isTheAdminsOnly_andChatOnly():
    assert chatForms.FORM_TOOLS[TOOL].toolsets == ('admin', )
    content, form = chatForms.runFormTool('admin', TOOL, mapping(), ADMIN,
                                          'store')
    assert form is None
    assert json.loads(content) == {'error': f'{TOOL} works in the chat only'}


# ============================ photos ============================ #


def test_thePhotosAddedOnTheForm_arePutOnByTheServer_theModelGetsCounts(
        storage, model, monkeypatch):
    store = createTestStore(ADMIN)
    rows = [['Name', 'Price']] + [[f'P{n}', n] for n in range(1, 61)]
    res, _ = openImport(storage, model, csvOf(rows), mapping())
    [form] = res['components']
    photo = urlOf(f'users/{ADMIN}/chat-photos/mug.jpg')
    theirs = urlOf(f'users/{OTHER_ADMIN}/chat-photos/mug.jpg')
    sent = [{**row, 'imageUrl': photo} for row in form['products']]
    calls_ = []

    def putOn(model_, userId, products):
        calls_.append(products)
        return {'products': products[1:], 'notSaved': products[:1]}
    monkeypatch.setattr(chatForms, 'setUploadedProductPhotos', putOn)
    refused = model()

    with pytest.raises(ValueError, match='^The photo of product 1 must be a '
                       'photo you uploaded$'):
        submit(res, form, [{**sent[0], 'imageUrl': theirs}] + sent[1:])
    assert refused.requests == []
    assert db.read({'storeId': store['_id']}, 'Products') == []

    saved = model(reply('Added with photos.'))
    submit(res, form, sent)

    stored = storedProducts(store['_id'])
    # At most a batch per call, each product with its own photo.
    assert [len(chunk) for chunk in calls_] == [PRODUCT_BATCH_MAX, 10]
    assert calls_[0][0] == {'productId': stored['P1']['_id'],
                            'imageUrl': photo}
    assert answerContent(saved).startswith(
        'Added 60 products from catalog.csv; 58 photos put on, 2 not.')
    details = formAnswer(saved)
    assert (details['photosOn'], details['photosNotOn']) == (58, 2)
    # The model never gets a photo's URL.
    assert 'chat-photos' not in answerContent(saved)


def test_aPhotoCallThatRefuses_countsItsPhotosAsNotOn(storage, model,
                                                     monkeypatch):
    createTestStore(ADMIN)
    res, _ = openImport(storage, model, csvOf([['Name', 'Price'],
                                               ['Mug', 2]]), mapping())
    [form] = res['components']

    def refusing(*args):
        raise ValueError('Today\'s photo limit is reached')
    monkeypatch.setattr(chatForms, 'setUploadedProductPhotos', refusing)
    saved = model(reply('Added.'))

    submit(res, form, [{**form['products'][0], 'imageUrl': urlOf(
        f'users/{ADMIN}/chat-photos/mug.jpg')}])

    assert answerContent(saved).startswith(
        'Added 1 product from catalog.csv; 0 photos put on, 1 not.')


# ====================== importStoreProducts ====================== #


def newRows(*names) -> list[dict]:
    return [{'name': name, 'price': 1, 'inventory': 1} for name in names]


def test_importStoreProducts_leavesOutWhatPassesTheStoresCap(clean,
                                                             monkeypatch):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Old', 1)
    monkeypatch.setattr(ProductActions, 'PRODUCTS_PER_STORE_MAX', 3)

    saved = importStoreProducts(ADMIN, newRows('A', 'B', 'C'), 'form1')

    assert [row['name'] for row in saved['added']] == ['A', 'B']
    assert saved['skipped'] == [{'position': 3, 'name': 'C',
                                 'reason': IMPORT_STORE_FULL}]


def test_importStoreProducts_countsTheRoomAgain_beforeEachBatch(clean,
                                                                monkeypatch):
    """A product added by a racing request during the first batch takes
    room from the second."""
    store = createTestStore(ADMIN)
    monkeypatch.setattr(ProductActions, 'PRODUCTS_PER_STORE_MAX',
                        PRODUCT_BATCH_MAX + 2)
    real = ProductActions._insertBatch

    def racing(storeId, batch, formId):
        stored = real(storeId, batch, formId)
        if len(batch) == PRODUCT_BATCH_MAX:
            addProduct(storeId, 'Racer', 1)
        return stored
    monkeypatch.setattr(ProductActions, '_insertBatch', racing)

    saved = importStoreProducts(
        ADMIN, newRows(*[f'P{n}' for n in range(PRODUCT_BATCH_MAX + 3)]),
        'form1')

    assert len(saved['added']) == PRODUCT_BATCH_MAX + 1
    assert [row['reason'] for row in saved['skipped']] == [
        IMPORT_STORE_FULL] * 2


@pytest.mark.parametrize('keptBefore', [True, False],
                         ids=['standalone', 'transaction'])
def test_aBatchThatLosesARace_isAddedRowByRow(clean, monkeypatch,
                                             keptBefore):
    """A racing add takes a name between the check and the batch: a
    standalone mongod keeps the rows before it, a transaction none; each
    row not stored is then added on its own, the taken one left out."""
    store = createTestStore(ADMIN)
    realCreateMany = db.createMany

    def losing(documents, collection, session=None):
        if keptBefore:
            realCreateMany(documents[:1], collection)
        addProduct(store['_id'], 'B', 1)
        raise BulkWriteError({'writeErrors': [{'code': 11000}]})
    monkeypatch.setattr(db, 'createMany', losing)

    saved = importStoreProducts(ADMIN, newRows('A', 'B', 'C'), 'form1')

    assert [row['name'] for row in saved['added']] == ['A', 'C']
    assert saved['skipped'] == [{'position': 2, 'name': 'B',
                                 'reason': IMPORT_NAME_TAKEN}]
    assert sorted(storedProducts(store['_id'])) == ['A', 'B', 'C']


def test_aBatchFailingOnAnythingElse_raises(clean, monkeypatch):
    createTestStore(ADMIN)

    def failing(documents, collection, session=None):
        raise BulkWriteError({'writeErrors': [{'code': 121}]})
    monkeypatch.setattr(db, 'createMany', failing)

    with pytest.raises(BulkWriteError):
        importStoreProducts(ADMIN, newRows('A'), 'form1')


# ============================ the readers ============================ #


@pytest.mark.parametrize('text, price', [
    ('250', 250.0), ('₱1,250.50', 1250.5), ('PHP 99', 99.0),
    ('Php 99', 99.0), ('99 USD', 99.0), ('USD99', 99.0), ('99₱', 99.0),
    ('12,50', 12.5), ('1.250,75', 1250.75), ('1,250', 1250.0),
    ('1.250.000', 1250000.0), (' $ 5 ', 5.0), ('€ 3.5', 3.5),
    ('-5', -5.0), ('-$5', -5.0), ('', None), ('  ', None),
])
def test_price_readsWhatAnAdminWrites(text, price):
    assert productImport._price(text) == price


@pytest.mark.parametrize('text', [
    'free', 'PHP', '1.2.3,4,5', '09:30:00', '11/2/2026', '2 for 100',
    '₱99.99 (was 120)', '1e3', '1.5k', '(5)', '5%', 'P99', '--5', '1 2',
    '.',
])
def test_price_thatIsNotOneNumber_isRefused(text):
    with pytest.raises(ValueError, match=productImport.BAD_PRICE):
        productImport._price(text)


@pytest.mark.parametrize('text, stock', [
    ('12', (12, True)), ('1,200', (1200, True)), ('3.0', (3, True)),
    ('0', (0, True)), ('', (None, False)), ('Unlimited', (None, False)),
    ('NO LIMIT', (None, False)), ('∞', (None, False)),
])
def test_stock_readsACountOrNone(text, stock):
    assert productImport._stock(text) == stock


@pytest.mark.parametrize('text', ['some', '2.5'])
def test_stock_thatIsNoWholeNumber_isRefused(text):
    with pytest.raises(ValueError):
        productImport._stock(text)


def test_sheetsOf_splitsAnXlsxsTextIntoItsSheets_andDropsACutLine():
    text = ('Sheet: A\nname,price\nMug,2\n\nSheet: B\nx\n'
            '[Cut: the file goes on]')
    sheets = productImport.sheetsOf(text, XLSX_TYPE)
    assert sheets == [
        productImport.Sheet('A', [['name', 'price'], ['Mug', '2']]),
        productImport.Sheet('B', [['x']])]
    assert productImport.sheetsOf('a,b\n1,2', 'text/csv') == [
        productImport.Sheet(None, [['a', 'b'], ['1', '2']])]


def test_theProductLimits_areSharedWithTheClient():
    for name in ('PRODUCT_BATCH_MAX', 'PRODUCT_IMPORT_ROWS_MAX',
                 'PRODUCT_NAME_MAX_LENGTH', 'PRODUCT_DESCRIPTION_MAX_LENGTH'):
        assert hasattr(sharedConstants, name)


def test_spreadsheetText_withASheet_readsOnlyThatSheetsRows_pastTheTenth():
    import spreadsheetText
    book = sheetXlsx({f'S{n}': '<row r="1">' + inline('A1', f'cell{n}') +
                      '</row>' for n in range(1, 13)})

    text = spreadsheetText.spreadsheetText(book, XLSX_TYPE, 'x', 10_000,
                                           sheet=' s12 ').text

    assert 'cell12' in text
    assert 'cell1\n' not in text and 'cell11' not in text
    assert [sheet.name for sheet in productImport.sheetsOf(
        text, XLSX_TYPE)] == [f'S{n}' for n in range(1, 13)]
