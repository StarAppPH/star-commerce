"""mcpRuntime.py — what the generated mcpServer.py runs on (hand-written).

- OurJwtVerifier: the MCP servers accept OUR access token (the JWT app.py
  issues, Flask-JWT-Extended, HS256 with JWT_SECRET_KEY) as the bearer
  token. A missing, forged or expired one is a 401 before any tool runs.
- callerUserId / sessionStoreId: the injected route params (mcpTools.
  INJECTED_PARAMS), read from the connection, never from the AI.
- callRoute: runs the route method and turns its failure into a ToolError
  the AI can read, with routeErrors' messages (a 500's text never leaks).
- ArgumentsHidingServer: the SDK checks a tool's arguments against its
  signature before the route runs; its refusal names the argument and what
  it should be, never the value sent.
- buildApp: one Starlette app serving each audience's server at
  /<audience>/mcp (streamable HTTP, stateless, JSON responses).
"""

import contextlib
import logging
import os

import jwt as pyjwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict
from starlette.applications import Starlette
from starlette.routing import Mount

import AppConfig  # noqa: F401 -- loads server/.env (JWT_SECRET_KEY)
from routeErrors import routeFailure

MCP_PORT = 5004
FLASK_PORT = 5003
LOCAL_HOST = '127.0.0.1'
# Flask-JWT-Extended's default; app.py sets no other algorithm.
JWT_ALGORITHM = 'HS256'
JWT_SECRET_MIN_LENGTH = 32
# The store the chat session is at: set by our chat route on the MCP
# connection from the chat's own document, never by the AI.
STORE_HEADER = 'x-store-id'
# Every token is ours: one client id for all of them.
CLIENT_ID = 'starCommerce'
MCP_PATH = '/mcp'


def _jwtSecret() -> str:
    secret = os.getenv('JWT_SECRET_KEY')
    if not secret:
        raise ValueError('JWT_SECRET_KEY environment variable is not set')
    if (os.getenv('ENVIRONMENT') != 'localdev'
            and len(secret) < JWT_SECRET_MIN_LENGTH):
        raise ValueError('JWT_SECRET_KEY must be a random secret of at least '
                         f'{JWT_SECRET_MIN_LENGTH} characters outside localdev')
    return secret


class OurJwtVerifier:
    """Accepts an unexpired access token app.py signed; refuses anything
    else (None makes the SDK answer 401)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = pyjwt.decode(token,
                                  _jwtSecret(),
                                  algorithms=[JWT_ALGORITHM],
                                  options={'require': ['exp', 'sub']})
        except pyjwt.PyJWTError:
            return None
        subject = claims.get('sub')
        if claims.get('type') != 'access' or not isinstance(
                subject, str) or not subject:
            return None
        return AccessToken(token=token,
                           client_id=CLIENT_ID,
                           scopes=[],
                           expires_at=claims['exp'],
                           subject=subject)


def callerUserId() -> str:
    """The verified JWT's subject: the userId every tool acts as."""
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError('Sign in again to continue')
    return token.subject


def sessionStoreId(ctx: Context) -> str:
    """The store the chat session is at (STORE_HEADER)."""
    request = ctx.request_context.request
    storeId = request.headers.get(STORE_HEADER) if request is not None else None
    if not storeId:
        raise ToolError('No store is open in this chat')
    return storeId


def callRoute(route, **kwargs) -> dict:
    """Run the route method. A failure the caller can act on (ValueError,
    PermissionError, ...) reaches the AI with its message; any other one
    with routeErrors' generic message and error id, logged once."""
    try:
        return route(**kwargs)
    except ToolError:
        raise
    except Exception as error:
        failure = routeFailure(error, status=400)
        logging.log(failure.level, {
            'function': route.__name__,
            'event': 'mcpToolError',
            'traceback': failure.traceback
        })
        raise ToolError(failure.message) from None


def _hidingInput(argModel: type) -> type:
    """`argModel` (the SDK's model of a tool's arguments) with
    hide_input_in_errors: a refusal keeps the argument's name and what it
    should be, but no longer quotes the value (input_value), nested models
    (NewProduct) included. Same name, so the message reads the same."""
    return type(argModel.__name__, (argModel,), {
        '__module__': argModel.__module__,
        'model_config': ConfigDict(**argModel.model_config,
                                   hide_input_in_errors=True),
    })


class ArgumentsHidingServer(MCPServer):
    """An MCPServer whose argument check never quotes a value. The SDK
    validates a tool's arguments before the tool (and so the route) runs,
    and its ToolError would otherwise carry each refused value, which may
    be whatever the AI was told: the route's own checks never quote one
    either. The SDK reads FuncMetadata.arg_model live on every call, so
    swapping it after registration is enough."""

    def add_tool(self, fn, name: str | None = None, **kwargs) -> None:
        super().add_tool(fn, name=name, **kwargs)
        tool = self._tool_manager.get_tool(name or fn.__name__)
        tool.fn_metadata.arg_model = _hidingInput(tool.fn_metadata.arg_model)


def _baseUrl(port: int) -> str:
    return f'http://{LOCAL_HOST}:{port}'


def newServer(audience: str) -> MCPServer:
    """An audience's MCP server, accepting our JWT as its bearer token. Its
    argument refusals never quote a value (ArgumentsHidingServer)."""
    return ArgumentsHidingServer(
        name=f'starCommerce-{audience}',
        token_verifier=OurJwtVerifier(),
        auth=AuthSettings(
            issuer_url=_baseUrl(FLASK_PORT),
            resource_server_url=f'{_baseUrl(MCP_PORT)}/{audience}{MCP_PATH}',
            # Our JWT carries no audience: the routes themselves decide who
            # may call them (requireAdmin / requireCustomer).
            validate_token_resource=False,
        ),
    )


def buildApp(servers: dict[str, MCPServer]) -> Starlette:
    """Every audience's server at /<audience>/mcp, in one app. Starlette
    runs no mounted app's lifespan, so this one runs each session manager."""
    apps = {
        audience: server.streamable_http_app(streamable_http_path=MCP_PATH,
                                             stateless_http=True,
                                             json_response=True)
        for audience, server in servers.items()
    }

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for server in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=[
        Mount(f'/{audience}', app=app) for audience, app in apps.items()
    ],
                     lifespan=lifespan)


def run(servers: dict[str, MCPServer]) -> None:
    """Serve the MCP servers locally, beside Flask (localdev only for now:
    the cloud deployment is not set up yet)."""
    if os.getenv('ENVIRONMENT') != 'localdev':
        raise SystemExit('The MCP server runs in localdev only for now')
    _jwtSecret()
    import uvicorn
    uvicorn.run(buildApp(servers), host=LOCAL_HOST, port=MCP_PORT)
