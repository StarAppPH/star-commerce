import copy
import datetime
import os

# --- No cloud database, ever ------------------------------------------------
# This block runs before ANY project import: the mongoDb singletons (db, and
# pubSubMockDb at its first operation) connect when mongoDb is first imported,
# and a mongoDb() built under clouddev or cloudprod connects to that
# environment's cluster (MONGO_URI, when the shell has it set; unset, mongoDb
# raises). So the suite refuses to
# start outside localdev, the cloud database variables are unset, and
# pymongo's MongoClient is replaced by a subclass that refuses every
# non-local host - before mongoDb's `from pymongo import MongoClient` binds
# the name. A test that switches ENVIRONMENT to a cloud value (the warning
# paths, the sign-in refusals) and builds a client then fails instead of
# connecting.
if os.environ.get('ENVIRONMENT') != 'localdev':
    import pytest
    raise pytest.UsageError('the tests run with ENVIRONMENT=localdev only')

# --- One database per xdist worker -------------------------------------------
# Shared across the fleet's conftest.py files (hand-synced, not enforced by
# test_sharedFiles.py yet). Under `pytest -n`, every worker wipes and seeds
# the database it runs against, so each one gets its own: the worker id goes
# in front of the 'Scratch' suffix (test<project>Scratch becomes
# test<project>_gw0Scratch, _gw1Scratch...), so a worker's database still
# passes the scratch guard below unchanged. Renamed here, before that guard
# and before anything imports mongoDb (its singletons pick the database when
# first imported). A subprocess a test starts inherits both variables, so the
# id goes in once. A name without the suffix is left for the guard to refuse.
# The controller process has no PYTEST_XDIST_WORKER and keeps the name.
_XDIST_WORKER = os.environ.get('PYTEST_XDIST_WORKER', '')
_databaseName = os.environ.get('databaseName', '')
if (_XDIST_WORKER and _databaseName.endswith('Scratch')
        and not _databaseName.endswith(f'_{_XDIST_WORKER}Scratch')):
    os.environ['databaseName'] = (
        f"{_databaseName[:-len('Scratch')]}_{_XDIST_WORKER}Scratch")

# The suite wipes the database it runs against, and the default localdev
# database (teststarCommerce) is where dumpProdDataToLocalDev.cmd restores
# the prod dump. So it only runs against a scratch database: databaseName must
# end with 'Scratch'.
if not os.environ.get('databaseName', '').endswith('Scratch'):
    import pytest
    raise pytest.UsageError(
        'the tests wipe their database: set databaseName to a scratch '
        'database, e.g. databaseName=teststarCommerceScratch')

# MONGO_URI is the cloudprod (and clouddev) cluster (mongoDb.py); MONGO_DUMP_URI
# is the read-only connection string tools/dumpProdDataToLocalDev.cmd dumps the
# prod database with. The MONGO_PASSWORD pair is scrub-only: no template file
# sets or reads it, but older fleet dump scripts still set it. Add any other cluster URI a
# shell of yours may carry (another service's MONGO_URI_*).
_CLOUD_DB_ENV_VARS = ('MONGO_URI', 'MONGO_DUMP_URI', 'MONGO_PASSWORD',
                      'MONGO_PASSWORD_ENCODED')
for _name in _CLOUD_DB_ENV_VARS:
    os.environ.pop(_name, None)

# The variables a sibling test server starts with (testServices.py) are the
# shell's: recorded by name here, before AppConfig loads this repo's
# server/.env into the environment, so a sibling reads its own .env, not
# ours. An xdist worker inherits the controller's list, and so does a pytest
# run a test starts (its environment holds the outer run's .env already).
if not _XDIST_WORKER and not os.environ.get('STAR_TEST_SERVICES_AUTOSTART'):
    import json as _json
    os.environ['STAR_TEST_SHELL_ENV_KEYS'] = _json.dumps(sorted(os.environ))

import pymongo
import pymongo.mongo_client

try:  # pymongo >= 4.9 splits the clients into these modules
    import pymongo.synchronous.mongo_client as _syncMongoClientModule
except ImportError:
    _syncMongoClientModule = None

_LOCAL_DB_HOSTS = ('localhost', '127.0.0.1')


def _isLocalDbHost(host) -> bool:
    if host is None:
        return True  # pymongo's default: localhost:27017
    hosts = [host] if isinstance(host, str) else list(host)
    for entry in hosts:
        if not isinstance(entry, str):
            return False
        address = entry
        if address.startswith('mongodb://'):
            address = address[len('mongodb://'):]
        elif '://' in address:
            # mongodb+srv:// and anything else: never local
            return False
        # Credentials end at the LAST '@' before the path: an '@' in the
        # path or query ('...example.com/?appName=x@localhost') is not one.
        address = address.split('/', 1)[0].split('?', 1)[0].rsplit('@', 1)[-1]
        for node in address.split(','):
            if node.rsplit(':', 1)[0] not in _LOCAL_DB_HOSTS:
                return False
    return True


class _LocalOnlyMongoClient(pymongo.mongo_client.MongoClient):
    """A MongoClient that refuses every non-local host. A subclass, so
    isinstance checks against pymongo's MongoClient still hold."""

    def __init__(self, host=None, *args, **kwargs):
        if not _isLocalDbHost(host):
            # The host is not quoted: a URI carries its credentials.
            raise AssertionError(
                'a test tried to open a MongoDB client to a non-local host')
        super().__init__(host, *args, **kwargs)


# Every name a MongoClient can be imported by. The async client gets the same
# refusal: nothing here uses it, so nothing may open one to a cloud host.
_CLIENT_MODULES = [pymongo, pymongo.mongo_client]
if _syncMongoClientModule is not None:
    _CLIENT_MODULES.append(_syncMongoClientModule)
for _module in _CLIENT_MODULES:
    _module.MongoClient = _LocalOnlyMongoClient

if hasattr(pymongo, 'AsyncMongoClient'):

    class _LocalOnlyAsyncMongoClient(pymongo.AsyncMongoClient):
        """The async MongoClient, refusing every non-local host."""

        def __init__(self, host=None, *args, **kwargs):
            if not _isLocalDbHost(host):
                raise AssertionError(
                    'a test tried to open a MongoDB client to a non-local host')
            super().__init__(host, *args, **kwargs)

    pymongo.AsyncMongoClient = _LocalOnlyAsyncMongoClient

import pytest
from pydantic import BaseModel, RootModel
from AppConfig import AppConfig

# AppConfig loads server/.env (localdev) on import: pop again, before mongoDb
# builds its singletons, should the .env hold a cloud database variable.
for _name in _CLOUD_DB_ENV_VARS:
    os.environ.pop(_name, None)

from mongoDb import db
from objects import UserView, validate_route_response

# --- No cloud database, ever (the session half) ------------------------------
# The import-time guard above already holds; the session fixture re-asserts it
# for anything that imported pymongo's MongoClient another way, and keeps the
# cloud variables unset should a test (or a .env load) have set one.


@pytest.fixture(autouse=True, scope='session')
def _neverConnectToACloudDatabase():
    import mongoDb as mongoDbModule

    with pytest.MonkeyPatch.context() as patch:
        for name in _CLOUD_DB_ENV_VARS:
            patch.delenv(name, raising=False)
        patch.setattr(mongoDbModule, 'MongoClient', _LocalOnlyMongoClient)
        for module in _CLIENT_MODULES:
            patch.setattr(module, 'MongoClient', _LocalOnlyMongoClient)
        yield


def pytest_configure(config) -> None:
    if AppConfig().getIsCloudEnvironment():
        pytest.exit('A cloud environment is set. Skipping tests.')
    if _ramRefusal:  # the RAM gate, below
        import testRamGate
        pytest.exit(_ramRefusal[0], returncode=testRamGate.REFUSED_EXIT_CODE)


# --- Parallel workers (pytest-xdist), the RAM gate and the test servers -----
# Shared across the fleet's conftest.py files (hand-synced). The RAM numbers
# and the decision live in testRamGate.py, the sibling servers in
# testServices.py (their docstrings have the formulas and the knobs). In the
# controller process only (an xdist worker has PYTEST_XDIST_WORKER set), and
# not for --help, --version, --fixtures, --markers or --collect-only:
# - the session's test-server variables are set (workers inherit them), the
#   run's Job Object is created and recorded test servers whose run is gone
#   are killed (the orphan sweep);
# - `-n auto` gets as many workers as the available memory holds, and a run
#   whose workers do not fit is refused before any test starts, naming the
#   other test processes that hold the memory; `-n <number>` runs as given or
#   not at all;
# - when the run ends, every sibling server a test of this run started (on
#   its first call to that sibling, through services/__request.py) is killed.
#   A server that was already up is used and left running.
# Under -n the tests go out a file at a time (--dist loadfile) unless --dist
# is passed: a test module's fixtures and seeded data stay in one worker.
_sessionLines = []
_ramRefusal = []

# >>> star-app per-repo: test servers
# A sibling's test database is test<its projectName>For<ours>Scratch unless
# named here (by services/ file name).
TEST_SERVICE_DATABASES = {}
# What one worker of this suite takes (testRamGate.py's docstring: how it is
# measured); PYTEST_WORKER_GB overrides it.
TEST_WORKER_GB = 0.75
# <<< star-app per-repo: test servers


def _isWorker() -> bool:
    return bool(_XDIST_WORKER)


def _runsTests(config) -> bool:
    """False for --help, --version, --fixtures, --markers, --collect-only."""
    option = config.option
    return not any(getattr(option, name, False) for name in (
        'help', 'version', 'showfixtures', 'show_fixtures_per_test', 'markers',
        'collectonly'))


def _startTestServerSession() -> None:
    import json

    import testServices

    os.environ[testServices.OWNER_ENV] = str(os.getpid())
    os.environ[testServices.DATABASES_ENV] = json.dumps(TEST_SERVICE_DATABASES)
    if not testServices.autostart():
        os.environ[testServices.AUTOSTART_ENV] = '0'
        return
    os.environ[testServices.AUTOSTART_ENV] = '1'
    _sessionLines.extend(testServices.startSession())


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config) -> None:
    # Runs before xdist's own (also tryfirst; a conftest registers later),
    # which resolves -n auto and turns --dist's default 'no' into 'load'.
    import testRamGate

    option = config.option
    numprocesses = getattr(option, 'numprocesses', None)
    if numprocesses and option.dist == 'no':
        option.dist = 'loadfile'
    if _isWorker() or not _runsTests(config):
        return
    _startTestServerSession()
    if config.getoption('usepdb'):
        return  # one process, no workers to size
    auto = numprocesses in ('auto', 'logical')
    try:
        decision, availableGB, ramSettings = testRamGate.gate(
            None if auto else (numprocesses or 0), os.cpu_count() or 1,
            TEST_WORKER_GB)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    if auto:
        option.numprocesses = decision.workers
    if not decision.run:
        # Refused in pytest_configure: pytest.exit is only handled there.
        _ramRefusal.append(testRamGate.refusal(
            availableGB, decision, testRamGate.otherTestProcesses()))
        return
    if auto:
        _sessionLines.append(
            f'xdist: {decision.workers} workers (available {availableGB:.1f} GB, '
            f'{ramSettings.perWorkerGB:g} GB/worker, '
            f'reserve {ramSettings.reserveGB:g} GB)')


def pytest_sessionstart(session) -> None:
    # Written even under -q, which hides the report header.
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    if reporter is not None:
        for line in _sessionLines:
            reporter.write_line(line)


def pytest_terminal_summary(terminalreporter) -> None:
    if _isWorker() or not _runsTests(terminalreporter.config):
        return
    import testServices
    for line in testServices.summaryLines(testServices.ownedServers()):
        terminalreporter.write_line(line)


def pytest_unconfigure(config) -> None:
    if not _isWorker() and _runsTests(config):
        import testServices
        testServices.stopSession()


# --- Published messages ------------------------------------------------------
# Every payload a publisher hands to pubSub.publishMessage, recorded in memory
# so a test can pin it (lastPublishedPayload) without writing the shared
# pubSubMockDb, which only pubSubMockDataGenerator.py may write. The real
# publishMessage still runs: under pytest it neither publishes nor captures.

_publishedMessages = []


@pytest.fixture(autouse=True)
def _recordPublishedMessages(monkeypatch):
    import pubSub as pubSubModule

    original = pubSubModule.pubSub.publishMessage

    def recordingPublish(message, *args, **kwargs):
        _publishedMessages.append({
            'topicName': kwargs.get('topicName'),
            'message': copy.deepcopy(message),
        })
        return original(message, *args, **kwargs)

    _publishedMessages.clear()
    monkeypatch.setattr(pubSubModule.pubSub, 'publishMessage',
                        recordingPublish)
    yield


def lastPublishedPayload(functionName):
    """The message the last publish of `functionName` (a publisher route or
    a module-level publisher) sent in this test."""
    payloads = publishedPayloads(functionName)
    assert payloads, f'{functionName} published nothing'
    return payloads[-1]


def publishedPayloads(functionName) -> list:
    """Every message `functionName` published in this test, oldest first
    (empty when it published nothing). `functionName + '_'` keeps a
    publisher whose name only starts the same out."""
    return [
        row['message'] for row in _publishedMessages
        if (row['topicName'] or '').startswith(functionName + '_')
    ]


# --- AppCreator route metadata ----------------------------------------------
# The dict AppCreator._route_meta builds from a decorated route, with every
# key set, for tests that feed the generators a route without decorating one.
# The default meta is a typed route; pass responseModel=None for a
# PubSubRequests handler (or a route AppCreator must refuse).


def routeMeta(**overrides) -> dict:
    meta = {
        'httpMethod': 'POST',
        'jwtRequired': False,
        'createAccessToken': False,
        'serviceOnly': False,
        'successMessage': 'Done',
        'responseModel': UserView,
    }
    meta.update(overrides)
    return meta


def apiRoutes() -> dict:
    """Every ApiRequests route, as AppCreator reads them."""
    from AppCreator import AppCreator
    return AppCreator(apiRequests=None)._get_user_defined_methods('app')


# --- Route response check ----------------------------------------------------
# route_config checks every response against its responseModel in localdev
# only, where tests run, and RAISES on a mismatch; every other environment
# skips the check. responseCheckOff turns it off for a test whose route
# deliberately returns what no model describes (corrupt data a fail-open path
# passes through, or a mocked upstream shape the test pins): the response is
# returned unchanged, as everywhere but localdev.


class _NotLocalDev:

    def getisLocalDevEnvironment(self) -> bool:
        return False


@pytest.fixture
def responseCheckOff(monkeypatch):
    import route_config
    monkeypatch.setattr(route_config, 'Environment', _NotLocalDev)


# --- Response model helpers --------------------------------------------------
# The route response tests validate a response against its model and pin its
# exact keys: validation rejects a key the model does not declare, but not a
# declared optional key the route stopped sending.


def modelKeys(model) -> set:
    """Every key `model` declares, by alias, plus its computed fields: exactly
    the keys of a response that sends every field."""
    return ({field.alias or name
             for name, field in model.model_fields.items()}
            | set(model.model_computed_fields))


def hiddenModelKeys(model) -> set:
    """The fields `model` accepts but keeps out of the generated TS type
    (SkipJsonSchema)."""
    return modelKeys(model) - set(
        model.model_json_schema(mode='serialization')['properties'])


def assertMatchesModel(model, res) -> None:
    """Validate a route's response, or a published payload, against `model`
    exactly as route_config's check does (validate_route_response). A deep
    copy is validated: should a validator ever mutate its input (the bug the
    check's localdev guard exists for), it still could not rewrite the
    response the test goes on to assert on."""
    validate_route_response(model, copy.deepcopy(res))


def assertExactShape(model, res) -> None:
    """`res` validates against `model` and carries exactly its keys."""
    assertMatchesModel(model, res)
    assert set(res) == modelKeys(model)


def assertRowKeys(rows, model) -> None:
    """Every row carries exactly `model`'s keys."""
    for row in rows:
        assert set(row) == modelKeys(model)


def dumpedKeys(model, doc) -> set:
    """The keys of `doc` read as `model` and dumped back, by alias: every
    field of the model, set or defaulted. Compares a stored document with
    what a full dump of its model would carry."""
    return set(
        model.model_validate(copy.deepcopy(doc)).model_dump(by_alias=True))


# --- Fake documents ----------------------------------------------------------
# A unit test that patches a db module returns documents from its fake
# (mockDb.read.return_value = ...). A hand-written dict there drifts from the
# model: when a field is renamed or removed, the fake keeps the old shape and
# the test passes against data the real database never returns. So every
# fake document is built here, through its model. test_mock_drift.py flags a
# raw dict fake; a deliberately raw one (a legacy shape) carries
# `# raw-doc: <reason>`.


def _declaredKeys(model) -> set:
    """The keys `model` reads from a dict: each field's alias (its name when
    it has none), and every field name too where the model populates by name.
    An exclude=True field is still declared."""
    keys = modelKeys(model)
    if model.model_config.get('populate_by_name'):
        keys |= set(model.model_fields)
    return keys


def _unknownKeys(value, raw, path: str) -> list[str]:
    """The paths (`bir2303.tin`, `roles[0].x`) of the keys in `raw` that the
    model validated from it does not declare, walking nested models and lists
    of them alongside the input. Free-form dict fields are not walked."""
    if isinstance(value, RootModel):
        return _unknownKeys(value.root, raw, path)
    if isinstance(value, list) and isinstance(raw, list) and len(value) == len(raw):
        return [
            key for index, (item, rawItem) in enumerate(zip(value, raw))
            for key in _unknownKeys(item, rawItem, f'{path}[{index}]')
        ]
    if not (isinstance(value, BaseModel) and isinstance(raw, dict)):
        return []
    model = type(value)
    prefix = f'{path}.' if path else ''
    unknown = [f'{prefix}{key}' for key in raw if key not in _declaredKeys(model)]
    for name, field in model.model_fields.items():
        for key in {field.alias or name, name}:
            if key in raw:
                unknown += _unknownKeys(getattr(value, name), raw[key],
                                        f'{prefix}{key}')
    return unknown


def fakeDoc(model, **fields) -> dict:
    """A stored `model` document: `fields` validated through the model, dumped
    by alias (_id, _version), defaults included.

    A key the model does not declare raises, at any depth (nested models and
    lists of them): the models ignore extra keys, so validation alone would
    let a renamed or removed field through. A value the model rewrites
    ('' -> None, a stripped string) arrives rewritten: to feed a raw value,
    set it on the returned dict."""
    instance = model.model_validate(fields)
    unknown = _unknownKeys(instance, fields, '')
    if unknown:
        raise AssertionError(
            f'fakeDoc({model.__name__}): {unknown} not declared by the model. '
            'Rename or drop them to match it.')
    return instance.model_dump(by_alias=True)


# --- Route test helpers --------------------------------------------------------


def wipeCollections(collections) -> None:
    for collection in collections:
        db.delete({}, collection)


def utcNow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def appToday() -> datetime.date:
    """Today in the app timezone (AppConfig.Timezone), as a date."""
    from AppConfig import Timezone
    return datetime.datetime.now(Timezone().getTimezone()).date()


def createTestUser(userId, userType='superAdmin', name='testUserName') -> dict:
    """A Users document with `userType`'s roles (superAdmin: every role),
    stored and returned."""
    from objects import User
    from roles import AllRoles, UserRoles
    user = User(_id=userId,
                name=name,
                email=f'{userId}@test.com',
                createdAt=utcNow(),
                roles=UserRoles(AllRoles().getAllRoles()),
                _version=0)
    user.setRole(userType)
    return db.create(user.model_dump(by_alias=True), 'Users')


# --- Sign-in -------------------------------------------------------------------
# Firebase ID token verification without the network, and the users a sign-in
# test starts from. Used by test_auth_surface.py and
# test_route_responses_http.py.

TOKEN = 'aa-firebase-id-token'
TOKEN_UID = 'aa-token-uid'
TOKEN_EMAIL = 'aa-token-user@test.com'
RAW_UID = 'aa-raw-uid'
RAW_EMAIL = 'aa-raw-email-target@test.com'
OTHER_UID = 'aa-other-user'

# What a sign-in test wipes: the users, and the one-time superAdmin claim
# (AuthHandler.BOOTSTRAP_COLLECTION), which outlives an emptied Users.
SIGN_IN_COLLECTIONS = ['Users', 'Bootstrap']


def firebaseClaims(**overrides) -> dict:
    """What google-auth's verify_firebase_token decodes a valid token to."""
    from AuthHandler import DEFAULT_FIREBASE_PROJECT_ID
    claims = {
        'iss': f'https://securetoken.google.com/{DEFAULT_FIREBASE_PROJECT_ID}',
        'aud': DEFAULT_FIREBASE_PROJECT_ID,
        'sub': TOKEN_UID,
        'user_id': TOKEN_UID,
        'email': TOKEN_EMAIL,
        'email_verified': True,
        'name': 'Aa Token User',
        'firebase': {
            'sign_in_provider': 'google.com'
        },
    }
    claims.update(overrides)
    return claims


def guestClaims(uid: str, provider: str = 'anonymous',
                phoneNumber: str | None = None) -> dict:
    """A Firebase anonymous (or phone) sign-in's claims: no email."""
    claims = firebaseClaims(sub=uid,
                            user_id=uid,
                            firebase={'sign_in_provider': provider})
    for key in ('email', 'email_verified', 'name'):
        claims.pop(key)
    if phoneNumber is not None:
        claims['phone_number'] = phoneNumber
    return claims


@pytest.fixture
def firebase(monkeypatch):
    """Firebase verification without the network: TOKEN decodes to the
    claims the test sets (`firebase['claims']`), anything else fails to
    verify; `firebase['verified']` lists every (token, project) checked."""
    import AuthHandler as AuthHandlerModule

    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    # Not in Auth emulator mode, whatever the shell running pytest has set.
    monkeypatch.delenv('FIREBASE_AUTH_EMULATOR_HOST', raising=False)
    state = {'claims': firebaseClaims(), 'verified': []}

    def decode(idToken, projectId):
        state['verified'].append((idToken, projectId))
        if idToken != TOKEN:
            raise ValueError(f'Could not verify token {idToken}')
        return state['claims']

    monkeypatch.setattr(AuthHandlerModule, '_decodeFirebaseIdToken', decode)
    return state


@pytest.fixture
def anotherUser(monkeypatch):
    """Users is not empty, so a new sign-in gets no role."""
    wipeCollections(SIGN_IN_COLLECTIONS)
    createTestUser(OTHER_UID, userType='unauthorized', name='Aa Other')
    yield
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    wipeCollections(SIGN_IN_COLLECTIONS)


@pytest.fixture
def emptyUsers(monkeypatch):
    """An empty Users collection, with the superAdmin claim still open."""
    wipeCollections(SIGN_IN_COLLECTIONS)
    yield
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    wipeCollections(SIGN_IN_COLLECTIONS)


# --- Stores and products -------------------------------------------------------
# An admin's store and its products, made through the routes, for
# test_stores_and_customers.py, test_products.py and test_mcp_server.py.

# The details createUserStore requires besides the name.
STORE_DETAILS = {
    'description': 'Fresh bread and pastries',
    'contactNumber': '+639171234567',
    'address': '12 Rizal St, Quezon City',
}


def createTestStore(adminId: str, name: str = 'Bakery A',
                    currency: str = 'PHP') -> dict:
    """An admin (createTestUser) and its store, as createUserStore sends
    it."""
    from ApiRequests import ApiRequests
    createTestUser(adminId)
    return ApiRequests().createUserStore(adminId, name, **STORE_DETAILS,
                                         currency=currency)


def addProduct(storeId: str, name: str, price: float, soldCount: int = 0,
               isAvailable: bool = True, description: str | None = None,
               inventory: int = 10, *,
               reservedInventory: list[dict] | None = None):
    """A Products document of the store, stored and returned.
    `reservedInventory` is a list of {'orderId', 'quantity'} dicts."""
    from objects import Product, productNameKey
    product = Product(storeId=storeId,
                      name=name,
                      nameKey=productNameKey(name),
                      inventory=inventory,
                      reservedInventory=reservedInventory or [],
                      description=description,
                      price=price,
                      soldCount=soldCount,
                      isAvailable=isAvailable,
                      createdAt=datetime.datetime.now(
                          tz=datetime.timezone.utc),
                      _version=0)
    return db.create(product.model_dump(by_alias=True), 'Products')


def addOrder(storeId: str, customerId: str, items: list[dict],
             status: str = 'pending',
             createdAt: datetime.datetime | None = None, *,
             currency: str = 'PHP',
             contactNumber: str = '+639171234567',
             **fields):
    """An Orders document, stored and returned. `items` is a list of
    {'productId', 'name', 'price', 'quantity'} dicts; the total is summed
    from them. createdAt defaults to now (UTC); a naive one is refused, so
    a test cannot seed a local time 8 hours off. The total and expiresAt
    are createOrder's (orderTotal, orderExpiresAt). `fields` sets any other
    Order field (paymentMethod, paidAt, ...)."""
    from actions.OrderActions import orderExpiresAt, orderTotal
    from objects import Order
    if createdAt is not None and createdAt.utcoffset() is None:
        raise ValueError('addOrder needs a timezone-aware createdAt')
    createdAt = createdAt or datetime.datetime.now(tz=datetime.timezone.utc)
    order = Order(storeId=storeId,
                  customerId=customerId,
                  items=items,
                  total=orderTotal(items),
                  currency=currency,
                  contactNumber=contactNumber,
                  status=status,
                  createdAt=createdAt,
                  expiresAt=orderExpiresAt(createdAt),
                  _version=0,
                  **fields)
    return db.create(order.model_dump(by_alias=True), 'Orders')


@pytest.fixture
def noExpirySweep(monkeypatch):
    """The order routes without their expiry sweep (cancelExpiredOrders),
    for tests that seed pending orders placed long ago (or before a frozen
    clock) to test the list filters: the sweep would cancel them first.
    test_checkout.py tests the sweep."""
    import actions.OrderActions as orderActions
    monkeypatch.setattr(orderActions, 'cancelExpiredOrders',
                        lambda storeId=None: 0)


def orderIds(res: dict) -> list[str]:
    """The _ids of an order list response, in the order sent."""
    return [order['_id'] for order in res['orders']]


def appTime(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
            second: int = 0, millisecond: int = 0) -> datetime.datetime:
    """An instant on the app's wall clock (AppConfig.Timezone, UTC+8), timezone
    aware, to millisecond precision (what MongoDB stores)."""
    from AppConfig import Timezone
    return datetime.datetime(year, month, day, hour, minute, second,
                             millisecond * 1000, tzinfo=Timezone().getTimezone())


def storeWithProducts(adminId: str, name: str = 'Bakery A',
                      currency: str = 'PHP') -> dict:
    """A store with three available products and one hidden one."""
    store = createTestStore(adminId, name, currency)
    addProduct(store['_id'], 'Pandesal', 5, soldCount=120,
               description='Soft bread roll')
    addProduct(store['_id'], 'Ensaymada', 45, soldCount=80)
    addProduct(store['_id'], 'Ube Cake', 650, soldCount=80)
    addProduct(store['_id'], 'Old Recipe', 10, soldCount=999,
               isAvailable=False)
    return store


def guestAt(firebase, store: dict, uid: str) -> str:
    """The Customers _id of guest `uid` after it opens the store's link."""
    from ApiRequests import ApiRequests
    firebase['claims'] = guestClaims(uid)
    return ApiRequests().startGuestSession(TOKEN, store['slug'])['_id']


def verifiedGuestAt(firebase, store: dict, uid: str,
                    phoneNumber: str = '+639171234567') -> str:
    """guestAt, then the guest's phone number verified (verifyCustomerPhone),
    so it can place an order."""
    from ApiRequests import ApiRequests
    customerId = guestAt(firebase, store, uid)
    firebase['claims'] = guestClaims(uid, 'phone', phoneNumber)
    ApiRequests().verifyCustomerPhone(customerId, TOKEN)
    return customerId
