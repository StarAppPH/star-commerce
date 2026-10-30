"""Payments made by hand to the store, called directly: payOrder with an
option that has no provider (awaitingPayment), addPaymentReference,
confirmOrderPayment, and how cancelling such an order refunds nothing
through the provider.

Time and seeding are test_checkout.py's (its clock at T0, its _shop, whose
store takes every way to pay: conftest's ALL_PAYMENT_METHODS).
"""

import asyncio
import base64
import datetime
import io

import pytest
from google.api_core.exceptions import NotFound
from PIL import Image

import actions.OrderActions as orderActions
import mcpServer
import photoStorage
from actions.FulfillmentActions import _refund
from actions.OrderActions import NOT_MOVABLE, ORDER_NOT_FOUND, TOO_MANY_UNPAID
from actions.PaymentActions import (MANUAL_PAYMENT_REFERENCE_LABEL,
                                    NO_RECEIPT,
                                    NO_REFERENCE_NEEDED, NOT_CONFIRMABLE,
                                    NOT_PAYABLE, RECEIPT_GONE)
from ApiRequests import ApiRequests
from conftest import (assertExactShape, createTestStore, takePayments,
                      verifiedGuestAt)
from objects import (MANUAL_PAYMENT_REFERENCE_MAX_LENGTH,
                     MAX_PENDING_ORDERS_PER_STORE, MyOrderRow,
                     PaymentReceiptResponse, StoreOrderRow, projection_of)
from test_checkout import (ADMIN, OTHER_ADMIN, OTHER_GUEST, SENTINEL, T0,
                           _fresh, _order, _place, _product, _shop,
                           charges, clean, clock, exactly)
from test_fulfillment import refunds
from test_photos import BUCKET, FakeBlob, FakeBucket, downloadUrl

api = ApiRequests()


def _awaiting(firebase, method: str = 'bankTransfer', quantity: int = 2):
    """An order the customer chose to pay by hand with `method`:
    (store, customerId, the order as payOrder answered it, its product)."""
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, quantity))
    paid = api.payOrder(customerId, store['_id'], order['_id'], method)
    return store, customerId, paid, pandesal


# ----------------------------- payOrder by hand ----------------------------- #


def test_payOrder_byHand_awaitsTheStore_holdingTheUnits_withoutACharge(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 2))
    reserved = _product(pandesal['_id'])
    placed = _order(order['_id'])

    res = api.payOrder(customerId, store['_id'], order['_id'],
                       'bankTransfer', None)

    assertExactShape(MyOrderRow, res)
    stored = _order(order['_id'])
    assert res['status'] == stored['status'] == 'awaitingPayment'
    assert (stored['paymentMethod'], stored['paymentProvider'],
            stored['paymentReference'], stored['paidAt']) == (
                'bankTransfer', None, None, None)
    assert stored['_version'] == placed['_version'] + 1
    assert charges.calls == []
    assert _product(pandesal['_id']) == reserved


def test_payOrder_anOrderAwaitingPayment_cannotBePaidAgain(
        clean, firebase, clock, charges):
    store, customerId, paid, _ = _awaiting(firebase)
    before = _order(paid['_id'])

    with pytest.raises(ValueError,
                       match=exactly(NOT_PAYABLE['awaitingPayment'])):
        api.payOrder(customerId, store['_id'], paid['_id'], 'card', 'demo')

    assert _order(paid['_id']) == before
    assert charges.calls == []


def test_anOrderAwaitingPayment_neverExpires_andCountsAsUnpaid(
        clean, firebase, clock):
    store, customerId, paid, pandesal = _awaiting(firebase)
    for _ in range(MAX_PENDING_ORDERS_PER_STORE - 1):
        _place(customerId, store, (pandesal, 1))
    clock.advance(days=30)

    assert orderActions.cancelExpiredOrders() == (
        MAX_PENDING_ORDERS_PER_STORE - 1)
    assert _order(paid['_id'])['status'] == 'awaitingPayment'
    # The pending ones expired; the one awaiting payment still counts.
    for _ in range(MAX_PENDING_ORDERS_PER_STORE - 1):
        _place(customerId, store, (pandesal, 1))
    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (pandesal, 1))


# --------------------------- addPaymentReference --------------------------- #


def test_addPaymentReference_savesIt_stripped_andALaterOneReplacesIt(
        clean, firebase, clock):
    store, customerId, paid, _ = _awaiting(firebase)

    res = api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                  '  BDO-REF-123  ')
    assertExactShape(MyOrderRow, res)
    assert res['manualPaymentReference'] == 'BDO-REF-123'
    res = api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                  'BDO-REF-456')

    stored = _order(paid['_id'])
    assert stored['manualPaymentReference'] == res[
        'manualPaymentReference'] == 'BDO-REF-456'
    assert stored['status'] == 'awaitingPayment'


@pytest.mark.parametrize('reference, message', [
    ('', f'{MANUAL_PAYMENT_REFERENCE_LABEL} is 1 to '
     f'{MANUAL_PAYMENT_REFERENCE_MAX_LENGTH} characters'),
    ('x' * (MANUAL_PAYMENT_REFERENCE_MAX_LENGTH + 1),
     f'{MANUAL_PAYMENT_REFERENCE_LABEL} is 1 to '
     f'{MANUAL_PAYMENT_REFERENCE_MAX_LENGTH} characters'),
    (f'{SENTINEL}\x00', f'{MANUAL_PAYMENT_REFERENCE_LABEL} cannot contain '
     'control characters'),
    (5, f'{MANUAL_PAYMENT_REFERENCE_LABEL} must be text'),
])
def test_addPaymentReference_refusesABadReference_withoutQuotingIt(
        clean, firebase, clock, reference, message):
    store, customerId, paid, _ = _awaiting(firebase)
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(message)):
        api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                reference)

    assert _order(paid['_id']) == before


def test_addPaymentReference_aPendingOrder_mustChooseHowToPayFirst(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))

    with pytest.raises(ValueError,
                       match=exactly(NO_REFERENCE_NEEDED['pending'])):
        api.addPaymentReference(customerId, store['_id'], order['_id'],
                                'REF-1')


def test_addPaymentReference_anotherCustomersOrder_isNotFound(
        clean, firebase, clock):
    store, customerId, paid, _ = _awaiting(firebase)
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.addPaymentReference(anotherCustomer, store['_id'], paid['_id'],
                                'REF-1')

    assert _order(paid['_id']) == before


# --------------------------- confirmOrderPayment --------------------------- #


def test_confirmOrderPayment_makesItPaid_thenItCanBeCompleted(
        clean, firebase, clock):
    store, customerId, paid, pandesal = _awaiting(firebase)
    api.addPaymentReference(customerId, store['_id'], paid['_id'], 'REF-9')
    reserved = _product(pandesal['_id'])
    clock.advance(hours=5)

    res = api.confirmOrderPayment(ADMIN, paid['_id'])

    assertExactShape(StoreOrderRow, res)
    stored = _order(paid['_id'])
    assert res['status'] == stored['status'] == 'paid'
    assert res['paidAt'] == stored['paidAt'] == T0 + datetime.timedelta(
        hours=5)
    assert (res['paymentMethod'], res['paymentProvider'],
            res['manualPaymentReference'], res['paymentReference']) == (
                'bankTransfer', None, 'REF-9', None)
    assert _product(pandesal['_id']) == reserved
    completed = api.completeOrder(ADMIN, paid['_id'])
    assert completed['status'] == 'completed'


@pytest.mark.parametrize('status', ['pending', 'paid'])
def test_confirmOrderPayment_refusesAnOrderNotAwaitingPayment(
        clean, firebase, clock, status):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    if status == 'paid':
        api.payOrder(customerId, store['_id'], order['_id'], 'card', 'demo')
    before = _order(order['_id'])

    with pytest.raises(ValueError, match=exactly(NOT_CONFIRMABLE[status])):
        api.confirmOrderPayment(ADMIN, order['_id'])

    assert _order(order['_id']) == before


def test_confirmOrderPayment_anotherStoresOrder_isNotFound(
        clean, firebase, clock):
    _, _, paid, _ = _awaiting(firebase)
    takePayments(createTestStore(OTHER_ADMIN, 'Coffee B'))
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.confirmOrderPayment(OTHER_ADMIN, paid['_id'])

    assert _order(paid['_id']) == before


def test_completeOrder_refusesAnOrderAwaitingPayment(clean, firebase, clock):
    _, _, paid, _ = _awaiting(firebase, 'cash')

    with pytest.raises(ValueError,
                       match=exactly(NOT_MOVABLE['awaitingPayment'])):
        api.completeOrder(ADMIN, paid['_id'])


# ------------------------------- cancelling ------------------------------- #


def test_cancelMyOrder_anOrderAwaitingPayment_isCancelled_withoutARefund(
        clean, firebase, clock, refunds):
    store, customerId, paid, pandesal = _awaiting(firebase)

    res = api.cancelMyOrder(customerId, store['_id'], paid['_id'])

    assert (res['status'], res['cancelReason'], res['refundedAt']) == (
        'cancelled', 'byCustomer', None)
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert refunds.calls == []


def test_cancelStoreOrder_aConfirmedPaymentByHand_isNotRefundedByTheApp(
        clean, firebase, clock, refunds):
    _, _, paid, pandesal = _awaiting(firebase, 'check')
    api.confirmOrderPayment(ADMIN, paid['_id'])

    res = api.cancelStoreOrder(ADMIN, paid['_id'])

    assert (res['status'], res['refundedAt'], res['refundReference']) == (
        'cancelled', None, None)
    assert refunds.calls == []
    assert _product(pandesal['_id'])['reservedInventory'] == []


def test_refund_ofAnOrderPaidByHand_movesNothing(clean, firebase, clock,
                                                 refunds):
    store, customerId, products = _shop(firebase)
    orderId = _fresh(store, customerId, [(products['Pandesal'], 1)],
                     status='cancelled', paymentMethod='cash', paidAt=T0)
    order = _order(orderId)

    assert _refund(order, projection_of(StoreOrderRow)) == order
    assert refunds.calls == []


# ------------------------------ the MCP tools ------------------------------ #


def test_theManualPaymentTools_areOnTheirSidesServers():
    async def names(side):
        return {tool.name for tool in await mcpServer.servers[side]
                .list_tools()}

    assert 'addPaymentReference' in asyncio.run(names('customer'))
    assert 'confirmOrderPayment' in asyncio.run(names('admin'))
    assert 'confirmOrderPayment' not in asyncio.run(names('customer'))


# ------------------------------ receipts ------------------------------ #


class ReceiptBlob(FakeBlob):

    def delete(self, **kwargs):
        if self.name not in self.bucket.objects:
            raise NotFound('gone')
        del self.bucket.objects[self.name]


class ReceiptBucket(FakeBucket):
    """FakeBucket whose blobs can be deleted."""

    def blob(self, name):
        return ReceiptBlob(self, name)

    def get_blob(self, name, **kwargs):
        found = super().get_blob(name, **kwargs)
        if found is None:
            return None
        blob = ReceiptBlob(self, name)
        blob.content_type, blob.size = found.content_type, found.size
        return blob


@pytest.fixture
def bucket(monkeypatch) -> ReceiptBucket:
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    fake = ReceiptBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: fake)
    return fake


def _photo(size=(40, 30), mode='RGB', fmt='PNG', exif=None) -> bytes:
    out = io.BytesIO()
    image = Image.new(mode, size, 'red')
    if exif is not None:
        image.save(out, fmt, exif=exif)
    else:
        image.save(out, fmt)
    return out.getvalue()


def _upload(bucket, customerId: str, name: str, data: bytes,
            contentType: str = 'image/png') -> str:
    """A photo the customer uploaded to their own folder: its URL."""
    path = f'users/{customerId}/chat/{name}'
    bucket.objects[path] = {'data': data, 'contentType': contentType,
                            'metadata': None}
    return downloadUrl(path)


def _receipts(bucket, store: dict) -> dict:
    prefix = f"stores/{store['_id']}/receipts/"
    return {name: o for name, o in bucket.objects.items()
            if name.startswith(prefix)}


def test_addPaymentReceipt_aReceiptPhoto_isKeptWhereOnlyTheServerReadsIt(
        clean, firebase, clock, bucket):
    store, customerId, paid, _ = _awaiting(firebase)
    exif = Image.Exif()
    exif[0x010F] = 'PhoneMaker'
    url = _upload(bucket, customerId, 'receipt.jpg',
                  _photo((3200, 1000), fmt='JPEG', exif=exif), 'image/jpeg')

    res = api.addPaymentReceipt(customerId, store['_id'], paid['_id'], url)

    assertExactShape(MyOrderRow, res)
    assert res['paymentReceiptAt'] == T0
    assert res['manualPaymentReference'] is None
    stored = _order(paid['_id'])
    [(path, kept)] = _receipts(bucket, store).items()
    assert stored['paymentReceiptPath'] == path
    assert 'paymentReceiptPath' not in res
    # A JPEG with no metadata and no download token, scaled to fit.
    assert kept['contentType'] == 'image/jpeg' and kept['metadata'] is None
    with Image.open(io.BytesIO(kept['data'])) as image:
        assert image.format == 'JPEG'
        assert image.size == (photoStorage.RECEIPT_MAX_SIDE, 500)
        assert not image.getexif()

    shown = api.getPaymentReceipt(ADMIN, paid['_id'])

    assertExactShape(PaymentReceiptResponse, shown)
    prefix = 'data:image/jpeg;base64,'
    assert shown['image'].startswith(prefix)
    assert base64.b64decode(shown['image'][len(prefix):]) == kept['data']


def test_addPaymentReceipt_aLaterReceipt_replacesAndDeletesTheEarlierOne(
        clean, firebase, clock, bucket):
    store, customerId, paid, _ = _awaiting(firebase)
    first = _upload(bucket, customerId, 'one.png', _photo())
    api.addPaymentReference(customerId, store['_id'], paid['_id'], 'REF-1')
    api.addPaymentReceipt(customerId, store['_id'], paid['_id'], first)
    [earlier] = _receipts(bucket, store)

    res = api.addPaymentReceipt(
        customerId, store['_id'], paid['_id'],
        _upload(bucket, customerId, 'two.webp', _photo(fmt='WEBP'),
                'image/webp'))

    [later] = _receipts(bucket, store)
    assert later != earlier
    assert _order(paid['_id'])['paymentReceiptPath'] == later
    assert earlier not in bucket.objects
    # The reference sent before is kept.
    assert res['manualPaymentReference'] == 'REF-1'


@pytest.mark.parametrize('url, message', [
    ('https://example.com/receipt.png',
     'receiptUrl is not a photo of a receipt uploaded to this app'),
    (None, None),
])
def test_addPaymentReceipt_refusesAReceiptThatIsNotTheCustomers(
        clean, firebase, clock, bucket, url, message):
    store, customerId, paid, _ = _awaiting(firebase)
    if url is None:
        url = _upload(bucket, 'someone-else', 'receipt.png', _photo())
        message = 'receiptUrl must be a photo of a receipt you uploaded'
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(message)):
        api.addPaymentReceipt(customerId, store['_id'], paid['_id'], url)

    assert _order(paid['_id']) == before
    assert _receipts(bucket, store) == {}


def test_addPaymentReceipt_aReceiptForAnOrderNotAwaitingPayment_keepsNothing(
        clean, firebase, clock, bucket):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    url = _upload(bucket, customerId, 'receipt.png', _photo())

    with pytest.raises(ValueError,
                       match=exactly(NO_REFERENCE_NEEDED['pending'])):
        api.addPaymentReceipt(customerId, store['_id'], order['_id'], url)

    assert _receipts(bucket, store) == {}


def test_getPaymentReceipt_refusals(clean, firebase, clock, bucket):
    store, customerId, paid, _ = _awaiting(firebase)
    takePayments(createTestStore(OTHER_ADMIN, 'Coffee B'))

    with pytest.raises(ValueError, match=exactly(NO_RECEIPT)):
        api.getPaymentReceipt(ADMIN, paid['_id'])
    api.addPaymentReceipt(customerId, store['_id'], paid['_id'],
                          _upload(bucket, customerId, 'r.png', _photo()))
    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.getPaymentReceipt(OTHER_ADMIN, paid['_id'])
    bucket.objects.clear()
    with pytest.raises(ValueError, match=exactly(RECEIPT_GONE)):
        api.getPaymentReceipt(ADMIN, paid['_id'])


def test_addPaymentReceipt_isNoChatOrMcpTool():
    async def names(side):
        return {tool.name for tool in await mcpServer.servers[side]
                .list_tools()}

    assert 'addPaymentReceipt' not in asyncio.run(names('customer'))
    assert 'getPaymentReceipt' not in asyncio.run(names('admin'))
