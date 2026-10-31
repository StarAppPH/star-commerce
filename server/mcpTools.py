"""mcpTools.py — mark a method of a tool class as an MCP tool.

    class ProductTools():

        @mcp_tool('customer',
                  thinkingText=['Browsing the shelves',
                                'Looking through the products',
                                'Checking what is in stock'],
                  responseModel=ProductListResponse,
                  twinOf=None)
        def listStoreProducts(self, userId: str, storeId: str, ...) -> dict:
            \"\"\"The docstring is the tool description the AI reads.\"\"\"
            return listStoreProducts(ProductListResponse, userId, storeId, ...)

A route serves a person, a tool serves the AI: they never stack. A tool
class lives in its domain's actions module, under the route class, and is
mixed into ToolRequests.py (never into ApiRequests). When a route and a
tool do one job, both are one statement calling the same module-level
action, each with its own model, and the tool names its route in
`twinOf` (test_route_tool_twins.py keeps the pair from drifting).

mcpCreator.py (run by AppCreator) turns every marked method into a tool of
the generated server/mcpServer.py, on the MCP server of each audience named
here, and chatToolsCreator.py into a tool of the chat route.

`responseModel` (required) is the tool's output schema: a ResponseModel all
the way down, and in localdev every call checks the return value against
it, on every path (MCP server, chat, a direct call), as route_config does
for a route.

`twinOf` (required, keyword-only): None for a job only the AI does, else
the route FUNCTION doing the same job (`twinOf=PolicyActions.
getMyStorePolicy`). A tool carries no roleAccess: every action checks who
may run it first (requireAdmin, requireCustomer, requireOwnerStore, ...).

`thinkingText` (required: at least MIN_THINKING_TEXTS) is what the chat
shows, and voice mode says, while the tool runs: one picked at random each
time the assistant calls it (chatThinking.py). chatToolsCreator writes them
into chatTools.THINKING_TEXTS.

The tool's arguments are its params, minus the INJECTED_PARAMS: those come
from the connection (mcpRuntime.py), never from the AI, so the AI can never
choose whom it acts as or which store it reaches.

This module imports nothing from the MCP SDK: the Flask app imports it with
every route module.
"""

import functools
import inspect

# The MCP servers a tool can be listed on: one per kind of user.
MCP_AUDIENCES = ('admin', 'customer')

# Route params a tool fills in from the connection, never from the AI:
# userId is the verified JWT's subject; storeId is the store the chat
# session is at, set by our own server on the connection (the
# X-Store-Id header), never by the AI.
INJECTED_PARAMS = ('userId', 'storeId')

# A tool's thinking texts: at least this many, each a short phrase.
MIN_THINKING_TEXTS = 3
THINKING_TEXT_MAX_LENGTH = 60


def _validThinkingText(thinkingText) -> tuple[str, ...]:
    if not isinstance(thinkingText, (list, tuple)):
        raise ValueError('mcp_tool thinkingText must be a list of strings')
    if len(thinkingText) < MIN_THINKING_TEXTS:
        raise ValueError(f'mcp_tool thinkingText needs at least '
                         f'{MIN_THINKING_TEXTS} texts')
    for text in thinkingText:
        if not isinstance(text, str) or not text.strip():
            raise ValueError('mcp_tool thinkingText holds only non-empty '
                             'strings')
        if text != text.strip() or len(text) > THINKING_TEXT_MAX_LENGTH:
            raise ValueError(f'mcp_tool thinkingText {text!r} must be '
                             f'trimmed and at most {THINKING_TEXT_MAX_LENGTH} '
                             'characters')
    if len(set(thinkingText)) != len(thinkingText):
        raise ValueError('mcp_tool thinkingText lists a text twice')
    return tuple(thinkingText)


def _validTwinOf(name: str, twinOf):
    """`twinOf` is None or a route function; anything else is refused."""
    if twinOf is None:
        return None
    if isinstance(twinOf, str) or not callable(twinOf):
        raise ValueError(f'mcp_tool {name}: twinOf is the route function '
                         '(twinOf=<RouteClass>.<route>) or None')
    if getattr(twinOf, 'mcpAudiences', None):
        raise ValueError(f'mcp_tool {name}: twinOf names a tool, not a '
                         'route')
    if not getattr(twinOf, 'httpMethod', None):
        raise ValueError(f'mcp_tool {name}: twinOf names a function '
                         'without @route_config: it must be a route')
    return twinOf


def mcp_tool(*audiences: str, thinkingText: list[str], responseModel,
             twinOf, description: str | None = None):
    """List the method as an MCP tool for `audiences` (see MCP_AUDIENCES).
    Never on a route: a function carrying @route_config is refused.
    `thinkingText` is required: at least MIN_THINKING_TEXTS distinct phrases
    the chat shows while it runs. `responseModel` (required) is what it
    returns, checked in localdev on every call. `twinOf` (required) is None
    or the route function doing the same job. `description`, when given, is
    the tool description in place of a docstring: text built at import
    (from constants such as a limit or a list of choices) that a docstring,
    never an f-string, cannot hold."""
    if not audiences:
        raise ValueError('mcp_tool needs at least one audience')
    unknown = [a for a in audiences if a not in MCP_AUDIENCES]
    if unknown:
        raise ValueError(f'Unknown MCP audience {unknown}: '
                         f'use {MCP_AUDIENCES}')
    if len(set(audiences)) != len(audiences):
        raise ValueError('mcp_tool lists an audience twice')
    texts = _validThinkingText(thinkingText)

    def decorator(func):
        name = getattr(func, '__name__', repr(func))
        if getattr(func, 'httpMethod', None):
            raise ValueError(f'mcp_tool {name}: a tool is no route: give it '
                             'its own method in a tool class (ToolRequests)')
        # A ResponseModel all the way down, or the import fails here.
        from responseModelCheck import assertResponseModel
        assertResponseModel(name, responseModel)
        twin = _validTwinOf(name, twinOf)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            result = func(*args, **kwargs)
            # Localdev only, read per call: route_config's check.
            from route_config import check_route_response
            check_route_response(wrapped, result)
            return result

        wrapped.mcpAudiences = tuple(audiences)
        wrapped.mcpThinkingText = texts
        wrapped.responseModel = responseModel
        wrapped.twinOf = twin
        if description is not None:
            # mcpCreator reads the docstring of the unwrapped tool.
            func.__doc__ = description
            wrapped.__doc__ = description
        return wrapped

    return decorator
