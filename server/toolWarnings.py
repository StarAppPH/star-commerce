"""toolWarnings.py — what the store's data lacks, told to the admin's chat
model beside a read tool's result.

When the admin's chat runs one of the read tools of FINDERS, the tool
message gets a `warnings` list next to the route's own keys: each warning
names a gap (WARNINGS: a product with no photo, an order paid a week ago
and not handed over, a policy answer not given), how many items have it in
the whole store (`count`, not only the rows the tool listed) and the ids of
those the model has not been told about lately (`ids`, at most
WARNING_IDS_MAX). The route's response is never changed: an HTTP caller,
the MCP servers and the customer's chat get none of this.

Not spammy: each (warning, item) told to the model is kept as a
ToolWarningShown document until its warning's cooldown is over, and is
left out until then; a warning with no item left is not sent at all. A
TurnWarnings collects one turn's: it leaves out what the turn already told,
and ChatActions writes them (markShown) only once the turn is saved, so a
turn that fails hides nothing. A new item (another product with no photo)
is told at once.

Finding or writing them never fails the tool or the turn (logged as a
warning). The messages are the app's own text: they never quote a name,
note or anything else the store or a customer wrote.
"""

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Callable

from pydantic_core import to_json

from mongoDb import db
from objects import ToolWarningShown

TOOL_WARNINGS = 'ToolWarningsShown'
# The toolset whose read tools carry warnings: the admin's chat.
WARNING_TOOLSET = 'admin'
# The most ids one warning lists, and the most items of one warning read
# to find them (the newest-told ones among those are left out).
WARNING_IDS_MAX = 10
WARNING_ITEMS_READ_MAX = 100
# The thresholds.
LOW_STOCK_UNITS = 3
STALE_ORDER_DAYS = 7
UNANSWERED_RETURN_DAYS = 3
STALE_RETURN_DAYS = 7
# How far back a cancelled order is checked for a refund that did not go
# through (keeps the read on the index as a store's orders grow).
REFUND_LOOKBACK_DAYS = 90
# How long an item told to the model is left out, by urgency.
URGENT_COOLDOWN = datetime.timedelta(days=1)
STOCK_COOLDOWN = datetime.timedelta(days=3)
SETUP_COOLDOWN = datetime.timedelta(days=7)
# The policy answers a customer is told to ask the store about while unset.
POLICY_ANSWERS_WARNED = ('acceptsReturns', 'defectReportDays',
                         'paidOrderCancellation')
# The store details warned about while unset.
STORE_DETAILS_WARNED = ('contactNumber', 'address', 'openingHours',
                        'location', 'primaryColor', 'secondaryColor',
                        'photoStyle')
UTC = datetime.timezone.utc


@dataclass(frozen=True)
class WarningRule:
    cooldown: datetime.timedelta
    message: str


WARNINGS = {
    'noPhoto': WarningRule(
        SETUP_COOLDOWN,
        'Products shown in the shop with no photo. Offer to make photos '
        '(makeProductPhotoChoices).'),
    'outOfStock': WarningRule(
        STOCK_COOLDOWN,
        'Products shown in the shop with 0 units left: customers see them '
        'sold out. Ask whether to restock them (updateStoreProduct).'),
    'lowStock': WarningRule(
        STOCK_COOLDOWN,
        f'Products shown in the shop with {LOW_STOCK_UNITS} or fewer units '
        'left. Ask whether to restock them (updateStoreProduct).'),
    'hidden': WarningRule(
        SETUP_COOLDOWN,
        'Products hidden from customers (isAvailable false). Ask whether any '
        'should be shown.'),
    'staleUnfulfilled': WarningRule(
        URGENT_COOLDOWN,
        f'Orders paid more than {STALE_ORDER_DAYS} days ago and not handed '
        'over yet. Ask whether they were delivered or picked up '
        '(completeOrder).'),
    'staleAwaitingPayment': WarningRule(
        URGENT_COOLDOWN,
        f'Orders waiting more than {STALE_ORDER_DAYS} days for a payment '
        'made by hand, holding their units. Ask whether the money arrived '
        '(confirmOrderPayment) or to cancel them (cancelStoreOrder).'),
    'refundMissing': WarningRule(
        URGENT_COOLDOWN,
        'Cancelled orders paid through a payment provider whose refund did '
        'not go through (refundedAt null). Tell the owner to check them '
        'with the provider.'),
    'unanswered': WarningRule(
        URGENT_COOLDOWN,
        f'Return requests waiting more than {UNANSWERED_RETURN_DAYS} days '
        'for a decision. Ask whether to approve or reject them '
        '(approveReturn, rejectReturn).'),
    'staleApproved': WarningRule(
        URGENT_COOLDOWN,
        f'Returns approved more than {STALE_RETURN_DAYS} days ago and not '
        'received yet. Ask whether the items came back (receiveReturn).'),
    'noPaymentMethods': WarningRule(
        URGENT_COOLDOWN,
        'The store has not said how it takes payment, so every order is '
        'refused. Offer to set it (updateMyStorePolicy).'),
    'noDeliveryZones': WarningRule(
        URGENT_COOLDOWN,
        'The store offers delivery but has no delivery zones, so no '
        'delivery can be booked. Offer to draw them (showDeliveryZonesForm).'),
    'policyUnset': WarningRule(
        SETUP_COOLDOWN,
        'Policy answers not given yet (ids are the field names): customers '
        'who ask are told to ask the store. Offer to set them '
        '(updateMyStorePolicy).'),
    'storeDetailMissing': WarningRule(
        SETUP_COOLDOWN,
        'Store details not set yet (ids are the field names): the contact '
        'number, address and opening hours (updateMyStoreDetails), the map '
        'pin (showStoreLocationForm), the brand colors and photo style '
        '(updateMyStoreTheme). Offer to add them.'),
}


@dataclass(frozen=True)
class Finding:
    """The items of one warning, store-wide: `count` in all, `ids` the
    first WARNING_ITEMS_READ_MAX of them."""
    count: int
    ids: list[str]


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _found(ids: list[str]) -> Finding:
    return Finding(len(ids), ids)


def _facetFindings(collection: str, storeId: str, base: dict,
                   facets: dict[str, tuple[dict, dict]]) -> dict:
    """One read of the store's `collection` (`base` narrowing it first):
    each warning of `facets` ({code: (match, sort)}) with its count and its
    first WARNING_ITEMS_READ_MAX ids, in that sort."""
    stages = {}
    for code, (match, sort) in facets.items():
        stages[code] = [{'$match': match}, {'$sort': sort},
                        {'$limit': WARNING_ITEMS_READ_MAX},
                        {'$project': {'_id': 1}}]
        stages[f'{code}Count'] = [{'$match': match}, {'$count': 'count'}]
    result = db.aggregate([{'$match': {'storeId': storeId, **base}},
                           {'$facet': stages}], collection, findOne=True)
    findings = {}
    for code in facets:
        counted = result[f'{code}Count']
        if counted:
            findings[code] = Finding(counted[0]['count'],
                                     [row['_id'] for row in result[code]])
    return findings


def _productFindings(storeId: str, result: dict,
                     now: datetime.datetime) -> dict:
    from actions.ProductActions import PRODUCTS
    byName = {'nameKey': 1}
    shown = {'isAvailable': True}
    return _facetFindings(PRODUCTS, storeId, {}, {
        'noPhoto': ({**shown, 'photoUrl': None}, byName),
        'outOfStock': ({**shown, 'inventory': 0}, byName),
        'lowStock': ({**shown,
                      'inventory': {'$gt': 0, '$lte': LOW_STOCK_UNITS}},
                     byName),
        'hidden': ({'isAvailable': False}, byName),
    })


def _orderFindings(storeId: str, result: dict,
                   now: datetime.datetime) -> dict:
    from actions.OrderActions import ORDERS
    stale = now - datetime.timedelta(days=STALE_ORDER_DAYS)
    oldestFirst = {'createdAt': 1}
    return _facetFindings(ORDERS, storeId, {
        'status': {'$in': ['paid', 'awaitingPayment', 'cancelled']},
    }, {
        'staleUnfulfilled': ({'status': 'paid', 'paidAt': {'$lt': stale}},
                             oldestFirst),
        'staleAwaitingPayment': ({'status': 'awaitingPayment',
                                  'createdAt': {'$lt': stale}}, oldestFirst),
        # Paid through a provider: one paid by hand the store refunds itself.
        'refundMissing': ({
            'status': 'cancelled',
            'createdAt': {'$gte': now - datetime.timedelta(
                days=REFUND_LOOKBACK_DAYS)},
            'paidAt': {'$ne': None},
            'paymentProvider': {'$ne': None},
            'refundedAt': None,
        }, oldestFirst),
    })


def _returnFindings(storeId: str, result: dict,
                    now: datetime.datetime) -> dict:
    from actions.ReturnActions import RETURNS
    oldestFirst = {'createdAt': 1}
    return _facetFindings(RETURNS, storeId, {
        'status': {'$in': ['requested', 'approved']},
    }, {
        'unanswered': ({'status': 'requested', 'createdAt': {
            '$lt': now - datetime.timedelta(days=UNANSWERED_RETURN_DAYS)}},
                       oldestFirst),
        'staleApproved': ({'status': 'approved', 'decidedAt': {
            '$lt': now - datetime.timedelta(days=STALE_RETURN_DAYS)}},
                          oldestFirst),
    })


def _policyFindings(storeId: str, result: dict,
                    now: datetime.datetime) -> dict:
    """From getMyStorePolicy's own result: no read."""
    findings = {}
    if not result.get('authorizedPaymentMethods'):
        findings['noPaymentMethods'] = _found(['authorizedPaymentMethods'])
    if ('delivery' in (result.get('deliveryOptions') or [])
            and not result.get('deliveryZones')):
        findings['noDeliveryZones'] = _found(['deliveryZones'])
    unset = [field for field in POLICY_ANSWERS_WARNED
             if field in result and result[field] is None]
    if unset:
        findings['policyUnset'] = _found(unset)
    return findings


def _storeFindings(storeId: str, result: dict,
                   now: datetime.datetime) -> dict:
    """From getUserStore's own result: no read. None for a store not set
    up yet (no name): the setup chat asks for its details."""
    store = result.get('store')
    if not isinstance(store, dict) or store.get('name') is None:
        return {}
    missing = [field for field in STORE_DETAILS_WARNED
               if store.get(field) is None]
    return {'storeDetailMissing': _found(missing)} if missing else {}


# The read tools whose results carry warnings, each with what finds them
# (storeId, the route's result, now) -> {code: Finding}.
FINDERS: dict[str, Callable[[str, dict, datetime.datetime], dict]] = {
    'listMyStoreProducts': _productFindings,
    'getStoreOrders': _orderFindings,
    'getStoreReturns': _returnFindings,
    'getMyStorePolicy': _policyFindings,
    'getUserStore': _storeFindings,
}


def _key(storeId: str, code: str, itemId: str) -> str:
    return f'{storeId}:{code}:{itemId}'


def _recentlyShown(storeId: str, findings: dict,
                   now: datetime.datetime) -> set[tuple[str, str]]:
    """The (code, itemId) of `findings` told to the model within their
    cooldown. expireAt is checked too: the TTL monitor deletes late."""
    keys = {_key(storeId, code, itemId): (code, itemId)
            for code, finding in findings.items() for itemId in finding.ids}
    if not keys:
        return set()
    shown = db.read({'_id': {'$in': list(keys)}, 'expireAt': {'$gt': now}},
                    TOOL_WARNINGS, projection={'_id': 1})
    return {keys[row['_id']] for row in shown}


def warningsFor(name: str, storeId: str, result: dict,
                leftOut: set[tuple[str, str]],
                now: datetime.datetime) -> tuple[list[dict], list[tuple]]:
    """The warnings of the tool `name`'s `result` at `storeId`, without
    the items of `leftOut` and those told within their cooldown, and the
    (code, itemId) they tell."""
    findings = FINDERS[name](storeId, result, now)
    leftOut = leftOut | _recentlyShown(storeId, findings, now)
    warnings, told = [], []
    for code, finding in findings.items():
        ids = [itemId for itemId in finding.ids
               if (code, itemId) not in leftOut][:WARNING_IDS_MAX]
        if not ids:
            continue
        warnings.append({'code': code, 'count': finding.count, 'ids': ids,
                         'message': WARNINGS[code].message})
        told.extend((code, itemId) for itemId in ids)
    return warnings, told


class TurnWarnings:
    """One chat turn's warnings at `storeId`: attach() adds them to a read
    tool's result, leaving out what the turn already told; `told` is what
    markShown writes once the turn is saved."""

    def __init__(self, storeId: str):
        self.storeId = storeId
        self.told: list[tuple[str, str]] = []

    def attach(self, toolset: str, name, content: str,
               maxChars: int) -> str:
        """The tool message `content` of the tool `name`, with its
        warnings. Unchanged for another toolset or tool, a refusal, a
        result with nothing to warn about, or one the warnings would take
        past `maxChars` (chatModel.CHAT_TOOL_RESULT_MAX_CHARS)."""
        if toolset != WARNING_TOOLSET or name not in FINDERS:
            return content
        try:
            result = json.loads(content)
        except ValueError:
            return content
        if not isinstance(result, dict) or 'error' in result:
            return content
        try:
            warnings, told = warningsFor(name, self.storeId, result,
                                         set(self.told), _now())
        except Exception as error:
            logging.warning({'event': 'toolWarningsSkipped', 'tool': name,
                             'errorClass': type(error).__name__})
            return content
        if not warnings:
            return content
        withWarnings = to_json({**result, 'warnings': warnings}).decode()
        if len(withWarnings) > maxChars:
            return content
        self.told.extend(told)
        return withWarnings


def markShown(storeId: str, told: list[tuple[str, str]]) -> None:
    """Keep each (code, itemId) of `told` as told now, so it is left out
    until its warning's cooldown is over."""
    if not told:
        return
    now = _now()
    operations = []
    for code, itemId in told:
        key = _key(storeId, code, itemId)
        shown = ToolWarningShown(
            _id=key, storeId=storeId, code=code, itemId=itemId,
            lastShownAt=now,
            expireAt=now + WARNINGS[code].cooldown).model_dump(
                by_alias=True, exclude={'id', 'version'})
        operations.append({'filter': {'_id': key},
                           'update': {'$set': shown,
                                      '$setOnInsert': {'_version': 0}},
                           'upsert': True})
    try:
        db.bulkUpdate(operations, TOOL_WARNINGS)
    except Exception as error:
        logging.warning({'event': 'toolWarningsNotMarked',
                         'errorClass': type(error).__name__})
