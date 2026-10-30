"""mcpTools.py — mark a route as an MCP tool.

    @mcp_tool('customer', thinkingText=['Browsing the shelves',
                                        'Looking through the products',
                                        'Checking what is in stock'])
    @route_config(httpMethod='POST', jwtRequired=True, ...)
    def listStoreProducts(self, userId: str, storeId: str, ...) -> dict:
        \"\"\"The docstring is the tool description the AI reads.\"\"\"

mcpCreator.py (run by AppCreator) turns every marked route into a tool of
the generated server/mcpServer.py, on the MCP server of each audience named
here. The tool calls the route method itself, so it runs the same checks
and returns the same responseModel as the HTTP route.

`thinkingText` (required: at least MIN_THINKING_TEXTS) is what the chat
shows, and voice mode says, while the tool runs: one picked at random each
time the assistant calls it (chatThinking.py). chatToolsCreator writes them
into chatTools.THINKING_TEXTS.

The tool's arguments are the route's, minus the INJECTED_PARAMS: those come
from the connection (mcpRuntime.py), never from the AI, so the AI can never
choose whom it acts as or which store it reaches.

This module imports nothing from the MCP SDK: the Flask app imports it with
every route module.
"""

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


def mcp_tool(*audiences: str, thinkingText: list[str],
             description: str | None = None):
    """List the route as an MCP tool for `audiences` (see MCP_AUDIENCES).
    Goes OUTSIDE @route_config. `thinkingText` is required: at least
    MIN_THINKING_TEXTS distinct phrases the chat shows while it runs. `description`, when given, is the tool
    description in place of a docstring: text built at import (from
    constants such as a limit or a list of choices) that a docstring, never
    an f-string, cannot hold."""
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
        func.mcpAudiences = tuple(audiences)
        func.mcpThinkingText = texts
        if description is not None:
            # mcpCreator reads the docstring of the unwrapped route.
            inspect.unwrap(func).__doc__ = description
            func.__doc__ = description
        return func

    return decorator
