"""The store's open switch and its required policy questions.

Covers:
  - unansweredPolicyQuestions (objects/policy.py, pure): returns No hides
    every return question, nonReturnableItems among them; additionalTerms,
    deliveryMethod and deliveryAreas are never required; a store that
    delivers with no zone counts the zones as one; the hours to cancel a
    paid order only with withinHours; unset payment methods are unanswered.
    The client's policyQuestions.ts reads the same constants.
  - Policy.normalized clears nonReturnableItems with acceptsReturns false.
  - setMyStoreOpen (route and twin tool): opening refused, naming each
    unanswered question in plain words; allowed once complete; closing
    always allowed; a new store, and one stored before the switch, closed.
  - createOrder (so the cart checkout too): refused while closed, and while
    open with a required question unanswered (an answer changed after
    opening), never naming the store's questions; taken once open and
    complete.
  - The shop (startGuestSession) carries takingOrders; the workspace's
    getUserStore carries isOpen beside the store row, the AI's getUserStore
    on the store; the policy card carries unansweredQuestions.
  - The storeNotOpen tool warning: once complete and closed, never while a
    question is left or once open.
"""
import json
import re

import pytest

import chatCards
import toolWarnings
from actions.StoreActions import (OPEN_NEEDS_POLICY, STORE_NOT_OPEN,
                                  STORE_NOT_READY)
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (OPEN_STORE_ANSWERS, TOKEN, addProduct,
                      assertExactShape, createTestStore, guestAt,
                      guestClaims, storeWithProducts, takePayments,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (POLICY_ANSWER_FIELDS, POLICY_QUESTION_LABELS,
                     CustomerSessionResponse, MyStoreScreenResponse, Policy,
                     StoreOpenResponse, policyQuestionsText,
                     unansweredPolicyQuestions)
from sharedConstants import (POLICY_OPTIONAL_QUESTIONS,
                             POLICY_RETURN_QUESTIONS, POLICY_ZONES_QUESTION)
from test_chat import ADMIN as CHAT_ADMIN
from test_chat import FAR, clock, frozenNow, model, offline
from test_toolWarnings import adminRuns, at, told

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'OrderLocks', 'Carts', 'Conversations', 'ChatTurns',
    'ChatUsage', 'ToolWarningsShown'
]
ADMIN = 'so-admin'
GUEST = 'so-guest-uid'
CASH = {'method': 'cash', 'provider': None, 'eWallets': None, 'banks': None,
        'checkPayees': None}
# Every required question answered, pickup only, no returns.
COMPLETE = {'authorizedPaymentMethods': [CASH],
            'paidOrderCancellation': 'notAllowed',
            'deliveryOptions': ['pickup'], 'handoverDays': 1,
            'acceptsReturns': False, 'defectReportDays': 7}
# A delivery zone with its policy (setMyDeliveryZones).
ZONE = {'name': 'Makati', 'deliveryFee': 60, 'deliveryDays': 1,
        'minimumOrder': None, 'freeDeliveryFrom': None,
        'points': [{'latitude': 14.54, 'longitude': 121.00},
                   {'latitude': 14.54, 'longitude': 121.04},
                   {'latitude': 14.57, 'longitude': 121.04},
                   {'latitude': 14.57, 'longitude': 121.00}]}

api = ApiRequests()
tools = ToolRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def answers(**given) -> dict:
    return {**dict.fromkeys(POLICY_ANSWER_FIELDS), **given}


def storedStore(storeId: str) -> dict:
    return db.read({'_id': storeId}, 'Stores', findOne=True)


def refusal(call, *args) -> str:
    with pytest.raises(ValueError) as caught:
        call(*args)
    return str(caught.value)


# ------------------ A. the required questions (pure) ------------------ #


def test_nothingAnswered_isEveryRequiredQuestion_inTheChatsOrder():
    assert unansweredPolicyQuestions(answers(), None) == [
        'authorizedPaymentMethods', 'paidOrderCancellation',
        'deliveryOptions', 'handoverDays', 'acceptsReturns',
        'defectReportDays']


def test_aCompletePolicy_leavesNothing():
    assert unansweredPolicyQuestions(answers(**COMPLETE), None) == []


def test_returnsNo_hidesEveryReturnQuestion_nonReturnableItemsToo():
    policy = answers(**COMPLETE)
    assert 'nonReturnableItems' in POLICY_RETURN_QUESTIONS
    assert unansweredPolicyQuestions(policy, None) == []

    returnsOn = {**policy, 'acceptsReturns': True}
    assert unansweredPolicyQuestions(returnsOn, None) == list(
        POLICY_RETURN_QUESTIONS)
    # The defect days apply whatever the return answer.
    noDefect = {**policy, 'defectReportDays': None}
    assert unansweredPolicyQuestions(noDefect, None) == ['defectReportDays']


@pytest.mark.parametrize('optional', POLICY_OPTIONAL_QUESTIONS)
def test_anOptionalQuestion_isNeverRequired(optional):
    assert set(POLICY_OPTIONAL_QUESTIONS) == {
        'additionalTerms', 'deliveryMethod', 'deliveryAreas'}
    delivers = answers(**{**COMPLETE, 'deliveryOptions': ['delivery']},
                       **{optional: None})
    assert optional not in unansweredPolicyQuestions(delivers, [ZONE])
    assert unansweredPolicyQuestions(delivers, [ZONE]) == []


def test_deliveryWithoutAZone_countsTheZonesAsOne():
    delivers = answers(**{**COMPLETE, 'deliveryOptions': ['pickup',
                                                           'delivery']})
    assert unansweredPolicyQuestions(delivers, None) == [
        POLICY_ZONES_QUESTION]
    assert unansweredPolicyQuestions(delivers, []) == [POLICY_ZONES_QUESTION]
    # A zone drawn before zones had policies is no zone.
    assert unansweredPolicyQuestions(delivers, [{'points': ZONE['points']}]
                                     ) == [POLICY_ZONES_QUESTION]
    assert unansweredPolicyQuestions(delivers, [ZONE]) == []
    # Pickup only: no zone needed.
    assert unansweredPolicyQuestions(answers(**COMPLETE), []) == []


def test_theHoursToCancel_applyOnlyWithinHours():
    policy = answers(**COMPLETE)
    assert unansweredPolicyQuestions(
        {**policy, 'paidOrderCancellation': 'beforeHandover'}, None) == []
    assert unansweredPolicyQuestions(
        {**policy, 'paidOrderCancellation': 'withinHours'}, None) == [
            'paidOrderCancellationHours']


def test_handoverDays_applyOnlyWithPickup():
    deliveryOnly = answers(**{**COMPLETE, 'deliveryOptions': ['delivery'],
                              'handoverDays': None})
    assert unansweredPolicyQuestions(deliveryOnly, [ZONE]) == []
    pickup = answers(**{**COMPLETE, 'handoverDays': None})
    assert unansweredPolicyQuestions(pickup, None) == ['handoverDays']


@pytest.mark.parametrize('methods', [None, []])
def test_unsetPaymentMethods_areUnanswered(methods):
    policy = answers(**{**COMPLETE, 'authorizedPaymentMethods': methods})
    assert unansweredPolicyQuestions(policy, None) == [
        'authorizedPaymentMethods']


def test_aMissingAnswer_isUnanswered_andEveryQuestionHasPlainWords():
    """A stored policy may lack a key (one from before the answer)."""
    assert unansweredPolicyQuestions({}, None) == unansweredPolicyQuestions(
        answers(), None)
    for words in POLICY_QUESTION_LABELS.values():
        assert words and words == words.strip() and not re.search(
            r'[A-Z][a-z]+[A-Z]', words)


def test_theClient_readsTheSameConstants():
    """client/lib/policyQuestions.ts counts an old policy card with the
    server's own lists (generated serverConstants.ts), so the two cannot
    drift: the file imports them and keeps no list of its own."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'client' / 'lib' /
              'policyQuestions.ts').read_text(encoding='utf-8')
    for name in ('POLICY_OPTIONAL_QUESTIONS', 'POLICY_RETURN_QUESTIONS',
                 'POLICY_ZONES_QUESTION'):
        assert name in source
    assert "OPTIONAL_QUESTIONS: PolicyQuestion[] = [" not in source
    assert "RETURN_QUESTIONS: PolicyQuestion[] = [" not in source


# ---------------- B. normalization clears nonReturnableItems ---------------- #


def test_normalized_clearsNonReturnableItems_withReturnsOff():
    import datetime
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    policy = Policy(storeId='s', createdAt=now, updatedAt=now,
                    acceptsReturns=False, nonReturnableItems='cakes',
                    defectReportDays=3)
    normalized = policy.normalized()
    assert normalized.nonReturnableItems is None
    assert normalized.defectReportDays == 3
    kept = Policy(storeId='s', createdAt=now, updatedAt=now,
                  acceptsReturns=True, nonReturnableItems='cakes')
    assert kept.normalized().nonReturnableItems == 'cakes'


def test_updateMyStorePolicy_clearsNonReturnableItems_andRefusesItSentNow(
        clean):
    createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, acceptsReturns=True, returnWindowDays=7,
                              nonReturnableItems='cakes')

    res = tools.updateMyStorePolicy(ADMIN, acceptsReturns=False)

    assert res['nonReturnableItems'] is None
    message = refusal(lambda: tools.updateMyStorePolicy(
        ADMIN, nonReturnableItems='bread'))
    assert message == ('nonReturnableItems applies only when returns are '
                       'accepted (acceptsReturns): ask again')


# ------------------------- C. the open switch ------------------------- #


def test_aNewStore_isClosed_andTheWorkspaceSaysSo(clean):
    store = createTestStore(ADMIN)

    assert storedStore(store['_id'])['isOpen'] is False
    screen = api.getUserStore(ADMIN)
    assertExactShape(MyStoreScreenResponse, screen)
    assert screen['isOpen'] is False
    assert 'isOpen' not in screen['store']
    # The AI's store sends isOpen only while open.
    assert 'isOpen' not in tools.getUserStore(ADMIN)['store']


def test_openingWithQuestionsLeft_isRefused_namingThemInPlainWords(clean):
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, authorizedPaymentMethods=[CASH],
                              deliveryOptions=['pickup', 'delivery'])
    missing = ['paidOrderCancellation', POLICY_ZONES_QUESTION, 'handoverDays',
               'acceptsReturns', 'defectReportDays']
    before = storedStore(store['_id'])

    for call in (api.setMyStoreOpen, tools.setMyStoreOpen):
        with pytest.raises(ValueError, match=exactly(OPEN_NEEDS_POLICY.format(
                missing=policyQuestionsText(missing)))) as caught:
            call(ADMIN, True)
        assert 'paidOrderCancellation' not in str(caught.value)
        assert 'draw your delivery zones on the map' in str(caught.value)

    assert storedStore(store['_id']) == before


def test_openingACompleteStore_opens_andClosingAlwaysWorks(clean):
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, **COMPLETE)

    opened = api.setMyStoreOpen(ADMIN, True)

    assertExactShape(StoreOpenResponse, opened)
    assert opened == {'isOpen': True}
    assert storedStore(store['_id'])['isOpen'] is True
    assert api.getUserStore(ADMIN)['isOpen'] is True
    assert tools.getUserStore(ADMIN)['store']['isOpen'] is True

    # Closing works even with a question left.
    tools.updateMyStorePolicy(ADMIN, clear=['defectReportDays'])
    assert tools.setMyStoreOpen(ADMIN, False) == {'isOpen': False}
    assert storedStore(store['_id'])['isOpen'] is False
    assert api.getUserStore(ADMIN)['isOpen'] is False


def test_aStoreStoredBeforeTheSwitch_readsAsClosed(clean):
    store = createTestStore(ADMIN)
    db.db['Stores'].update_one({'_id': store['_id']},
                               {'$unset': {'isOpen': ''}})

    assert api.getUserStore(ADMIN)['isOpen'] is False
    assert 'isOpen' not in tools.getUserStore(ADMIN)['store']


def test_openRefusesANonBool_aCustomer_andAStoreNotSetUp(clean, firebase):
    store = createTestStore(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    assert refusal(api.setMyStoreOpen, ADMIN, 'yes') == 'open is true or false'
    with pytest.raises(PermissionError):
        api.setMyStoreOpen(customerId, False)
    from conftest import createTestUser
    createTestUser('so-no-store')
    assert refusal(api.setMyStoreOpen, 'so-no-store', False) == (
        'Create your store first')


# --------------------------- D. createOrder --------------------------- #


def test_createOrder_atAClosedStore_isRefused(clean, firebase):
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, **COMPLETE)
    pandesal = addProduct(store['_id'], 'Pandesal', 5)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    order = [{'productId': pandesal['_id'], 'quantity': 1}]

    with pytest.raises(ValueError, match=exactly(STORE_NOT_OPEN)):
        tools.createOrder(customerId, store['_id'], order)
    tools.addToCart(customerId, store['_id'], order)
    with pytest.raises(ValueError, match=exactly(STORE_NOT_OPEN)):
        tools.checkoutMyCart(customerId, store['_id'])
    with pytest.raises(ValueError, match=exactly(STORE_NOT_OPEN)):
        tools.previewMyCheckout(customerId, store['_id'])
    assert db.read({}, 'Orders') == []

    api.setMyStoreOpen(ADMIN, True)
    res = tools.createOrder(customerId, store['_id'], order)
    assert res['order']['status'] == 'pending'


def test_createOrder_atAnOpenStore_withAQuestionLeft_isRefused(clean,
                                                               firebase):
    """Returns turned on after opening adds questions: orders stop until
    they are answered, and the store stays open."""
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, **COMPLETE)
    api.setMyStoreOpen(ADMIN, True)
    pandesal = addProduct(store['_id'], 'Pandesal', 5)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    order = [{'productId': pandesal['_id'], 'quantity': 1}]

    # updateMyStorePolicy itself needs the return window with returns on:
    # the rest is left for later.
    tools.updateMyStorePolicy(ADMIN, acceptsReturns=True, returnWindowDays=7)

    with pytest.raises(ValueError, match=exactly(STORE_NOT_READY)) as caught:
        tools.createOrder(customerId, store['_id'], order)
    assert 'returnConditions' not in str(caught.value)
    assert storedStore(store['_id'])['isOpen'] is True
    assert db.read({}, 'Orders') == []

    tools.updateMyStorePolicy(ADMIN, returnConditions='unused',
                              nonReturnableItems='cakes',
                              refundMethod='originalPayment',
                              returnShippingPaidBy='customer')
    assert tools.createOrder(customerId, store['_id'],
                             order)['order']['status'] == 'pending'


def test_aStoreThatDelivers_withNoZoneYet_stillTakesPickups(clean, firebase):
    """The zones stop only a delivery (bookableFulfillments), never a
    pickup: the open store still takes orders, though it cannot open while
    they are missing."""
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, **COMPLETE)
    api.setMyStoreOpen(ADMIN, True)
    tools.updateMyStorePolicy(ADMIN, deliveryOptions=['pickup', 'delivery'])
    pandesal = addProduct(store['_id'], 'Pandesal', 5)
    customerId = verifiedGuestAt(firebase, store, GUEST)

    res = tools.createOrder(customerId, store['_id'],
                            [{'productId': pandesal['_id'], 'quantity': 1}])

    assert res['order']['fulfillment'] == 'pickup'
    api.setMyStoreOpen(ADMIN, False)
    assert POLICY_QUESTION_LABELS[POLICY_ZONES_QUESTION] in refusal(
        api.setMyStoreOpen, ADMIN, True)


def test_theTestStores_openForOrders_withOnlyTheMissingAnswers(clean):
    """conftest's takePayments opens the store, giving only the answers it
    lacks (OPEN_STORE_ANSWERS)."""
    store = createTestStore(ADMIN)
    tools.updateMyStorePolicy(ADMIN, handoverDays=4)
    takePayments(store)
    policy = db.read({'storeId': store['_id']}, 'Policies', findOne=True)
    assert policy['handoverDays'] == 4
    assert policy['acceptsReturns'] == OPEN_STORE_ANSWERS['acceptsReturns']
    assert storedStore(store['_id'])['isOpen'] is True


# ------------------------------ E. the shop ------------------------------ #


def _shopStore(firebase) -> dict:
    firebase['claims'] = guestClaims(GUEST)
    res = api.startGuestSession(TOKEN, 'bakery-a')
    assertExactShape(CustomerSessionResponse, {
        **res, 'store': {**res['store'], 'takingOrders': True}})
    return res['store']


def test_theShop_carriesWhetherTheStoreTakesOrders(clean, firebase):
    store = createTestStore(ADMIN)

    assert 'takingOrders' not in _shopStore(firebase)

    tools.updateMyStorePolicy(ADMIN, **COMPLETE)
    api.setMyStoreOpen(ADMIN, True)
    assert _shopStore(firebase)['takingOrders'] is True
    # isOpen itself is the owner's: the shop gets only takingOrders.
    assert 'isOpen' not in _shopStore(firebase)

    # Open, but a question left: no orders.
    tools.updateMyStorePolicy(ADMIN, clear=['handoverDays'])
    assert 'takingOrders' not in _shopStore(firebase)
    assert storedStore(store['_id'])['isOpen'] is True


def test_theCustomerPrompt_saysAClosedStoreTakesNoOrders():
    import chatModel
    assert "isn\\'t taking orders yet" not in chatModel.CHAT_PROMPTS[
        'customer']
    assert "isn't taking orders yet" in chatModel.CHAT_PROMPTS['customer']
    admin = chatModel.CHAT_PROMPTS['admin']
    assert 'setMyStoreOpen' in admin and 'once at most' in admin


# ------------------------- F. the policy card ------------------------- #


def test_thePolicyCard_carriesTheServersUnansweredQuestions():
    context = chatCards.CardContext('store-1')
    context._currency = 'PHP'
    result = {**answers(**{**COMPLETE, 'acceptsReturns': True}),
              'deliveryZones': [], 'updatedAt': None}
    step = [{'role': 'assistant', 'toolCalls': [
        {'id': 'c1', 'name': 'getMyStorePolicy', 'arguments': '{}'}]},
        {'role': 'tool', 'toolCallId': 'c1',
         'content': json.dumps(result)}]

    [card] = chatCards.build(step, {}, context)

    assert card['kind'] == 'policy'
    assert card['unansweredQuestions'] == list(POLICY_RETURN_QUESTIONS)


def test_aStoredCardWithoutTheField_stillValidates():
    from objects import PolicyCard
    card = PolicyCard.model_validate({
        'kind': 'policy', 'currency': 'PHP', **answers()})
    assert card.unansweredQuestions is None


# ------------------------ G. the storeNotOpen warning ------------------------ #


def test_storeNotOpen_isTold_onceCompleteAndClosed(model, monkeypatch):
    """adminRuns chats as test_chat's admin (CHAT_ADMIN)."""
    try:
        wipeCollections(COLLECTIONS)
        at(monkeypatch, FAR)
        createTestStore(CHAT_ADMIN)
        # Questions left: no storeNotOpen.
        assert 'storeNotOpen' not in told(adminRuns(model,
                                                    'getMyStorePolicy'))

        tools.updateMyStorePolicy(CHAT_ADMIN, **COMPLETE)
        assert told(adminRuns(model, 'getMyStorePolicy')).get(
            'storeNotOpen') == (1, ['isOpen'])
        # Told once: getUserStore shares its cooldown.
        assert 'storeNotOpen' not in told(adminRuns(model, 'getUserStore'))

        at(monkeypatch, FAR + toolWarnings.SETUP_COOLDOWN)
        assert told(adminRuns(model, 'getUserStore')).get(
            'storeNotOpen') == (1, ['isOpen'])

        api.setMyStoreOpen(CHAT_ADMIN, True)
        at(monkeypatch, FAR + 3 * toolWarnings.SETUP_COOLDOWN)
        assert 'storeNotOpen' not in told(adminRuns(model, 'getUserStore'))
        assert 'storeNotOpen' not in told(adminRuns(model,
                                                    'getMyStorePolicy'))
    finally:
        wipeCollections(COLLECTIONS)
