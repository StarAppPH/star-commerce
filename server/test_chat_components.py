"""The chat's cards and forms (chatCards.py, chatForms.py): the toolsets,
the setup chat of an admin whose store is not set up yet, the cards built
from what the tools returned, the forms the form tools open, and form
answers, checked against what they answer and acted on by the model.

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import re

import pytest

import chatCards
import chatForms
import chatModel
import chatTools
import mcpServer
from actions import ChatActions
from ApiRequests import ApiRequests
from conftest import (TOKEN, addOrder, assertExactShape, createTestStore,
                      firebaseClaims, guestAt, storeWithProducts,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (CHAT_COMPONENTS_PER_TURN_MAX, ChatReplyResponse,
                     ConversationTurnsResponse)
from test_chat import (ADMIN, GUEST, calls, clean, clock, frozenNow, model,
                       offline, reply, stubTool)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clean', 'clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Returns', 'Conversations', 'ChatTurns', 'ChatUsage'
]
api = ApiRequests()

# An order row's delivery fields for a pickup (OrderDeliveryRow).
PICKUP = {'fulfillment': 'pickup', 'deliveryFee': 0,
          'deliveryDistanceKm': None, 'deliveryAddress': None,
          'deliveryLatitude': None, 'deliveryLongitude': None,
          'deliveryNotes': None}


@pytest.fixture
def wiped():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def adminSays(text, conversationId=None, form=None, userId=ADMIN) -> dict:
    return api.sendAdminChatMessage(userId, text, conversationId, form)


def customerSays(customerId, storeId, text, conversationId=None,
                 form=None) -> dict:
    return api.sendStoreChatMessage(customerId, storeId, text,
                                    conversationId, form)


def signedUpAdmin(firebase) -> str:
    """An admin as sign-up leaves them: a user, an empty store (no slug)
    and an empty policy."""
    firebase['claims'] = firebaseClaims()
    return api.loginWithGoogle({'idToken': TOKEN})['_id']


def turns(conversationId: str) -> list[dict]:
    return sorted(db.read({'conversationId': conversationId}, 'ChatTurns'),
                  key=lambda turn: turn['seq'])


def lastUserContent(fake) -> str:
    return fake.requests[-1]['messages'][-1]['content'] if fake.requests[
        -1]['messages'][-1]['role'] == 'user' else next(
            m['content'] for m in reversed(fake.requests[-1]['messages'])
            if m['role'] == 'user')


def formAnswerDetails(content: str) -> dict:
    """The details a form answer's userContent carries for the model."""
    marker = '\n\n' + chatForms.FORM_ANSWER_PREFIX
    assert marker in content
    return json.loads(content.split(marker, 1)[1])


# ----------------------------- A. toolsets ----------------------------- #


def test_setupToolset_isTheStoreSetupOnly():
    names = [t['function']['name'] for t in chatTools.TOOLS['adminSetup']]
    assert names == ['createUserStore', 'getUserStore',
                     'showStoreDetailsForm']
    assert set(chatTools.TOOL_CALLS['adminSetup']) == set(
        chatForms.SETUP_ROUTE_TOOLS)
    assert 'adminSetup' in chatModel.CHAT_PROMPTS
    assert 'decline in one short sentence' in chatModel.CHAT_PROMPTS[
        'adminSetup']


def test_formTools_areTheChatsOwn_neverOnTheMcpServers():
    import asyncio

    async def listed(audience):
        return {t.name for t in await mcpServer.servers[audience].list_tools()}

    for audience in ('admin', 'customer'):
        mcpNames = asyncio.run(listed(audience))
        chatNames = {t['function']['name']
                     for t in chatTools.TOOLS[audience]}
        forms = chatForms.formToolNames(audience)
        assert forms and forms <= chatNames
        assert not forms & mcpNames
        assert chatNames - forms == mcpNames


def test_formToolSchemas_takeNoIdentity():
    for toolset in ('admin', 'customer', 'adminSetup'):
        for tool in chatForms.formToolSchemas(toolset):
            properties = tool['function']['parameters'].get('properties', {})
            assert 'userId' not in properties
            assert 'storeId' not in properties


def test_bothPrompts_tellTheModelAboutCardsAndFormAnswers():
    for toolset in ('admin', 'customer', 'adminSetup'):
        assert '"Form answer:"' in chatModel.CHAT_PROMPTS[toolset]
    assert 'never repeat what a card shows' in chatModel.CHAT_PROMPTS['admin']
    assert 'never repeat what a card shows' in chatModel.CHAT_PROMPTS[
        'customer']


# ----------------------------- B. the setup chat ----------------------------- #


def test_anAdminWithAnUnsetStore_chatsWithTheSetupTools(wiped, firebase,
                                                        model):
    adminId = signedUpAdmin(firebase)
    fake = model(
        calls(('c1', 'showStoreDetailsForm',
               json.dumps({'name': 'Kalye Coffee', 'currency': 'USD'}))),
        reply('Fill in your store details below.'))

    res = adminSays('I want to sell coffee online', userId=adminId)

    assertExactShape(ChatReplyResponse, res)
    request = fake.requests[0]
    assert request['messages'][0]['content'] == chatModel.CHAT_PROMPTS[
        'adminSetup']
    assert request['tools'] == chatTools.TOOLS['adminSetup']
    assert json.loads(fake.requests[1]['messages'][-1]['content']) == {
        'result': chatForms.FORM_SHOWN}
    [form] = res['components']
    assert form['kind'] == 'storeDetailsForm'
    assert form['name'] == 'Kalye Coffee' and form['currency'] == 'USD'
    assert form['description'] is None and form['contactNumber'] is None
    assert form['formId']
    # The setup chat is listed like any other.
    listed = api.listMyConversations(adminId, None, None, 10)
    assert [row['_id'] for row in listed['conversations']] == [
        res['conversationId']]


def test_theStoreDetailsAnswer_isActedOnByTheModel(wiped, firebase, model):
    adminId = signedUpAdmin(firebase)
    model(calls(('c1', 'showStoreDetailsForm', '{}')), reply('Fill it in.'))
    first = adminSays('Set up my store', userId=adminId)
    formId = first['components'][0]['formId']
    answer = {'kind': 'storeDetails', 'formId': formId,
              'name': 'Kalye Coffee', 'description': 'Small-batch coffee',
              'currency': 'PHP', 'contactNumber': '+639171234567',
              'address': '14 Maginhawa St', 'openingHours': ''}
    created = json.dumps({key: answer[key] for key in (
        'name', 'description', 'currency', 'contactNumber', 'address')})
    fake = model(calls(('c2', 'createUserStore', created)),
                 reply('Your store is live.'))

    res = adminSays(None, first['conversationId'], answer, userId=adminId)

    content = fake.requests[0]['messages'][-1]['content']
    assert content.startswith('Store details: Kalye Coffee · PHP · '
                              '+639171234567 · 14 Maginhawa St')
    # The reply carries the summary the user sees in place of the form.
    assert res['userText'] == content.split('\n\n')[0]
    details = formAnswerDetails(content)
    assert details['kind'] == 'storeDetails'
    assert details['openingHours'] is None  # "" is no answer
    [card] = res['components']
    assert card['kind'] == 'store' and card['name'] == 'Kalye Coffee'
    assert isinstance(card['slug'], str)
    saved = turns(first['conversationId'])
    assert saved[1]['userText'].startswith('Store details: Kalye Coffee')
    assert saved[1]['userContent'] == content
    assert saved[1]['formAnswer']['formId'] == formId
    # The store is set up now: the next message gets the admin's tools.
    fake = model(reply('What next?'))
    adminSays('Thanks', first['conversationId'], userId=adminId)
    assert fake.requests[0]['tools'] == chatTools.TOOLS['admin']
    assert fake.requests[0]['messages'][0]['content'] == (
        chatModel.CHAT_PROMPTS['admin'])
    # The form answer is replayed to the model as it read it.
    history = [m['content'] for m in fake.requests[0]['messages']
               if m['role'] == 'user']
    assert content in history


def test_aFormAnswer_isRefusedBeforeTheModelRuns(wiped, firebase, model):
    adminId = signedUpAdmin(firebase)
    model(calls(('c1', 'showStoreDetailsForm', '{}')), reply('Fill it in.'))
    first = adminSays('Set up my store', userId=adminId)
    cid = first['conversationId']
    formId = first['components'][0]['formId']
    good = {'kind': 'storeDetails', 'formId': formId, 'name': 'A',
            'description': 'B', 'currency': 'PHP',
            'contactNumber': '+639171234567', 'address': 'C'}
    fake = model()

    refusals = [
        (lambda: adminSays(None, cid, {**good, 'formId': 'no-such-form'},
                           userId=adminId), chatForms.FORM_NOT_FOUND),
        (lambda: adminSays(None, cid, {'kind': 'location', 'formId': formId,
                                       'latitude': 1, 'longitude': 2},
                           userId=adminId), chatForms.FORM_DOES_NOT_MATCH),
        (lambda: adminSays('hello', cid, good, userId=adminId),
         chatForms.MESSAGE_AND_FORM),
        (lambda: adminSays(None, None, good, userId=adminId),
         chatForms.FORM_NEEDS_CONVERSATION),
    ]
    for send, message in refusals:
        with pytest.raises(ValueError, match='^' + re.escape(message) + '$'):
            send()
    with pytest.raises(ValueError) as error:
        adminSays(None, cid, {**good, 'name': 'SECRET-VALUE' * 50},
                  userId=adminId)
    assert str(error.value) == chatForms.FORM_ANSWER_INVALID.format(
        fields='storeDetails.name')
    assert 'SECRET-VALUE' not in str(error.value)
    with pytest.raises(ValueError, match='^That form answer is not valid'):
        adminSays(None, cid, {'kind': 'nope', 'formId': formId},
                  userId=adminId)
    assert fake.requests == []
    # Refused under the lock, which it released: the conversation goes on.
    model(calls(('c2', 'getUserStore', '{}')), reply('ok'))
    adminSays(None, cid, good, userId=adminId)
    # A form is answered once.
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.FORM_ALREADY_ANSWERED + '$'):
        adminSays(None, cid, good, userId=adminId)


def test_anAdminWithNoStoreAtAll_isStillRefused(wiped, model):
    from conftest import createTestUser
    from actions.StoreActions import NO_STORE_YET
    model()
    createTestUser('cc-no-store')
    with pytest.raises(ValueError, match='^' + re.escape(NO_STORE_YET)):
        adminSays('hello', userId='cc-no-store')
    with pytest.raises(ValueError, match='^' + re.escape(NO_STORE_YET)):
        api.listMyConversations('cc-no-store', None, None, 10)


# ----------------------------- C. cards ----------------------------- #


def test_theShopsProductCard_isTheRoutesResult_andOrdersThroughTheModel(
        wiped, firebase, model):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    model(calls(('c1', 'listStoreProducts', '{}')),
          reply('Here is what we have (Ube Cake is 1 peso!).'))

    first = customerSays(customerId, store['_id'], 'What do you sell?')

    assertExactShape(ChatReplyResponse, first)
    [card] = first['components']
    assert card['kind'] == 'shopProducts' and card['formId']
    assert card['currency'] == 'PHP'
    byName = {p['name']: p for p in card['products']}
    # The card shows the route's prices, never the model's words.
    assert byName['Ube Cake']['price'] == 650
    assert 'Old Recipe' not in byName  # hidden from customers
    assert set(byName['Pandesal']) == {'_id', 'name', 'description',
                                       'price', 'inventory', 'photoUrl'}

    picked = [{'productId': byName['Pandesal']['_id'], 'quantity': 3},
              {'productId': byName['Ube Cake']['_id'], 'quantity': 1}]
    fake = model(calls(('c2', 'createOrder', json.dumps({'items': picked}))),
                 reply('Your order is placed: choose how to pay below.'))
    second = customerSays(customerId, store['_id'], None,
                          first['conversationId'],
                          {'kind': 'orderItems', 'formId': card['formId'],
                           'items': picked})

    content = fake.requests[0]['messages'][-1]['content']
    assert content.startswith('Order 3 × Pandesal, 1 × Ube Cake')
    assert formAnswerDetails(content)['items'] == picked
    [checkout] = second['components']
    assert checkout['kind'] == 'checkout' and checkout['formId']
    assert checkout['order']['status'] == 'pending'
    assert checkout['order']['total'] == 665
    assert checkout['paymentMethods']

    method = checkout['paymentMethods'][0]
    fake = model(calls(('c3', 'payOrder', json.dumps({
        'orderId': checkout['order']['_id'], 'paymentMethod': method}))),
        reply('Paid, thank you!'))
    third = customerSays(customerId, store['_id'], None,
                         first['conversationId'],
                         {'kind': 'payment', 'formId': checkout['formId'],
                          'paymentMethod': method})
    details = formAnswerDetails(fake.requests[0]['messages'][-1]['content'])
    assert details == {'kind': 'payment',
                       'orderId': checkout['order']['_id'],
                       'paymentMethod': method}
    [paid] = third['components']
    assert paid['kind'] == 'myOrders'
    assert paid['orders'][0]['status'] == 'paid'

    # The client shows a form answered by the turn whose answeredFormId it is.
    page = api.getConversationTurns(customerId, first['conversationId'])
    assertExactShape(ConversationTurnsResponse, page)
    bySeq = {turn['seq']: turn for turn in page['turns']}
    assert bySeq[1]['answeredFormId'] == card['formId']
    assert bySeq[2]['answeredFormId'] == checkout['formId']
    assert bySeq[0]['answeredFormId'] is None
    assert bySeq[0]['components'][0]['formId'] == card['formId']


def test_anAnswer_mustPickFromTheCard(wiped, firebase, model):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    model(calls(('c1', 'listStoreProducts', '{}')), reply('Here.'))
    first = customerSays(customerId, store['_id'], 'Menu?')
    card = first['components'][0]
    productId = card['products'][0]['_id']
    model()
    for items, message in (
            ([{'productId': 'not-on-the-card', 'quantity': 1}],
             chatForms.NOT_ON_THE_CARD),
            ([{'productId': productId, 'quantity': 0}],
             chatForms.NOT_ON_THE_CARD),
            ([{'productId': productId, 'quantity': 1}] * 2,
             chatForms.PICKED_TWICE)):
        with pytest.raises(ValueError, match='^' + message + '$'):
            customerSays(customerId, store['_id'], None,
                         first['conversationId'],
                         {'kind': 'orderItems', 'formId': card['formId'],
                          'items': items})
    # Another customer cannot answer it: the conversation is not theirs.
    other = verifiedGuestAt(firebase, store, 'cc-other-guest',
                            '+639181234567')
    with pytest.raises(ValueError, match='^Conversation not found$'):
        customerSays(other, store['_id'], None, first['conversationId'],
                     {'kind': 'orderItems', 'formId': card['formId'],
                      'items': [{'productId': productId, 'quantity': 1}]})


def test_aChangedOrder_updatesItsRowOnTheCardThatListsIt(wiped, model):
    store = createTestStore(ADMIN)
    item = [{'productId': 'p1', 'name': 'Barako', 'price': 620,
             'quantity': 2}]
    paidOne = addOrder(store['_id'], 'cust-1', item, 'paid',
                       paymentMethod='gcash')
    paidTwo = addOrder(store['_id'], 'cust-2', item, 'paid',
                       paymentMethod='maya')
    model(calls(('c1', 'getStoreOrders', json.dumps({'status': 'paid'}))),
          calls(('c2', 'completeOrder',
                 json.dumps({'orderId': paidOne['_id']}))),
          reply('Done: one handed over.'))

    res = adminSays('Hand over the first order')

    [card] = res['components']
    assert card['kind'] == 'storeOrders'
    statuses = {o['_id']: o['status'] for o in card['orders']}
    assert statuses == {paidOne['_id']: 'completed',
                        paidTwo['_id']: 'paid'}
    assert card['formId']  # one paid order left to hand over

    fake = model(calls(('c3', 'completeOrder',
                        json.dumps({'orderId': paidTwo['_id']}))),
                 reply('Handed over.'))
    answered = adminSays(None, res['conversationId'],
                         {'kind': 'handover', 'formId': card['formId'],
                          'orderIds': [paidTwo['_id']]})
    content = fake.requests[0]['messages'][-1]['content']
    assert content.startswith('Handed over:\n- #' + paidTwo['_id'][-6:].upper())
    assert formAnswerDetails(content)['orderIds'] == [paidTwo['_id']]
    [single] = answered['components']
    assert single['kind'] == 'storeOrders' and single['formId'] is None
    assert single['orders'][0]['status'] == 'completed'


def test_aCompletedOrder_cannotBeHandedOverFromTheCard(wiped, model):
    store = createTestStore(ADMIN)
    item = [{'productId': 'p1', 'name': 'Barako', 'price': 620,
             'quantity': 2}]
    paid = addOrder(store['_id'], 'cust-1', item, 'paid',
                    paymentMethod='gcash')
    done = addOrder(store['_id'], 'cust-2', item, 'completed',
                    paymentMethod='gcash')
    model(calls(('c1', 'getStoreOrders', '{}')), reply('Here.'))
    res = adminSays('Orders?')
    card = res['components'][0]
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.NOT_ON_THE_CARD + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'handover', 'formId': card['formId'],
                   'orderIds': [paid['_id'], done['_id']]})


def test_aReturnDecision_needsARequestedReturnAndAReasonToReject(
        wiped, model, monkeypatch):
    store = createTestStore(ADMIN)
    row = {
        '_id': 'ret-1', 'orderId': 'ord-1', 'status': 'requested',
        'items': [{'name': 'Dripper', 'price': 850, 'quantity': 1}],
        'reason': 'defective', 'note': 'Cracked', 'refundAmount': 850,
        'currency': 'PHP', 'createdAt': '2099-06-15T12:00:00Z',
        'decidedAt': None, 'rejectReason': None, 'cancelledAt': None,
        'receivedAt': None, 'refundedAt': None,
        'contactNumber': '+639171234567', 'restocked': None,
        'refundReference': None,
    }
    stubTool(monkeypatch, 'admin', 'getStoreReturns',
             lambda userId, storeId, args: {'returns': [row]})
    model(calls(('c1', 'getStoreReturns', '{}')), reply('One request.'))
    res = adminSays('Returns?')
    [card] = res['components']
    assert card['kind'] == 'storeReturns' and card['formId']
    assert card['returns'][0]['note'] == 'Cracked'
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.REJECT_NEEDS_REASON + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'returnDecision', 'formId': card['formId'],
                   'returnId': 'ret-1', 'decision': 'reject',
                   'reason': '  '})
    with pytest.raises(ValueError, match='^' + chatForms.NOT_ON_THE_CARD):
        adminSays(None, res['conversationId'],
                  {'kind': 'returnDecision', 'formId': card['formId'],
                   'returnId': 'ret-2', 'decision': 'approve'})
    fake = model(reply('Rejected.'))
    adminSays(None, res['conversationId'],
              {'kind': 'returnDecision', 'formId': card['formId'],
               'returnId': 'ret-1', 'decision': 'reject',
               'reason': 'Dropped by the customer'})
    content = fake.requests[0]['messages'][-1]['content']
    assert content.startswith('Reject the return of 1 × Dripper: Dropped')
    assert formAnswerDetails(content) == {
        'kind': 'returnDecision', 'returnId': 'ret-1', 'decision': 'reject',
        'reason': 'Dropped by the customer'}
    assert store['_id']


def test_aRefusedTool_showsNoCard(wiped, model):
    createTestStore(ADMIN)
    model(calls(('c1', 'completeOrder', json.dumps({'orderId': 'nope'}))),
          reply('That order does not exist.'))
    res = adminSays('Complete order nope')
    assert res['components'] == []


def test_aTurn_keepsItsLatestComponents(wiped, model):
    createTestStore(ADMIN)
    script = [calls((f'c{i}', 'getMyStorePolicy', '{}'))
              for i in range(CHAT_COMPONENTS_PER_TURN_MAX + 2)]
    model(*script, reply('Here it is, many times.'))
    res = adminSays('Policy please, again and again')
    assert len(res['components']) == CHAT_COMPONENTS_PER_TURN_MAX
    assert {c['kind'] for c in res['components']} == {'policy'}


def test_formTools_openTheirForms(wiped, firebase, model):
    store = storeWithProducts(ADMIN)
    model(calls(('c1', 'showPolicyForm', json.dumps({'section': 'delivery'})),
                ('c2', 'showNewProductsForm', json.dumps({'products': [
                    {'name': 'Barako', 'price': 520},
                    {'name': 'Bad price', 'price': -3}]})),
                ('c3', 'showStoreLocationForm', '{}'),
                ('c4', 'showPhotoRequestForm', '{}')),
          reply('Fill these in.'))
    res = adminSays('Set up delivery and add products')
    kinds = [c['kind'] for c in res['components']]
    # No photo style yet: the photo form refused, the others opened.
    assert kinds == ['policyForm', 'newProductsForm', 'locationForm']
    policyForm, productsForm, locationForm = res['components']
    assert policyForm['section'] == 'delivery'
    assert policyForm['deliveryOptions'] is None
    assert productsForm['products'][0] == {
        'name': 'Barako', 'price': 520, 'inventory': None,
        'description': None}
    assert productsForm['products'][1]['price'] is None
    assert locationForm['location'] is None
    assert locationForm['address'] == store['address']

    customerId = guestAt(firebase, store, GUEST)
    model(calls(('c5', 'showPhoneVerification', '{}')),
          reply('Verify your number below.'))
    shown = customerSays(customerId, store['_id'], 'Order a cake')
    [verify] = shown['components']
    assert verify['kind'] == 'phoneVerificationForm'
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.PHONE_NOT_VERIFIED + '$'):
        customerSays(customerId, store['_id'], None, shown['conversationId'],
                     {'kind': 'phoneVerified', 'formId': verify['formId']})


def test_aPolicyAnswer_keepsItsSectionsFields(wiped, model):
    createTestStore(ADMIN)
    model(calls(('c1', 'showPolicyForm', json.dumps({'section': 'payment'}))),
          reply('Answer below.'))
    res = adminSays('Payment policy')
    form = res['components'][0]
    fake = model(reply('Saved.'))
    adminSays(None, res['conversationId'], {
        'kind': 'policy', 'formId': form['formId'],
        'acceptedPaymentMethods': ['gcash', 'card'],
        'paidOrderCancellation': 'beforeHandover',
        # Not asked by the payment section: dropped.
        'offersFreeDelivery': True})
    content = fake.requests[0]['messages'][-1]['content']
    assert formAnswerDetails(content) == {
        'kind': 'policy', 'acceptedPaymentMethods': ['gcash', 'card'],
        'paidOrderCancellation': 'beforeHandover'}
    assert 'Payment methods: GCash, card' in content
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.FORM_ALREADY_ANSWERED + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'policy', 'formId': form['formId'],
                   'paidOrderCancellation': 'notAllowed'})


def test_cards_fromEveryCardTool_fitTheirModels():
    """A canned result of every card tool makes a valid card."""
    context = chatCards.CardContext('store-1')
    context._currency = 'PHP'
    order = {'_id': 'o1', 'status': 'paid', 'items': [
        {'name': 'A', 'price': 1, 'quantity': 1}], 'total': 1,
        'currency': 'PHP', 'contactNumber': '+639171234567',
        'createdAt': '2099-01-01T00:00:00Z', 'paymentMethod': 'gcash',
        'paidAt': '2099-01-01T00:00:00Z', 'expiresAt': '2099-01-01T00:30:00Z',
        'completedAt': None, 'cancelledAt': None, 'cancelReason': None,
        'refundedAt': None, 'paymentReference': 'x', 'refundReference': None,
        'fulfillment': 'delivery', 'deliveryFee': 50,
        'deliveryDistanceKm': 5.7, 'deliveryAddress': '1 Main St', 'deliveryLatitude': 14.5,
        'deliveryLongitude': 121.0, 'deliveryNotes': None}
    ret = {'_id': 'r1', 'orderId': 'o1', 'status': 'requested', 'items': [
        {'name': 'A', 'price': 1, 'quantity': 1}], 'reason': 'defective',
        'note': None, 'refundAmount': 1, 'currency': 'PHP',
        'createdAt': '2099-01-01T00:00:00Z', 'decidedAt': None,
        'rejectReason': None, 'cancelledAt': None, 'receivedAt': None,
        'refundedAt': None, 'contactNumber': '+639171234567',
        'restocked': None, 'refundReference': None}
    adminProduct = {'_id': 'p1', 'name': 'A', 'description': None,
                    'price': 1, 'inventory': 2, 'reservedQuantity': 1,
                    'isAvailable': True, 'soldCount': 0}
    policy = {field: None for field in chatForms.POLICY_SECTION_FIELDS[
        'returns'] + chatForms.POLICY_SECTION_FIELDS['payment']
        + chatForms.POLICY_SECTION_FIELDS['delivery']
        + chatForms.POLICY_SECTION_FIELDS['other']}
    store = {'_id': 's1', 'name': 'A', 'slug': 'a', 'currency': 'PHP',
             'description': 'd', 'contactNumber': '+639171234567',
             'address': 'x', 'openingHours': None,
             'location': {'latitude': 1, 'longitude': 2}}
    results = {
        'listStoreProducts': {'currency': 'PHP', 'products': [
            {'_id': 'p1', 'name': 'A', 'description': None, 'price': 1,
             'inventory': 2}]},
        'listMyStoreProducts': {'currency': 'PHP', 'products': [adminProduct]},
        'addStoreProducts': {'currency': 'PHP', 'products': [adminProduct]},
        'addStoreProduct': {**adminProduct, 'reservedInventory': [
            {'orderId': 'o1', 'quantity': 1}]},
        'updateStoreProduct': {**adminProduct, 'reservedInventory': []},
        'setProductPhoto': {**adminProduct, 'reservedInventory': [],
                            'photoUrl': 'https://x'},
        'getStoreOrders': {'orders': [order]},
        'completeOrder': order,
        'cancelStoreOrder': order,
        'getMyOrders': {'orders': [order]},
        'payOrder': order,
        'cancelMyOrder': order,
        'createOrder': {'order': {**order, 'status': 'pending'},
                        'paymentMethods': ['gcash']},
        'getStoreReturns': {'returns': [ret]},
        'approveReturn': ret,
        'rejectReturn': ret,
        'receiveReturn': ret,
        'getMyReturns': {'returns': [ret]},
        'requestReturn': ret,
        'cancelMyReturn': ret,
        'getMyStorePolicy': policy,
        'updateMyStorePolicy': policy,
        'getStorePolicy': policy,
        'generateProductPhotos': {'photos': [{'_id': 'g1', 'url': 'https://x'}]},
        'getUserStore': {'store': store},
        'createUserStore': store,
        'setUserStoreLocation': store,
    }
    assert set(results) == set(chatCards.CARDS)
    for name, result in results.items():
        steps = [{'role': 'assistant', 'toolCalls': [
            {'id': 'c1', 'name': name, 'arguments': '{}'}]},
            {'role': 'tool', 'toolCallId': 'c1',
             'content': json.dumps(result)}]
        built = chatCards.build(steps, {}, context)
        assert len(built) == 1, name
    nothing = chatCards.build([
        {'role': 'assistant', 'toolCalls': [
            {'id': 'c1', 'name': 'getUserStore', 'arguments': '{}'}]},
        {'role': 'tool', 'toolCallId': 'c1',
         'content': json.dumps({'store': None})}], {}, context)
    assert nothing == []


def test_aTurnsComponents_areStoredAndPagedWithIt(wiped, model):
    createTestStore(ADMIN)
    model(calls(('c1', 'getMyStorePolicy', '{}')), reply('Your policy.'))
    res = adminSays('Policy?')
    [saved] = turns(res['conversationId'])
    assert saved['components'] == [
        {**c} for c in saved['components']] and len(saved['components']) == 1
    assert saved['components'][0]['kind'] == 'policy'
    assert saved['formAnswer'] is None and saved['userContent'] is None
    page = api.getConversationTurns(ADMIN, res['conversationId'])
    assert page['turns'][0]['components'][0]['kind'] == 'policy'
    assert ChatActions.CHAT_TURNS == chatForms.CHAT_TURNS


def test_cards_aLaterRowUpdatesTheCardThatListsIt():
    """getStoreOrders, then completeOrder of one of its orders: one card,
    the row as it is now, still answerable while a paid order is left."""
    context = chatCards.CardContext('store-1')
    context._currency = 'PHP'

    def order(orderId, status):
        return {'_id': orderId, 'status': status, 'items': [
            {'name': 'A', 'price': 1, 'quantity': 1}], 'total': 1,
            'currency': 'PHP', 'contactNumber': '+639171234567',
            'createdAt': '2099-01-01T00:00:00Z', 'paymentMethod': 'gcash',
            'paidAt': '2099-01-01T00:00:00Z', 'completedAt': None,
            'cancelReason': None, **PICKUP}

    def step(callId, name, result):
        return [{'role': 'assistant', 'toolCalls': [
            {'id': callId, 'name': name, 'arguments': '{}'}]},
            {'role': 'tool', 'toolCallId': callId,
             'content': json.dumps(result)}]

    listed = step('c1', 'getStoreOrders',
                  {'orders': [order('o1', 'paid'), order('o2', 'paid')]})
    completeOne = step('c2', 'completeOrder', order('o1', 'completed'))
    completeTwo = step('c3', 'completeOrder', order('o2', 'completed'))
    unlisted = step('c4', 'completeOrder', order('o3', 'completed'))

    [card] = chatCards.build(listed + completeOne, {}, context)
    assert [o['status'] for o in card['orders']] == ['completed', 'paid']
    assert card['formId']
    [card] = chatCards.build(listed + completeOne + completeTwo, {}, context)
    assert [o['status'] for o in card['orders']] == ['completed', 'completed']
    assert card['formId'] is None  # nothing left to hand over
    cards = chatCards.build(listed + unlisted, {}, context)
    assert [len(c['orders']) for c in cards] == [2, 1]
    # A refusal makes no card.
    refused = step('c5', 'completeOrder', {'error': 'Order not found'})
    assert chatCards.build(refused, {}, context) == []
