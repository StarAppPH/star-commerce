"""Store policies: the empty Store and Policy every admin gets at sign-up,
the createUserStore fill, the three policy routes (direct and over MCP), the
superAdmin bootstrap around the sign-up transaction, and the slug / policy
indexes. Firebase verification is the conftest `firebase` fixture."""

import datetime
import inspect
import time
import typing

import pymongo.collection
import pytest
from pymongo.errors import DuplicateKeyError

from actions.OrderActions import STORE_ID_NOT_TEXT
from actions.PolicyActions import (ANSWER_RULES, CLEAR_FIELDS, NOT_APPLICABLE,
                                   NOTHING_SENT, POLICY_CHANGED, TEXT_LIMITS,
                                   UPDATE_POLICY_DESCRIPTION, PolicyActions)
from actions.StoreActions import ALREADY_HAS_A_STORE, NO_STORE_YET, SET_UP
from ApiRequests import ApiRequests
from conftest import (STORE_DETAILS, TOKEN, TOKEN_UID, assertExactShape,
                      createTestStore, createTestUser, firebaseClaims,
                      guestAt, wipeCollections)
from mongoDb import LEGACY_STORE_SLUG_INDEX, STORE_SLUG_INDEX, db
from objects import (DEFAULT_STORE_CURRENCY, POLICY_ANSWER_FIELDS,
                     POLICY_DELIVERY_ANSWERS, POLICY_RETURN_ANSWERS,
                     POLICY_TERMS_MAX_LENGTH, POLICY_TEXT_MAX_LENGTH, PRODUCT_PRICE_MAX,
                     Policy,
                     PolicyField, PolicyView, Store, StorePolicyResponse,
                     StoreView)
from test_mcp_server import _auth, _text, callTool

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders'
]
ADMIN = 'po-admin'
OTHER_ADMIN = 'po-other-admin'
GUEST = 'po-guest-uid'
SIGNUP_UID = 'po-signup-uid'
UNSET_STORE_OWNER = 'po-unset-owner'
NONE_ANSWERS = dict.fromkeys(POLICY_ANSWER_FIELDS)

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


# ----------------------------- helpers ----------------------------- #


def _signIn(firebase, uid: str = TOKEN_UID) -> dict:
    """A Google sign-in of `uid` through loginWithGoogle."""
    firebase['claims'] = firebaseClaims(sub=uid,
                                        user_id=uid,
                                        email=f'{uid}@test.com')
    return api.loginWithGoogle({'idToken': TOKEN})


def _storeDocs() -> list[dict]:
    return db.read({}, 'Stores')


def _policyDoc(storeId: str) -> dict | None:
    return db.read({'storeId': storeId}, 'Policies', findOne=True)


def _answers(doc: dict) -> dict:
    return {field: doc[field] for field in POLICY_ANSWER_FIELDS}


def _expected(**answers) -> dict:
    return {**NONE_ANSWERS, **answers}


def _naive(moment):
    return moment.replace(tzinfo=None)


def _refusal(call, *args, **kwargs) -> str:
    """The message of the ValueError `call` raises."""
    with pytest.raises(ValueError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


def _unchanged(storeId: str, before: dict) -> None:
    """The policy document is exactly as `before` read it."""
    assert _policyDoc(storeId) == before


def _setUpStore(adminId: str = ADMIN) -> dict:
    return createTestStore(adminId)


def _fakeTransaction(uid: str, failAt: str, rollBack: bool = True):
    """A db.createTransaction for a standalone mongod, which has no
    rollback: it runs the function and, when that raises, deletes what the
    sign-up of `uid` wrote, as an aborted transaction would. `failAt` is
    the collection whose db.create raises (None: the function itself)."""

    def createTransaction(updateFunction):
        realCreate = db.create

        def create(data, collection, session=None):
            if collection == failAt:
                raise RuntimeError('write failed')
            return realCreate(data, collection, session=session)

        db.create = create
        try:
            return updateFunction(None)
        except Exception:
            if not rollBack:
                raise
            for store in db.read({'ownerUserId': uid}, 'Stores'):
                db.delete({'storeId': store['_id']}, 'Policies')
            db.delete({'_id': uid}, 'Users')
            db.delete({'ownerUserId': uid}, 'Stores')
            raise
        finally:
            del db.create  # back to the class's method

    return createTransaction


def _indexNames(plan) -> set:
    """Every indexName in an explain() plan tree."""
    found = set()
    if isinstance(plan, dict):
        for key, value in plan.items():
            if key == 'indexName':
                found.add(value)
            else:
                found |= _indexNames(value)
    elif isinstance(plan, list):
        for item in plan:
            found |= _indexNames(item)
    return found


def _storeDoc(ownerUserId: str, **fields) -> dict:
    return Store(ownerUserId=ownerUserId,
                 currency=DEFAULT_STORE_CURRENCY,
                 location=None,
                 createdAt=datetime.datetime.now(tz=datetime.timezone.utc),
                 _version=0,
                 **fields).model_dump(by_alias=True)


# ----------------------------- sign-up ----------------------------- #


def test_signUp_createsOneEmptyStoreAndPolicy_andStaysNotSetUp(
        firebase, anotherUser):
    res = _signIn(firebase)

    assert res['_id'] == TOKEN_UID
    # The other user from anotherUser made one Users doc: not the first.
    assert res['userType'] is None
    [stored] = [s for s in _storeDocs() if s['ownerUserId'] == TOKEN_UID]
    StoreView.model_validate(stored)
    assert db.read({}, 'Stores', countOnly=True) == 1
    assert stored['currency'] == DEFAULT_STORE_CURRENCY == 'PHP'
    for field in ('name', 'slug', 'description', 'contactNumber', 'address'):
        assert stored.get(field) is None
    assert stored['location'] is None
    assert stored['_version'] == 0
    assert db.read({}, 'Policies', countOnly=True) == 1
    policy = _policyDoc(stored['_id'])
    PolicyView.model_validate(policy)
    assert _answers(policy) == NONE_ANSWERS
    assert policy['createdAt'] == policy['updatedAt'] == stored['createdAt']
    assert policy['_version'] == 0

    # A returning sign-in creates nothing and changes nothing.
    again = _signIn(firebase)
    assert again['_id'] == TOKEN_UID
    assert _storeDocs() == [stored]
    assert _policyDoc(stored['_id']) == policy

    # The store is still "no store yet" to every reader.
    assert api.getUserStore(TOKEN_UID) == {'store': None}
    assert _refusal(api.getMyStorePolicy, TOKEN_UID) == NO_STORE_YET
    assert _refusal(api.updateMyStorePolicy, TOKEN_UID,
                    acceptsReturns=False) == NO_STORE_YET
    # Create your store first comes before "send at least one answer".
    assert _refusal(api.updateMyStorePolicy, TOKEN_UID) == NO_STORE_YET
    assert _policyDoc(stored['_id']) == policy


def test_adminWithNoStoresDoc_isToldToCreateTheStoreFirst(clean):
    createTestUser(ADMIN)

    assert _refusal(api.getMyStorePolicy, ADMIN) == NO_STORE_YET
    assert _refusal(api.updateMyStorePolicy, ADMIN,
                    acceptsReturns=True) == NO_STORE_YET
    assert _refusal(api.updateMyStorePolicy, ADMIN) == NO_STORE_YET
    assert _storeDocs() == []
    assert db.read({}, 'Policies') == []


# ----------------------------- createUserStore ----------------------------- #


def test_createUserStore_fillsTheSignUpStore(clean, firebase, anotherUser):
    _signIn(firebase)
    [empty] = [s for s in _storeDocs() if s['ownerUserId'] == TOKEN_UID]
    policyBefore = _policyDoc(empty['_id'])

    created = api.createUserStore(TOKEN_UID, ' Bakery A ', **STORE_DETAILS)

    assert created['_id'] == empty['_id']
    [stored] = [s for s in _storeDocs() if s['ownerUserId'] == TOKEN_UID]
    assert db.read({}, 'Stores', countOnly=True) == 1
    assert stored['_version'] == empty['_version'] + 1
    assert stored['createdAt'] == empty['createdAt']
    assert stored['slug'] == 'bakery-a'
    assert stored['name'] == 'Bakery A'
    assert api.getUserStore(TOKEN_UID) == {'store': created}
    # The sign-up Policy stays the one policy of the store.
    assert db.read({}, 'Policies', countOnly=True) == 1
    assert _policyDoc(empty['_id']) == policyBefore

    # A second call is refused and changes nothing.
    assert _refusal(api.createUserStore, TOKEN_UID, 'Bakery B',
                    **STORE_DETAILS) == ALREADY_HAS_A_STORE
    assert [s for s in _storeDocs() if s['ownerUserId'] == TOKEN_UID] == [
        stored
    ]


def test_createUserStore_forAnAdminWithNoStoreDoc_createsStoreAndPolicy(
        clean):
    store = createTestStore(ADMIN)

    [stored] = _storeDocs()
    assert stored['_id'] == store['_id']
    assert stored['_version'] == 0
    policy = _policyDoc(store['_id'])
    assert _answers(policy) == NONE_ANSWERS
    assert db.read({}, 'Policies', countOnly=True) == 1
    assert _refusal(api.createUserStore, ADMIN, 'Again',
                    **STORE_DETAILS) == ALREADY_HAS_A_STORE


def test_createUserStore_losingTheRaceToASetUp_isAlreadyHasAStore(
        clean, firebase, anotherUser, monkeypatch):
    _signIn(firebase)
    [empty] = [s for s in _storeDocs() if s['ownerUserId'] == TOKEN_UID]
    realFind = db.findOneAndUpdate

    def racing(query, update, collectionName, **kwargs):
        # A racing call sets the store up between the pre-read and the fill.
        if collectionName == 'Stores':
            return None
        return realFind(query, update, collectionName, **kwargs)

    monkeypatch.setattr(db, 'findOneAndUpdate', racing)

    assert _refusal(api.createUserStore, TOKEN_UID, 'Bakery A',
                    **STORE_DETAILS) == ALREADY_HAS_A_STORE
    stored = db.read({'_id': empty['_id']}, 'Stores', findOne=True)
    assert stored.get('slug') is None


def test_storesThatAreNotSetUp_coexist_underThePartialSlugIndex(
        clean, firebase, anotherUser):
    for uid in ('po-a', 'po-b', 'po-c'):
        _signIn(firebase, uid)

    stores = _storeDocs()
    assert len(stores) == 3
    assert all(s.get('slug') is None for s in stores)
    assert db.read({}, 'Policies', countOnly=True) == 3
    assert {p['storeId'] for p in db.read({}, 'Policies')} == {
        s['_id'] for s in stores
    }

    # Two of them set up under the same name get distinct slugs.
    first = api.createUserStore('po-a', 'Bakery A', **STORE_DETAILS)
    second = api.createUserStore('po-b', 'Bakery A', **STORE_DETAILS)
    assert first['slug'] == 'bakery-a'
    assert second['slug'].startswith('bakery-a-')
    slugs = [s['slug'] for s in _storeDocs() if s.get('slug')]
    assert len(slugs) == len(set(slugs)) == 2


# ----------------------------- customers ----------------------------- #


def test_aStoreThatIsNotSetUp_isNotFoundToCustomers(clean, firebase,
                                                    anotherUser):
    _signIn(firebase, UNSET_STORE_OWNER)
    [unset] = [s for s in _storeDocs() if s.get('slug') is None]
    realStore = createTestStore(ADMIN)
    customerId = guestAt(firebase, realStore, GUEST)
    customers = db.read({}, 'Customers', countOnly=True)

    # startGuestSession never matches a store without a slug, whatever is
    # sent as the slug.
    for slug in (None, 5, ['bakery-a'], {'$ne': None}, {'$exists': False}):
        firebase['claims'] = firebaseClaims(
            sub='po-g2', user_id='po-g2',
            firebase={'sign_in_provider': 'anonymous'})
        assert _refusal(api.startGuestSession, TOKEN, slug) == 'Store not found'
    assert db.read({}, 'Customers', countOnly=True) == customers

    assert _refusal(api.listStoreProducts, customerId,
                    unset['_id']) == 'Store not found'
    assert _refusal(api.getStorePolicy, customerId,
                    unset['_id']) == 'Store not found'
    assert _refusal(api.getStorePolicy, customerId,
                    'no-such-store') == 'Store not found'


def test_startGuestSession_queryUsesThePartialSlugIndex(clean, firebase,
                                                        monkeypatch):
    store = createTestStore(ADMIN)
    stores = db.db['Stores']
    realRead = db.readResponse
    captured = []

    def spy(model, query, collectionName, *args, **kwargs):
        if collectionName == 'Stores':
            captured.append(query)
        return realRead(model, query, collectionName, *args, **kwargs)

    monkeypatch.setattr(db, 'readResponse', spy)
    guestAt(firebase, store, GUEST)
    [query] = captured
    assert query == {'slug': {'$eq': store['slug'], **SET_UP['slug']}}

    used = _indexNames(stores.find(query).explain()['queryPlanner'])
    assert STORE_SLUG_INDEX in used
    # Why the filter is restated: without it the partial index is not
    # eligible for the plain slug lookup of a string.
    bare = _indexNames(
        stores.find({'slug': store['slug']}).explain()['queryPlanner'])
    if STORE_SLUG_INDEX in bare:
        # This server proves string equality implies the filter itself: a
        # null lookup (what an unguarded None would run) is then the case
        # the restated filter protects.
        nullPlan = _indexNames(stores.find({'slug': None}).explain()
                               ['queryPlanner'])
        assert STORE_SLUG_INDEX not in nullPlan
    else:
        assert STORE_SLUG_INDEX not in bare


# ----------------------------- updateMyStorePolicy ----------------------------- #


def test_updateMyStorePolicy_changesOnlyTheSentFields(clean):
    store = _setUpStore()
    storeId = store['_id']
    start = _policyDoc(storeId)
    assert _answers(start) == NONE_ANSWERS

    # Falsy but real values are applied, a text is stored stripped.
    time.sleep(0.01)
    first = api.updateMyStorePolicy(ADMIN,
                                    sendsMarketingMessages=False,
                                    deliveryFee=0,
                                    handoverDays=0,
                                    freeDeliveryMinimum=0,
                                    deliveryOptions=['pickup', 'delivery'],
                                    additionalTerms='  No haggling  ')

    assertExactShape(StorePolicyResponse, first)
    stored = _policyDoc(storeId)
    PolicyView.model_validate(stored)
    want = _expected(sendsMarketingMessages=False,
                     deliveryFee=0,
                     handoverDays=0,
                     freeDeliveryMinimum=0,
                     deliveryOptions=['pickup', 'delivery'],
                     additionalTerms='No haggling')
    assert _answers(stored) == want
    assert {f: first[f] for f in POLICY_ANSWER_FIELDS} == want
    assert stored['_version'] == start['_version'] + 1
    assert stored['updatedAt'] > start['updatedAt']
    assert first['updatedAt'] == stored['updatedAt']
    for key in ('_id', 'storeId', 'createdAt'):
        assert stored[key] == start[key]

    # A second call changes only what it sends; "" on text and on a
    # Literal, and whitespace, mean unchanged.
    time.sleep(0.01)
    second = api.updateMyStorePolicy(ADMIN,
                                     refundMethod='',
                                     returnConditions='   ',
                                     deliveryAreas='',
                                     acceptsReturns=False,
                                     paidOrderCancellation='beforeHandover',
                                     acceptedPaymentMethods=['gcash', 'card'])
    after = _policyDoc(storeId)
    want = {
        **want, 'acceptsReturns': False,
        'paidOrderCancellation': 'beforeHandover',
        'acceptedPaymentMethods': ['gcash', 'card']
    }
    assert _answers(after) == want
    assert {f: second[f] for f in POLICY_ANSWER_FIELDS} == want
    assert after['_version'] == stored['_version'] + 1
    assert after['updatedAt'] > stored['updatedAt']
    for key in ('_id', 'storeId', 'createdAt'):
        assert after[key] == start[key]

    # clear sets an answer back to not said, and only that one.
    time.sleep(0.01)
    api.updateMyStorePolicy(ADMIN, clear=['deliveryFee', 'handoverDays'])
    cleared = _policyDoc(storeId)
    want = {**want, 'deliveryFee': None, 'handoverDays': None}
    assert _answers(cleared) == want
    assert cleared['_version'] == after['_version'] + 1
    assert cleared['updatedAt'] > after['updatedAt']

    # Clearing an answer that is already unset still counts as an update.
    api.updateMyStorePolicy(ADMIN, clear=['returnConditions'])
    assert _policyDoc(storeId)['_version'] == cleared['_version'] + 1

    # getMyStorePolicy and getStorePolicy send the same answer.
    mine = api.getMyStorePolicy(ADMIN)
    assertExactShape(StorePolicyResponse, mine)
    assert _answers(mine) == want


def test_updateMyStorePolicy_refusesClearMisuse_andAnEmptyUpdate(clean):
    store = _setUpStore()
    storeId = store['_id']
    api.updateMyStorePolicy(ADMIN, deliveryFee=5.5, handoverDays=2)
    before = _policyDoc(storeId)

    for badClear in (['bogus'], 'deliveryFee', [5], ['deliveryFee', None],
                     ['updatedAt'], ['storeId']):
        assert _refusal(api.updateMyStorePolicy, ADMIN,
                        clear=badClear) == CLEAR_FIELDS
    # A field both cleared and given a value.
    assert _refusal(api.updateMyStorePolicy, ADMIN, deliveryFee=5,
                    clear=['deliveryFee']) == (
                        'deliveryFee is both cleared and given a value: '
                        'send one')
    # An empty update: nothing sent, nothing cleared, or only blanks.
    for kwargs in ({}, {'clear': []}, {'returnConditions': '',
                                        'deliveryAreas': '   ',
                                        'refundMethod': '',
                                        'additionalTerms': '\t'}):
        assert _refusal(api.updateMyStorePolicy, ADMIN,
                        **kwargs) == NOTHING_SENT
    _unchanged(storeId, before)


def test_updateMyStorePolicy_normalisesWhatNoLongerApplies(clean):
    store = _setUpStore()
    storeId = store['_id']
    full = _expected(acceptsReturns=True,
                     returnWindowDays=14,
                     returnConditions='unused',
                     nonReturnableItems='cakes',
                     refundMethod='storeCredit',
                     returnShippingPaidBy='customer',
                     defectReportDays=3,
                     deliveryOptions=['pickup', 'delivery'],
                     deliveryAreas='Quezon City',
                     deliveryFee=50,
                     freeDeliveryMinimum=500,
                     handoverDays=2,
                     acceptedPaymentMethods=['gcash'],
                     paidOrderCancellation='beforeHandover',
                     sendsMarketingMessages=True,
                     additionalTerms='terms')
    api.updateMyStorePolicy(ADMIN, **full)
    start = _policyDoc(storeId)
    assert _answers(start) == full

    # No returns: the stale return answers are cleared silently, but the
    # non-returnable items and the defect days stay.
    time.sleep(0.01)
    res = api.updateMyStorePolicy(ADMIN, acceptsReturns=False)
    returnsOff = _policyDoc(storeId)
    want = {
        **full, 'acceptsReturns': False,
        **dict.fromkeys(POLICY_RETURN_ANSWERS)
    }
    assert want['nonReturnableItems'] == 'cakes'
    assert want['defectReportDays'] == 3
    assert _answers(returnsOff) == want
    assert {f: res[f] for f in POLICY_ANSWER_FIELDS} == want
    assert returnsOff['_version'] == start['_version'] + 1
    assert returnsOff['updatedAt'] > start['updatedAt']
    for key in ('_id', 'storeId', 'createdAt'):
        assert returnsOff[key] == start[key]

    # Pickup only: the delivery answers are cleared silently.
    api.updateMyStorePolicy(ADMIN, deliveryOptions=['pickup'])
    pickup = _policyDoc(storeId)
    want = {
        **want, 'deliveryOptions': ['pickup'],
        **dict.fromkeys(POLICY_DELIVERY_ANSWERS)
    }
    assert _answers(pickup) == want
    assert pickup['_version'] == returnsOff['_version'] + 1

    # A value sent now that normalisation would blank is refused by name,
    # and nothing is written.
    for kwargs, field in (({'returnWindowDays': 5}, 'returnWindowDays'),
                          ({'refundMethod': 'exchange'}, 'refundMethod'),
                          ({'returnConditions': 'x'}, 'returnConditions'),
                          ({'returnShippingPaidBy': 'store'},
                           'returnShippingPaidBy'),
                          ({'deliveryFee': 10}, 'deliveryFee'),
                          ({'deliveryAreas': 'Manila'}, 'deliveryAreas'),
                          ({'freeDeliveryMinimum': 1},
                           'freeDeliveryMinimum')):
        message = _refusal(api.updateMyStorePolicy, ADMIN, **kwargs)
        assert message == f'{field} {NOT_APPLICABLE[field]}: ask again'
        assert 'applies only' in message
    # Sent together with the flip that blanks them.
    assert 'returnWindowDays applies only when returns are accepted' in (
        _refusal(api.updateMyStorePolicy, ADMIN, acceptsReturns=False,
                 returnWindowDays=7))
    assert 'deliveryFee applies only with delivery' in _refusal(
        api.updateMyStorePolicy, ADMIN, deliveryOptions=['pickup'],
        deliveryFee=3)
    _unchanged(storeId, pickup)

    # Delivery back on with its answers in the same call is fine; so is a
    # delivery answer while deliveryOptions is not said yet (None applies).
    api.updateMyStorePolicy(ADMIN,
                            deliveryOptions=['delivery'],
                            deliveryFee=20)
    assert _policyDoc(storeId)['deliveryFee'] == 20
    api.updateMyStorePolicy(ADMIN, clear=['deliveryOptions'])
    api.updateMyStorePolicy(ADMIN, deliveryAreas='Pasig')
    assert _policyDoc(storeId)['deliveryAreas'] == 'Pasig'

    # Returns on without a window is refused; with a stored window it is not.
    assert 'returnWindowDays' in _refusal(api.updateMyStorePolicy, ADMIN,
                                          acceptsReturns=True)
    _expectedAfter = _policyDoc(storeId)
    api.updateMyStorePolicy(ADMIN, acceptsReturns=True, returnWindowDays=9)
    assert _policyDoc(storeId)['returnWindowDays'] == 9
    api.updateMyStorePolicy(ADMIN, acceptsReturns=True)
    assert _policyDoc(storeId)['_version'] == _expectedAfter['_version'] + 2
    # Clearing the window of a store that accepts returns is refused.
    assert 'returnWindowDays' in _refusal(api.updateMyStorePolicy, ADMIN,
                                          clear=['returnWindowDays'])

    # Clearing acceptsReturns keeps the stored window; pickup plus clearing
    # a delivery answer is fine; a field sent as '' and cleared is cleared.
    api.updateMyStorePolicy(ADMIN, clear=['acceptsReturns'])
    kept = _policyDoc(storeId)
    assert kept['acceptsReturns'] is None and kept['returnWindowDays'] == 9
    api.updateMyStorePolicy(ADMIN, deliveryOptions=['pickup'],
                            clear=['deliveryFee'])
    assert _policyDoc(storeId)['deliveryFee'] is None
    api.updateMyStorePolicy(ADMIN, returnConditions='x')
    api.updateMyStorePolicy(ADMIN, returnConditions='', clear=['returnConditions'])
    assert _policyDoc(storeId)['returnConditions'] is None
    api.updateMyStorePolicy(ADMIN, acceptsReturns=True)

    # Duplicates are refused.
    before = _policyDoc(storeId)
    for kwargs, field in (({'deliveryOptions': ['pickup', 'pickup']},
                           'deliveryOptions'),
                          ({'acceptedPaymentMethods': ['gcash', 'gcash']},
                           'acceptedPaymentMethods')):
        assert f'{field} lists an option twice' in _refusal(
            api.updateMyStorePolicy, ADMIN, **kwargs)
    _unchanged(storeId, before)


def test_updateMyStorePolicy_refusesBadValues_withoutQuotingThem(clean):
    store = _setUpStore()
    storeId = store['_id']
    before = _policyDoc(storeId)
    sentinel = 'ZZSENTINEL'
    bad = [
        ('returnWindowDays', 0),
        ('returnWindowDays', 366),
        ('returnWindowDays', 123456789),
        ('returnWindowDays', -4),
        ('returnWindowDays', True),
        ('returnWindowDays', '5'),
        ('returnWindowDays', 1.5),
        ('returnWindowDays', 5.0),
        ('defectReportDays', 0),
        ('defectReportDays', 366),
        ('handoverDays', -1),
        ('handoverDays', 366),
        ('deliveryFee', -1),
        ('deliveryFee', PRODUCT_PRICE_MAX + 1),
        ('deliveryFee', float('nan')),
        ('deliveryFee', float('inf')),
        ('deliveryFee', True),
        ('freeDeliveryMinimum', -0.01),
        ('acceptsReturns', 'yes'),
        ('acceptsReturns', 1),
        ('sendsMarketingMessages', 0),
        ('refundMethod', sentinel),
        ('returnShippingPaidBy', sentinel),
        ('paidOrderCancellation', sentinel),
        ('deliveryOptions', []),
        ('deliveryOptions', 'pickup'),
        ('deliveryOptions', ['pickup', sentinel]),
        ('acceptedPaymentMethods', []),
        ('acceptedPaymentMethods', ['card', sentinel]),
        ('acceptedPaymentMethods', {'card': 1}),
        ('returnConditions', 'a' * (POLICY_TEXT_MAX_LENGTH + 1)),
        ('nonReturnableItems', sentinel + 'b' * POLICY_TEXT_MAX_LENGTH),
        ('deliveryAreas', sentinel + 'c' * POLICY_TEXT_MAX_LENGTH),
        ('additionalTerms', sentinel + 'd' * POLICY_TERMS_MAX_LENGTH),
        ('returnConditions', f'{sentinel}\x00'),
        ('deliveryAreas', 5),
    ]
    for field, value in bad:
        message = _refusal(api.updateMyStorePolicy, ADMIN, **{field: value})
        assert field in message, (field, value, message)
        assert sentinel not in message
        for echo in ('123456789', str(PRODUCT_PRICE_MAX + 1)):
            assert echo not in message
        if field in TEXT_LIMITS and not isinstance(value, str):
            assert message == f'{field} must be text'
        elif field in TEXT_LIMITS:
            assert str(TEXT_LIMITS[field]) in message or (
                'control' in message)
        else:
            assert message == f'{field} is {ANSWER_RULES[field]}', (field,
                                                                  value)
    _unchanged(storeId, before)

    # The edges themselves are accepted.
    api.updateMyStorePolicy(ADMIN, deliveryFee=5)
    assert _policyDoc(storeId)['deliveryFee'] == 5
    api.updateMyStorePolicy(ADMIN,
                            returnWindowDays=1,
                            defectReportDays=365,
                            handoverDays=365,
                            deliveryFee=PRODUCT_PRICE_MAX,
                            freeDeliveryMinimum=0.01)
    assert _answers(_policyDoc(storeId)) == _expected(
        returnWindowDays=1,
        defectReportDays=365,
        handoverDays=365,
        deliveryFee=PRODUCT_PRICE_MAX,
        freeDeliveryMinimum=0.01)


def test_updateMyStorePolicy_acceptsTextsAtTheirLimit(clean):
    store = _setUpStore()
    texts = {
        'returnConditions': 'a' * POLICY_TEXT_MAX_LENGTH,
        'nonReturnableItems': 'b' * POLICY_TEXT_MAX_LENGTH,
        'deliveryAreas': 'c' * POLICY_TEXT_MAX_LENGTH,
        'additionalTerms': 'd' * POLICY_TERMS_MAX_LENGTH,
    }

    api.updateMyStorePolicy(ADMIN, **texts)

    assert _answers(_policyDoc(store['_id'])) == _expected(**texts)
    for field, text in texts.items():
        before = _policyDoc(store['_id'])
        message = _refusal(api.updateMyStorePolicy, ADMIN,
                           **{field: text + 'e'})
        assert str(len(text)) in message and field not in text
        _unchanged(store['_id'], before)


def test_updateMyStorePolicy_writesOneGuardedUpdate_afterEnsurePolicy(
        clean, monkeypatch):
    store = _setUpStore()
    storeId = store['_id']
    start = _policyDoc(storeId)
    realFind = db.findOneAndUpdate
    calls = []

    def spy(query, update, collectionName, **kwargs):
        if collectionName == 'Policies':
            calls.append((query, kwargs.get('upsert', True)))
        return realFind(query, update, collectionName, **kwargs)

    monkeypatch.setattr(db, 'findOneAndUpdate', spy)

    api.updateMyStorePolicy(ADMIN, handoverDays=3)

    [ensure, write] = calls
    assert ensure == ({'storeId': storeId}, True)
    guarded, upsert = write
    assert upsert is False
    assert guarded == {'_id': start['_id'], '_version': start['_version']}
    assert _policyDoc(storeId)['handoverDays'] == 3


def test_updateMyStorePolicy_aLostVersionRace_isRefusedAndWritesNothing(
        clean, monkeypatch):
    store = _setUpStore()
    storeId = store['_id']
    start = _policyDoc(storeId)
    realRead = db.read
    raced = []

    def racing(query, collectionName, *args, **kwargs):
        found = realRead(query, collectionName, *args, **kwargs)
        if collectionName == 'Policies' and not raced:
            raced.append(True)
            # Another call saves between this read and the guarded write.
            db.db['Policies'].update_one(
                {'storeId': storeId},
                {'$inc': {'_version': 1},
                 '$set': {'sendsMarketingMessages': True}})
        return found

    monkeypatch.setattr(db, 'read', racing)

    assert _refusal(api.updateMyStorePolicy, ADMIN,
                    handoverDays=3) == POLICY_CHANGED == (
                        'The policy just changed, try again')
    monkeypatch.undo()

    after = _policyDoc(storeId)
    # Only the racer's state is stored.
    assert _answers(after) == _expected(sendsMarketingMessages=True)
    assert after['_version'] == start['_version'] + 1
    assert after['updatedAt'] == start['updatedAt']

    # Trying again works.
    api.updateMyStorePolicy(ADMIN, handoverDays=3)
    assert _answers(_policyDoc(storeId)) == _expected(
        sendsMarketingMessages=True, handoverDays=3)

    # A policy deleted between ensurePolicy and the read is the same refusal.
    realRead2 = db.read

    def vanishing(query, collectionName, *args, **kwargs):
        if collectionName == 'Policies' and kwargs.get('findOne'):
            return None
        return realRead2(query, collectionName, *args, **kwargs)

    monkeypatch.setattr(db, 'read', vanishing)
    assert _refusal(api.updateMyStorePolicy, ADMIN,
                    handoverDays=4) == POLICY_CHANGED
    monkeypatch.undo()
    assert _policyDoc(storeId)['handoverDays'] == 3


def test_policyOfAStoreWithNoPolicyDocument(clean):
    store = _setUpStore()
    storeId = store['_id']
    created = db.read({'_id': storeId}, 'Stores', findOne=True)['createdAt']
    db.delete({'storeId': storeId}, 'Policies')

    # Reading sends all-null answers stamped with the store's creation, and
    # writes nothing.
    empty = api.getMyStorePolicy(ADMIN)
    assertExactShape(StorePolicyResponse, empty)
    assert {f: empty[f] for f in POLICY_ANSWER_FIELDS} == NONE_ANSWERS
    assert _naive(empty['updatedAt']) == _naive(created)
    assert _policyDoc(storeId) is None

    # An empty update and a refused answer create no document.
    assert _refusal(api.updateMyStorePolicy, ADMIN) == NOTHING_SENT
    assert _refusal(api.updateMyStorePolicy, ADMIN,
                    returnWindowDays=0).startswith('returnWindowDays')
    assert _policyDoc(storeId) is None

    # A real update creates exactly one, from the store's creation.
    api.updateMyStorePolicy(ADMIN, handoverDays=1)
    assert db.read({}, 'Policies', countOnly=True) == 1
    policy = _policyDoc(storeId)
    assert _naive(policy['createdAt']) == _naive(created)
    assert policy['_version'] == 1
    assert _answers(policy) == _expected(handoverDays=1)


# ----------------------------- reading ----------------------------- #


def test_readingPolicies_asAdminAndCustomer(clean, firebase):
    store = _setUpStore()
    other = _setUpStore(OTHER_ADMIN)
    # The same second name gives another slug, not a clash.
    customerId = guestAt(firebase, store, GUEST)
    api.updateMyStorePolicy(ADMIN,
                            acceptsReturns=True,
                            returnWindowDays=7,
                            deliveryOptions=['pickup'],
                            acceptedPaymentMethods=['gcash'])
    api.updateMyStorePolicy(OTHER_ADMIN, acceptsReturns=False)

    mine = api.getMyStorePolicy(ADMIN)
    theirs = api.getStorePolicy(customerId, store['_id'])

    assertExactShape(StorePolicyResponse, mine)
    assertExactShape(StorePolicyResponse, theirs)
    assert mine == theirs
    for key in ('_id', '_version', 'storeId'):
        assert key not in mine
    assert _answers(mine) == _expected(acceptsReturns=True,
                                       returnWindowDays=7,
                                       deliveryOptions=['pickup'],
                                       acceptedPaymentMethods=['gcash'])
    # The other store's admin has their own policy, untouched by ours.
    assert api.getMyStorePolicy(OTHER_ADMIN)['acceptsReturns'] is False
    assert _answers(_policyDoc(other['_id'])) == _expected(
        acceptsReturns=False)

    # Roles: a customer on an admin route, an admin on a customer route.
    with pytest.raises(PermissionError, match='Only a store admin'):
        api.getMyStorePolicy(customerId)
    with pytest.raises(PermissionError, match='Only a store admin'):
        api.updateMyStorePolicy(customerId, acceptsReturns=True)
    with pytest.raises(PermissionError, match='Only a customer'):
        api.getStorePolicy(ADMIN, store['_id'])
    assert _refusal(api.getStorePolicy, customerId,
                    {'$ne': None}) == STORE_ID_NOT_TEXT
    assert _answers(_policyDoc(store['_id'])) == _answers(mine)


# ----------------------------- MCP ----------------------------- #


def test_e2e_theAdminSetsThePolicy_theCustomerReadsIt(clean, firebase):
    store = _setUpStore()
    createTestStore(OTHER_ADMIN, 'Other Shop')
    customerId = guestAt(firebase, store, GUEST)

    saved = callTool(_auth(ADMIN), 'updateMyStorePolicy', {
        'acceptsReturns': True,
        'returnWindowDays': 7,
        'refundMethod': 'originalPayment',
        'userId': OTHER_ADMIN,
    }, audience='admin')

    assert not saved.is_error, _text(saved)
    assert _answers(_policyDoc(store['_id'])) == _expected(
        acceptsReturns=True,
        returnWindowDays=7,
        refundMethod='originalPayment')
    # The AI cannot pick another user: the other store's policy is untouched.
    assert _answers(_policyDoc(
        db.read({'ownerUserId': OTHER_ADMIN}, 'Stores',
                findOne=True)['_id'])) == NONE_ANSWERS
    mine = callTool(_auth(ADMIN), 'getMyStorePolicy', {}, audience='admin')
    direct = api.getMyStorePolicy(ADMIN)
    assert _answers(mine.structured_content) == _answers(direct)
    assert mine.structured_content['updatedAt'].startswith(
        direct['updatedAt'].strftime('%Y-%m-%dT%H:%M:%S'))

    read = callTool(_auth(customerId, store['_id']), 'getStorePolicy', {
        'storeId': 'someone-elses-store'
    })

    assert not read.is_error, _text(read)
    assert read.structured_content == mine.structured_content
    assert _answers(read.structured_content) == _answers(direct)

    # A refused answer reaches the AI with our message, without the value.
    refused = callTool(_auth(ADMIN), 'updateMyStorePolicy',
                       {'refundMethod': 'ZZSENTINEL'}, audience='admin')
    assert refused.is_error
    assert f'refundMethod is {ANSWER_RULES["refundMethod"]}' in _text(refused)
    assert 'ZZSENTINEL' not in _text(refused)


def test_theToolSchemas_leaveOutTheInjectedParams():
    import asyncio

    import mcpServer

    async def tools(audience):
        listed = await mcpServer.servers[audience].list_tools()
        return {t.name: t.input_schema for t in listed}

    admin = asyncio.run(tools('admin'))
    customer = asyncio.run(tools('customer'))

    assert admin['getMyStorePolicy'].get('properties', {}) == {}
    assert customer['getStorePolicy'].get('properties', {}) == {}
    assert set(admin['updateMyStorePolicy']['properties']) == set(
        POLICY_ANSWER_FIELDS) | {'clear'}
    assert 'required' not in admin['updateMyStorePolicy'] or not admin[
        'updateMyStorePolicy']['required']


# ----------------------------- superAdmin bootstrap ----------------------------- #


def _claimHolder() -> str | None:
    from AuthHandler import BOOTSTRAP_COLLECTION, SUPER_ADMIN_CLAIM_ID
    claim = db.read({'_id': SUPER_ADMIN_CLAIM_ID}, BOOTSTRAP_COLLECTION,
                    findOne=True)
    return None if claim is None else claim['userId']


@pytest.mark.parametrize('failAt', ['Users', 'Policies'])
def test_signUp_failingAfterTheClaim_retryStillGetsSuperAdmin(
        failAt, monkeypatch, firebase, emptyUsers):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    with monkeypatch.context() as patch:
        patch.setattr(db, 'createTransaction',
                      _fakeTransaction(TOKEN_UID, failAt))
        with pytest.raises(RuntimeError, match='write failed'):
            _signIn(firebase)
    # The claim outlives the failed sign-up; nothing else was kept.
    assert _claimHolder() == TOKEN_UID
    assert db.read({}, 'Users') == []
    assert _storeDocs() == []
    assert db.read({}, 'Policies') == []

    res = _signIn(firebase)

    assert res['userType'] == 'superAdmin'
    assert db.read({}, 'Users', countOnly=True) == 1
    [store] = _storeDocs()
    assert store['ownerUserId'] == TOKEN_UID
    assert _answers(_policyDoc(store['_id'])) == NONE_ANSWERS


def test_signUp_failingAfterTheClaim_retryAfterAnotherUserSignedUp(
        monkeypatch, firebase, emptyUsers):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    with monkeypatch.context() as patch:
        patch.setattr(db, 'createTransaction',
                      _fakeTransaction(TOKEN_UID, 'Stores'))
        with pytest.raises(RuntimeError, match='write failed'):
            _signIn(firebase)
    assert _claimHolder() == TOKEN_UID

    # Another user signs up in between: Users is no longer empty.
    between = _signIn(firebase, 'po-between')
    assert between['userType'] is None
    assert db.read({}, 'Users', countOnly=True) == 1

    retry = _signIn(firebase)

    assert retry['_id'] == TOKEN_UID
    assert retry['userType'] == 'superAdmin'
    assert _claimHolder() == TOKEN_UID
    assert db.read({}, 'Users', countOnly=True) == 2
    assert db.read({}, 'Stores', countOnly=True) == 2
    assert db.read({}, 'Policies', countOnly=True) == 2
    assert _signIn(firebase, 'po-between')['userType'] is None


def test_signUp_aThirdUidNeverGetsTheClaim(monkeypatch, firebase,
                                           anotherUser):
    from AuthHandler import BOOTSTRAP_COLLECTION, SUPER_ADMIN_CLAIM_ID
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    db.create({
        '_id': SUPER_ADMIN_CLAIM_ID,
        '_version': 0,
        'userId': 'po-holder'
    }, BOOTSTRAP_COLLECTION)

    res = _signIn(firebase, 'po-third')

    assert res['userType'] is None
    assert _claimHolder() == 'po-holder'
    assert db.read({'_id': 'po-third'}, 'Users',
                   findOne=True)['roles'] is not None


# ----------------------------- drift pins ----------------------------- #


def test_policyAnswerFields_areDeclaredOnceEverywhere():
    answers = set(POLICY_ANSWER_FIELDS)
    params = set(
        inspect.signature(PolicyActions.updateMyStorePolicy).parameters
    ) - {'self', 'userId', 'clear'}

    assert set(typing.get_args(PolicyField)) == answers
    assert set(StorePolicyResponse.model_fields) - {'updatedAt'} == answers
    assert set(ANSWER_RULES) == answers
    assert params == answers
    assert answers == set(Policy.model_fields) - {
        'id', 'version', 'storeId', 'createdAt', 'updatedAt'
    }
    # The text answers, each with a max length.
    textFields = {
        field
        for field in POLICY_ANSWER_FIELDS
        if typing.get_args(Policy.model_fields[field].annotation)[0] is str
    }
    assert set(TEXT_LIMITS) == textFields == {
        'returnConditions', 'nonReturnableItems', 'deliveryAreas',
        'additionalTerms'
    }
    assert all(isinstance(limit, int) and limit > 0
               for limit in TEXT_LIMITS.values())
    assert TEXT_LIMITS['additionalTerms'] == POLICY_TERMS_MAX_LENGTH
    # The description the AI reads names every answer, the not-applicable
    # map covers the dependent answers, and MCP offers answers plus clear.
    for field in POLICY_ANSWER_FIELDS:
        assert field in UPDATE_POLICY_DESCRIPTION
    assert set(NOT_APPLICABLE) == set(POLICY_RETURN_ANSWERS +
                                      POLICY_DELIVERY_ANSWERS)


# ----------------------------- indexes ----------------------------- #


@pytest.fixture
def slugIndexes():
    """Stores and Policies empty around the test, and every index back as
    ensureIndexes builds them afterwards."""
    wipeCollections(['Stores', 'Policies'])
    yield db.db['Stores']
    wipeCollections(['Stores', 'Policies'])
    stores = db.db['Stores']
    for name in (LEGACY_STORE_SLUG_INDEX, STORE_SLUG_INDEX):
        if name in stores.index_information():
            stores.drop_index(name)
    db.ensureIndexes()


def test_ensureIndexes_replacesTheOldSlugIndex_andRunsTwiceAsANoOp(
        slugIndexes, monkeypatch):
    stores = slugIndexes
    stores.drop_index(STORE_SLUG_INDEX)
    stores.create_index('slug', unique=True)
    assert LEGACY_STORE_SLUG_INDEX in stores.index_information()
    # The old index collides on the slug of stores not set up yet.
    stores.insert_one(_storeDoc('po-1'))
    with pytest.raises(DuplicateKeyError):
        stores.insert_one(_storeDoc('po-2'))
    stores.delete_many({})

    dropped = []
    realDrop = pymongo.collection.Collection.drop_index

    def drop(self, index_or_name, *args, **kwargs):
        # The new index exists before the old one goes: the slug stays unique.
        dropped.append((index_or_name, STORE_SLUG_INDEX
                        in self.index_information()))
        return realDrop(self, index_or_name, *args, **kwargs)

    monkeypatch.setattr(pymongo.collection.Collection, 'drop_index', drop)

    db.ensureIndexes()

    assert dropped == [(LEGACY_STORE_SLUG_INDEX, True)]
    info = stores.index_information()
    assert LEGACY_STORE_SLUG_INDEX not in info
    assert info[STORE_SLUG_INDEX]['unique'] is True
    assert info[STORE_SLUG_INDEX]['key'] == [('slug', 1)]
    assert info[STORE_SLUG_INDEX]['partialFilterExpression'] == {
        'slug': {'$type': 'string'}
    }
    assert db.db['Policies'].index_information()['storeId_1']['unique']
    # Each xdist worker runs on its own *Scratch database.
    assert db.db.name.endswith('Scratch')
    # Stores that are not set up now coexist.
    stores.insert_one(_storeDoc('po-1'))
    stores.insert_one(_storeDoc('po-2'))
    # A set-up slug is still unique, and so is a Policies storeId.
    stores.insert_one(_storeDoc('po-3', slug='taken'))
    with pytest.raises(DuplicateKeyError):
        stores.insert_one(_storeDoc('po-4', slug='taken'))
    db.db['Policies'].insert_one({'storeId': 's1', '_version': 0})
    with pytest.raises(DuplicateKeyError):
        db.db['Policies'].insert_one({'storeId': 's1', '_version': 0})

    # Running it again changes nothing and drops nothing.
    dropped.clear()
    db.ensureIndexes()
    assert dropped == []
    assert stores.index_information() == info


@pytest.mark.parametrize('failAt', ['Stores', 'Policies'])
def test_signUp_partialFailure_withoutRollback_isMadeUpByCreateUserStore(
        failAt, monkeypatch, firebase, emptyUsers):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    with monkeypatch.context() as patch:
        patch.setattr(db, 'createTransaction',
                      _fakeTransaction(TOKEN_UID, failAt, rollBack=False))
        with pytest.raises(RuntimeError, match='write failed'):
            _signIn(firebase)
    assert _claimHolder() == TOKEN_UID
    assert db.read({}, 'Users', countOnly=True) == 1
    assert db.read({}, 'Policies') == []

    # The retry returns the stored user, who holds the claim's user.
    retry = _signIn(firebase)
    assert retry['_id'] == TOKEN_UID
    assert db.read({}, 'Users', countOnly=True) == 1
    assert api.getUserStore(TOKEN_UID) == {'store': None}
    assert _refusal(api.getMyStorePolicy, TOKEN_UID) == NO_STORE_YET

    # createUserStore makes up for what the sign-up did not write.
    created = api.createUserStore(TOKEN_UID, 'Bakery A', **STORE_DETAILS)
    assert db.read({'ownerUserId': TOKEN_UID}, 'Stores',
                   countOnly=True) == 1
    assert db.read({}, 'Policies', countOnly=True) == 1
    assert _answers(_policyDoc(created['_id'])) == NONE_ANSWERS
    assert _answers(api.getMyStorePolicy(TOKEN_UID)) == NONE_ANSWERS
