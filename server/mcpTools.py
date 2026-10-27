"""mcpTools.py — mark a route as an MCP tool.

    @mcp_tool('customer')
    @route_config(httpMethod='POST', jwtRequired=True, ...)
    def listStoreProducts(self, userId: str, storeId: str, ...) -> dict:
        \"\"\"The docstring is the tool description the AI reads.\"\"\"

mcpCreator.py (run by AppCreator) turns every marked route into a tool of
the generated server/mcpServer.py, on the MCP server of each audience named
here. The tool calls the route method itself, so it runs the same checks
and returns the same responseModel as the HTTP route.

The tool's arguments are the route's, minus the INJECTED_PARAMS: those come
from the connection (mcpRuntime.py), never from the AI, so the AI can never
choose whom it acts as or which store it reaches.

This module imports nothing from the MCP SDK: the Flask app imports it with
every route module.
"""

# The MCP servers a tool can be listed on: one per kind of user.
MCP_AUDIENCES = ('admin', 'customer')

# Route params a tool fills in from the connection, never from the AI:
# userId is the verified JWT's subject; storeId is the store the chat
# session is at, set by our own server on the connection (the
# X-Store-Id header), never by the AI.
INJECTED_PARAMS = ('userId', 'storeId')


def mcp_tool(*audiences: str):
    """List the route as an MCP tool for `audiences` (see MCP_AUDIENCES).
    Goes OUTSIDE @route_config."""
    if not audiences:
        raise ValueError('mcp_tool needs at least one audience')
    unknown = [a for a in audiences if a not in MCP_AUDIENCES]
    if unknown:
        raise ValueError(f'Unknown MCP audience {unknown}: '
                         f'use {MCP_AUDIENCES}')
    if len(set(audiences)) != len(audiences):
        raise ValueError('mcp_tool lists an audience twice')

    def decorator(func):
        func.mcpAudiences = tuple(audiences)
        return func

    return decorator
