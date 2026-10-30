"""The generated MCP servers (mcpCreator.py, mcpRuntime.py, mcpServer.py).

- mcpServer.py is current, and the generator refuses a route that breaks a
  tool rule;
- the bearer token is OUR access token: a missing, forged, expired or
  refresh token is a 401 before any tool runs;
- a tool acts as the JWT's subject at the store the connection names,
  never at one the AI picks, and reaches the AI with the route's message.

The end-to-end tests drive the real Starlette app in memory
(httpx2.ASGITransport) with the SDK's own client.
"""

import asyncio
import contextlib
import datetime
import os
import typing
from typing import Literal

import httpx2
import jwt as pyjwt
import pytest
from flask import Flask
from flask_jwt_extended import (JWTManager, create_access_token,
                                create_refresh_token)
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp.server.mcpserver.utilities.func_metadata import func_metadata

import mcpCreator
import mcpRuntime
import mcpServer
from ApiRequests import ApiRequests
from mcpTools import mcp_tool
from mongoDb import db
from objects import NoContentResponse
from conftest import (BRAND_COLORS, STORE_DETAILS, addOrder, addProduct, createTestStore,
                      createTestUser, guestAt, storeWithProducts,
                      verifiedGuestAt, wipeCollections)

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Customers', 'Products', 'Orders'
]
# A test tool's thinking texts (mcpTools needs at least three).
THINKING = ['Working on it', 'Doing the thing', 'Almost there']
ADMIN = 'mc-admin'
OTHER_ADMIN = 'mc-other-admin'
GUEST = 'mc-guest-uid'

api = ApiRequests()
# The address the real server binds: the SDK refuses any other Host header
# (DNS rebinding protection).
BASE_URL = f'http://{mcpRuntime.LOCAL_HOST}:{mcpRuntime.MCP_PORT}'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _jwtApp() -> Flask:
    """A Flask app configured as app.py configures Flask-JWT-Extended."""
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = os.environ['JWT_SECRET_KEY']
    JWTManager(app)
    return app


def accessToken(identity: str, **kwargs) -> str:
    with _jwtApp().app_context():
        return create_access_token(identity=identity, **kwargs)


def _verify(token: str):
    return asyncio.run(mcpRuntime.OurJwtVerifier().verify_token(token))


# ----------------------------- generated file ----------------------------- #


def test_generatedMcpServer_isCurrent():
    with open(mcpCreator.OUTPUT_PATH, encoding='utf-8') as f:
        assert f.read() == mcpCreator.generatedSource(), (
            'mcpServer.py is stale: run `cd server && python AppCreator.py`')


def test_eachAudienceListsOnlyItsTools():
    async def names(audience):
        return {t.name for t in await mcpServer.servers[audience].list_tools()}

    assert asyncio.run(names('customer')) == {'listStoreProducts',
                                              'getMyOrders', 'createOrder',
                                              'payOrder', 'cancelMyOrder',
                                              'getStorePolicy',
                                              'requestReturn', 'getMyReturns',
                                              'cancelMyReturn'}
    assert asyncio.run(names('admin')) == {
        'getUserStore', 'createUserStore', 'addStoreProduct',
        'addStoreProducts', 'updateStoreProduct', 'listMyStoreProducts',
        'getStoreOrders', 'completeOrder', 'cancelStoreOrder',
        'getMyStorePolicy', 'updateMyStorePolicy', 'updateMyStoreTheme',
        'generateProductPhotos', 'setProductPhoto', 'getStoreReturns',
        'approveReturn', 'rejectReturn', 'receiveReturn', 'listVoices',
        'setUserStoreLocation', 'suggestStoreAddress', 'updateMyStoreDetails',
        'makeProductPhotoChoices'
    }


def test_aToolsArguments_leaveOutTheInjectedParams():
    async def schema():
        tools = await mcpServer.servers['customer'].list_tools()
        [tool] = [t for t in tools if t.name == 'listStoreProducts']
        return tool.input_schema

    properties = asyncio.run(schema())['properties']
    assert set(properties) == {'search', 'limit'}


def test_anAdminToolsArguments_leaveOutTheInjectedParams():
    async def tools():
        return {t.name: t for t in await mcpServer.servers['admin'].list_tools()}

    byName = asyncio.run(tools())
    create = byName['createUserStore'].input_schema

    assert set(create['properties']) == {
        'name', 'description', 'contactNumber', 'address', 'primaryColor',
        'secondaryColor', 'currency', 'openingHours'
    }
    assert set(create['required']) == {
        'name', 'description', 'contactNumber', 'primaryColor',
        'secondaryColor'
    }
    assert 'PHP' in str(create['properties']['currency'])
    assert 'XYZ' not in str(create['properties']['currency'])
    assert byName['getUserStore'].input_schema.get('properties', {}) == {}


# ----------------------------- generator rules ----------------------------- #


def _route(jwtRequired=True, createAccessToken=False, serviceOnly=False,
           audiences=('customer',), responseModel=NoContentResponse):
    """What @mcp_tool over @route_config leaves on a method, without the
    route_config wrapper."""

    def decorator(func):
        func.jwtRequired = jwtRequired
        func.createAccessToken = createAccessToken
        func.serviceOnly = serviceOnly
        func.responseModel = responseModel
        return mcp_tool(*audiences, thinkingText=THINKING)(func)

    return decorator


@_route()
def goodTool(self, userId: str, storeId: str,
             kind: Literal['a', 'b'] = 'a', note: str | None = None) -> dict:
    """A good tool."""


@_route(jwtRequired=False)
def publicTool(self, userId: str) -> dict:
    """Public."""


@_route(createAccessToken=True)
def tokenTool(self, userId: str) -> dict:
    """Issues a token."""


@_route(serviceOnly=True)
def serviceTool(self, userId: str) -> dict:
    """Service only."""


@_route(responseModel=None)
def noModelTool(self, userId: str) -> dict:
    """No response model."""


@_route()
def noUserTool(self, storeId: str) -> dict:
    """No userId."""


@_route()
def undocumentedTool(self, userId: str) -> dict:
    pass


@_route()
def ctxTool(self, userId: str, ctx: str) -> dict:
    """Shadows the context."""


@_route()
def unannotatedTool(self, userId: str, search) -> dict:
    """Unannotated."""


@_route()
def varArgsTool(self, userId: str, *rest: str) -> dict:
    """Var args."""


SENTINEL = object()


@_route()
def objectDefaultTool(self, userId: str, value: str = SENTINEL) -> dict:
    """An object default."""


@_route()
def dottedAnnotationTool(self, userId: str,
                         value: typing.Optional[str] = None) -> dict:
    """A dotted annotation."""


def test_toolSpec_ofAGoodRoute():
    spec = mcpCreator.toolSpec('goodTool', goodTool)

    assert spec.audiences == ('customer',)
    assert spec.description == 'A good tool.'
    assert spec.injected == ('userId', 'storeId')
    assert [(p.name, p.annotation, p.default) for p in spec.params] == [
        ('kind', "Literal['a', 'b']", "'a'"),
        ('note', 'str | None', 'None'),
    ]
    assert spec.returns == 'NoContentResponse'
    assert spec.imports == (('objects', 'NoContentResponse'),
                            ('typing', 'Literal'))


def test_render_callsTheRouteWithTheInjectedParams():
    source = mcpCreator.render([mcpCreator.toolSpec('goodTool', goodTool)])

    assert 'from typing import Literal' in source
    assert ("def goodTool(ctx: Context, kind: Literal['a', 'b'] = 'a', "
            'note: str | None = None) -> NoContentResponse:') in source
    assert 'userId=callerUserId(),' in source
    assert 'storeId=sessionStoreId(ctx),' in source
    compile(source, 'mcpServer.py', 'exec')


@pytest.mark.parametrize('name, func, problem', [
    ('publicTool', publicTool, 'needs jwtRequired=True'),
    ('tokenTool', tokenTool, 'token-issuing or serviceOnly'),
    ('serviceTool', serviceTool, 'token-issuing or serviceOnly'),
    ('noUserTool', noUserTool, 'needs a userId param'),
    ('noModelTool', noModelTool, 'needs a responseModel'),
    ('undocumentedTool', undocumentedTool, 'needs a docstring'),
    ('ctxTool', ctxTool, 'would shadow the MCP context'),
    ('unannotatedTool', unannotatedTool, 'needs a type annotation'),
    ('varArgsTool', varArgsTool, 'cannot be tool arguments'),
    ('objectDefaultTool', objectDefaultTool, 'no plain literal'),
    ('dottedAnnotationTool', dottedAnnotationTool, 'as a bare name'),
    ('good.Tool', goodTool, 'the name must match'),
])
def test_toolSpec_refusesARouteThatBreaksARule(name, func, problem):
    with pytest.raises(ValueError, match=problem):
        mcpCreator.toolSpec(name, func)


@pytest.mark.parametrize('audiences, problem', [
    ((), 'at least one audience'),
    (('everyone',), 'Unknown MCP audience'),
    (('admin', 'admin'), 'an audience twice'),
])
def test_mcpTool_refusesBadAudiences(audiences, problem):
    with pytest.raises(ValueError, match=problem):
        mcp_tool(*audiences, thinkingText=THINKING)


# ----------------------------- bearer token ----------------------------- #


def test_verifier_acceptsOurAccessToken():
    token = _verify(accessToken('mc-someone'))

    assert token.subject == 'mc-someone'


@pytest.mark.parametrize('token', [
    'not-a-jwt',
    pyjwt.encode({'sub': 'mc-someone', 'type': 'access',
                  'exp': 4102444800}, 'another-secret-of-32-characters!!',
                 algorithm='HS256'),
])
def test_verifier_refusesAForgedToken(token):
    assert _verify(token) is None


def test_verifier_refusesAnExpiredOrRefreshToken():
    expired = accessToken('mc-someone',
                          expires_delta=datetime.timedelta(seconds=-1))
    with _jwtApp().app_context():
        refresh = create_refresh_token(identity='mc-someone')

    assert _verify(expired) is None
    assert _verify(refresh) is None


def test_callRoute_hidesAnUnexpectedErrorsText():
    def broken():
        raise RuntimeError('mc internal detail')

    with pytest.raises(mcpRuntime.ToolError) as error:
        mcpRuntime.callRoute(broken)

    assert 'mc internal detail' not in str(error.value)
    assert 'Something went wrong on our side' in str(error.value)


# ----------------------------- end to end ----------------------------- #


@contextlib.asynccontextmanager
async def _inMemoryHttp(headers: dict):
    """An httpx2 client to a fresh in-memory app, its session managers
    running (Starlette runs no lifespan for an ASGITransport)."""
    app = mcpRuntime.buildApp(mcpServer.servers)
    async with contextlib.AsyncExitStack() as stack:
        for server in mcpServer.servers.values():
            await stack.enter_async_context(server.session_manager.run())
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url=BASE_URL,
                                      headers=headers) as http:
            yield http


def callTool(headers: dict, name: str, arguments: dict,
             audience: str = 'customer'):
    """Call a tool with the SDK's own client over the in-memory app."""

    async def run():
        async with _inMemoryHttp(headers) as http:
            transport = streamable_http_client(f'{BASE_URL}/{audience}/mcp',
                                               http_client=http)
            async with Client(transport) as client:
                return await client.call_tool(name, arguments)

    return asyncio.run(run())


def _auth(userId: str, storeId: str | None = None) -> dict:
    headers = {'Authorization': f'Bearer {accessToken(userId)}'}
    if storeId is not None:
        headers[mcpRuntime.STORE_HEADER] = storeId
    return headers


def _text(result) -> str:
    return ' '.join(getattr(c, 'text', '') for c in result.content)


def test_e2e_listStoreProducts_asTheCustomer_atTheConnectionsStore(
        clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId, store['_id']), 'listStoreProducts',
                      {'limit': 2})

    assert not result.is_error, _text(result)
    assert result.structured_content == api.listStoreProducts(
        customerId, store['_id'], limit=2)
    assert [p['name'] for p in result.structured_content['products']
            ] == ['Pandesal', 'Ensaymada']


def test_e2e_theAICannotPickAnotherUserOrStore(clean, firebase):
    """userId and storeId sent as arguments are no tool arguments: they
    are dropped, and the call runs as the JWT's customer at the
    connection's store."""
    store = storeWithProducts(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    addProduct(other['_id'], 'Latte', 150)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId, store['_id']), 'listStoreProducts', {
        'storeId': other['_id'],
        'userId': 'mc-someone-else'
    })

    assert not result.is_error, _text(result)
    names = [p['name'] for p in result.structured_content['products']]
    assert names == ['Pandesal', 'Ensaymada', 'Ube Cake']


def test_e2e_withoutAStoreHeader_isAToolError(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId), 'listStoreProducts', {})

    assert result.is_error
    assert 'No store is open in this chat' in _text(result)


def test_e2e_theRoutesRefusal_reachesTheAI(clean, firebase):
    store = storeWithProducts(ADMIN)

    result = callTool(_auth(ADMIN, store['_id']), 'listStoreProducts', {})

    assert result.is_error
    assert 'Only a customer can do this' in _text(result)


@pytest.mark.parametrize('audience', ['customer', 'admin'])
@pytest.mark.parametrize('headers', [
    {},
    {'Authorization': 'Bearer not-a-jwt'},
])
def test_e2e_withoutOurJwt_isA401(headers, audience):
    async def post():
        async with _inMemoryHttp({}) as http:
            return await http.post(
                f'/{audience}/mcp',
                headers={
                    **headers, 'Accept': 'application/json, text/event-stream'
                },
                json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'tools/list'
                })

    assert asyncio.run(post()).status_code == 401


ADMIN_STORE = {'name': 'Bakery A', **STORE_DETAILS}


def test_e2e_adminOnboarding_getUserStoreThenCreateUserStore(clean):
    createTestUser(ADMIN)

    before = callTool(_auth(ADMIN), 'getUserStore', {}, audience='admin')
    assert not before.is_error, _text(before)
    assert before.structured_content == {'store': None}

    created = callTool(_auth(ADMIN), 'createUserStore',
                       {**ADMIN_STORE, 'openingHours': 'Mon-Sat 7am-7pm'},
                       audience='admin')

    assert not created.is_error, _text(created)
    store = api.getUserStore(ADMIN)['store']
    assert {**created.structured_content, **BRAND_COLORS} == store
    assert store['currency'] == 'PHP'
    assert store['slug'] == 'bakery-a'
    assert store['openingHours'] == 'Mon-Sat 7am-7pm'
    assert store['location'] is None
    after = callTool(_auth(ADMIN), 'getUserStore', {}, audience='admin')
    assert after.structured_content == {'store': store}
    assert len(db.read({}, 'Stores')) == 1


def test_e2e_createUserStore_theAICannotPickAnotherOwner(clean):
    createTestUser(ADMIN)
    createTestUser(OTHER_ADMIN)

    result = callTool(_auth(ADMIN), 'createUserStore',
                      {**ADMIN_STORE, 'userId': OTHER_ADMIN},
                      audience='admin')

    assert not result.is_error, _text(result)
    [stored] = db.read({}, 'Stores')
    assert stored['ownerUserId'] == ADMIN


def test_e2e_createUserStore_aRefusedDetailReachesTheAI_thenRetrySucceeds(
        clean):
    createTestUser(ADMIN)
    bad = {**ADMIN_STORE, 'contactNumber': '09171234567'}

    refused = callTool(_auth(ADMIN), 'createUserStore', bad, audience='admin')

    assert refused.is_error
    assert 'international format' in _text(refused)
    assert db.read({}, 'Stores') == []

    retried = callTool(_auth(ADMIN), 'createUserStore', ADMIN_STORE,
                       audience='admin')

    assert not retried.is_error, _text(retried)
    assert len(db.read({}, 'Stores')) == 1


def test_e2e_createUserStore_anUnsupportedCurrencyIsAToolError(clean):
    createTestUser(ADMIN)

    result = callTool(_auth(ADMIN), 'createUserStore',
                      {**ADMIN_STORE, 'currency': 'XYZ'}, audience='admin')

    assert result.is_error
    assert db.read({}, 'Stores') == []


PRODUCT_TOOL_ARGUMENTS = [
    ('addStoreProduct', {'name': 'Tea', 'price': 1, 'inventory': 1}),
    ('addStoreProducts', {'products': [{'name': 'Tea', 'price': 1,
                                        'inventory': 1}]}),
    ('updateStoreProduct', {'productId': 'mc-no-product', 'price': 1}),
    ('listMyStoreProducts', {}),
]


@pytest.mark.parametrize('tool, arguments', [
    ('getUserStore', {}),
    ('createUserStore', ADMIN_STORE),
] + PRODUCT_TOOL_ARGUMENTS)
def test_e2e_adminTools_refuseACustomer(clean, firebase, tool, arguments):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId), tool, arguments, audience='admin')

    assert result.is_error
    assert 'Only a store admin can do this' in _text(result)
    assert len(db.read({}, 'Stores')) == 1


@pytest.mark.parametrize('tool, arguments', [
    ('createUserStore', ADMIN_STORE),
] + PRODUCT_TOOL_ARGUMENTS)
def test_e2e_adminToolsAreNotOnTheCustomerServer(clean, firebase, tool,
                                                 arguments):
    store = createTestStore(OTHER_ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    try:
        result = callTool(_auth(customerId, store['_id']), tool, arguments)
        refused = result.is_error
    except Exception:  # the SDK may raise for an unknown tool
        refused = True

    assert refused
    assert len(db.read({}, 'Stores')) == 1
    assert db.read({}, 'Products') == []


# ----------------------------- admin product tools ----------------------------- #

PRODUCT_FIELDS = {'name', 'price', 'inventory', 'description', 'isAvailable'}


def _resolved(schema: dict, node: dict) -> dict:
    """`node` with a local $ref followed."""
    ref = node.get('$ref')
    if ref is None:
        return node
    return schema['$defs'][ref.rsplit('/', 1)[-1]]


def test_theAdminProductToolsArguments_leaveOutTheInjectedParams():
    async def tools():
        return {t.name: t for t in await mcpServer.servers['admin'].list_tools()}

    byName = asyncio.run(tools())

    add = byName['addStoreProduct'].input_schema
    assert set(add['properties']) == PRODUCT_FIELDS
    assert set(add['required']) == {'name', 'price', 'inventory'}

    batch = byName['addStoreProducts'].input_schema
    assert set(batch['properties']) == {'products'}
    assert set(batch['required']) == {'products'}
    item = _resolved(batch, batch['properties']['products']['items'])
    assert set(item['properties']) == PRODUCT_FIELDS
    assert set(item['required']) == {'name', 'price', 'inventory'}
    # NewProduct is types only: the route checks the limits.
    for key in ('maxLength', 'minLength', 'minimum', 'maximum'):
        assert key not in str(item)

    update = byName['updateStoreProduct'].input_schema
    assert set(update['properties']) == {'productId'} | PRODUCT_FIELDS
    assert set(update['required']) == {'productId'}

    listing = byName['listMyStoreProducts'].input_schema
    assert set(listing['properties']) == {'search', 'limit'}
    assert not listing.get('required')


PANDESAL = {'name': 'Pandesal', 'price': 5, 'inventory': 100}


def test_e2e_adminProducts_addBatchListUpdate(clean, firebase):
    store = createTestStore(ADMIN)
    headers = _auth(ADMIN)

    added = callTool(headers, 'addStoreProduct', PANDESAL, audience='admin')

    assert not added.is_error, _text(added)
    stored = db.read({'name': 'Pandesal'}, 'Products', findOne=True)
    assert added.structured_content['_id'] == stored['_id']
    assert stored['storeId'] == store['_id']
    assert added.structured_content['reservedInventory'] == []

    batch = callTool(headers, 'addStoreProducts', {
        'products': [{'name': 'Tea', 'price': 3, 'inventory': 0,
                      'isAvailable': False},
                     {'name': 'Cake', 'price': 9.5, 'inventory': 4}]
    }, audience='admin')

    assert not batch.is_error, _text(batch)
    rows = batch.structured_content['products']
    assert [r['name'] for r in rows] == ['Tea', 'Cake']
    assert [r['reservedQuantity'] for r in rows] == [0, 0]
    assert [r['isAvailable'] for r in rows] == [False, True]

    listed = callTool(headers, 'listMyStoreProducts', {}, audience='admin')

    assert not listed.is_error, _text(listed)
    assert listed.structured_content == api.listMyStoreProducts(ADMIN)
    assert [p['name'] for p in listed.structured_content['products']
            ] == ['Cake', 'Pandesal', 'Tea']

    updated = callTool(headers, 'updateStoreProduct',
                       {'productId': stored['_id'], 'price': 99.5,
                        'inventory': 0}, audience='admin')

    assert not updated.is_error, _text(updated)
    assert (updated.structured_content['price'],
            updated.structured_content['inventory']) == (99.5, 0)
    assert updated.structured_content['name'] == 'Pandesal'
    again = callTool(headers, 'listMyStoreProducts', {'search': 'pan'},
                     audience='admin')
    assert [(p['price'], p['inventory']) for p in
            again.structured_content['products']] == [(99.5, 0)]

    # A customer sees the sold-out product with its inventory, not the
    # hidden one.
    customerId = guestAt(firebase, store, GUEST)
    seen = callTool(_auth(customerId, store['_id']), 'listStoreProducts', {})
    assert [(p['name'], p['inventory'])
            for p in seen.structured_content['products']
            ] == [('Cake', 4), ('Pandesal', 0)]


@pytest.mark.parametrize('tool, arguments, message', [
    ('addStoreProduct', {**PANDESAL, 'price': -1},
     'The price is a number from 0 to 10000000'),
    ('addStoreProducts', {'products': [PANDESAL, {**PANDESAL, 'name': ''}]},
     'Product 2: the name is 1 to 80 characters'),
    ('updateStoreProduct', {'productId': 'mc-any'},
     'Send at least one field to change'),
    ('updateStoreProduct', {'productId': 'mc-no-such-product', 'price': 1},
     'Product not found'),
])
def test_e2e_adminProducts_aRefusalReachesTheAIAsAToolError(
        clean, tool, arguments, message):
    createTestStore(ADMIN)

    result = callTool(_auth(ADMIN), tool, arguments, audience='admin')

    assert result.is_error
    assert message in _text(result)
    assert db.read({}, 'Products') == []


def test_e2e_adminProducts_aTakenNameIsAToolError(clean):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pandesal', 5)

    result = callTool(_auth(ADMIN), 'addStoreProduct', PANDESAL,
                      audience='admin')

    assert result.is_error
    assert 'A product with this name already exists' in _text(result)
    assert len(db.read({}, 'Products')) == 1


def test_e2e_adminProducts_aWrongTypeIsAToolError_andAddsNothing(clean):
    createTestStore(ADMIN)

    result = callTool(_auth(ADMIN), 'addStoreProducts',
                      {'products': [{**PANDESAL, 'price': 'abc'}]},
                      audience='admin')

    assert result.is_error
    assert db.read({}, 'Products') == []


def test_e2e_adminProducts_theAICannotPickAnotherStoreOrUser(clean):
    createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')

    result = callTool(_auth(ADMIN), 'addStoreProduct',
                      {**PANDESAL, 'storeId': other['_id'],
                       'userId': OTHER_ADMIN}, audience='admin')

    assert not result.is_error, _text(result)
    [stored] = db.read({}, 'Products')
    assert stored['storeId'] == db.read({'ownerUserId': ADMIN}, 'Stores',
                                        findOne=True)['_id']


# ----------------------------- order tools ----------------------------- #

ORDER_ITEMS = [{'productId': 'p1', 'name': 'Ube Cake', 'price': 650,
                'quantity': 2}]
ORDER_SENTINEL = 'ZQ-secret-7'
ORDER_BASE = datetime.datetime(2026, 1, 15, 8, 0, tzinfo=datetime.timezone.utc)
ISO_DATE_REFUSAL = 'dates must be ISO YYYY-MM-DD dates'


def _orderIds(result) -> list[str]:
    return [o['_id'] for o in result.structured_content['orders']]


def _withoutCreatedAt(res: dict) -> list[dict]:
    """The orders of a response, minus their times (createdAt, expiresAt,
    paidAt, completedAt, cancelledAt, refundedAt): MCP sends a time as
    text, a direct call as a datetime."""
    return [{k: v for k, v in o.items()
             if k not in ('createdAt', 'expiresAt', 'paidAt', 'completedAt',
                          'cancelledAt', 'refundedAt')}
            for o in res['orders']]


def test_theOrderToolsArguments_leaveOutTheInjectedParams():
    async def tools(audience):
        return {t.name: t for t in await mcpServer.servers[audience].list_tools()}

    admin = asyncio.run(tools('admin'))['getStoreOrders'].input_schema
    customer = asyncio.run(tools('customer'))['getMyOrders'].input_schema

    assert set(admin['properties']) == {
        'status', 'period', 'startDate', 'endDate', 'limit'
    }
    assert not admin.get('required')
    assert set(customer['properties']) == {'status', 'limit'}
    assert not customer.get('required')
    # status and period are plain text: the route's own check refuses a bad
    # one with a fixed message, where the SDK's enum check would quote it.
    assert 'enum' not in str(admin['properties']['status'])
    assert 'enum' not in str(admin['properties']['period'])
    assert 'enum' not in str(customer['properties']['status'])


def test_e2e_getStoreOrders_listsTheAdminsStoreOrders(clean, firebase):
    store = storeWithProducts(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    customerId = guestAt(firebase, store, GUEST)
    older, newer = [
        addOrder(store['_id'], customerId, ORDER_ITEMS, status,
                 ORDER_BASE + datetime.timedelta(minutes=i))['_id']
        for i, status in enumerate(['pending', 'paid'])
    ]
    addOrder(other['_id'], customerId, ORDER_ITEMS)

    everything = callTool(_auth(ADMIN), 'getStoreOrders', {},
                          audience='admin')
    confirmed = callTool(_auth(ADMIN), 'getStoreOrders',
                         {'status': 'paid'}, audience='admin')
    limited = callTool(_auth(ADMIN), 'getStoreOrders', {'limit': 1},
                       audience='admin')

    assert not everything.is_error, _text(everything)
    assert _orderIds(everything) == [newer, older]
    assert _withoutCreatedAt(everything.structured_content) == (
        _withoutCreatedAt(api.getStoreOrders(ADMIN)))
    assert _orderIds(confirmed) == [newer]
    assert _orderIds(limited) == [newer]


def test_e2e_getStoreOrders_period_onTheRealClock(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = addOrder(store['_id'], customerId, ORDER_ITEMS,
                      createdAt=now)['_id']
    addOrder(store['_id'], customerId, ORDER_ITEMS,
             createdAt=now - datetime.timedelta(days=30))

    week = callTool(_auth(ADMIN), 'getStoreOrders', {'period': 'last7Days'},
                    audience='admin')

    assert not week.is_error, _text(week)
    assert _orderIds(week) == [recent]


@pytest.mark.parametrize('arguments, message', [
    ({'status': ORDER_SENTINEL}, 'status is one of'),
    ({'period': ORDER_SENTINEL}, 'period is one of'),
    ({'startDate': ORDER_SENTINEL}, ISO_DATE_REFUSAL),
    ({'endDate': '2026-02-30'}, ISO_DATE_REFUSAL),
    ({'period': 'today', 'startDate': '2026-03-10'}, 'not both'),
    ({'limit': 0}, 'limit is a whole number from 1 to 50'),
    ({'limit': ORDER_SENTINEL}, ''),
])
def test_e2e_getStoreOrders_aBadArgument_isAToolError_thatDoesNotQuoteIt(
        clean, arguments, message):
    createTestStore(ADMIN)

    result = callTool(_auth(ADMIN), 'getStoreOrders', arguments,
                      audience='admin')

    assert result.is_error
    assert message in _text(result)
    assert ORDER_SENTINEL not in _text(result)


def test_e2e_getMyOrders_listsTheCustomersOwnOrdersAtTheConnectionsStore(
        clean, firebase):
    store = storeWithProducts(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    customerId = guestAt(firebase, store, GUEST)
    otherCustomerId = guestAt(firebase, store, 'mc-other-guest-uid')
    older = addOrder(store['_id'], customerId, ORDER_ITEMS,
                     createdAt=ORDER_BASE)['_id']
    newer = addOrder(store['_id'], customerId, ORDER_ITEMS, 'paid',
                     ORDER_BASE + datetime.timedelta(minutes=1))['_id']
    addOrder(store['_id'], otherCustomerId, ORDER_ITEMS)
    addOrder(other['_id'], customerId, ORDER_ITEMS)

    # storeId and userId sent as arguments are no tool arguments: they are
    # dropped, and the call runs as the JWT's customer at the connection's
    # store.
    result = callTool(_auth(customerId, store['_id']), 'getMyOrders', {
        'storeId': other['_id'],
        'userId': otherCustomerId
    })
    confirmed = callTool(_auth(customerId, store['_id']), 'getMyOrders',
                         {'status': 'paid'})

    assert not result.is_error, _text(result)
    assert _orderIds(result) == [newer, older]
    assert all('contactNumber' not in o
               for o in result.structured_content['orders'])
    assert _withoutCreatedAt(result.structured_content) == _withoutCreatedAt(
        api.getMyOrders(customerId, store['_id']))
    assert _orderIds(confirmed) == [newer]


@pytest.mark.parametrize('arguments, message', [
    ({'status': ORDER_SENTINEL}, 'status is one of'),
    ({'limit': 51}, 'limit is a whole number from 1 to 50'),
    ({'limit': ORDER_SENTINEL}, ''),
])
def test_e2e_getMyOrders_aBadArgument_isAToolError_thatDoesNotQuoteIt(
        clean, firebase, arguments, message):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId, store['_id']), 'getMyOrders',
                      arguments)

    assert result.is_error
    assert message in _text(result)
    assert ORDER_SENTINEL not in _text(result)


def test_e2e_orderTools_refuseTheOtherRole(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)
    addOrder(store['_id'], customerId, ORDER_ITEMS)

    asCustomer = callTool(_auth(customerId), 'getStoreOrders', {},
                          audience='admin')
    asAdmin = callTool(_auth(ADMIN, store['_id']), 'getMyOrders', {})

    assert asCustomer.is_error
    assert 'Only a store admin can do this' in _text(asCustomer)
    assert asAdmin.is_error
    assert 'Only a customer can do this' in _text(asAdmin)
    # getStoreOrders is not on the customer server at all.
    try:
        wrongServer = callTool(_auth(customerId, store['_id']),
                               'getStoreOrders', {}).is_error
    except Exception:  # the SDK may raise for an unknown tool
        wrongServer = True
    assert wrongServer


# ----------------------------- arguments never quoted ----------------------------- #


def _registeredTools() -> list[tuple[str, object]]:
    """(audience, tool) of every tool on every server, so a new tool is
    covered without being named here."""
    return [(audience, tool) for audience, server in mcpServer.servers.items()
            for tool in server._tool_manager.list_tools()]


def _toolIds() -> list[str]:
    return [f'{audience}-{tool.name}' for audience, tool in _registeredTools()]


@pytest.mark.parametrize('audience, tool', _registeredTools(), ids=_toolIds())
def test_everyToolsArgumentRefusal_namesTheFieldButNeverQuotesTheValue(
        audience, tool):
    fields = list(tool.fn_metadata.arg_model.model_fields)
    # A dict is the wrong type for every kind of field (str, int, bool,
    # Literal, list), so each one is refused by the SDK's own check.
    for field in fields:
        result = callTool(_auth(ADMIN, 'mc-any-store'), tool.name,
                          {field: {'k': ORDER_SENTINEL}}, audience=audience)

        assert result.is_error, (tool.name, field)
        assert field in _text(result), (tool.name, field)
        assert ORDER_SENTINEL not in _text(result), (tool.name, field)


def test_addStoreProducts_aBadPriceInsideAnItem_isRefusedWithoutQuotingIt():
    result = callTool(_auth(ADMIN), 'addStoreProducts', {
        'products': [{'name': 'Pandesal', 'price': ORDER_SENTINEL,
                      'inventory': 1}]
    }, audience='admin')

    assert result.is_error
    assert 'price' in _text(result)
    assert ORDER_SENTINEL not in _text(result)


@pytest.mark.parametrize('audience, tool', _registeredTools(), ids=_toolIds())
def test_everyToolsArgumentModel_hidesInputInErrors_andKeepsItsSchema(
        audience, tool):
    argModel = tool.fn_metadata.arg_model
    original = argModel.__bases__[0]

    assert argModel.model_config.get('hide_input_in_errors') is True
    assert argModel.__name__ == original.__name__
    assert 'hide_input_in_errors' not in original.model_config
    # No enum or type drift against what the function declares: the schema
    # the SDK derives from the original function, not from our wrapper.
    skip = [tool.context_kwarg] if tool.context_kwarg else []
    fromFunction = func_metadata(tool.fn, skip_names=skip,
                                 structured_output=True).arg_model
    assert tool.parameters == fromFunction.model_json_schema(by_alias=True)
    assert tool.parameters == original.model_json_schema()


@pytest.mark.parametrize('products', [
    '[{"name": "Pandesal", "price": "ZQ-secret-7", "inventory": 1}]',
    'ZQ-secret-7',
])
def test_addStoreProducts_productsAsAJsonString_isRefusedWithoutQuotingIt(
        products):
    result = callTool(_auth(ADMIN), 'addStoreProducts',
                      {'products': products}, audience='admin')

    assert result.is_error
    assert ORDER_SENTINEL not in _text(result)
    assert db.read({}, 'Products') == []


# ----------------------------- checkout ----------------------------- #


def test_theCheckoutToolsArguments_leaveOutTheInjectedParams():
    async def tools():
        listed = await mcpServer.servers['customer'].list_tools()
        return {t.name: t for t in listed}

    byName = asyncio.run(tools())

    assert set(byName['createOrder'].input_schema['properties']) == {
        'items', 'fulfillment', 'delivery'}
    assert set(byName['payOrder'].input_schema['properties']) == {
        'orderId', 'paymentMethod'}
    assert 'enum' not in str(
        byName['payOrder'].input_schema['properties']['paymentMethod'])


def test_e2e_listCreateAndPayAnOrder_overMcp(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    headers = _auth(customerId, store['_id'])

    listing = callTool(headers, 'listStoreProducts', {'limit': 3})
    ube = next(p for p in listing.structured_content['products']
               if p['name'] == 'Ube Cake')
    created = callTool(headers, 'createOrder', {
        'items': [{'productId': ube['_id'], 'quantity': 2}]})

    assert not created.is_error, _text(created)
    order = created.structured_content['order']
    assert created.structured_content['paymentMethods'] == [
        'card', 'gcash', 'maya', 'grabPay', 'bankTransfer']
    assert (order['status'], order['total']) == ('pending', 1300)

    paid = callTool(headers, 'payOrder', {
        'orderId': order['_id'], 'paymentMethod': 'maya'})

    assert not paid.is_error, _text(paid)
    assert paid.structured_content['status'] == 'paid'
    assert paid.structured_content['paymentMethod'] == 'maya'
    assert db.read({'_id': ube['_id']}, 'Products', findOne=True)[
        'inventory'] == 8


@pytest.mark.parametrize('tool, arguments', [
    ('createOrder', {'items': [{'productId': ORDER_SENTINEL,
                                'quantity': 1}]}),
    ('createOrder', {'items': [{'productId': 'p1',
                                'quantity': ORDER_SENTINEL}]}),
    ('createOrder', {'items': [{'productId': 'p1', 'quantity': True}]}),
    ('createOrder', {'items': ORDER_SENTINEL}),
    ('payOrder', {'orderId': ORDER_SENTINEL, 'paymentMethod': 'card'}),
    ('payOrder', {'orderId': 'o1', 'paymentMethod': ORDER_SENTINEL}),
])
def test_e2e_checkoutRefusals_reachTheAI_withoutQuotingTheValue(
        clean, firebase, tool, arguments):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId, store['_id']), tool, arguments)

    assert result.is_error
    assert ORDER_SENTINEL not in _text(result)
    assert db.read({}, 'Orders') == []
