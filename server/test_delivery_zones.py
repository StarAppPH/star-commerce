"""Delivery zones: the areas an admin draws on the map, each with its own
delivery policy (setMyDeliveryZones, from the admin chat's
showDeliveryZonesForm): a name, a flat fee, the days a delivery takes, and
optionally a minimum order and a total from which delivery is free. Their
checks (deliveryZoneProblem, zonesOverlap: zones may share a border, never
overlap), and where they hold: a store without a zone takes no delivery, a
pin outside every zone is refused on the customer's pin form and by
createOrder, and the zone under the pin prices the order.

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import random
import re

import pytest

import chatForms
from actions.OrderActions import (BELOW_ZONE_MINIMUM, NO_DELIVERY_ZONES_YET,
                                  OUTSIDE_DELIVERY_ZONES)
from actions.StoreActions import (DELIVERY_ZONES_NOT_A_LIST,
                                  TOO_MANY_DELIVERY_ZONES, ZONES_OVERLAP)
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import addProduct, assertExactShape
from mongoDb import db
from objects import (DELIVERY_ZONES_MAX, ChatReplyResponse,
                     DeliveryZonesResponse, NewDeliveryPoint,
                     NewDeliveryZone, StorePolicyResponse, deliveryFeeFor,
                     deliveryZoneAt, deliveryZoneProblem, ruledZones,
                     zonesOverlap)
from test_chat import calls, clock, frozenNow, model, offline, reply
from test_chat_components import formAnswerDetails
from test_delivery import (ADMIN, HOME, STORE_PIN, clean, customerSays,
                           items, openPin, pinOf, shop, stored)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline', 'clean']

api = ApiRequests()
tools = ToolRequests()

BOTH = {'deliveryOptions': ['pickup', 'delivery']}


def corners(*pairs) -> list[dict]:
    return [{'latitude': latitude, 'longitude': longitude}
            for latitude, longitude in pairs]


def zone(name: str, points: list[dict], fee: float = 50, days: int = 1,
         minimum: float | None = None, freeFrom: float | None = None) -> dict:
    """A zone as setMyDeliveryZones takes it and the store keeps it."""
    return {'name': name, 'points': points, 'deliveryFee': fee,
            'deliveryDays': days, 'minimumOrder': minimum,
            'freeDeliveryFrom': freeFrom}


def rules(zone: dict) -> dict:
    """A zone's policy, without its shape (getStorePolicy's)."""
    return {key: value for key, value in zone.items() if key != 'points'}


# A box around HOME (Makati); Taguig shares its east border; a triangle far
# north of both.
MAKATI_BOX = corners((14.54, 121.01), (14.54, 121.04), (14.57, 121.04),
                     (14.57, 121.01))
TAGUIG_BOX = corners((14.54, 121.04), (14.54, 121.07), (14.57, 121.07),
                     (14.57, 121.04))
NORTH_TRIANGLE = corners((14.70, 121.05), (14.70, 121.10), (14.75, 121.10))
MAKATI = zone('Makati', MAKATI_BOX, fee=60, days=1)
TAGUIG = zone('Taguig', TAGUIG_BOX, fee=80, days=2, minimum=500,
              freeFrom=2000)
NORTH = zone('Bulacan', NORTH_TRIANGLE, fee=150, days=3)
# An L: its notch (the top right) is outside.
L_SHAPE = corners((0, 0), (0, 2), (1, 2), (1, 1), (2, 1), (2, 0))
FAR = {'address': '1 Far Rd, Bulacan', 'latitude': 14.9, 'longitude': 121.2,
       'notes': None}
# On the border Makati and Taguig share.
ON_THE_BORDER = {'address': '1 Border St', 'latitude': 14.555,
                 'longitude': 121.04, 'notes': None}
IN_TAGUIG = {'address': '9 BGC', 'latitude': 14.555, 'longitude': 121.05,
             'notes': None}


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def pairs(points: list[dict]) -> list[tuple[float, float]]:
    return [(p['latitude'], p['longitude']) for p in points]


def zonedShop(firebase, zones: list[dict], **policy):
    """test_delivery's shop, delivering, with exactly `zones` drawn, and a
    Cookie Tin (45, 100 in stock) among its products."""
    store, customerId, products = shop(firebase, zones=[], **BOTH, **policy)
    api.setMyDeliveryZones(ADMIN, zones)
    products['Cookie Tin'] = addProduct(store['_id'], 'Cookie Tin', 45,
                                        inventory=100)
    return store, customerId, products


# ----------------------------- A. the shapes ----------------------------- #


@pytest.mark.parametrize('point, name', [
    ((14.5547, 121.0244), 'Makati'),   # HOME
    ((14.72, 121.09), 'Bulacan'),      # in the triangle
    ((14.555, 121.05), 'Taguig'),
    ((14.555, 121.04), 'Makati'),      # the shared border: the first listed
    ((14.60, 121.02), None),           # between them
    ((14.74, 121.06), None),           # beside the triangle's slope
])
def test_aPin_fallsInTheFirstZoneThatHoldsIt(point, name):
    found = deliveryZoneAt(*point, [MAKATI, TAGUIG, NORTH])
    assert (found['name'] if found else None) == name


def test_aConcaveZone_leavesItsNotchOutside():
    zones = [zone('L', L_SHAPE)]
    assert deliveryZoneAt(0.5, 1.5, zones) is not None
    assert deliveryZoneAt(1.5, 0.5, zones) is not None
    assert deliveryZoneAt(1.5, 1.5, zones) is None


@pytest.mark.parametrize('zones', [None, [], [{'points': MAKATI_BOX}]])
def test_noZoneWithAPolicy_holdsNoPin(zones):
    """A zone drawn before zones had policies (shape only) takes no pin."""
    assert deliveryZoneAt(HOME['latitude'], HOME['longitude'], zones) is None
    assert ruledZones(zones) == []


@pytest.mark.parametrize('first, second, overlap', [
    (MAKATI_BOX, TAGUIG_BOX, False),           # a shared border
    (MAKATI_BOX, NORTH_TRIANGLE, False),       # apart
    (MAKATI_BOX, MAKATI_BOX, True),            # the same zone twice
    (MAKATI_BOX, list(reversed(MAKATI_BOX)), True),
    (MAKATI_BOX, corners((14.55, 121.03), (14.55, 121.05), (14.56, 121.05),
                         (14.56, 121.03)), True),   # sides cross
    (MAKATI_BOX, corners((14.55, 121.02), (14.55, 121.03), (14.56, 121.03),
                         (14.56, 121.02)), True),   # one inside the other
    (L_SHAPE, corners((1, 1), (1, 2), (2, 2), (2, 1)), False),  # the notch
])
def test_zonesOverlap_unlessTheyOnlyShareABorder(first, second, overlap):
    assert zonesOverlap(pairs(first), pairs(second)) is overlap
    assert zonesOverlap(pairs(second), pairs(first)) is overlap


def _turn(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def sharedSidePairs(seed: int, count: int, sameSide: bool):
    """`count` random zones around Cebu that share their first side a-b
    exactly (as the map snaps it), each with one or two more corners: on
    opposite sides of the line a-b, or (sameSide) both on one side. The
    second zone runs the shared side the other way, or the same way."""
    rng = random.Random(seed)

    def point():
        return (rng.uniform(10.3, 10.5), rng.uniform(123.95, 124.05))

    found = []
    while len(found) < count:
        a, b = point(), point()
        first = [a, b, *(point() for _ in range(rng.randint(1, 2)))]
        second = [b, a, *(point() for _ in range(rng.randint(1, 2)))]
        if deliveryZoneProblem(first) or deliveryZoneProblem(second):
            continue
        firstSides = {_turn(a, b, c) > 0 for c in first[2:]}
        secondSides = {_turn(a, b, c) > 0 for c in second[2:]}
        if len(firstSides) != 1 or len(secondSides) != 1:
            continue    # a zone that reaches across the line a-b
        if (firstSides == secondSides) is not sameSide:
            continue
        if rng.random() < 0.5:
            second = [a, b, *reversed(second[2:])]
        found.append((first, second))
    return found


def test_zonesThatShareASideExactly_neverOverlap():
    """The middle of a shared side is rarely exactly in line with it in
    floating point: it counts as on the border, never inside."""
    for first, second in sharedSidePairs(7, 1000, sameSide=False):
        assert zonesOverlap(first, second) is False, (first, second)
        assert zonesOverlap(second, first) is False, (first, second)


def test_zonesThatShareASide_onTheSameSideOfIt_overlap():
    for first, second in sharedSidePairs(8, 300, sameSide=True):
        assert zonesOverlap(first, second) is True, (first, second)
        assert zonesOverlap(second, first) is True, (first, second)


def test_aPinOnASharedSide_fallsInTheFirstZoneListed():
    """The first zone listed, whichever it is (deliveryZoneAt)."""
    for first, second in sharedSidePairs(9, 300, sameSide=False):
        a, b = first[0], first[1]
        one = zone('One', corners(*first))
        two = zone('Two', corners(*second))
        for t in (0.5, 0.25, 0.9):
            pin = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            assert deliveryZoneAt(*pin, [one, two])['name'] == 'One', pin
            assert deliveryZoneAt(*pin, [two, one])['name'] == 'Two', pin


@pytest.mark.parametrize('shape, problem', [
    ([(0, 0), (0, 1)], 'draw 3 to 100 corners'),
    ([(0, 0)] * 101, 'draw 3 to 100 corners'),
    ([(0, 0), (0, 1), (1, 1), (0, 1)], 'a corner is placed twice'),
    ([(0, 0), (1, 1), (1, 0), (0, 1)],
     'its sides cross: draw it without crossing lines'),
    ([(0, 0), (0, 2), (0, 1), (1, 1)],
     'its sides cross: draw it without crossing lines'),
    ([(0, 0), (1, 1), (2, 2)], 'its corners lie in one line'),
])
def test_aShapeThatIsNoZone_isRefused(shape, problem):
    assert deliveryZoneProblem(shape) == problem


@pytest.mark.parametrize('itemsTotal, fee', [
    (0, 80), (1999.99, 80), (2000, 0), (5000, 0)])
def test_aZonesFee_isFreeFromItsFreeDeliveryTotal(itemsTotal, fee):
    assert deliveryFeeFor(TAGUIG, itemsTotal) == fee
    assert deliveryFeeFor(MAKATI, itemsTotal) == 60


# ----------------------------- B. saving them ----------------------------- #


def test_setMyDeliveryZones_savesEachZoneWithItsPolicy(clean, firebase):
    store, _, _ = zonedShop(firebase, [])

    res = api.setMyDeliveryZones(ADMIN, [MAKATI, TAGUIG, NORTH])

    assertExactShape(DeliveryZonesResponse, res)
    assert res == {'deliveryZones': [MAKATI, TAGUIG, NORTH]}
    assert db.read({'_id': store['_id']}, 'Stores',
                   findOne=True)['deliveryZones'] == [MAKATI, TAGUIG, NORTH]


def test_aZonesTextAndAmounts_areTidied(clean, firebase):
    zonedShop(firebase, [])
    sent = {**MAKATI, 'name': '  Makati  ', 'deliveryFee': 60.004,
            'minimumOrder': 250}

    res = api.setMyDeliveryZones(ADMIN, [sent])

    assert res['deliveryZones'] == [{**MAKATI, 'minimumOrder': 250.0}]


def test_theMcpModels_areTakenToo(clean, firebase):
    zonedShop(firebase, [])
    sent = NewDeliveryZone(**{**MAKATI, 'points': [
        NewDeliveryPoint(**p) for p in MAKATI_BOX]})

    assert api.setMyDeliveryZones(ADMIN, [sent]) == {
        'deliveryZones': [MAKATI]}


def test_noZone_clearsThem_soTheStoreTakesNoDelivery(clean, firebase):
    store, customerId, products = zonedShop(firebase, [MAKATI])

    assert api.setMyDeliveryZones(ADMIN, []) == {'deliveryZones': []}
    with pytest.raises(ValueError, match=exactly(NO_DELIVERY_ZONES_YET)):
        tools.createOrder(customerId, store['_id'],
                        items(products['Pandesal']), 'delivery', HOME)


@pytest.mark.parametrize('zones, refusal', [
    ('everywhere', DELIVERY_ZONES_NOT_A_LIST),
    ([zone(f'Z{i}', MAKATI_BOX) for i in range(DELIVERY_ZONES_MAX + 1)],
     TOO_MANY_DELIVERY_ZONES),
    ([MAKATI, 'north'], 'Zone 2: a zone has a name, points and its delivery '
     'policy'),
    ([MAKATI, {**NORTH, 'points': 'ZQ-secret-7'}],
     'Zone 2: points is a list of corners'),
    ([{**MAKATI, 'points': [*MAKATI_BOX[:2],
                            {'latitude': 'ZQ-secret-7', 'longitude': 121}]}],
     'Zone 1: a latitude is a number from -90 to 90'),
    ([{**MAKATI, 'points': corners((0, 0), (1, 1), (1, 0), (0, 1))}],
     'Zone 1: its sides cross: draw it without crossing lines'),
    ([{**MAKATI, 'name': '   '}], 'Zone 1: the name is 1 to 40 characters'),
    ([{**MAKATI, 'name': None}], 'Zone 1: the name must be text'),
    ([{**MAKATI, 'name': 'M' * 41}], 'Zone 1: the name is 1 to 40 characters'),
    ([{**MAKATI, 'deliveryFee': None}],
     'Zone 1: the delivery fee is an amount from 0 to 10000000'),
    ([{**MAKATI, 'deliveryFee': -1}],
     'Zone 1: the delivery fee is an amount from 0 to 10000000'),
    ([{**MAKATI, 'deliveryFee': True}],
     'Zone 1: the delivery fee is an amount from 0 to 10000000'),
    ([{**MAKATI, 'deliveryFee': 'ZQ-secret-7'}],
     'Zone 1: the delivery fee is an amount from 0 to 10000000'),
    ([{**MAKATI, 'deliveryDays': 1.5}],
     'Zone 1: the delivery days is a whole number from 0 to 365'),
    ([{**MAKATI, 'deliveryDays': 366}],
     'Zone 1: the delivery days is a whole number from 0 to 365'),
    ([{**MAKATI, 'minimumOrder': -5}],
     'Zone 1: the minimum order is an amount from 0 to 10000000'),
    ([{**MAKATI, 'freeDeliveryFrom': 'free'}],
     'Zone 1: the free delivery total is an amount from 0 to 10000000'),
    ([MAKATI, {**TAGUIG, 'name': 'makati'}],
     'Zone 2: zone 1 has that name already'),
    ([NORTH, MAKATI, {**TAGUIG, 'points': corners(
        (14.55, 121.03), (14.55, 121.05), (14.56, 121.05), (14.56, 121.03))}],
     ZONES_OVERLAP.format(later=3, earlier=2)),
])
def test_aBadZone_isRefused_byItsPosition_andNothingIsSaved(
        clean, firebase, zones, refusal):
    store, _, _ = zonedShop(firebase, [])

    with pytest.raises(ValueError, match=exactly(refusal)):
        api.setMyDeliveryZones(ADMIN, zones)
    assert db.read({'_id': store['_id']}, 'Stores',
                   findOne=True)['deliveryZones'] == []


def test_onlyAnAdmin_drawsZones(clean, firebase):
    _, customerId, _ = zonedShop(firebase, [])

    with pytest.raises(PermissionError):
        api.setMyDeliveryZones(customerId, [MAKATI])


# ----------------------------- C. createOrder ----------------------------- #


def test_aStoreWithoutAZone_takesNoDelivery(clean, firebase):
    store, customerId, products = zonedShop(firebase, [])

    with pytest.raises(ValueError, match=exactly(NO_DELIVERY_ZONES_YET)):
        tools.createOrder(customerId, store['_id'],
                        items(products['Pandesal']), 'delivery', HOME)
    # Pickup still works, and is chosen for the customer.
    res = tools.createOrder(customerId, store['_id'],
                          items(products['Pandesal']))
    assert res['order']['fulfillment'] == 'pickup'


def test_aZoneDrawnBeforePolicies_takesNoDelivery(clean, firebase):
    store, customerId, products = zonedShop(firebase, [])
    db.update({'_id': store['_id'], '_version': stored_store(store)},
              {'deliveryZones': [{'points': MAKATI_BOX}]}, 'Stores')

    with pytest.raises(ValueError, match=exactly(NO_DELIVERY_ZONES_YET)):
        tools.createOrder(customerId, store['_id'],
                        items(products['Pandesal']), 'delivery', HOME)


def stored_store(store: dict) -> int:
    return db.read({'_id': store['_id']}, 'Stores', findOne=True)['_version']


def test_theZoneUnderThePin_pricesTheOrder(clean, firebase):
    store, customerId, products = zonedShop(firebase, [MAKATI, TAGUIG])

    res = tools.createOrder(customerId, store['_id'],
                          items(products['Cookie Tin'], 12), 'delivery',
                          IN_TAGUIG)

    order = res['order']
    assert (order['deliveryZone'], order['deliveryFee'],
            order['deliveryDistanceKm'], order['total']) == (
        'Taguig', 80, None, 12 * 45 + 80)
    assert stored(order['_id'])['deliveryZone'] == 'Taguig'


def test_aPinOnASharedBorder_takesTheFirstZonesPolicy(clean, firebase):
    store, customerId, products = zonedShop(firebase, [MAKATI, TAGUIG])

    res = tools.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'delivery',
                          ON_THE_BORDER)

    assert (res['order']['deliveryZone'], res['order']['deliveryFee']) == (
        'Makati', 60)


def test_fromAZonesFreeDeliveryTotal_deliveryIsFree(clean, firebase):
    store, customerId, products = zonedShop(firebase, [TAGUIG])

    res = tools.createOrder(customerId, store['_id'],
                          items(products['Cookie Tin'], 45), 'delivery',
                          IN_TAGUIG)

    assert (res['order']['deliveryFee'], res['order']['total']) == (0, 2025)


def test_belowAZonesMinimum_isRefused_andReservesNothing(clean, firebase):
    store, customerId, products = zonedShop(firebase, [TAGUIG])
    before = products['Cookie Tin']['inventory']

    with pytest.raises(ValueError, match=exactly(BELOW_ZONE_MINIMUM.format(
            zone='Taguig', minimum=500, currency='PHP'))):
        tools.createOrder(customerId, store['_id'],
                        items(products['Cookie Tin'], 11), 'delivery',
                        IN_TAGUIG)
    assert db.read({}, 'Orders') == []
    assert db.read({'_id': products['Cookie Tin']['_id']}, 'Products',
                   findOne=True)['inventory'] == before


def test_aDeliveryOutsideEveryZone_isRefused_andReservesNothing(
        clean, firebase):
    store, customerId, products = zonedShop(firebase, [MAKATI])

    with pytest.raises(ValueError, match=exactly(OUTSIDE_DELIVERY_ZONES)):
        tools.createOrder(customerId, store['_id'],
                        items(products['Pandesal']), 'delivery', FAR)
    assert db.read({}, 'Orders') == []


def test_aPickup_ignoresTheZones(clean, firebase):
    store, customerId, products = zonedShop(firebase, [TAGUIG])

    res = tools.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'pickup')

    assert (res['order']['fulfillment'], res['order']['deliveryZone'],
            res['order']['deliveryFee']) == ('pickup', None, 0)


def test_aStoreThatIsNotPinned_stillDeliversIntoItsZones(clean, firebase):
    """Zones price a delivery, so the store's pin is no longer needed."""
    store, customerId, products = shop(firebase, pinned=False, zones=[],
                                       **BOTH)
    api.setMyDeliveryZones(ADMIN, [MAKATI])

    res = tools.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'delivery', HOME)

    assert res['order']['deliveryZone'] == 'Makati'


# --------------------------- D. the customer's pin --------------------------- #


def test_thePinForm_carriesTheZones_andTheCurrency(clean, firebase, model):
    store, customerId, _ = zonedShop(firebase, [MAKATI, TAGUIG])

    res = openPin(model, customerId, store)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form == {'kind': 'deliveryPinForm', 'formId': form['formId'],
                    'storeLocation': STORE_PIN, 'currency': 'PHP',
                    'deliveryZones': [MAKATI, TAGUIG]}


def test_thePinForm_refusesWithoutAZone(clean, firebase, model):
    store, customerId, _ = zonedShop(firebase, [])
    fake = model(calls(('c1', 'showDeliveryPinForm', '{}')),
                 reply('Pickup it is.'))

    res = customerSays(customerId, store['_id'], 'Deliver it please')

    assert res['components'] == []
    assert json.loads(fake.requests[1]['messages'][-1]['content']) == {
        'error': NO_DELIVERY_ZONES_YET}


def test_aPinOutsideTheZones_isRefused_andOneInside_namesItsZone(
        clean, firebase, model):
    store, customerId, _ = zonedShop(firebase, [MAKATI, TAGUIG])
    first = openPin(model, customerId, store)
    [form] = first['components']
    cid = first['conversationId']
    model()

    with pytest.raises(ValueError, match=exactly(OUTSIDE_DELIVERY_ZONES)):
        customerSays(customerId, store['_id'], None, cid, pinOf(form, FAR))

    fake = model(reply('Taguig: 80 pesos, 2 days. Your address?'))
    second = customerSays(customerId, store['_id'], None, cid,
                          pinOf(form, IN_TAGUIG))
    assert second['userText'] == ('Deliver to my pin at 14.55500, 121.05000 '
                                  '(Taguig)')
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'deliveryPin', 'latitude': 14.555, 'longitude': 121.05,
        'zone': rules(TAGUIG)}


# --------------------------- E. the admin's map --------------------------- #


def adminSays(text, conversationId=None, form=None) -> dict:
    return api.sendAdminChatMessage(ADMIN, text, conversationId, form)


def openZones(model) -> dict:
    model(calls(('c1', 'showDeliveryZonesForm', '{}')),
          reply('Draw where you deliver.'))
    return adminSays('We deliver')


def test_theZonesForm_opensWithTheZonesSoFar(clean, firebase, model):
    zonedShop(firebase, [MAKATI])

    res = openZones(model)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form == {'kind': 'deliveryZonesForm', 'formId': form['formId'],
                    'currency': 'PHP', 'storeLocation': STORE_PIN,
                    'zones': [MAKATI]}


def test_theZonesAnswer_listsWhatTheStoreSaved(clean, firebase, model):
    shop(firebase, pinned=False, zones=[])
    first = openZones(model)
    [form] = first['components']
    assert (form['storeLocation'], form['zones']) == (None, [])
    answer = {'kind': 'deliveryZones', 'formId': form['formId']}
    api.setMyDeliveryZones(ADMIN, [MAKATI, TAGUIG])
    fake = model(reply('Saved. A name for all of them?'))

    second = adminSays(None, first['conversationId'], answer)

    assert second['userText'] == 'Saved 2 delivery zones: Makati, Taguig.'
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'deliveryZones', 'zones': [rules(MAKATI), rules(TAGUIG)]}
    model()
    with pytest.raises(ValueError,
                       match=exactly(chatForms.FORM_ALREADY_ANSWERED)):
        adminSays(None, first['conversationId'], answer)


def test_noZoneSaved_saysNoDeliveryYet(clean, firebase, model):
    shop(firebase, zones=[])
    first = openZones(model)
    [form] = first['components']
    model(reply('Noted.'))

    second = adminSays(None, first['conversationId'],
                       {'kind': 'deliveryZones', 'formId': form['formId']})

    assert second['userText'] == ('No delivery zones: no delivery orders '
                                  'until I draw one.')


def test_theZonesForm_isTheAdminsOnly(clean, firebase, model):
    store, customerId, _ = zonedShop(firebase, [MAKATI])
    fake = model(calls(('c1', 'showDeliveryZonesForm', '{}')),
                 reply('Sorry.'))

    res = customerSays(customerId, store['_id'], 'Where do you deliver?')

    assert res['components'] == []
    assert json.loads(fake.requests[1]['messages'][-1]['content']) == {
        'error': 'Unknown tool showDeliveryZonesForm'}


# ----------------------------- F. the policy ----------------------------- #


def test_thePolicy_listsEachZonesRules_withoutItsShape(clean, firebase):
    store, customerId, _ = zonedShop(firebase, [MAKATI, TAGUIG])

    res = tools.getStorePolicy(customerId, store['_id'])

    assertExactShape(StorePolicyResponse, res)
    assert res['deliveryZones'] == [rules(MAKATI), rules(TAGUIG)]
    assert tools.getMyStorePolicy(ADMIN)['deliveryZones'] == [
        rules(MAKATI), rules(TAGUIG)]


def test_aStoreThatDoesNotDeliver_listsNoZone(clean, firebase):
    store, customerId, _ = zonedShop(firebase, [MAKATI])
    tools.updateMyStorePolicy(ADMIN, deliveryOptions=['pickup'])

    assert tools.getStorePolicy(customerId,
                              store['_id'])['deliveryZones'] == []


def test_aSave_dropsTheRetiredFreeDeliveryAnswers(clean, firebase):
    store, _, _ = zonedShop(firebase, [MAKATI])
    db.update({'storeId': store['_id']} | {
        '_id': db.read({'storeId': store['_id']}, 'Policies',
                       findOne=True)['_id'],
        '_version': db.read({'storeId': store['_id']}, 'Policies',
                            findOne=True)['_version']},
        {'offersFreeDelivery': True, 'freeDeliveryMinimum': 1000},
        'Policies')

    res = tools.updateMyStorePolicy(ADMIN, handoverDays=2)

    assert 'offersFreeDelivery' not in res
    saved = db.read({'storeId': store['_id']}, 'Policies', findOne=True)
    assert 'offersFreeDelivery' not in saved
    assert 'freeDeliveryMinimum' not in saved
