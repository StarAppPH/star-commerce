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

import mcpCreator
import mcpRuntime
import mcpServer
from ApiRequests import ApiRequests
from mcpTools import mcp_tool
from mongoDb import db
from objects import NoContentResponse
from conftest import (STORE_DETAILS, addProduct, createTestStore,
                      createTestUser, guestAt, storeWithProducts,
                      wipeCollections)

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products']
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

    assert asyncio.run(names('customer')) == {'listStoreProducts'}
    assert asyncio.run(names('admin')) == {'getUserStore', 'createUserStore'}


def test_aToolsArguments_leaveOutTheInjectedParams():
    async def schema():
        [tool] = await mcpServer.servers['customer'].list_tools()
        return tool.input_schema

    properties = asyncio.run(schema())['properties']
    assert set(properties) == {'search', 'limit'}


def test_anAdminToolsArguments_leaveOutTheInjectedParams():
    async def tools():
        return {t.name: t for t in await mcpServer.servers['admin'].list_tools()}

    byName = asyncio.run(tools())
    create = byName['createUserStore'].input_schema

    assert set(create['properties']) == {
        'name', 'description', 'contactNumber', 'address', 'currency',
        'openingHours'
    }
    assert set(create['required']) == {
        'name', 'description', 'contactNumber', 'address'
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
        return mcp_tool(*audiences)(func)

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
        mcp_tool(*audiences)


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
    assert created.structured_content == store
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


@pytest.mark.parametrize('tool, arguments', [
    ('getUserStore', {}),
    ('createUserStore', ADMIN_STORE),
])
def test_e2e_adminTools_refuseACustomer(clean, firebase, tool, arguments):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    result = callTool(_auth(customerId), tool, arguments, audience='admin')

    assert result.is_error
    assert 'Only a store admin can do this' in _text(result)
    assert len(db.read({}, 'Stores')) == 1


def test_e2e_adminToolsAreNotOnTheCustomerServer(clean, firebase):
    store = createTestStore(OTHER_ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    try:
        result = callTool(_auth(customerId, store['_id']), 'createUserStore',
                          ADMIN_STORE)
        refused = result.is_error
    except Exception:  # the SDK may raise for an unknown tool
        refused = True

    assert refused
    assert len(db.read({}, 'Stores')) == 1
