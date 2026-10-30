"""Delivery areas: the shapes an admin draws on the map (setMyDeliveryZones,
from the admin chat's showDeliveryZonesForm), their checks
(deliveryZoneProblem), and where they hold: a delivery pin outside every
area is refused on the customer's pin form and by createOrder. A store with
no area delivers anywhere, as before areas existed.

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import re

import pytest

import chatForms
from actions.OrderActions import OUTSIDE_DELIVERY_ZONES
from actions.StoreActions import (DELIVERY_ZONES_NOT_A_LIST,
                                  TOO_MANY_DELIVERY_ZONES)
from ApiRequests import ApiRequests
from conftest import assertExactShape
from mongoDb import db
from objects import (DELIVERY_ZONES_MAX, ChatReplyResponse,
                     DeliveryZonesResponse, NewDeliveryPoint,
                     NewDeliveryZone, deliveryZoneProblem,
                     insideDeliveryZones)
from test_chat import calls, clock, frozenNow, model, offline, reply
from test_chat_components import formAnswerDetails
from test_delivery import (ADMIN, DELIVERS, HOME, HOME_FEE, STORE_PIN,
                           clean, customerSays, items, openPin, pinOf, shop)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline', 'clean']

api = ApiRequests()


def corners(*pairs) -> list[dict]:
    return [{'latitude': latitude, 'longitude': longitude}
            for latitude, longitude in pairs]


# A box around HOME (Makati), and one far north of it.
MAKATI = {'points': corners((14.54, 121.01), (14.54, 121.04),
                            (14.57, 121.04), (14.57, 121.01))}
NORTH = {'points': corners((14.70, 121.05), (14.70, 121.10),
                           (14.75, 121.10))}
# An L: its notch (the top right) is outside.
L_SHAPE = {'points': corners((0, 0), (0, 2), (1, 2), (1, 1), (2, 1),
                             (2, 0))}
FAR = {'address': '1 Far Rd, Bulacan', 'latitude': 14.9, 'longitude': 121.2,
       'notes': None}


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def pairs(zone: dict) -> list[tuple[float, float]]:
    return [(p['latitude'], p['longitude']) for p in zone['points']]


# ----------------------------- A. the shapes ----------------------------- #


@pytest.mark.parametrize('point, inside', [
    ((14.5547, 121.0244), True),   # HOME, in the Makati box
    ((14.72, 121.09), True),       # in the northern triangle
    ((14.60, 121.02), False),      # between them
    ((14.74, 121.06), False),      # beside the triangle's slope
])
def test_aPin_isInsideWhenAnyAreaHoldsIt(point, inside):
    assert insideDeliveryZones(*point, [MAKATI, NORTH]) is inside


def test_aConcaveArea_leavesItsNotchOutside():
    assert insideDeliveryZones(0.5, 1.5, [L_SHAPE]) is True
    assert insideDeliveryZones(1.5, 0.5, [L_SHAPE]) is True
    assert insideDeliveryZones(1.5, 1.5, [L_SHAPE]) is False


@pytest.mark.parametrize('zones', [None, []])
def test_noArea_isAnywhere(zones):
    assert insideDeliveryZones(FAR['latitude'], FAR['longitude'], zones)


@pytest.mark.parametrize('shape, problem', [
    ([(0, 0), (0, 1)], 'draw 3 to 100 corners'),
    ([(0, 0)] * 101, 'draw 3 to 100 corners'),
    ([(0, 0), (0, 1), (1, 1), (0, 1)], 'a corner is placed twice'),
    # A bow tie: its sides cross in the middle.
    ([(0, 0), (1, 1), (1, 0), (0, 1)],
     'its sides cross: draw it without crossing lines'),
    # A side running back over another one.
    ([(0, 0), (0, 2), (0, 1), (1, 1)],
     'its sides cross: draw it without crossing lines'),
    ([(0, 0), (1, 1), (2, 2)], 'its corners lie in one line'),
])
def test_aShapeThatIsNoArea_isRefused(shape, problem):
    assert deliveryZoneProblem(shape) == problem


@pytest.mark.parametrize('zone', [MAKATI, NORTH, L_SHAPE])
def test_aClosedShape_isAnArea(zone):
    assert deliveryZoneProblem(pairs(zone)) is None


# ----------------------------- B. saving them ----------------------------- #


def test_setMyDeliveryZones_savesTheAreas_andSendsThemBack(clean, firebase):
    store, _, _ = shop(firebase)

    res = api.setMyDeliveryZones(ADMIN, [MAKATI, NORTH])

    assertExactShape(DeliveryZonesResponse, res)
    assert res == {'deliveryZones': [MAKATI, NORTH]}
    saved = db.read({'_id': store['_id']}, 'Stores', findOne=True)
    assert saved['deliveryZones'] == [MAKATI, NORTH]


def test_theMcpModels_areTakenToo(clean, firebase):
    shop(firebase)
    zone = NewDeliveryZone(points=[NewDeliveryPoint(**p)
                                   for p in MAKATI['points']])

    assert api.setMyDeliveryZones(ADMIN, [zone]) == {
        'deliveryZones': [MAKATI]}


def test_noArea_clearsThem_soTheStoreDeliversAnywhere(clean, firebase):
    store, _, _ = shop(firebase)
    api.setMyDeliveryZones(ADMIN, [MAKATI])

    assert api.setMyDeliveryZones(ADMIN, []) == {'deliveryZones': []}
    assert db.read({'_id': store['_id']}, 'Stores',
                   findOne=True)['deliveryZones'] == []


@pytest.mark.parametrize('zones, refusal', [
    ('everywhere', DELIVERY_ZONES_NOT_A_LIST),
    ([MAKATI] * (DELIVERY_ZONES_MAX + 1), TOO_MANY_DELIVERY_ZONES),
    ([MAKATI, 'north'], 'Area 2: points is a list of corners'),
    ([MAKATI, {'points': 'ZQ-secret-7'}],
     'Area 2: points is a list of corners'),
    ([{'points': [*MAKATI['points'][:2],
                  {'latitude': 'ZQ-secret-7', 'longitude': 121}]}],
     'Area 1: a latitude is a number from -90 to 90'),
    ([{'points': [*MAKATI['points'][:2],
                  {'latitude': 14.5, 'longitude': 181}]}],
     'Area 1: a longitude is a number from -180 to 180'),
    ([NORTH, {'points': corners((0, 0), (1, 1), (1, 0), (0, 1))}],
     'Area 2: its sides cross: draw it without crossing lines'),
    ([{'points': corners((0, 0), (0, 1))}], 'Area 1: draw 3 to 100 corners'),
])
def test_aBadArea_isRefused_byItsPosition_andNothingIsSaved(
        clean, firebase, zones, refusal):
    store, _, _ = shop(firebase)

    with pytest.raises(ValueError, match=exactly(refusal)):
        api.setMyDeliveryZones(ADMIN, zones)
    assert db.read({'_id': store['_id']}, 'Stores',
                   findOne=True).get('deliveryZones') is None


def test_onlyAnAdmin_drawsAreas_andOnlyOnItsOwnStore(clean, firebase):
    _, customerId, _ = shop(firebase)

    with pytest.raises(PermissionError):
        api.setMyDeliveryZones(customerId, [MAKATI])
    assert [store.get('deliveryZones')
            for store in db.read({}, 'Stores')] == [None]


# ----------------------------- C. createOrder ----------------------------- #


def test_aDeliveryInsideAnArea_isPlaced(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    api.setMyDeliveryZones(ADMIN, [NORTH, MAKATI])

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal'], 2), 'delivery', HOME)

    assert res['order']['deliveryFee'] == HOME_FEE


def test_aDeliveryOutsideEveryArea_isRefused_andReservesNothing(
        clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    api.setMyDeliveryZones(ADMIN, [MAKATI])

    with pytest.raises(ValueError, match=exactly(OUTSIDE_DELIVERY_ZONES)):
        api.createOrder(customerId, store['_id'],
                        items(products['Pandesal']), 'delivery', FAR)
    assert db.read({}, 'Orders') == []
    assert db.read({'_id': products['Pandesal']['_id']}, 'Products',
                   findOne=True)['inventory'] == \
        products['Pandesal']['inventory']


def test_aStoreWithoutAreas_deliversAnywhere(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'delivery', FAR)

    assert res['order']['fulfillment'] == 'delivery'


def test_aPickup_ignoresTheAreas(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    api.setMyDeliveryZones(ADMIN, [MAKATI])

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'pickup')

    assert res['order']['fulfillment'] == 'pickup'


# --------------------------- D. the customer's pin --------------------------- #


def test_thePinForm_carriesTheAreas_onlyWhenThereAreSome(clean, firebase,
                                                         model):
    store, customerId, _ = shop(firebase, **DELIVERS)
    api.setMyDeliveryZones(ADMIN, [MAKATI])

    res = openPin(model, customerId, store)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form == {'kind': 'deliveryPinForm', 'formId': form['formId'],
                    'storeLocation': STORE_PIN, 'deliveryZones': [MAKATI]}


def test_aPinOutsideTheAreas_isRefused_andOneInsideIsTaken(clean, firebase,
                                                           model):
    store, customerId, _ = shop(firebase, **DELIVERS)
    api.setMyDeliveryZones(ADMIN, [MAKATI])
    first = openPin(model, customerId, store)
    [form] = first['components']
    cid = first['conversationId']
    model()

    with pytest.raises(ValueError, match=exactly(OUTSIDE_DELIVERY_ZONES)):
        customerSays(customerId, store['_id'], None, cid, pinOf(form, FAR))

    model(reply('House number, street and a landmark?'))
    second = customerSays(customerId, store['_id'], None, cid, pinOf(form))
    assert second['userText'] == 'Deliver to my pin at 14.55470, 121.02440'


# --------------------------- E. the admin's map --------------------------- #


def adminSays(text, conversationId=None, form=None) -> dict:
    return api.sendAdminChatMessage(ADMIN, text, conversationId, form)


def test_theAreasForm_opensWithTheAreasSoFar(clean, firebase, model):
    shop(firebase)
    api.setMyDeliveryZones(ADMIN, [MAKATI])
    model(calls(('c1', 'showDeliveryZonesForm', '{}')),
          reply('Draw where you deliver.'))

    res = adminSays('We deliver')

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form == {'kind': 'deliveryZonesForm', 'formId': form['formId'],
                    'storeLocation': STORE_PIN, 'deliveryZones': [MAKATI]}


def test_theAreasAnswer_saysWhatTheStoreSaved(clean, firebase, model):
    shop(firebase, pinned=False)
    model(calls(('c1', 'showDeliveryZonesForm', '{}')),
          reply('Draw where you deliver.'))
    first = adminSays('We deliver')
    [form] = first['components']
    assert (form['storeLocation'], form['deliveryZones']) == (None, [])
    answer = {'kind': 'deliveryZones', 'formId': form['formId']}
    api.setMyDeliveryZones(ADMIN, [MAKATI, NORTH])
    fake = model(reply('Saved. A name for these areas?'))

    second = adminSays(None, first['conversationId'], answer)

    assert second['userText'] == 'Saved 2 delivery areas.'
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'deliveryZones', 'areas': 2}
    model()
    with pytest.raises(ValueError,
                       match=exactly(chatForms.FORM_ALREADY_ANSWERED)):
        adminSays(None, first['conversationId'], answer)


def test_noAreasSaved_isDeliveringAnywhere(clean, firebase, model):
    shop(firebase)
    model(calls(('c1', 'showDeliveryZonesForm', '{}')),
          reply('Draw where you deliver.'))
    first = adminSays('We deliver')
    [form] = first['components']
    model(reply('Noted.'))

    second = adminSays(None, first['conversationId'],
                       {'kind': 'deliveryZones', 'formId': form['formId']})

    assert second['userText'] == 'Deliver anywhere: no delivery areas drawn.'


def test_theAreasForm_isTheAdminsOnly(clean, firebase, model):
    store, customerId, _ = shop(firebase, **DELIVERS)
    fake = model(calls(('c1', 'showDeliveryZonesForm', '{}')),
                 reply('Sorry.'))

    res = customerSays(customerId, store['_id'], 'Where do you deliver?')

    assert res['components'] == []
    assert json.loads(fake.requests[1]['messages'][-1]['content']) == {
        'error': 'Unknown tool showDeliveryZonesForm'}
