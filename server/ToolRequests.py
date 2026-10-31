"""
ToolRequests.py — the AI's tools, beside ApiRequests (the people's routes).

Each method marked @mcp_tool (mcpTools.py) is a tool of the generated
mcpServer.py (mcpCreator.py) and of the chat route (chatToolsCreator.py).
Tool classes live in their domain's actions module, under its route class
(`class PolicyTools` under `class PolicyActions`), and are combined here
the way ApiRequests combines the route classes. A tool is never a route:
ApiRequests never mixes a tool class in.

A tool is one statement, `return <action>(<Model>, <params...>)`, with the
docstring the AI reads: the action is a module-level function of the same
module, taking the model first. A route doing the same job calls the same
action with its own model, and the tool names it in `twinOf`
(test_route_tool_twins.py).

ADDING A NEW TOOL CLASS: define it in its actions module, under the route
class, and add it to the ToolRequests bases below.
"""

from actions.FulfillmentActions import FulfillmentTools
from actions.OrderActions import OrderTools
from actions.PaymentActions import PaymentTools
from actions.PhotoActions import PhotoTools
from actions.PolicyActions import PolicyTools
from actions.ProductActions import ProductTools
from actions.ReturnActions import ReturnTools
from actions.StoreActions import StoreTools
from actions.VoiceActions import VoiceTools


class ToolRequests(StoreTools, ProductTools, OrderTools, PaymentTools,
                   FulfillmentTools, ReturnTools, PolicyTools, VoiceTools,
                   PhotoTools):
    pass
