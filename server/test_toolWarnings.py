"""The warnings an admin's read tool results carry (toolWarnings.py): what
the store's data lacks, store-wide, told to the model once per item and
left out until the warning's cooldown is over; marked as told only once the
turn is saved; never on the route's own response, another toolset, a
refusal or a result the warnings would take past the cap."""

import datetime
import json
import typing

import pytest

import chatModel
import chatTools
import toolWarnings
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addOrder, addProduct, createTestStore, takePayments,
                      wipeCollections)
from mongoDb import db
from objects import Return, ToolWarningCode
from test_chat import (ADMIN, FAR, calls, clock, frozenNow, model, offline,
                       reply)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Returns', 'Conversations', 'ChatTurns', 'ChatUsage',
    'ChatThinking', toolWarnings.TOOL_WARNINGS
]
DAY = datetime.timedelta(days=1)
ITEM = {'productId': 'p-1', 'name': 'Pandesal', 'price': 5, 'quantity': 2}
api = ApiRequests()
tools = ToolRequests()


def wipeDatabase() -> None:
    wipeCollections(COLLECTIONS)


def at(monkeypatch, instant: datetime.datetime) -> None:
    monkeypatch.setattr(toolWarnings, '_now', lambda: instant)


def adminRuns(fake, name: str, arguments: str = '{}') -> dict:
    """One admin turn calling the tool `name`: its tool message, parsed."""
    model = fake(calls(('c1', name, arguments)), reply('ok'))
    api.sendAdminChatMessage(ADMIN, 'show me')
    return json.loads(model.requests[1]['messages'][-1]['content'])


def told(result: dict) -> dict:
    """{code: (count, ids)} of a tool message's warnings."""
    return {w['code']: (w['count'], w['ids'])
            for w in result.get('warnings', [])}


def shownDoc(storeId: str, code: str, itemId: str) -> dict | None:
    return db.read({'_id': f'{storeId}:{code}:{itemId}'},
                   toolWarnings.TOOL_WARNINGS, findOne=True)


def addReturn(storeId: str, status: str, createdAt: datetime.datetime,
              decidedAt: datetime.datetime | None = None) -> dict:
    ret = Return(storeId=storeId, customerId='cu-1', orderId='o-1',
                 items=[ITEM], reason='defective', refundAmount=10,
                 currency='PHP', contactNumber='+639171234567',
                 status=status, createdAt=createdAt, decidedAt=decidedAt,
                 _version=0)
    return db.create(ret.model_dump(by_alias=True), 'Returns')


def test_everyWarning_isACode_andEveryFinder_anAdminReadTool():
    assert set(toolWarnings.WARNINGS) == set(typing.get_args(ToolWarningCode))
    assert set(toolWarnings.FINDERS) <= set(chatTools.TOOL_CALLS['admin'])
    assert "`warnings`" in chatModel.CHAT_PROMPTS['admin']
    assert "`warnings`" not in chatModel.CHAT_PROMPTS['customer']


def test_productWarnings_toldOnce_aNewItemBreaksThrough_backAfterCooldown(
        model, monkeypatch):
    try:
        wipeDatabase()
        at(monkeypatch, FAR)
        storeId = createTestStore(ADMIN)['_id']
        empty = addProduct(storeId, 'Ensaymada', 45, inventory=0)
        low = addProduct(storeId, 'Ube Cake', 650, inventory=2)
        hidden = addProduct(storeId, 'Old Recipe', 10, isAvailable=False)
        db.db['Products'].update_one({'_id': low['_id']},
                                     {'$set': {'photoUrl': 'https://p/1'}})

        # A search for one product still warns store-wide.
        first = adminRuns(model, 'listMyStoreProducts',
                          '{"search": "Ube"}')

        assert [p['name'] for p in first['products']] == ['Ube Cake']
        assert told(first) == {
            'noPhoto': (1, [empty['_id']]),
            'outOfStock': (1, [empty['_id']]),
            'lowStock': (1, [low['_id']]),
            'hidden': (1, [hidden['_id']]),
        }
        assert first['warnings'][0]['message'] == (
            toolWarnings.WARNINGS['noPhoto'].message)
        doc = shownDoc(storeId, 'noPhoto', empty['_id'])
        assert doc['lastShownAt'] == FAR
        assert doc['expireAt'] == FAR + toolWarnings.SETUP_COOLDOWN
        assert shownDoc(storeId, 'lowStock', low['_id'])['expireAt'] == (
            FAR + toolWarnings.STOCK_COOLDOWN)

        # The next turn: nothing to tell, so no warnings key at all.
        again = adminRuns(model, 'listMyStoreProducts')
        assert 'warnings' not in again
        assert tools.listMyStoreProducts(ADMIN).keys() == {'currency',
                                                         'products'}

        # A new product with no photo is told at once; the count is all.
        new = addProduct(storeId, 'Pandesal', 5)
        assert told(adminRuns(model, 'listMyStoreProducts')) == {
            'noPhoto': (2, [new['_id']])}

        # Past the stock cooldown, not the setup one.
        at(monkeypatch, FAR + toolWarnings.STOCK_COOLDOWN + DAY)
        assert told(adminRuns(model, 'listMyStoreProducts')) == {
            'outOfStock': (1, [empty['_id']]),
            'lowStock': (1, [low['_id']]),
        }
    finally:
        wipeDatabase()


def test_orderAndReturnWarnings_areStoreWide_andTheUrgentOnesDaily(
        model, monkeypatch):
    try:
        wipeDatabase()
        at(monkeypatch, FAR)
        storeId = createTestStore(ADMIN)['_id']
        stalePaid = addOrder(storeId, 'cu-1', [ITEM], 'paid',
                             FAR - 9 * DAY, paidAt=FAR - 8 * DAY,
                             paymentMethod='cash')
        addOrder(storeId, 'cu-1', [ITEM], 'paid', FAR - 2 * DAY,
                 paidAt=FAR - 2 * DAY, paymentMethod='cash')
        waiting = addOrder(storeId, 'cu-1', [ITEM], 'awaitingPayment',
                           FAR - 8 * DAY, paymentMethod='bankTransfer')
        failedRefund = addOrder(
            storeId, 'cu-1', [ITEM], 'cancelled', FAR - 5 * DAY,
            paidAt=FAR - 5 * DAY, paymentMethod='card',
            paymentProvider='demo', cancelledAt=FAR - 4 * DAY,
            cancelReason='byStore')
        # Paid by hand: the store refunds it itself.
        addOrder(storeId, 'cu-1', [ITEM], 'cancelled', FAR - 5 * DAY,
                 paidAt=FAR - 5 * DAY, paymentMethod='cash',
                 cancelledAt=FAR - 4 * DAY, cancelReason='byStore')
        addOrder(storeId, 'cu-1', [ITEM], 'completed', FAR - 1 * DAY,
                 paidAt=FAR - 1 * DAY, completedAt=FAR,
                 paymentMethod='cash')
        unanswered = addReturn(storeId, 'requested', FAR - 4 * DAY)
        addReturn(storeId, 'requested', FAR - 1 * DAY)
        approved = addReturn(storeId, 'approved', FAR - 9 * DAY,
                             FAR - 8 * DAY)

        orders = adminRuns(model, 'getStoreOrders',
                           '{"status": "completed", "limit": 1}')

        assert [o['status'] for o in orders['orders']] == ['completed']
        assert told(orders) == {
            'staleUnfulfilled': (1, [stalePaid['_id']]),
            'staleAwaitingPayment': (1, [waiting['_id']]),
            'refundMissing': (1, [failedRefund['_id']]),
        }
        assert told(adminRuns(model, 'getStoreReturns')) == {
            'unanswered': (1, [unanswered['_id']]),
            'staleApproved': (1, [approved['_id']]),
        }
        assert 'warnings' not in adminRuns(model, 'getStoreOrders')

        at(monkeypatch, FAR + toolWarnings.URGENT_COOLDOWN)
        assert set(told(adminRuns(model, 'getStoreOrders'))) == {
            'staleUnfulfilled', 'staleAwaitingPayment', 'refundMissing'}
    finally:
        wipeDatabase()


def test_policyAndStoreWarnings_comeFromTheResult_byFieldName(
        model, monkeypatch):
    try:
        wipeDatabase()
        at(monkeypatch, FAR)
        store = createTestStore(ADMIN)

        policy = adminRuns(model, 'getMyStorePolicy')
        details = adminRuns(model, 'getUserStore')

        assert told(policy) == {
            'noPaymentMethods': (1, ['authorizedPaymentMethods']),
            'policyUnset': (3, ['acceptsReturns', 'defectReportDays',
                                'paidOrderCancellation']),
        }
        # createTestStore gives the contact number, address and colors.
        assert told(details) == {
            'storeDetailMissing': (3, ['openingHours', 'location',
                                       'photoStyle'])}

        takePayments(store)
        tools.updateMyStorePolicy(ADMIN, deliveryOptions=['pickup',
                                                        'delivery'])
        assert told(adminRuns(model, 'getMyStorePolicy')) == {
            'noDeliveryZones': (1, ['deliveryZones'])}
    finally:
        wipeDatabase()


def test_aTurnThatFails_marksNothing_soTheNextOneTells(model, monkeypatch):
    try:
        wipeDatabase()
        at(monkeypatch, FAR)
        storeId = createTestStore(ADMIN)['_id']
        product = addProduct(storeId, 'Pandesal', 5)
        model(calls(('c1', 'listMyStoreProducts', '{}')),
              RuntimeError('the model broke'))

        with pytest.raises(Exception):
            api.sendAdminChatMessage(ADMIN, 'show me')

        assert db.read({}, toolWarnings.TOOL_WARNINGS) == []
        assert told(adminRuns(model, 'listMyStoreProducts')) == {
            'noPhoto': (1, [product['_id']])}
    finally:
        wipeDatabase()


def test_oneTurn_tellsAnItemOnce_andLeavesOtherToolsetsAndRefusalsAlone(
        model, monkeypatch):
    try:
        wipeDatabase()
        at(monkeypatch, FAR)
        storeId = createTestStore(ADMIN)['_id']
        addProduct(storeId, 'Pandesal', 5)
        fake = model(calls(('c1', 'listMyStoreProducts', '{}'),
                           ('c2', 'listMyStoreProducts', '{}')),
                     reply('ok'))

        api.sendAdminChatMessage(ADMIN, 'show me twice')

        first, second = [json.loads(m['content'])
                         for m in fake.requests[1]['messages']
                         if m['role'] == 'tool']
        assert set(told(first)) == {'noPhoto'} and 'warnings' not in second

        turn = toolWarnings.TurnWarnings(storeId)
        content = '{"currency":"PHP","products":[]}'
        assert turn.attach('customer', 'listMyStoreProducts', content,
                           16000) == content
        assert turn.attach('admin', 'updateStoreProduct', content,
                           16000) == content
        refusal = chatModel.toolError('Product not found')
        assert turn.attach('admin', 'listMyStoreProducts', refusal,
                           16000) == refusal
        # Past the cap: the result as it was, and nothing told.
        db.deleteMany({}, toolWarnings.TOOL_WARNINGS)
        assert turn.attach('admin', 'listMyStoreProducts', content,
                           len(content) + 10) == content
        assert turn.told == []
    finally:
        wipeDatabase()


def test_aReadThatFails_neverFailsTheTool(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('the database is down')

    monkeypatch.setattr(toolWarnings.db, 'aggregate', broken)
    content = '{"orders":[]}'

    assert toolWarnings.TurnWarnings('store-1').attach(
        'admin', 'getStoreOrders', content, 16000) == content
    assert 'toolWarningsSkipped' in [r.msg['event'] for r in caplog.records
                                     if isinstance(r.msg, dict)]


def test_aWriteThatFails_neverFailsTheTurn(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('the database is down')

    monkeypatch.setattr(toolWarnings.db, 'bulkUpdate', broken)

    toolWarnings.markShown('store-1', [('noPhoto', 'p-1')])

    assert 'toolWarningsNotMarked' in [r.msg['event'] for r in caplog.records
                                       if isinstance(r.msg, dict)]
