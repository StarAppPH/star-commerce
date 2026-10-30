"""The chat's cards and forms (chatCards.py, chatForms.py): the toolsets,
the setup chat of an admin whose store is not set up yet, the cards built
from what the tools returned, the forms the form tools open, form answers,
checked against what they answer and acted on by the model, and what the
model asks in chat and then does with the route tools directly (the store's
details, a policy section, a return).

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import re
from urllib.parse import quote

import pytest

import chatCards
import chatForms
import chatModel
import chatTools
import mcpServer
from actions import ChatActions
from ApiRequests import ApiRequests
import photoStorage
from conftest import (TEST_GEOCODED_ADDRESS, TOKEN, addOrder, assertExactShape, createTestStore,
                      firebaseClaims, guestAt, storeWithProducts,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (CHAT_COMPONENTS_PER_TURN_MAX, POLICY_ANSWER_FIELDS,
                     ChatReplyResponse, ConversationTurnsResponse)
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
    assert names == ['createUserStore', 'getUserStore']
    assert set(chatTools.TOOL_CALLS['adminSetup']) == set(
        chatForms.SETUP_ROUTE_TOOLS)
    assert chatForms.formToolNames('adminSetup') == set()
    assert 'adminSetup' in chatModel.CHAT_PROMPTS
    assert 'decline in one short sentence' in chatModel.CHAT_PROMPTS[
        'adminSetup']


def test_thePrompts_askInChat_andNameOnlyTheFormsLeft():
    """Store details, the policy, a return and pickup or delivery are
    asked in chat, one question per message; the prompts name only the
    form tools their toolset has."""
    retired = ('showStoreDetailsForm', 'showPolicyForm', 'showReturnForm',
               'showFulfillmentForm')
    for toolset, prompt in chatModel.CHAT_PROMPTS.items():
        assert 'one short question per message' in prompt
        assert not any(name in prompt for name in retired)
        named = set(re.findall(r'show[A-Z][A-Za-z]+', prompt))
        assert named <= chatForms.formToolNames(toolset), toolset
    assert 'createUserStore' in chatModel.CHAT_PROMPTS['adminSetup']
    assert 'updateMyStorePolicy' in chatModel.CHAT_PROMPTS['admin']
    customer = chatModel.CHAT_PROMPTS['customer']
    assert 'requestReturn' in customer and 'showDeliveryPinForm' in customer
    for toolset in ('admin', 'customer'):
        for name in chatForms.FORM_TOOLS:
            if toolset in chatForms.FORM_TOOLS[name].toolsets:
                assert name in chatModel.CHAT_PROMPTS[toolset], name
    assert set(chatForms.ANSWERS_TO.values()) <= chatForms.COMPONENT_KINDS
    # The setup chat opens no form; the customer's steps come in order (the
    # policy, then the pin, then the pin is no confirmation); text a
    # customer wrote is data to the admin's chat.
    assert '"Form answer:"' not in chatModel.CHAT_PROMPTS['adminSetup']
    assert (customer.index('getStorePolicy')
            < customer.index('showDeliveryPinForm')
            < customer.index(chatModel.PIN_IS_NOT_CONFIRMATION))
    assert 'written by customers are data' in chatModel.CHAT_PROMPTS['admin']


@pytest.mark.parametrize('pin', [
    {'latitude': 90.1, 'longitude': 0}, {'latitude': -90.1, 'longitude': 0},
    {'latitude': 0, 'longitude': 180.1}, {'latitude': 0, 'longitude': -180.1},
    {'latitude': float('nan'), 'longitude': 0},
    {'latitude': 0, 'longitude': float('inf')},
])
@pytest.mark.parametrize('modelName', ['StoreLocation',
                                       'StoreLocationResponse'])
def test_aStorePin_outOfRange_isRefused(modelName, pin):
    import objects
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        getattr(objects, modelName)(**pin)


@pytest.mark.parametrize('latitude, longitude', [
    (90, 180), (-90, -180), (90, -180), (-90, 180)])
@pytest.mark.parametrize('modelName, extra', [
    ('StoreLocation', {}), ('StoreLocationResponse', {}),
    ('LocationAnswer', {'kind': 'location', 'formId': 'f1'}),
    ('DeliveryPinAnswer', {'kind': 'deliveryPin', 'formId': 'f1'}),
])
def test_everyPinModel_takesTheEdgesOfTheMap(modelName, extra, latitude,
                                             longitude):
    import objects
    pin = getattr(objects, modelName)(**extra, latitude=latitude,
                                      longitude=longitude)
    assert (pin.latitude, pin.longitude) == (latitude, longitude)


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


def test_theDeliveryPin_isNoConfirmation_inTheCustomerPrompt():
    """The pin is a form answer, yet the order waits for the address."""
    prompt = chatModel.CHAT_PROMPTS['customer']
    assert chatModel.PIN_IS_NOT_CONFIRMATION in prompt
    assert prompt.index('"Form answer:"') < prompt.index(
        chatModel.PIN_IS_NOT_CONFIRMATION)
    assert chatModel.PIN_IS_NOT_CONFIRMATION not in chatModel.CHAT_PROMPTS[
        'admin']
    # A store that only delivers still gets the pin and the address.
    assert 'when that way is delivery, still do the delivery steps' in prompt
    # Delivery is priced by the zone under the pin (getStorePolicy's
    # deliveryZones), and a store with no zone's pin card refuses: the model
    # offers pickup.
    assert 'deliveryZones' in prompt and 'offer pickup' in prompt


@pytest.mark.parametrize('kind', ['location', 'deliveryPin'])
@pytest.mark.parametrize('field, value', [
    ('latitude', 91), ('latitude', -91), ('longitude', 181),
    ('latitude', float('nan')), ('longitude', float('inf')),
    ('latitude', float('-inf')),
])
def test_aPinAnswer_outOfRange_isRefused(kind, field, value):
    pin = {'kind': kind, 'formId': 'f1', 'latitude': 14.5, 'longitude': 121.0}
    assert chatForms.validAnswer(pin) is not None
    with pytest.raises(ValueError) as error:
        chatForms.validAnswer({**pin, field: value})
    assert str(error.value) == chatForms.FORM_ANSWER_INVALID.format(
        fields=f'{kind}.{field}')


def test_bothPrompts_tellTheModelAboutCardsAndFormAnswers():
    # The setup chat opens no form: its prompt has no form answer rule.
    for toolset in ('admin', 'customer'):
        assert '"Form answer:"' in chatModel.CHAT_PROMPTS[toolset]
    assert 'never repeat what a card shows' in chatModel.CHAT_PROMPTS['admin']
    assert 'never repeat what a card shows' in chatModel.CHAT_PROMPTS[
        'customer']


# ----------------------------- B. the setup chat ----------------------------- #


def test_anAdminWithAnUnsetStore_chatsWithTheSetupTools(wiped, firebase,
                                                        model):
    adminId = signedUpAdmin(firebase)
    fake = model(reply('Nice! What is your store called?'))

    res = adminSays('I want to sell coffee online', userId=adminId)

    assertExactShape(ChatReplyResponse, res)
    request = fake.requests[0]
    assert request['messages'][0]['content'] == chatModel.CHAT_PROMPTS[
        'adminSetup']
    assert request['tools'] == chatTools.TOOLS['adminSetup']
    assert res['components'] == []
    # The setup chat is listed like any other.
    listed = api.listMyConversations(adminId, None, None, 10)
    assert [row['_id'] for row in listed['conversations']] == [
        res['conversationId']]


def test_theStoreDetailsAskedInChat_createTheStore(wiped, firebase, model):
    """The owner answers one question per message; on their yes the model
    calls createUserStore with what they said (no opening hours: skipped)."""
    adminId = signedUpAdmin(firebase)
    model(reply('What is your store called?'))
    first = adminSays('Set up my store', userId=adminId)
    cid = first['conversationId']
    for said, asked in (('Kalye Coffee', 'What do you sell?'),
                        ('Small-batch coffee', 'Prices in PHP, right?'),
                        ('Yes', 'Your contact number? Customers see it.'),
                        ('+639171234567', 'Your address? Customers see it.'),
                        ('14 Maginhawa St', 'Opening hours? You can skip.'),
                        ('skip', 'Kalye Coffee, PHP, ... Create it?')):
        model(reply(asked))
        adminSays(said, cid, userId=adminId)
    created = json.dumps({'name': 'Kalye Coffee',
                          'description': 'Small-batch coffee',
                          'currency': 'PHP', 'contactNumber': '+639171234567',
                          'address': '14 Maginhawa St',
                          'primaryColor': '#6F4E37', 'secondaryColor': '#F5E6D3'})
    model(calls(('c1', 'createUserStore', created)),
          reply('Your store is live.'))

    res = adminSays('Yes', cid, userId=adminId)

    [card] = res['components']
    assert card['kind'] == 'store' and card['name'] == 'Kalye Coffee'
    assert card['openingHours'] is None and isinstance(card['slug'], str)
    assert api.getUserStore(adminId)['store']['address'] == '14 Maginhawa St'
    # The store is set up now: the next message gets the admin's tools.
    fake = model(reply('What next?'))
    adminSays('Thanks', cid, userId=adminId)
    assert fake.requests[0]['tools'] == chatTools.TOOLS['admin']
    assert fake.requests[0]['messages'][0]['content'] == (
        chatModel.CHAT_PROMPTS['admin'])


def test_theSetupPrompt_asksNoAddress_andOpensTheMapAfterCreating():
    setup = chatModel.CHAT_PROMPTS['adminSetup']
    assert 'Never ask for the address' in setup
    assert 'its address, ' not in setup and 'open the map' in setup
    assert setup.index('createUserStore') < setup.index('open the map')
    admin = chatModel.CHAT_PROMPTS['admin']
    assert (admin.index('setUserStoreLocation')
            < admin.index('suggestStoreAddress'))


@pytest.mark.parametrize('toolset, name, content, setsUp', [
    ('adminSetup', 'createUserStore', '{"name": "Kalye Coffee"}', True),
    ('adminSetup', 'createUserStore', '{"error": "Bad number"}', False),
    ('adminSetup', 'createUserStore', 'not json', False),
    ('adminSetup', 'getUserStore', '{"store": null}', False),
    ('admin', 'createUserStore', '{"name": "Kalye Coffee"}', False),
])
def test_setsUpTheStore(toolset, name, content, setsUp):
    assert chatForms.setsUpTheStore(toolset, name, content) is setsUp


def test_aStoreJustCreated_opensTheMapInTheSameTurn(wiped, firebase, model):
    """No address is asked: once createUserStore succeeds the turn goes on
    with the admin's tools, the model opens the map, and the pin it sends
    next gets the address suggested from it."""
    adminId = signedUpAdmin(firebase)
    created = json.dumps({'name': 'Kalye Coffee',
                          'description': 'Small-batch coffee',
                          'currency': 'PHP', 'contactNumber': '+639171234567',
                          'primaryColor': '#6F4E37',
                          'secondaryColor': '#F5E6D3'})
    fake = model(calls(('c1', 'createUserStore', created)),
                 calls(('c2', 'showStoreLocationForm', '{}')),
                 reply("Your store is live. I've put a map below, pin your "
                       'store.'))

    res = adminSays('Yes, create it', userId=adminId)

    assert fake.requests[0]['tools'] == chatTools.TOOLS['adminSetup']
    assert fake.requests[1]['tools'] == chatTools.TOOLS['admin']
    assert fake.requests[2]['tools'] == chatTools.TOOLS['admin']
    # The prompt stays the setup one for the rest of the turn.
    assert fake.requests[1]['messages'][0]['content'] == (
        chatModel.CHAT_PROMPTS['adminSetup'])
    store, form = res['components']
    assert store['kind'] == 'store' and store['address'] is None
    assert form['kind'] == 'locationForm'
    assert form['location'] is None and form['address'] is None
    assert api.getUserStore(adminId)['store']['address'] is None

    # The pin comes next: saved, then an address is suggested from it.
    pin = {'kind': 'location', 'formId': form['formId'],
           'latitude': 14.6507, 'longitude': 121.0494}
    model(calls(('c3', 'setUserStoreLocation',
                 json.dumps({'latitude': 14.6507, 'longitude': 121.0494}))),
          calls(('c4', 'suggestStoreAddress', '{}')),
          reply(f'Use {TEST_GEOCODED_ADDRESS} as your address?'))
    adminSays(None, res['conversationId'], pin, userId=adminId)
    stored = api.getUserStore(adminId)['store']
    assert stored['location'] == {'latitude': 14.6507, 'longitude': 121.0494}
    assert stored['address'] is None  # not before the owner's yes


def test_aRefusedCreate_keepsTheSetupTools(wiped, firebase, model):
    adminId = signedUpAdmin(firebase)
    bad = json.dumps({'name': 'Kalye Coffee', 'description': 'Coffee',
                      'currency': 'PHP', 'contactNumber': 'call me',
                      'primaryColor': '#6F4E37', 'secondaryColor': '#F5E6D3'})
    fake = model(calls(('c1', 'createUserStore', bad)),
                 calls(('c2', 'showStoreLocationForm', '{}')),
                 reply('That number does not look right. What is it?'))

    res = adminSays('Yes, create it', userId=adminId)

    assert fake.requests[1]['tools'] == chatTools.TOOLS['adminSetup']
    assert res['components'] == []
    tool = json.loads(fake.requests[2]['messages'][-1]['content'])
    assert set(tool) == {'error'}


def test_aRefusedStoreDetail_goesBackToTheModel(wiped, firebase, model):
    adminId = signedUpAdmin(firebase)
    bad = json.dumps({'name': 'Kalye Coffee', 'description': 'Coffee',
                      'currency': 'PHP', 'contactNumber': 'call me',
                      'address': '14 Maginhawa St',
                      'primaryColor': '#6F4E37', 'secondaryColor': '#F5E6D3'})
    fake = model(calls(('c1', 'createUserStore', bad)),
                 reply('That number does not look right. What is it?'))

    res = adminSays('Yes, create it', userId=adminId)

    assert res['components'] == []
    tool = json.loads(fake.requests[1]['messages'][-1]['content'])
    assert set(tool) == {'error'} and 'call me' not in tool['error']
    assert api.getUserStore(adminId)['store'] is None  # still not set up


def test_aFormAnswer_isRefusedBeforeTheModelRuns(wiped, model):
    createTestStore(ADMIN)
    model(calls(('c1', 'showStoreLocationForm', '{}')), reply('Pin it.'))
    first = adminSays('Pin my store')
    cid = first['conversationId']
    formId = first['components'][0]['formId']
    good = {'kind': 'location', 'formId': formId, 'latitude': 14.55,
            'longitude': 121.02}
    fake = model()

    refusals = [
        (lambda: adminSays(None, cid, {**good, 'formId': 'no-such-form'}),
         chatForms.FORM_NOT_FOUND),
        (lambda: adminSays(None, cid, {'kind': 'phoneVerified',
                                       'formId': formId}),
         chatForms.FORM_DOES_NOT_MATCH),
        (lambda: adminSays('hello', cid, good), chatForms.MESSAGE_AND_FORM),
        (lambda: adminSays(None, None, good),
         chatForms.FORM_NEEDS_CONVERSATION),
    ]
    for send, message in refusals:
        with pytest.raises(ValueError, match='^' + re.escape(message) + '$'):
            send()
    with pytest.raises(ValueError) as error:
        adminSays(None, cid, {**good, 'latitude': 'SECRET-VALUE' * 50})
    assert str(error.value) == chatForms.FORM_ANSWER_INVALID.format(
        fields='location.latitude')
    assert 'SECRET-VALUE' not in str(error.value)
    with pytest.raises(ValueError, match='^That form answer is not valid'):
        adminSays(None, cid, {'kind': 'storeDetails', 'formId': formId})
    assert fake.requests == []
    # Refused under the lock, which it released: the conversation goes on.
    fake = model(calls(('c2', 'getUserStore', '{}')), reply('ok'))
    res = adminSays(None, cid, good)
    assert res['userText'] == 'Pinned the store at 14.55000, 121.02000'
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'location', 'latitude': 14.55, 'longitude': 121.02}
    # A form is answered once.
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.FORM_ALREADY_ANSWERED + '$'):
        adminSays(None, cid, good)


def test_aRetiredComponentKind_isLeftOutOfTheTurnsPage(wiped, model):
    """A turn saved with a form since retired (store details, policy,
    return, fulfillment) still loads: the rest of its components show."""
    createTestStore(ADMIN)
    model(calls(('c1', 'getMyStorePolicy', '{}')), reply('Your policy.'))
    res = adminSays('Policy?')
    # A raw update: a retired kind has no model left to build it with.
    db.db['ChatTurns'].update_one(
        {'conversationId': res['conversationId']},
        {'$push': {'components': {'kind': 'policyForm', 'formId': 'old-form',
                                  'section': 'payment'}}})
    page = api.getConversationTurns(ADMIN, res['conversationId'])
    assertExactShape(ConversationTurnsResponse, page)
    assert [c['kind'] for c in page['turns'][0]['components']] == ['policy']
    assert chatForms.COMPONENT_KINDS == {
        'shopProducts', 'storeProducts', 'storeOrders', 'myOrders',
        'checkout', 'storeReturns', 'myReturns', 'policy', 'photos',
        'productPhotoChoice', 'store', 'locationForm', 'newProductsForm',
        'phoneVerificationForm', 'photoRequestForm', 'deliveryPinForm',
        'deliveryZonesForm'}
    # The retired form cannot be answered with a kind of today.
    fake = model()
    with pytest.raises(ValueError, match='^' + re.escape(
            chatForms.FORM_DOES_NOT_MATCH) + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'location', 'formId': 'old-form',
                   'latitude': 14.55, 'longitude': 121.02})
    assert fake.requests == []


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
    model(calls(('c2', 'showNewProductsForm', json.dumps({'products': [
                    {'name': 'Barako', 'price': 520},
                    {'name': 'Bad price', 'price': -3}]})),
                ('c3', 'showStoreLocationForm', '{}'),
                ('c4', 'showPhotoRequestForm', '{}')),
          reply('Fill these in.'))
    res = adminSays('Set up delivery and add products')
    kinds = [c['kind'] for c in res['components']]
    # No photo style yet: the photo form refused, the others opened.
    assert kinds == ['newProductsForm', 'locationForm']
    productsForm, locationForm = res['components']
    assert productsForm['products'][0] == {
        'name': 'Barako', 'price': 520, 'inventory': None,
        'description': None}
    assert productsForm['products'][1]['price'] is None
    assert locationForm['location'] is None
    assert locationForm['address'] == store['address']

    customerId = guestAt(firebase, store, GUEST)
    fake = model(calls(('c5', 'showPhoneVerification', '{}'),
                       ('c6', 'showReturnForm',
                        json.dumps({'orderId': 'o1'}))),
                 reply('Verify your number below.'))
    shown = customerSays(customerId, store['_id'], 'Order a cake')
    # A retired form tool is no tool: only phone verification opened.
    [verify] = shown['components']
    assert verify['kind'] == 'phoneVerificationForm'
    [retired] = [m for m in fake.requests[1]['messages']
                 if m['role'] == 'tool' and m.get('tool_call_id') == 'c6']
    assert json.loads(retired['content']) == {
        'error': 'Unknown tool showReturnForm'}
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.PHONE_NOT_VERIFIED + '$'):
        customerSays(customerId, store['_id'], None, shown['conversationId'],
                     {'kind': 'phoneVerified', 'formId': verify['formId']})


def test_aPolicySectionAskedInChat_isSavedByTheModel(wiped, model):
    """The owner answers the payment section one question at a time; the
    model reads it back, then saves only those answers."""
    createTestStore(ADMIN)
    api.updateMyStorePolicy(ADMIN, handoverDays=2)
    model(reply('Which payment methods do you take?'))
    cid = adminSays('Let us do the policy')['conversationId']
    model(reply('Can a customer cancel a paid order?'))
    adminSays('GCash and card', cid)
    model(reply('GCash and card; cancel before handover. Save?'))
    adminSays('Before handover', cid)
    model(calls(('c1', 'updateMyStorePolicy', json.dumps({
        'acceptedPaymentMethods': ['gcash', 'card'],
        'paidOrderCancellation': 'beforeHandover'}))),
        reply('Saved. Next: delivery.'))

    res = adminSays('Yes', cid)

    [card] = res['components']
    assert card['kind'] == 'policy'
    assert card['acceptedPaymentMethods'] == ['gcash', 'card']
    assert card['paidOrderCancellation'] == 'beforeHandover'
    # An answer of another section is kept.
    assert card['handoverDays'] == 2
    assert api.getMyStorePolicy(ADMIN)['acceptedPaymentMethods'] == [
        'gcash', 'card']


def test_aStorePin_isSaved_andItsAddressSavedOnlyOnAYes(wiped, model,
                                                         _fakeGeocoding):
    """The owner's pin from the map is saved by the model; Google's address
    for it is offered, and saved only once the owner says yes."""
    created = createTestStore(ADMIN)
    model(calls(('c1', 'showStoreLocationForm', '{}')), reply('Pin it.'))
    first = adminSays('I want to change my store location')
    cid = first['conversationId']
    pin = {'kind': 'location', 'formId': first['components'][0]['formId'],
           'latitude': 10.34344, 'longitude': 123.91151}
    fake = model(calls(('c2', 'setUserStoreLocation', json.dumps(
                     {'latitude': 10.34344, 'longitude': 123.91151})),
                       ('c3', 'suggestStoreAddress', '{}')),
                 reply('Saved. Use the address from your pin?'))

    pinned = adminSays(None, cid, pin)

    [card] = pinned['components']
    assert card['kind'] == 'store'
    assert card['location'] == {'latitude': 10.34344,
                                'longitude': 123.91151}
    [suggestion] = [m for m in fake.requests[1]['messages']
                    if m['role'] == 'tool' and m['tool_call_id'] == 'c3']
    assert json.loads(suggestion['content']) == {
        'suggestedAddress': TEST_GEOCODED_ADDRESS,
        'address': created['address']}
    [google] = _fakeGeocoding.calls
    assert google['params']['latlng'] == '10.3434400,123.9115100'
    stored = db.read({}, 'Stores', findOne=True)
    assert stored['address'] == created['address']  # not before a yes

    model(calls(('c4', 'updateMyStoreDetails', json.dumps(
              {'address': TEST_GEOCODED_ADDRESS}))),
          reply('Your address is updated.'))
    res = adminSays('Yes', cid)

    [card] = res['components']
    assert card['kind'] == 'store'
    assert card['address'] == TEST_GEOCODED_ADDRESS
    assert card['location'] == {'latitude': 10.34344,
                                'longitude': 123.91151}
    assert api.getUserStore(ADMIN)['store']['address'] == (
        TEST_GEOCODED_ADDRESS)


def test_aReturnAskedInChat_isRequestedByTheModel(wiped, firebase, model):
    """The customer names the items, the reason and a note in chat; the
    model confirms, then calls requestReturn with the order's item
    positions."""
    import datetime
    store = storeWithProducts(ADMIN)
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    pandesal = db.read({'storeId': store['_id'], 'name': 'Pandesal'},
                       'Products', findOne=True)
    order = addOrder(store['_id'], customerId, [
        {'productId': pandesal['_id'], 'name': 'Pandesal',
         'price': pandesal['price'], 'quantity': 3}], status='completed',
        completedAt=datetime.datetime.now(tz=datetime.timezone.utc))
    model(reply('Which items, and how many?'))
    cid = customerSays(customerId, store['_id'],
                       'I want to return some pandesal')['conversationId']
    model(reply('Why are they going back?'))
    customerSays(customerId, store['_id'], 'Two of them', cid)
    model(reply('2 Pandesal, defective, note "stale". Request it?'))
    customerSays(customerId, store['_id'], 'They were stale', cid)
    model(calls(('c1', 'requestReturn', json.dumps({
        'orderId': order['_id'], 'items': [{'item': 1, 'quantity': 2}],
        'reason': 'defective', 'note': 'stale'}))),
        reply('Requested: the store will decide.'))

    res = customerSays(customerId, store['_id'], 'Yes', cid)

    [card] = res['components']
    assert card['kind'] == 'myReturns'
    [ret] = card['returns']
    assert (ret['status'], ret['reason']) == ('requested', 'defective')
    assert ret['items'] == [{'name': 'Pandesal', 'price': pandesal['price'],
                             'quantity': 2}]
    saved = db.read({'orderId': order['_id']}, 'Returns', findOne=True)
    assert saved['note'] == 'stale'


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
    policy = {field: None for field in POLICY_ANSWER_FIELDS}
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
        'generateProductPhotos': {'photos': [{'_id': 'g1', 'url': 'https://x'}],
                                  'prompt': 'p'},
        'makeProductPhotoChoices': {
            'productId': 'p1', 'productName': 'A', 'photos': [
                {'_id': 'o1', 'url': 'https://x', 'source': 'original'},
                {'_id': 'g1', 'url': 'https://y', 'source': 'generated'}],
            'prompt': 'p', 'notGenerated': None},
        'getUserStore': {'store': store},
        'createUserStore': store,
        'setUserStoreLocation': store,
        'updateMyStoreDetails': store,
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


# ------------------------- new products with photos ------------------------- #

PHOTO_BUCKET = 'cc-test-bucket.firebasestorage.app'


@pytest.fixture
def photoBucket(monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, PHOTO_BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def uploadUrl(uid: str, name: str = 'mug.jpg') -> str:
    path = f'users/{uid}/chat-photos/{name}'
    return (f'https://{photoStorage.DOWNLOAD_HOST}/v0/b/{PHOTO_BUCKET}/o/'
            f'{quote(path, safe="")}?alt=media&token=t')


def openProductsForm(model) -> tuple[dict, dict]:
    model(calls(('c1', 'showNewProductsForm', '{}')), reply('Fill it in.'))
    res = adminSays('Add products')
    [form] = res['components']
    return res, form


def test_aNewProductsAnswer_carriesEachPhoto_toTheModel(wiped, photoBucket,
                                                         model):
    createTestStore(ADMIN)
    res, form = openProductsForm(model)
    fake = model(reply('Adding them.'))
    url = uploadUrl(ADMIN)

    adminSays(None, res['conversationId'], {
        'kind': 'newProducts', 'formId': form['formId'], 'products': [
            {'name': 'Mug', 'price': 250, 'inventory': 3, 'imageUrl': url},
            {'name': 'Plate', 'price': 90, 'inventory': 5}]})

    content = lastUserContent(fake)
    summary = content.split('\n\n' + chatForms.FORM_ANSWER_PREFIX)[0]
    assert summary == ('Add these products:\n'
                       '- Mug, 250 PHP, 3 in stock (with a photo)\n'
                       '- Plate, 90 PHP, 5 in stock')
    products = formAnswerDetails(content)['products']
    assert products[0]['imageUrl'] == url
    assert products[1]['imageUrl'] is None
    # The model is told to make the photo choices next.
    tool = chatForms.FORM_TOOLS['showNewProductsForm']
    assert 'makeProductPhotoChoices' in tool.description


def test_aNewProductsAnswer_withAPhotoNotTheirs_isRefused(wiped, photoBucket,
                                                         model):
    createTestStore(ADMIN)
    res, form = openProductsForm(model)
    fake = model()
    theirs = uploadUrl('someone-else')

    with pytest.raises(ValueError, match='^The photo of product 2 must be a '
                       'photo you uploaded$') as error:
        adminSays(None, res['conversationId'], {
            'kind': 'newProducts', 'formId': form['formId'], 'products': [
                {'name': 'Mug', 'price': 250, 'inventory': 3},
                {'name': 'Plate', 'price': 90, 'inventory': 5,
                 'imageUrl': theirs}]})
    assert theirs not in str(error.value)
    assert fake.requests == []


def test_aNewProductsForm_prefillsOnlyTheOwnersOwnPhotos(wiped, photoBucket,
                                                        model):
    createTestStore(ADMIN)
    mug = f'users/{ADMIN}/chat/a/mug.jpg'
    sheet = uploadUrl(ADMIN, 'price-list.png')
    model(calls(('c1', 'showNewProductsForm', json.dumps({'products': [
                    {'name': 'Mug', 'imageUrl': photoStorage.publicPathUrl(mug)},
                    {'name': 'Plate', 'imageUrl': uploadUrl('someone-else')},
                    {'name': 'Bowl', 'imageUrl': uploadUrl(ADMIN, 'list.pdf')},
                    {'name': 'Cup', 'imageUrl': 'https://example.com/cup.jpg'},
                    {'name': 'Tray', 'imageUrl': sheet},
                    {'name': 'Box', 'imageUrl': sheet},
                    {'name': 'Lid'}]}))),
          reply('I filled in what your files show.'))
    [form] = adminSays('Add these')['components']
    # Only an image the owner uploaded, on one row: a photo on several rows
    # shows them all (a list, a screenshot).
    assert form['photos'] == [mug, None, None, None, None, None, None]
    assert [p['name'] for p in form['products']] == [
        'Mug', 'Plate', 'Bowl', 'Cup', 'Tray', 'Box', 'Lid']
    tool = chatForms.FORM_TOOLS['showNewProductsForm']
    assert 'files they attached' in tool.description
    assert 'never a list or a screenshot' in tool.description


def test_aNewProductsAnswer_takesAPrefilledPhoto(wiped, photoBucket, model):
    createTestStore(ADMIN)
    url = uploadUrl(ADMIN, 'mug.png')
    model(calls(('c1', 'showNewProductsForm', json.dumps(
              {'products': [{'name': 'Mug', 'imageUrl': url}]}))),
          reply('Check the photo.'))
    res = adminSays('Add this mug')
    [form] = res['components']
    assert form['photos'] == [f'users/{ADMIN}/chat-photos/mug.png']
    fake = model(reply('Adding it.'))

    adminSays(None, res['conversationId'], {
        'kind': 'newProducts', 'formId': form['formId'], 'products': [
            {'name': 'Mug', 'price': 250, 'inventory': 3, 'imageUrl': url}]})

    products = formAnswerDetails(lastUserContent(fake))['products']
    assert products[0]['imageUrl'] == url


CHOICES = {'productId': 'p-mug', 'productName': 'Mug', 'photos': [
    {'_id': 'ph-original', 'url': 'https://x/o.png', 'source': 'original'},
    {'_id': 'ph-styled-1', 'url': 'https://x/1.jpg', 'source': 'generated'},
    {'_id': 'ph-styled-2', 'url': 'https://x/2.jpg', 'source': 'generated'}],
    'prompt': 'Create a professional photo', 'notGenerated': None}


def test_aPhotoChoiceCard_isAnsweredWithOneOfItsPhotos(wiped, model,
                                                       monkeypatch):
    createTestStore(ADMIN)
    stubTool(monkeypatch, 'admin', 'makeProductPhotoChoices',
             lambda userId, storeId, args: CHOICES)
    model(calls(('c1', 'makeProductPhotoChoices',
                 json.dumps({'productId': 'p-mug', 'imageUrl': 'u'}))),
          reply('Pick one.'))
    res = adminSays('Photos for the mug')
    assertExactShape(ChatReplyResponse, res)
    [card] = res['components']
    assert card['kind'] == 'productPhotoChoice' and card['formId']
    assert {k: card[k] for k in CHOICES} == CHOICES

    model()
    for productId, photoId in (('p-mug', 'not-on-the-card'),
                               ('p-mug', 'p-mug'),
                               ('another-product', 'ph-styled-1')):
        with pytest.raises(ValueError,
                           match='^' + chatForms.NOT_ON_THE_CARD + '$'):
            adminSays(None, res['conversationId'],
                      {'kind': 'productPhoto', 'formId': card['formId'],
                       'productId': productId, 'photoId': photoId})

    for photoId, said in (('ph-styled-2', 'Use styled photo 2 for Mug'),
                          ('ph-original', 'Use the original photo for Mug')):
        model(calls(('c1', 'makeProductPhotoChoices', '{}')),
              reply('Pick one.'))
        again = adminSays('Photos for the mug')
        [choice] = again['components']
        fake = model(reply('Done.'))
        adminSays(None, again['conversationId'],
                  {'kind': 'productPhoto', 'formId': choice['formId'],
                   'productId': 'p-mug', 'photoId': photoId})
        content = lastUserContent(fake)
        assert content.startswith(said + '\n\n')
        assert formAnswerDetails(content) == {
            'kind': 'productPhoto', 'productId': 'p-mug', 'photoId': photoId}
    # A card is answered once.
    model(reply('Done.'))
    adminSays(None, res['conversationId'],
              {'kind': 'productPhoto', 'formId': card['formId'],
               'productId': 'p-mug', 'photoId': 'ph-styled-1'})
    model()
    with pytest.raises(ValueError,
                       match='^' + chatForms.FORM_ALREADY_ANSWERED + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'productPhoto', 'formId': card['formId'],
                   'productId': 'p-mug', 'photoId': 'ph-original'})
