"""Store reset (resetMyStore): the admin's store and everything in it are
deleted and a new empty store takes its place, until STORE_RESET_ENDS_AT.

Called directly, with an HTTP check where the route's envelope, its status or
its log lines matter. Storage is a FakeBucket (photoStorage._bucket patched);
no Meta, Firebase or Google call.

Not testable locally: the local mongod is standalone, so only the
no-session path of db.createTransaction runs. The swap's atomic commit or
abort (the old store deleted and the new one created together) is not
proven here, only what each step does.

Every test runs with StoreActions._now patched to a time BEFORE the sunset
(today is 2026-10-30 and the suite must keep passing after Nov 5); the
sunset tests patch it themselves. The module-level fixtures (clean, storage)
follow test_photos.py and test_admin_products.py: the database is wiped
first (wipeDatabase) and again in the fixture's teardown, which runs even
when the test fails.
"""

import asyncio
import datetime
import importlib
import logging
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token
from google.api_core.exceptions import InternalServerError, NotFound

import AuthHandler
import chatTools
import mcpServer
import photoStorage
import pubSub as pubSubModule
from actions import ChatActions, PhotoActions, VoiceActions, usageCounters
from ApiRequests import ApiRequests
from conftest import (BRAND_COLORS, STORE_DETAILS, TOKEN, addOrder,
                      addProduct, assertExactShape, createTestStore, createTestUser,
                      guestClaims, wipeCollections)
from mongoDb import db
from objects import (ChatAttachment, ChatTurn, Conversation, Customer,
                     GeneratedPhoto, MyStoreResponse, Policy, Return)
from utils import generateRandomString

storeModule = importlib.import_module('actions.StoreActions')

ADMIN_A = 'rs-admin-a'
ADMIN_B = 'rs-admin-b'
CUSTOMER = 'rs-customer'
OTHER_CUSTOMER = 'rs-other-customer'
PHONE = '+639171234567'
SECRET = 'boom ' + 'secret message'
JWT_SECRET = 'rs-http-test-jwt-secret-0123456789abcdefghij'

UTC = datetime.timezone.utc
# 12:00 on 2026-10-30 in UTC+8, before the sunset.
BEFORE = datetime.datetime(2026, 10, 30, 4, 0, tzinfo=UTC)
TODAY = datetime.date(2026, 10, 30)
# 2026-11-05T00:00:00+08:00.
ENDS = datetime.datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
# Far ahead, so no TTL monitor deletes a counter mid-test.
FAR = datetime.datetime(2099, 3, 4, 5, 6, 7, tzinfo=UTC)

CHAT_USAGE = 'ChatUsage'
PHOTO_USAGE = 'PhotoUsage'
VOICE_USAGE = 'VoiceUsage'
USAGE_COLLECTIONS = [CHAT_USAGE, PHOTO_USAGE, VOICE_USAGE]
# The per-store counters a reset carries over: literal on purpose, so a
# kind renamed or added in a module fails here instead of silently being
# dropped from the carry-over.
STORE_KINDS = [(CHAT_USAGE, 'store'), (CHAT_USAGE, 'attachmentsStore'),
               (PHOTO_USAGE, 'photos'), (PHOTO_USAGE, 'productFinds'),
               (PHOTO_USAGE, 'colorFinds'), (VOICE_USAGE, 'sttStore'),
               (VOICE_USAGE, 'ttsStore')]
# The collections the reset empties for the old store, as storeReset's
# `deleted` names them.
DELETED_COLLECTIONS = {'ChatTurns', 'Conversations', 'Products', 'Orders',
                       'OrderLocks', 'Returns', 'Policies', 'GeneratedPhotos'}
STORE_DATA = ['Products', 'Orders', 'OrderLocks', 'Returns', 'Policies',
              'GeneratedPhotos', 'Conversations', 'ChatTurns']
KEPT = ['Users', 'Customers', 'Bootstrap']
COLLECTIONS = (['Stores'] + STORE_DATA + KEPT + USAGE_COLLECTIONS)

api = ApiRequests()


def wipeDatabase() -> None:
    wipeCollections(COLLECTIONS)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    wipeDatabase()
    monkeypatch.setattr(storeModule, '_now', lambda: BEFORE)
    yield
    wipeDatabase()


class FakeBlob:

    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def delete(self, **kwargs):
        self.bucket.events.append(('storage', 'delete', self.name))
        if self.name not in self.bucket.objects:
            raise NotFound('gone')
        del self.bucket.objects[self.name]
        self.bucket.deleted.append(self.name)


class FakeBucket:
    """list_blobs(prefix=) and blob.delete(), over a dict of name -> bytes.
    `listFailure(prefix)` returns the exception list_blobs raises for that
    prefix, or None."""

    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.listed = []
        self.events = []
        self.listFailure = lambda prefix: None

    def put(self, name):
        self.objects[name] = b'data'

    def list_blobs(self, prefix, **kwargs):
        self.events.append(('storage', 'list', prefix))
        self.listed.append(prefix)
        failure = self.listFailure(prefix)
        if failure is not None:
            raise failure
        return [FakeBlob(self, name) for name in sorted(self.objects)
                if name.startswith(prefix)]

    def blob(self, name):
        return FakeBlob(self, name)


@pytest.fixture
def storage(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    return bucket


# --------------------------------- seeds --------------------------------- #


def seedUsage(collection: str, kind: str, owner: str, count: int,
              day: datetime.date = TODAY,
              expireAt: datetime.datetime = FAR) -> str:
    """A daily counter as production writes it (takeQuota), with `count`
    and `expireAt`. Returns its key."""
    key = usageCounters.usageKey(kind, owner, day)
    assert usageCounters.takeQuota(collection, key, 10**9, expireAt, count)
    return key


def seedCustomer(customerId: str) -> None:
    customer = Customer(_id=customerId, phoneNumber=PHONE, createdAt=BEFORE,
                        _version=0)
    db.create(customer.model_dump(by_alias=True), 'Customers')


def seedConversation(userId: str, storeId: str, audience: str,
                     turnCount: int = 0, title: str = 'a title') -> dict:
    conversation = Conversation(_id=generateRandomString(),
                                userId=userId,
                                audience=audience,
                                storeId=storeId,
                                title=title,
                                turnCount=turnCount,
                                createdAt=BEFORE,
                                lastMessageAt=BEFORE,
                                busyUntil=None,
                                _version=0)
    return db.create(conversation.model_dump(by_alias=True), 'Conversations')


def seedTurn(conversation: dict, seq: int, paths: list[str] = ()) -> dict:
    """A turn of `conversation` that carried the files at `paths`."""
    turn = ChatTurn(
        conversationId=conversation['_id'],
        userId=conversation['userId'],
        storeId=conversation['storeId'],
        seq=seq,
        userText=f'question {seq}',
        attachments=[ChatAttachment(path=path, contentType='image/jpeg',
                                    name=path.rsplit('/', 1)[-1], size=4)
                     for path in paths],
        steps=[],
        replyText=f'answer {seq}',
        toolCallCount=0,
        usage={'promptTokens': 0, 'completionTokens': 0, 'cachedTokens': 0},
        model='test-model',
        createdAt=BEFORE,
        _version=0)
    return db.create(turn.model_dump(by_alias=True), 'ChatTurns')


def chatFile(uid: str, name: str) -> str:
    return f'users/{uid}/chat/{name}'


def seedStoreData(store: dict, adminId: str, customerId: str) -> None:
    """Products, orders (and the customer's lock), returns, generated
    photos and chats of `store`, the policy it already has aside."""
    storeId = store['_id']
    product = addProduct(storeId, 'Pandesal', 5)
    items = [{'productId': product['_id'], 'name': 'Pandesal', 'price': 5,
              'quantity': 2}]
    order = addOrder(storeId, customerId, items, status='completed')
    # OrderLocks has no model: createOrder writes this very shape.
    db.create({'_id': f'{customerId}:{storeId}', 'customerId': customerId,
               'storeId': storeId, 'n': 1, '_version': 0}, 'OrderLocks')
    returned = Return(storeId=storeId, customerId=customerId,
                      orderId=order['_id'], items=items,
                      reason='changedMind', refundAmount=5, currency='PHP',
                      contactNumber=PHONE, status='requested',
                      createdAt=BEFORE, _version=0)
    db.create(returned.model_dump(by_alias=True), 'Returns')
    photoPath = f'stores/{storeId}/photos/p1.jpg'
    photo = GeneratedPhoto(storeId=storeId, createdBy=adminId,
                           path=photoPath, url='https://example.com/p1',
                           sourcePaths=[f'users/{adminId}/ref1.jpg'],
                           model='test-image', createdAt=BEFORE, _version=0)
    db.create(photo.model_dump(by_alias=True), 'GeneratedPhotos')
    adminChat = seedConversation(adminId, storeId, 'admin', 1)
    seedTurn(adminChat, 0, [chatFile(adminId, f'{storeId}-a1.jpg')])
    customerChat = seedConversation(customerId, storeId, 'customer', 1)
    seedTurn(customerChat, 0, [chatFile(customerId, f'{storeId}-c1.jpg')])


def seedWorld() -> SimpleNamespace:
    """Two admins with set-up stores, each with data, two customers, every
    kind of counter and the superAdmin claim."""
    storeA = createTestStore(ADMIN_A, 'Bakery A')
    storeB = createTestStore(ADMIN_B, 'Cafe B')
    for store in (storeA, storeB):
        storeModule.ensurePolicy(store['_id'], BEFORE)
    seedCustomer(CUSTOMER)
    seedCustomer(OTHER_CUSTOMER)
    seedStoreData(storeA, ADMIN_A, CUSTOMER)
    seedStoreData(storeB, ADMIN_B, OTHER_CUSTOMER)
    assert AuthHandler._claimSuperAdmin(ADMIN_A)
    for index, (collection, kind) in enumerate(STORE_KINDS):
        for owner in (storeA['_id'], storeB['_id'], CUSTOMER, 'all'):
            seedUsage(collection, kind, owner, 10 + index)
    return SimpleNamespace(a=storeA, b=storeB)


def seedFiles(bucket: FakeBucket, world: SimpleNamespace) -> SimpleNamespace:
    """The Storage side of seedWorld, and extra chat turns that point at
    files (also at files that must never be deleted)."""
    a, b = world.a['_id'], world.b['_id']
    adminAChat = [c for c in db.read({'storeId': a}, 'Conversations')
                  if c['audience'] == 'admin'][0]
    customerAChat = [c for c in db.read({'storeId': a}, 'Conversations')
                     if c['audience'] == 'customer'][0]
    # Referenced by the deleted store's turns.
    mine = [chatFile(ADMIN_A, 'a1.jpg'), chatFile(ADMIN_A, 'a2.pdf'),
            chatFile(ADMIN_A, 'nested/deep.jpg'),
            chatFile(ADMIN_A, 'viaCustomerTurn.jpg')]
    notMine = [chatFile(CUSTOMER, 'c1.jpg'),
               # Skipped, never deleted: a HEIC name, a '..' path, another
               # user's folder, and another store's photo.
               chatFile(ADMIN_A, 'pic.heic'),
               f'users/{ADMIN_A}/chat/../../{ADMIN_B}/ref.jpg',
               f'users/{ADMIN_B}/ref.jpg', f'stores/{b}/photos/b1.jpg']
    seedTurn(adminAChat, 1, mine[:3] + notMine[1:2])
    seedTurn(adminAChat, 2, notMine[2:4])
    seedTurn(customerAChat, 1, [mine[3], notMine[0], notMine[4]])
    # Another store's turn of the same admin (as a customer there) refers
    # to a file of the admin's chat folder: it stays.
    elsewhere = seedConversation(ADMIN_A, b, 'customer', 1)
    seedTurn(elsewhere, 0, [chatFile(ADMIN_A, 'shared.jpg')])
    deleted = (
        [f'stores/{a}/photos/p1.jpg', f'stores/{a}/photos/p2.jpg',
         f'users/{ADMIN_A}/ref1.jpg', f'users/{ADMIN_A}/nested/ref2.png',
         f'users/{ADMIN_A}/chatter.jpg', f'users/{ADMIN_A}/chat.jpg']
        + mine)
    kept = (
        [f'stores/{b}/photos/b1.jpg', f'stores/{a}x/photos/z.jpg',
         f'users/{ADMIN_A}2/ref.jpg', f'users/{ADMIN_B}/ref.jpg',
         f'users/{CUSTOMER}/ref.jpg', chatFile(CUSTOMER, 'c1.jpg'),
         chatFile(ADMIN_A, 'shared.jpg'), chatFile(ADMIN_A, 'orphan.jpg'),
         chatFile(ADMIN_A, 'pic.heic')])
    for name in deleted + kept:
        bucket.put(name)
    bucket.deleted.clear()
    bucket.events.clear()
    return SimpleNamespace(deleted=deleted, kept=kept)


def storeDocs(storeId: str) -> dict:
    """Everything stored for the store `storeId`, by collection."""
    docs = {collection: sorted(db.read({'storeId': storeId}, collection),
                               key=lambda doc: doc['_id'])
            for collection in STORE_DATA}
    docs['Stores'] = db.read({'_id': storeId}, 'Stores')
    return docs


def everything() -> dict:
    return {collection: sorted(db.read({}, collection),
                               key=lambda doc: doc['_id'])
            for collection in COLLECTIONS}


def usageDocs() -> dict:
    return {collection: {doc['_id']: doc for doc in db.read({}, collection)}
            for collection in USAGE_COLLECTIONS}


def usageDoc(collection: str, key: str) -> dict | None:
    return db.read({'_id': key}, collection, findOne=True)


def ownStore(adminId: str) -> dict:
    return db.read({'ownerUserId': adminId}, 'Stores', findOne=True)


def emptyAdmin(adminId: str) -> dict:
    """An admin whose store is not set up yet (what sign-up leaves)."""
    createTestUser(adminId)
    return storeModule.createEmptyStore(adminId, BEFORE, None)


def logRecords(caplog, event: str) -> list:
    return [r for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == event]


def httpPost(monkeypatch, route, identity, body):
    """POST to the generated Flask app with a JWT for `identity`:
    (status code, body)."""
    monkeypatch.setenv('JWT_SECRET_KEY', JWT_SECRET)

    def noGcp(*args, **kwargs):
        raise AssertionError('a test tried to create the Pub/Sub clients')

    monkeypatch.setattr(pubSubModule.pubSub, '_ensureClients', noGcp)
    flaskApp = importlib.import_module('app').app
    with flaskApp.app_context():
        token = create_access_token(identity=identity)
    res = flaskApp.test_client().post(
        f'/{route}', json=body, headers={'Authorization': f'Bearer {token}'})
    return res.status_code, res.get_json()


# ------------------------------- 1. sunset ------------------------------- #


def test_theSunsetConstant_isNov5PhilippineTime_inServerAndClient():
    from sharedConstants import STORE_RESET_ENDS_AT
    assert STORE_RESET_ENDS_AT == '2026-11-05T00:00:00+08:00'
    assert storeModule.STORE_RESET_ENDS == ENDS
    generated = (Path(__file__).resolve().parent.parent / 'client' / 'lib' /
                 'generated' / 'serverConstants.ts').read_text('utf-8')
    assert ('export const STORE_RESET_ENDS_AT = '
            '"2026-11-05T00:00:00+08:00"') in generated


def test_oneSecondBeforeTheSunset_theResetWorks(monkeypatch, storage):
    createTestStore(ADMIN_A)
    monkeypatch.setattr(storeModule, '_now',
                        lambda: ENDS - datetime.timedelta(seconds=1))

    res = api.resetMyStore(ADMIN_A, 'Bakery A')

    assert res == {'store': None}


@pytest.mark.parametrize('now', [
    ENDS,
    ENDS + datetime.timedelta(seconds=1),
    datetime.datetime(2027, 1, 1, tzinfo=UTC),
], ids=['atTheInstant', 'oneSecondAfter', 'farAfter'])
def test_atOrAfterTheSunset_itIs403_andTouchesNothing(monkeypatch, storage,
                                                      now):
    world = seedWorld()
    files = seedFiles(storage, world)
    before = everything()
    monkeypatch.setattr(storeModule, '_now', lambda: now)

    def touched(*args, **kwargs):
        raise AssertionError('the sunset must come before any read or write')

    with monkeypatch.context() as patched:
        for name in ('read', 'delete', 'deleteMany', 'createTransaction',
                     'findOneAndUpdate'):
            patched.setattr(db, name, touched)
        patched.setattr(photoStorage, '_bucket', touched)
        # Not even an admin: the sunset is checked first.
        for caller in (ADMIN_A, CUSTOMER, 'nobody'):
            with pytest.raises(PermissionError,
                               match='^Store reset is no longer available$'):
                api.resetMyStore(caller, 'Bakery A')

    assert everything() == before
    assert sorted(storage.objects) == sorted(files.deleted + files.kept)


def test_pastTheSunset_overHttp_is403_withTheMessage(monkeypatch, storage):
    createTestStore(ADMIN_A)
    monkeypatch.setattr(storeModule, '_now', lambda: ENDS)

    status, body = httpPost(monkeypatch, 'resetMyStore', ADMIN_A,
                            {'userId': ADMIN_A, 'confirmName': 'Bakery A'})

    assert status == 403
    assert body['message'] == 'Store reset is no longer available'
    assert ownStore(ADMIN_A)['name'] == 'Bakery A'


# ------------------------------ 2. refusals ------------------------------ #


def assertRefusedUntouched(world, files, storage, before, call, error,
                           message):
    with pytest.raises(error, match=message):
        call()
    assert everything() == before
    assert sorted(storage.objects) == sorted(files.deleted + files.kept)
    assert storage.deleted == []


def test_aCustomer_andAnUnknownUser_are403(storage):
    world = seedWorld()
    files = seedFiles(storage, world)
    before = everything()

    for caller in (CUSTOMER, 'rs-nobody'):
        assertRefusedUntouched(world, files, storage, before,
                               lambda: api.resetMyStore(caller, 'Bakery A'),
                               PermissionError, 'Only a store admin')


def test_anAdminWithoutAStore_is400(storage):
    createTestUser(ADMIN_A)
    before = everything()

    with pytest.raises(ValueError, match='^You have no store to reset$'):
        api.resetMyStore(ADMIN_A, 'reset')

    assert everything() == before
    assert storage.listed == []


@pytest.mark.parametrize('typed', [
    '', '   ', 'Bakery', 'Bakery A!', 'Bakery  A', 'Cafe B', 'reset', None, 5,
    ['Bakery A'], 'Bakery A​',
], ids=lambda value: repr(value))
def test_aWrongName_isRefused_andNothingIsDeleted(storage, typed):
    world = seedWorld()
    files = seedFiles(storage, world)
    before = everything()

    assertRefusedUntouched(world, files, storage, before,
                           lambda: api.resetMyStore(ADMIN_A, typed),
                           ValueError, "^Type your store's name to confirm$")


@pytest.mark.parametrize('typed', [
    'Bakery A', 'bakery a', '  BAKERY A  ', '\tBakery A\n',
    'ＢＡＫＥＲＹ Ａ',
], ids=['exact', 'lower', 'paddedUpper', 'whitespace', 'fullwidth'])
def test_theName_isMatchedFoldedAndTrimmed(storage, typed):
    createTestStore(ADMIN_A, 'Bakery A')

    assert api.resetMyStore(ADMIN_A, typed) == {'store': None}


def test_theName_isMatchedInAnyUnicodeForm(storage):
    composed = 'Café Niño'
    createTestStore(ADMIN_A, composed)
    typed = unicodedata.normalize('NFD', composed)
    assert typed != composed

    assert api.resetMyStore(ADMIN_A, typed) == {'store': None}


@pytest.mark.parametrize('typed', ['reset', '  RESET ', 'Reset'])
def test_aStoreNotSetUp_takesTheWordReset(storage, typed):
    old = emptyAdmin(ADMIN_A)

    assert api.resetMyStore(ADMIN_A, typed) == {'store': None}
    assert ownStore(ADMIN_A)['_id'] != old['_id']


@pytest.mark.parametrize('typed', ['', 'Bakery', 'resets', 'reset now', None])
def test_aStoreNotSetUp_refusesAnythingElse(storage, typed):
    old = emptyAdmin(ADMIN_A)
    before = everything()

    with pytest.raises(ValueError, match="^Type your store's name to confirm$"):
        api.resetMyStore(ADMIN_A, typed)

    assert everything() == before
    assert ownStore(ADMIN_A)['_id'] == old['_id']


def test_aSetUpStore_refusesTheWordReset(storage):
    createTestStore(ADMIN_A, 'Bakery A')
    before = everything()

    with pytest.raises(ValueError, match="^Type your store's name to confirm$"):
        api.resetMyStore(ADMIN_A, 'reset')

    assert everything() == before


def test_aStoreChangedMeanwhile_isStoreChanged_andNothingIsCreated(
        monkeypatch, storage):
    world = seedWorld()
    files = seedFiles(storage, world)
    realTransaction = db.createTransaction

    def racing(update):
        # The admin changes the store after the route read it.
        api.updateMyStoreTheme(ADMIN_A, primaryColor='#112233')
        return realTransaction(update)

    monkeypatch.setattr(db, 'createTransaction', racing)
    old = ownStore(ADMIN_A)
    stores = db.read({}, 'Stores')
    policies = db.read({}, 'Policies')

    with pytest.raises(ValueError, match='^Your store just changed, try '
                       'again$'):
        api.resetMyStore(ADMIN_A, 'Bakery A')

    assert ownStore(ADMIN_A)['_id'] == old['_id']
    assert ownStore(ADMIN_A)['primaryColor'] == '#112233'
    assert len(db.read({}, 'Stores')) == len(stores)
    assert len(db.read({}, 'Policies')) == len(policies)
    assert len(db.read({'storeId': world.a['_id']}, 'Products')) == 1
    assert sorted(storage.objects) == sorted(files.deleted + files.kept)
    assert storage.deleted == []


# ----------------------------- 3. a full reset ----------------------------- #


def test_aFullReset_deletesTheStoresData_andKeepsTheRest(storage, firebase):
    world = seedWorld()
    files = seedFiles(storage, world)
    oldId = world.a['_id']
    docsB = storeDocs(world.b['_id'])
    kept = {collection: db.read({}, collection) for collection in KEPT}
    usageBefore = usageDocs()

    res = api.resetMyStore(ADMIN_A, 'Bakery A')

    assertExactShape(MyStoreResponse, res)
    assert res == {'store': None}
    assert api.getUserStore(ADMIN_A) == {'store': None}
    gone = storeDocs(oldId)
    assert gone['Stores'] == []
    assert all(gone[collection] == [] for collection in STORE_DATA)
    assert storeDocs(world.b['_id']) == docsB
    for collection in KEPT:
        assert db.read({}, collection) == kept[collection]
    usageAfter = usageDocs()
    for collection in USAGE_COLLECTIONS:
        for key, doc in usageBefore[collection].items():
            assert usageAfter[collection][key] == doc
    new = ownStore(ADMIN_A)
    assert new['_id'] != oldId
    assert new.get('name') is None and new.get('slug') is None
    assert new['currency'] == 'PHP'
    assert new['_version'] == 0
    assert db.read({'ownerUserId': ADMIN_A}, 'Stores', countOnly=True) == 1
    assert sorted(storage.objects) == sorted(files.kept)
    assert sorted(storage.deleted) == sorted(files.deleted)


def test_theNewStore_hasAnEmptyPolicy(storage):
    createTestStore(ADMIN_A)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    new = ownStore(ADMIN_A)
    [policy] = db.read({'storeId': new['_id']}, 'Policies')
    assert policy['storeId'] == new['_id']
    assert policy['createdAt'] == new['createdAt']
    assert policy['updatedAt'] == new['createdAt']
    assert policy['_version'] == 0
    answers = set(Policy.model_fields) - {'storeId', 'createdAt', 'updatedAt'}
    assert answers
    assert all(policy.get(field) is None for field in answers)


def test_afterAReset_theSameNameAndSlugCanBeUsedAgain(storage, firebase):
    createTestStore(ADMIN_A, 'Bakery A')
    api.resetMyStore(ADMIN_A, 'Bakery A')
    newId = ownStore(ADMIN_A)['_id']

    created = api.createUserStore(ADMIN_A, 'Bakery A', **STORE_DETAILS)

    assert created['_id'] == newId
    assert created['slug'] == 'bakery-a'
    assert api.getUserStore(ADMIN_A) == {'store': {**created, **BRAND_COLORS}}
    # A set-up store takes its name again, not the word.
    with pytest.raises(ValueError, match="^Type your store's name"):
        api.resetMyStore(ADMIN_A, 'reset')
    assert api.resetMyStore(ADMIN_A, 'Bakery A') == {'store': None}


def test_aResetRightAfterAReset_takesTheWord(storage):
    createTestStore(ADMIN_A, 'Bakery A')
    api.resetMyStore(ADMIN_A, 'Bakery A')
    second = ownStore(ADMIN_A)['_id']

    assert api.resetMyStore(ADMIN_A, 'reset') == {'store': None}
    assert ownStore(ADMIN_A)['_id'] != second


def test_theOldShopLink_isStoreNotFound(storage, firebase):
    createTestStore(ADMIN_A, 'Bakery A')
    api.resetMyStore(ADMIN_A, 'Bakery A')
    firebase['claims'] = guestClaims('rs-new-guest')

    with pytest.raises(ValueError, match='^Store not found$'):
        api.startGuestSession(TOKEN, 'bakery-a')


def test_aStoreNotSetUp_isResetToo_andItsPolicyGoes(storage):
    old = emptyAdmin(ADMIN_A)
    assert db.read({'storeId': old['_id']}, 'Policies', countOnly=True) == 1

    api.resetMyStore(ADMIN_A, 'reset')

    assert db.read({'storeId': old['_id']}, 'Policies', countOnly=True) == 0
    assert db.read({'_id': old['_id']}, 'Stores', countOnly=True) == 0
    assert db.read({}, 'Policies', countOnly=True) == 1


def test_manyConversations_areDeletedInBatches_turnsBeforeTheirConversations(
        monkeypatch, storage, caplog):
    caplog.set_level(logging.INFO)
    store = createTestStore(ADMIN_A)
    other = createTestStore(ADMIN_B, 'Cafe B')
    # A conversation with no turns, and four with one turn each (one with a
    # file); turns written without any attachments key at all.
    conversations = [seedConversation(CUSTOMER, store['_id'], 'customer', 0)]
    for index in range(4):
        conversation = seedConversation(f'rs-guest-{index}', store['_id'],
                                        'customer', 1)
        seedTurn(conversation, 0,
                 [chatFile(ADMIN_A, 'batch.jpg')] if index == 0 else [])
        conversations.append(conversation)
    db.db['ChatTurns'].update_many(
        {'conversationId': conversations[2]['_id']},
        {'$unset': {'attachments': ''}})
    keptConversation = seedConversation(CUSTOMER, other['_id'], 'customer', 1)
    seedTurn(keptConversation, 0)
    storage.put(chatFile(ADMIN_A, 'batch.jpg'))
    monkeypatch.setattr(storeModule, 'RESET_TURNS_BATCH', 2)
    calls = []
    realDeleteMany = db.deleteMany

    def spy(query, collection, session=None):
        if collection in ('ChatTurns', 'Conversations'):
            ids = (query.get('conversationId') or query['_id'])['$in']
            calls.append((collection, tuple(ids)))
        return realDeleteMany(query, collection, session=session)

    monkeypatch.setattr(db, 'deleteMany', spy)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    assert db.read({'storeId': store['_id']}, 'Conversations') == []
    assert db.read({'storeId': store['_id']}, 'ChatTurns') == []
    assert db.read({'storeId': other['_id']}, 'ChatTurns', countOnly=True) == 1
    assert db.read({'_id': keptConversation['_id']}, 'Conversations',
                   countOnly=True) == 1
    assert [len(ids) for collection, ids in calls
            if collection == 'Conversations'] == [2, 2, 1]
    for index, (collection, ids) in enumerate(calls):
        if collection == 'Conversations':
            assert ('ChatTurns', ids) in calls[:index]
    [line] = logRecords(caplog, 'storeReset')
    assert line.msg['deleted']['ChatTurns'] == 4
    assert line.msg['deleted']['Conversations'] == 5
    assert chatFile(ADMIN_A, 'batch.jpg') not in storage.objects


# -------------------------------- 4. the race -------------------------------- #


def test_aWriteToTheOldId_afterTheReset_isNeverSeenFromTheNewStore(storage):
    world = seedWorld()
    oldId = world.a['_id']
    api.resetMyStore(ADMIN_A, 'Bakery A')
    newId = ownStore(ADMIN_A)['_id']

    # What a request that read the old store before the swap writes late.
    product = addProduct(oldId, 'Late Bread', 9)
    addOrder(oldId, CUSTOMER, [{'productId': product['_id'], 'name': 'Late',
                                'price': 9, 'quantity': 1}])
    conversation = seedConversation(CUSTOMER, oldId, 'customer', 1)
    seedTurn(conversation, 0)

    assert api.getUserStore(ADMIN_A) == {'store': None}
    for collection in ('Products', 'Orders', 'Conversations', 'ChatTurns',
                       'Returns', 'GeneratedPhotos'):
        assert db.read({'storeId': newId}, collection) == []
    assert db.read({'_id': oldId}, 'Stores') == []
    assert db.read({'storeId': oldId}, 'Products', countOnly=True) == 1


# ---------------------------- 5. the usage carry-over ---------------------------- #


def test_theCarriedKinds_areThePinnedOnes():
    assert usageCounters.usageKey('store', 'x', TODAY) == 'store:x:2026-10-30'
    assert (VoiceActions.STT, VoiceActions.TTS) == ('stt', 'tts')
    assert ChatActions.STORE_USAGE_KINDS[0] == CHAT_USAGE
    assert set(ChatActions.STORE_USAGE_KINDS[1]) == {'store',
                                                     'attachmentsStore'}
    assert PhotoActions.STORE_USAGE_KINDS == (
        PHOTO_USAGE, ('photos', 'productFinds', 'colorFinds'))
    assert VoiceActions.STORE_USAGE_KINDS[0] == VOICE_USAGE
    assert set(VoiceActions.STORE_USAGE_KINDS[1]) == {'sttStore', 'ttsStore'}
    assert sorted((collection, kind) for collection, kinds in (
        ChatActions.STORE_USAGE_KINDS, PhotoActions.STORE_USAGE_KINDS,
        VoiceActions.STORE_USAGE_KINDS) for kind in kinds) == sorted(
            STORE_KINDS)


@pytest.mark.parametrize('collection,kind', STORE_KINDS)
def test_everyStoreKind_landsOnTheNewIdWithItsCountAndExpiry(
        storage, collection, kind):
    store = createTestStore(ADMIN_A)
    expireAt = datetime.datetime(2099, 7, 8, 9, 10, 11, tzinfo=UTC)
    oldKey = seedUsage(collection, kind, store['_id'], 37, expireAt=expireAt)
    old = usageDoc(collection, oldKey)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    newId = ownStore(ADMIN_A)['_id']
    new = usageDoc(collection,
                   usageCounters.usageKey(kind, newId, TODAY))
    assert new['count'] == 37
    assert new['expireAt'] == old['expireAt'] == expireAt.replace(tzinfo=None) \
        or new['expireAt'] == old['expireAt']
    assert new['_version'] == 0
    assert usageDoc(collection, oldKey) == old


def test_onlyTheStoreCountersOfTodayAreCopied(storage):
    world = seedWorld()
    oldId = world.a['_id']
    yesterday = TODAY - datetime.timedelta(days=1)
    tomorrow = TODAY + datetime.timedelta(days=1)
    for collection, kind in STORE_KINDS:
        seedUsage(collection, kind, oldId, 5, day=yesterday)
        seedUsage(collection, kind, oldId, 6, day=tomorrow)
        # A zero counter copies nothing.
    seedUsage(CHAT_USAGE, 'store', oldId, 0, day=TODAY - datetime.timedelta(
        days=2))
    before = usageDocs()

    api.resetMyStore(ADMIN_A, 'Bakery A')

    newId = ownStore(ADMIN_A)['_id']
    after = usageDocs()
    added = {}
    for collection in USAGE_COLLECTIONS:
        added[collection] = {key for key in after[collection]
                             if key not in before[collection]}
        assert set(before[collection]) <= set(after[collection])
    assert {key for keys in added.values() for key in keys} == {
        usageCounters.usageKey(kind, newId, TODAY) for _, kind in STORE_KINDS}
    # Not the customer's counters, not the all-stores ones.
    for collection in USAGE_COLLECTIONS:
        assert all(newId in key for key in added[collection])


def test_aZeroCounter_isNotCopied(storage):
    store = createTestStore(ADMIN_A)
    seedUsage(CHAT_USAGE, 'store', store['_id'], 0)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    newId = ownStore(ADMIN_A)['_id']
    assert usageDoc(CHAT_USAGE,
                    usageCounters.usageKey('store', newId, TODAY)) is None


@pytest.mark.parametrize('now,copied,notCopied', [
    (datetime.datetime(2026, 10, 30, 15, 59, 59, tzinfo=UTC),
     datetime.date(2026, 10, 30), datetime.date(2026, 10, 31)),
    (datetime.datetime(2026, 10, 30, 16, 0, 0, tzinfo=UTC),
     datetime.date(2026, 10, 31), datetime.date(2026, 10, 30)),
], ids=['lastSecondOfTheAppDay', 'firstSecondOfTheNext'])
def test_theDayIsTheAppDay_utcPlus8(monkeypatch, storage, now, copied,
                                    notCopied):
    store = createTestStore(ADMIN_A)
    seedUsage(PHOTO_USAGE, 'photos', store['_id'], 3, day=copied)
    seedUsage(PHOTO_USAGE, 'photos', store['_id'], 4, day=notCopied)
    monkeypatch.setattr(storeModule, '_now', lambda: now)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    newId = ownStore(ADMIN_A)['_id']
    assert usageDoc(PHOTO_USAGE, usageCounters.usageKey(
        'photos', newId, copied))['count'] == 3
    assert usageDoc(PHOTO_USAGE, usageCounters.usageKey(
        'photos', newId, notCopied)) is None


@pytest.mark.parametrize('newCount,expected', [(2, 7), (9, 9)],
                         ids=['smallerIsRaised', 'largerIsKept'])
def test_max_raisesASmallerCounter_keepsALargerOne(newCount, expected):
    own = datetime.datetime(2098, 1, 2, 3, 4, 5, tzinfo=UTC)
    seedUsage(CHAT_USAGE, 'store', 'oldX', 7)
    newKey = seedUsage(CHAT_USAGE, 'store', 'newX', newCount, expireAt=own)
    db.db[CHAT_USAGE].update_one({'_id': newKey}, {'$set': {'_version': 3}})
    existing = usageDoc(CHAT_USAGE, newKey)

    storeModule._carryOverUsage('oldX', 'newX', TODAY, None)

    after = usageDoc(CHAT_USAGE, newKey)
    assert after['count'] == expected
    assert after['expireAt'] == existing['expireAt']
    assert after['_version'] == 3


def test_aCounterWithoutAnExpiry_getsTheDays(storage):
    # An old counter with no expireAt: no model writes one, so it is inserted.
    oldKey = usageCounters.usageKey('store', 'oldX', TODAY)
    db.db[CHAT_USAGE].insert_one({'_id': oldKey, 'count': 3, '_version': 0})

    storeModule._carryOverUsage('oldX', 'newX', TODAY, None)

    new = usageDoc(CHAT_USAGE, usageCounters.usageKey('store', 'newX', TODAY))
    assert new['count'] == 3
    assert new['expireAt'].replace(tzinfo=None) == usageCounters.usageExpiry(
        TODAY).replace(tzinfo=None)


def test_aFullPhotoCounter_stillRefusesAfterTheReset(monkeypatch, storage):
    store = createTestStore(ADMIN_A)
    seedUsage(PHOTO_USAGE, 'photos', store['_id'],
              PhotoActions.STORE_DAILY_PHOTO_CAP)
    api.resetMyStore(ADMIN_A, 'Bakery A')
    newId = ownStore(ADMIN_A)['_id']
    monkeypatch.setattr(PhotoActions, '_now', lambda: BEFORE)

    with pytest.raises(ValueError, match=re.escape(
            PhotoActions.PHOTO_CAP_REACHED)):
        PhotoActions._takePhotoQuota(newId, 1)

    assert usageDoc(PHOTO_USAGE, usageCounters.usageKey(
        'photos', newId, TODAY))['count'] == PhotoActions.STORE_DAILY_PHOTO_CAP


# ------------------------------- 6. Storage ------------------------------- #


def test_storage_deletesTheStoresAndTheAdminsFiles_andNothingElse(storage):
    world = seedWorld()
    files = seedFiles(storage, world)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    assert sorted(storage.deleted) == sorted(files.deleted)
    assert sorted(storage.objects) == sorted(files.kept)


def test_storage_afterTheMongoDeletes(monkeypatch, storage):
    world = seedWorld()
    seedFiles(storage, world)
    events = storage.events
    realTransaction = db.createTransaction
    realDeleteMany = db.deleteMany
    monkeypatch.setattr(db, 'createTransaction', lambda update: (
        events.append(('mongo', 'swap')), realTransaction(update))[1])
    monkeypatch.setattr(db, 'deleteMany', lambda *args, **kwargs: (
        events.append(('mongo', 'deleteMany')),
        realDeleteMany(*args, **kwargs))[1])

    api.resetMyStore(ADMIN_A, 'Bakery A')

    kinds = [event[:2] for event in events]
    swap = kinds.index(('mongo', 'swap'))
    firstDelete = kinds.index(('mongo', 'deleteMany'))
    firstStorage = min(index for index, event in enumerate(kinds)
                       if event[0] == 'storage')
    lastMongo = max(index for index, event in enumerate(kinds)
                    if event[0] == 'mongo')
    assert swap < firstDelete < firstStorage
    assert lastMongo < firstStorage


def test_aStorageFailure_stillAnswers200_andIsLoggedWithoutPaths(
        storage, caplog):
    caplog.set_level(logging.INFO)
    world = seedWorld()
    seedFiles(storage, world)
    storeFolder = photoStorage.storeFolder(world.a['_id'])
    storage.listFailure = lambda prefix: (
        InternalServerError('secret path ' + prefix)
        if prefix == storeFolder else None)

    res = api.resetMyStore(ADMIN_A, 'Bakery A')

    assert res == {'store': None}
    assert storeDocs(world.a['_id'])['Stores'] == []
    [warning] = logRecords(caplog, 'storeResetStorageFailed')
    assert warning.levelno == logging.WARNING
    assert warning.msg == {'event': 'storeResetStorageFailed',
                           'folder': 'store',
                           'errorClass': 'ServiceUnavailable'}
    [line] = logRecords(caplog, 'storeReset')
    assert line.levelno == logging.INFO
    assert line.msg['storageFailures'] == 1
    # The user and chat steps still ran.
    assert photoStorage.userFolder(ADMIN_A) in storage.listed
    assert chatFile(ADMIN_A, 'a1.jpg') not in storage.objects
    assert f'users/{ADMIN_A}/ref1.jpg' not in storage.objects
    assert f'stores/{world.a["_id"]}/photos/p1.jpg' in storage.objects


def test_aBareException_isCountedWithItsClass(storage, caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN_A)
    storage.listFailure = lambda prefix: Exception('secret ' + prefix)

    assert api.resetMyStore(ADMIN_A, 'Bakery A') == {'store': None}

    warnings = logRecords(caplog, 'storeResetStorageFailed')
    assert [w.msg['errorClass'] for w in warnings] == ['Exception',
                                                       'Exception']
    assert [w.msg['folder'] for w in warnings] == ['store', 'user']
    assert 'secret' not in repr([r.msg for r in caplog.records])


def test_aFileAlreadyGone_isNotCountedNorAnError(storage, caplog):
    caplog.set_level(logging.INFO)
    world = seedWorld()
    seedFiles(storage, world)
    realList = storage.list_blobs

    def listThenLose(prefix, **kwargs):
        blobs = realList(prefix, **kwargs)
        # Another process deletes the first file between list and delete.
        if blobs:
            storage.objects.pop(blobs[0].name, None)
        return blobs

    storage.list_blobs = listThenLose

    assert api.resetMyStore(ADMIN_A, 'Bakery A') == {'store': None}

    [line] = logRecords(caplog, 'storeReset')
    assert line.msg['storageFailures'] == 0
    assert logRecords(caplog, 'storeResetStorageFailed') == []


@pytest.mark.parametrize('withChatFiles,failures', [(False, 2), (True, 3)],
                         ids=['noChatPaths', 'chatPaths'])
def test_theStorageDeadline_countsEachStepThatNeededStorage(
        monkeypatch, storage, caplog, withChatFiles, failures):
    caplog.set_level(logging.INFO)
    store = createTestStore(ADMIN_A)
    if withChatFiles:
        conversation = seedConversation(ADMIN_A, store['_id'], 'admin', 1)
        seedTurn(conversation, 0, [chatFile(ADMIN_A, 'late.jpg')])
        storage.put(chatFile(ADMIN_A, 'late.jpg'))
    monkeypatch.setattr(storeModule, 'RESET_STORAGE_SECONDS', 0)

    assert api.resetMyStore(ADMIN_A, 'Bakery A') == {'store': None}

    warnings = logRecords(caplog, 'storeResetStorageFailed')
    assert len(warnings) == failures
    assert {w.msg['errorClass'] for w in warnings} == {'ServiceUnavailable'}
    [line] = logRecords(caplog, 'storeReset')
    assert line.msg['storageFailures'] == failures
    assert line.msg['files'] == 0


def test_chatFiles_ofTheDeletedStoresTurns_goWhoeverWroteTheTurn(storage):
    world = seedWorld()
    files = seedFiles(storage, world)

    api.resetMyStore(ADMIN_A, 'Bakery A')

    # Written by the customer's turn, in the admin's own chat folder.
    assert chatFile(ADMIN_A, 'viaCustomerTurn.jpg') in files.deleted
    assert chatFile(ADMIN_A, 'viaCustomerTurn.jpg') not in storage.objects
    # Another user's path in the same turns is never deleted.
    assert chatFile(CUSTOMER, 'c1.jpg') in storage.objects


# ------------------------- 7. a failed Mongo delete ------------------------- #


def failOn(monkeypatch, collection: str, nth: int = 1):
    realDeleteMany = db.deleteMany
    seen = []

    def failing(query, name, session=None):
        if name == collection:
            seen.append(name)
            if len(seen) == nth:
                raise RuntimeError(SECRET)
        return realDeleteMany(query, name, session=session)

    monkeypatch.setattr(db, 'deleteMany', failing)


def test_aFailedMongoDelete_stillDeletesFiles_logsAnError_andReRaises(
        monkeypatch, storage, caplog):
    caplog.set_level(logging.INFO)
    world = seedWorld()
    files = seedFiles(storage, world)
    oldId = world.a['_id']
    failOn(monkeypatch, 'Orders')

    with pytest.raises(RuntimeError, match=SECRET):
        api.resetMyStore(ADMIN_A, 'Bakery A')

    # The swap is done: the old store is gone, a new one is there.
    assert db.read({'_id': oldId}, 'Stores') == []
    assert ownStore(ADMIN_A)['_id'] != oldId
    assert sorted(storage.deleted) == sorted(files.deleted)
    [error] = logRecords(caplog, 'storeResetIncomplete')
    assert error.levelno == logging.ERROR
    assert error.msg == {'event': 'storeResetIncomplete',
                         'oldStoreId': oldId,
                         'errorClass': 'RuntimeError',
                         'storageFailures': 0}
    assert logRecords(caplog, 'storeReset') == []
    assert SECRET not in repr([r.msg for r in caplog.records])


def test_aFailureOnTheSecondBatch_keepsTheFilesOfTheFirst(
        monkeypatch, storage):
    store = createTestStore(ADMIN_A)
    monkeypatch.setattr(storeModule, 'RESET_TURNS_BATCH', 1)
    for index in range(3):
        conversation = seedConversation(f'rs-guest-{index}', store['_id'],
                                        'customer', 1)
        name = f'batch{index}.jpg'
        seedTurn(conversation, 0, [chatFile(ADMIN_A, name)])
        storage.put(chatFile(ADMIN_A, name))
    failOn(monkeypatch, 'ChatTurns', nth=2)

    with pytest.raises(RuntimeError):
        api.resetMyStore(ADMIN_A, 'Bakery A')

    remaining = {name for name in storage.objects}
    assert len(remaining) < 3
    assert any(chatFile(ADMIN_A, f'batch{i}.jpg') not in remaining
               for i in range(3))


def test_afterAFailedDelete_aSecondResetWorks_andLeavesTheLeftovers(
        monkeypatch, storage):
    world = seedWorld()
    oldId = world.a['_id']
    failOn(monkeypatch, 'Orders')
    with pytest.raises(RuntimeError):
        api.resetMyStore(ADMIN_A, 'Bakery A')
    monkeypatch.undo()
    monkeypatch.setattr(storeModule, '_now', lambda: BEFORE)
    leftovers = db.read({'storeId': oldId}, 'Orders')
    assert leftovers

    assert api.resetMyStore(ADMIN_A, 'reset') == {'store': None}

    assert db.read({'storeId': oldId}, 'Orders') == leftovers


def test_aFailedStorage_andAFailedMongoDelete_raiseTheMongoError(
        monkeypatch, storage, caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN_A)
    storage.listFailure = lambda prefix: InternalServerError('x')
    failOn(monkeypatch, 'Orders')

    with pytest.raises(RuntimeError, match=SECRET):
        api.resetMyStore(ADMIN_A, 'Bakery A')

    assert len(logRecords(caplog, 'storeResetStorageFailed')) == 2
    [error] = logRecords(caplog, 'storeResetIncomplete')
    assert error.msg['storageFailures'] == 2


def test_aFailedMongoDelete_overHttp_isA500_withTheGenericMessage(
        monkeypatch, storage, caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN_A)
    failOn(monkeypatch, 'Orders')

    status, body = httpPost(monkeypatch, 'resetMyStore', ADMIN_A,
                            {'userId': ADMIN_A, 'confirmName': 'Bakery A'})

    assert status == 500
    assert body['message'].startswith(
        'Something went wrong on our side. Please try again. (error id ')
    assert SECRET not in repr(body)
    assert SECRET not in repr([r.msg for r in caplog.records])


# --------------------- 8. deletePrefix and the folder helpers --------------------- #


def test_deletePrefix_acceptsOnlyTheTwoFolderBuilders(storage):
    storage.put('stores/x/photos/a.jpg')
    storage.put('users/x/ref.jpg')
    deadline = 10**12

    assert photoStorage.storeFolder('x') == 'stores/x/'
    assert photoStorage.userFolder('x') == 'users/x/'
    assert photoStorage.deletePrefix(photoStorage.storeFolder('x'),
                                     deadline) == 1
    assert photoStorage.deletePrefix(photoStorage.userFolder('x'),
                                     deadline) == 1


@pytest.mark.parametrize('prefix', [
    'stores/', 'users/', 'stores//', 'users//', 'stores/x', 'users/x',
    'stores/x/photos/', 'users/x/chat/', 'users/x/y/', 'other/x/',
    'stores/./', 'stores/../', 'users/../', 'users/./', '', None, 5,
    'stores/x\\y/', 'stores/x\ny/', 'stores/x\x00/', '/stores/x/',
    'Stores/x/', 'stores/x/ ', ' stores/x/',
])
def test_deletePrefix_refusesAnythingButOneFolder_withoutTouchingStorage(
        monkeypatch, prefix):
    def touched():
        raise AssertionError('Storage must not be reached')

    monkeypatch.setattr(photoStorage, '_bucket', touched)

    with pytest.raises(ValueError, match='^Not a folder Storage may delete$'):
        photoStorage.deletePrefix(prefix, 10**12)


def test_storePhotoPath_isBuiltOnTheStoreFolder():
    path = photoStorage.storePhotoPath('x', 'p')

    assert path.startswith(photoStorage.storeFolder('x'))
    assert path == 'stores/x/photos/p.jpg'
    assert (photoStorage.FOLDER_PREFIX_PATTERN.fullmatch(
        photoStorage.storeFolder('x')))
    assert photoStorage.STORE_FOLDER == 'stores'
    assert photoStorage.USER_FOLDER == 'users'


def test_deletePrefix_ofAUserFolder_keepsTheChatSubfolder(storage):
    for name in ('users/u/chat/a.jpg', 'users/u/chat/n/b.pdf',
                 'users/u/chatter.jpg', 'users/u/chat.jpg', 'users/u/r.png',
                 'users/u/n/r2.png'):
        storage.put(name)

    deleted = photoStorage.deletePrefix(photoStorage.userFolder('u'), 10**12)

    assert deleted == 4
    assert sorted(storage.objects) == ['users/u/chat/a.jpg',
                                       'users/u/chat/n/b.pdf']


def test_deleteOwnChatFiles_deletesOnlyTheUsersChatFiles(storage):
    for name in ('users/u/chat/a.jpg', 'users/u/chat/b.png',
                 'users/u/other.jpg', 'users/v/chat/c.jpg',
                 'users/u/chat/p.heic'):
        storage.put(name)
    paths = ['users/u/chat/a.jpg', 'users/u/chat/a.jpg', 'users/u/other.jpg',
             'users/v/chat/c.jpg', 'chat/a.jpg', 'users/u/chat/../other.jpg',
             'users/u/chat/p.heic', 'users/u/chat/missing.jpg']

    deleted = photoStorage.deleteOwnChatFiles('u', paths, 10**12)

    assert deleted == 1
    assert sorted(storage.deleted) == ['users/u/chat/a.jpg']
    assert sorted(storage.objects) == ['users/u/chat/b.png',
                                       'users/u/chat/p.heic',
                                       'users/u/other.jpg',
                                       'users/v/chat/c.jpg']


def test_deleteOwnChatFiles_withNothingToDelete_neverBuildsTheClient(
        monkeypatch):
    def touched():
        raise AssertionError('the bucket must not be built')

    monkeypatch.setattr(photoStorage, '_bucket', touched)

    assert photoStorage.deleteOwnChatFiles('u', [], 10**12) == 0
    assert photoStorage.deleteOwnChatFiles(
        'u', ['users/v/chat/c.jpg'], 10**12) == 0


# --------------------------------- 9. logs --------------------------------- #


def test_theLogs_carryIdsAndCountsOnly(storage, caplog):
    caplog.set_level(logging.DEBUG)
    world = seedWorld()
    seedFiles(storage, world)
    caplog.clear()

    api.resetMyStore(ADMIN_A, 'Bakery A')

    [line] = logRecords(caplog, 'storeReset')
    assert line.levelno == logging.INFO
    assert set(line.msg) == {'event', 'oldStoreId', 'newStoreId', 'deleted',
                             'files', 'storageFailures'}
    assert line.msg['oldStoreId'] == world.a['_id']
    assert line.msg['newStoreId'] == ownStore(ADMIN_A)['_id']
    assert set(line.msg['deleted']) == DELETED_COLLECTIONS
    assert line.msg['deleted']['Products'] == 1
    assert line.msg['deleted']['Orders'] == 1
    assert line.msg['deleted']['OrderLocks'] == 1
    assert line.msg['deleted']['Returns'] == 1
    assert line.msg['deleted']['Policies'] == 1
    assert line.msg['deleted']['GeneratedPhotos'] == 1
    assert line.msg['deleted']['Conversations'] == 2
    assert line.msg['files'] == 10
    assert line.msg['storageFailures'] == 0
    everythingLogged = ' '.join(f'{r.getMessage()} {r.msg}'
                                for r in caplog.records
                                if not r.name.startswith('pymongo'))
    conversations = db.read({}, 'Conversations')
    forbidden = ['Bakery A', 'bakery-a', 'users/', 'stores/', CUSTOMER,
                 'a title', 'question', 'answer', '.jpg'] + [
                     c['_id'] for c in conversations]
    for text in forbidden:
        assert text not in everythingLogged


def test_aResetWithNoData_logsZeroCounts(storage, caplog):
    caplog.set_level(logging.INFO)
    old = emptyAdmin(ADMIN_A)
    db.delete({'storeId': old['_id']}, 'Policies')

    api.resetMyStore(ADMIN_A, 'reset')

    [line] = logRecords(caplog, 'storeReset')
    assert line.msg['deleted'] == {name: 0 for name in DELETED_COLLECTIONS}
    assert line.msg['files'] == 0 and line.msg['storageFailures'] == 0


def test_aRefusal_overHttp_logsNeitherNameNorTypedText(
        monkeypatch, storage, caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN_A, 'Bakery A')

    status, body = httpPost(monkeypatch, 'resetMyStore', ADMIN_A,
                            {'userId': ADMIN_A,
                             'confirmName': 'Typed Wrong Name'})

    assert status == 400
    assert body['message'] == "Type your store's name to confirm"
    [record] = [r for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('function') == 'resetMyStore']
    assert record.msg['requestKeys'] == ['confirmName', 'userId']
    logged = ' '.join(f'{r.getMessage()} {r.msg}' for r in caplog.records)
    assert 'Typed Wrong Name' not in logged
    assert 'Bakery A' not in logged


def test_aSuccess_overHttp_logsOnlyTheRequestKeys(monkeypatch, storage,
                                                  caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN_A, 'Bakery A')

    status, body = httpPost(monkeypatch, 'resetMyStore', ADMIN_A,
                            {'userId': ADMIN_A, 'confirmName': 'Bakery A'})

    assert status == 200
    assert body['message'] == 'Store reset'
    assert body['data'] == {'store': None}
    [record] = [r for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('function') == 'resetMyStore']
    assert record.msg['requestKeys'] == ['confirmName', 'userId']
    logged = ' '.join(f'{r.getMessage()} {r.msg}' for r in caplog.records)
    assert 'Bakery A' not in logged


# ----------------------- 10. not a tool, and sign-up ----------------------- #


def test_resetMyStore_isNoChatToolAndNoMcpTool():
    chatNames = {tool['function']['name'] for tools in chatTools.TOOLS.values()
                 for tool in tools}
    assert 'resetMyStore' not in chatNames
    assert not hasattr(ApiRequests.resetMyStore, 'mcpAudiences')

    async def mcpNames():
        names = set()
        for server in mcpServer.servers.values():
            names |= {t.name for t in await server.list_tools()}
        return names

    assert 'resetMyStore' not in asyncio.run(mcpNames())


def test_signUp_createsTheSameStoreAndPolicy_asTheSharedBuilder(
        anotherUser):
    user = AuthHandler.signInIdentity('rs-new-admin', 'rs-new@test.com',
                                      'Rs New')

    store = ownStore('rs-new-admin')
    assert store['currency'] == 'PHP'
    assert store['location'] is None
    assert store['_version'] == 0
    assert store.get('name') is None and store.get('slug') is None
    assert store['createdAt'] == db.read({'_id': 'rs-new-admin'}, 'Users',
                                         findOne=True)['createdAt']
    [policy] = db.read({'storeId': store['_id']}, 'Policies')
    expected = storeModule.emptyPolicy(store['_id'], store['createdAt'])
    expected.pop('_id')
    assert {key: value for key, value in policy.items()
            if key != '_id'} == expected
    # createEmptyStore (the builder resetMyStore uses) gives the same shape.
    built = storeModule.createEmptyStore('rs-built-admin', BEFORE, None)
    assert set(built) == set(store)
    [builtPolicy] = db.read({'storeId': built['_id']}, 'Policies')
    assert set(builtPolicy) == set(policy)
    assert user['_id'] == 'rs-new-admin'
    wipeCollections(['Stores', 'Policies'])
