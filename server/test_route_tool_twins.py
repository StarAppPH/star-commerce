"""A route and a tool doing one job share one action and never drift apart.

A route serves a person, a tool serves the AI (mcpTools.py): they never
stack. When both exist for one job, the tool names its route in `twinOf`,
and the two are kept twins here.

Covers:
  - Pairs come only from `twinOf`. For each pair: twinOf is a real
    ApiRequests route, the tool is named as its route, both bodies are
    exactly `return <action>(<Model>, ...)` after the docstring (the model
    each one's own responseModel), both call the same action (resolved
    through each function's globals), with the same arguments after the
    model, and both have the same signature. The action is a module-level
    function, neither a route nor a tool.
  - Every tool is that one statement. A tool with twinOf=None is refused
    when (a) a route has its name, or (b) any route calls its action, at
    any statement of its body (an undeclared twin); a tool with a twin when
    a route other than that twin calls its action. Routes are indexed by
    every module-level action their body calls (each call resolved through
    the route's globals). And (c) a route is the twin of at most one tool.
  - The lookalike detector: a route that is no twin, whose body is close to
    the action of a twinOf=None tool, is likely the same job written twice.
    A = difflib ratio over normalised AST tokens (locals and params become
    V, constants their type, attribute, keyword and global names kept, the
    docstring dropped), B = the Jaccard index of the names called; a pair
    is flagged when A >= 0.85 and B >= 0.5 and both have at least 25
    tokens. A flagged pair that is truly two jobs goes in NOT_TWINS with
    its reason; an entry no longer flagged fails as stale.
  - Fixture tests drive the same functions on small fixtures: the
    decorator's refusals, each check failing, a valid pair passing, a
    renamed copy-paste caught and an unrelated pair left alone.
"""
import ast
import difflib
import inspect
import textwrap
from collections import defaultdict

import pytest

from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from mcpTools import mcp_tool
from objects.base import ResponseModel
from route_config import route_config

LOOKALIKE_RATIO = 0.85
LOOKALIKE_CALLS = 0.5
LOOKALIKE_MIN_TOKENS = 25

# (route, tool) -> why the detector's lookalike is two jobs, not twins.
NOT_TWINS: dict[tuple[str, str], str] = {}


# ---- Reading the code ----

def _members(root: type, stamp: str) -> dict:
    """Name -> function of every member of `root` (and its bases, in MRO
    order) carrying `stamp`: httpMethod for a route, mcpAudiences for a
    tool."""
    found = {}
    for cls in root.__mro__:
        for name, func in vars(cls).items():
            if callable(func) and hasattr(func, stamp):
                found.setdefault(name, func)
    return found


def routes() -> dict:
    return _members(ApiRequests, 'httpMethod')


def tools() -> dict:
    return _members(ToolRequests, 'mcpAudiences')


def functionDef(source: str) -> ast.FunctionDef:
    """The def in `source`, a function's own source (indented or not)."""
    try:
        return ast.parse(textwrap.dedent(source)).body[0]
    except IndentationError:
        # A docstring line left of the def stops dedent.
        return ast.parse('if 1:\n' + source).body[0].body[0]


def _withoutDocstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def sourceOf(func) -> str:
    return inspect.getsource(inspect.unwrap(func))


def bodyOf(func) -> list[ast.stmt]:
    """The function's statements after its docstring."""
    return _withoutDocstring(functionDef(sourceOf(func)).body)


def _globals(func) -> dict:
    return inspect.unwrap(func).__globals__


def isModuleLevelAction(action) -> bool:
    """A plain function of its module, neither a route nor a tool."""
    return (inspect.isfunction(action)
            and action.__qualname__ == action.__name__
            and not hasattr(action, 'httpMethod')
            and not hasattr(action, 'mcpAudiences'))


def thinProblem(func) -> str | None:
    """None when the body is exactly `return <action>(<Model>, ...)`, the
    model the function's own responseModel and the action a module-level
    function; else what is wrong."""
    name = func.__name__
    shape = (f'{name}: the body must be exactly '
             '`return <action>(<Model>, <params...>)` after the docstring')
    body = bodyOf(func)
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return shape
    call = body[0].value
    if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
            or not call.args or not isinstance(call.args[0], ast.Name)):
        return shape
    names = _globals(func)
    if names.get(call.args[0].id) is not getattr(func, 'responseModel', None):
        return (f'{name}: the first argument ({call.args[0].id}) must be its '
                'own responseModel')
    if not isModuleLevelAction(names.get(call.func.id)):
        return (f'{name}: {call.func.id} must be a module-level action, '
                'neither a route nor a tool')
    return None


def _call(func) -> ast.Call:
    return bodyOf(func)[0].value


def actionOf(func):
    """The action a thin function calls, resolved in its own globals."""
    return _globals(func)[_call(func).func.id]


def actionsCalled(func) -> set:
    """Every module-level action the body calls, at any statement: `f(...)`
    resolved through the function's globals, `module.f(...)` through the
    module it names there."""
    names = _globals(func)
    found = set()
    for statement in bodyOf(func):
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            called = None
            if isinstance(node.func, ast.Name):
                called = names.get(node.func.id)
            elif (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and inspect.ismodule(names.get(node.func.value.id))):
                called = getattr(names[node.func.value.id], node.func.attr,
                                 None)
            if isModuleLevelAction(called):
                found.add(called)
    return found


def _argsAfterModel(func) -> list[str]:
    call = _call(func)
    return ([ast.dump(arg) for arg in call.args[1:]]
            + [ast.dump(keyword) for keyword in call.keywords])


# ---- The checks ----

def pairProblems(tool, routesByName: dict) -> list[str]:
    """What keeps `tool` and its twinOf route from being twins."""
    name = tool.__name__
    route = tool.twinOf
    problems = []
    if routesByName.get(route.__name__) is not route:
        problems.append(f'{name}: twinOf {route.__qualname__} is not an '
                        'ApiRequests route')
    if route.__name__ != name:
        problems.append(f'{name}: its twin is the route {route.__name__}: '
                        'name the tool as its route')
    thin = [p for p in (thinProblem(tool), thinProblem(route)) if p]
    if thin:
        return problems + thin
    if actionOf(tool) is not actionOf(route):
        problems.append(f'{name}: the tool calls {actionOf(tool).__name__}, '
                        f'the route {actionOf(route).__name__}: twins call '
                        'the same action')
    if _argsAfterModel(tool) != _argsAfterModel(route):
        problems.append(f'{name}: the tool and the route pass different '
                        'arguments after the model')
    if inspect.signature(tool) != inspect.signature(route):
        problems.append(f'{name}: the tool {inspect.signature(tool)} and the '
                        f'route {inspect.signature(route)} differ in '
                        'signature')
    return problems


# Twins are single-statement, so a route calling a tool's action after
# another statement is never a valid twin: the (b) message offers both ways.
_TWIN_FIXES = ('give the route its own action (as getWaitingOnYou does), or '
               'make it a single-statement declared twin (the route\'s body '
               'exactly `return <action>(<Model>, ...)`, the tool\'s '
               'twinOf=<route>)')


def toolProblems(toolsByName: dict, routesByName: dict) -> list[str]:
    """Every tool is thin; and (a), (b), (c) of the module docstring."""
    routesByAction = defaultdict(list)
    for routeName, route in routesByName.items():
        for action in actionsCalled(route):
            routesByAction[action].append(routeName)
    twinsOf = defaultdict(list)
    problems = []
    for name, tool in toolsByName.items():
        thin = thinProblem(tool)
        if thin:
            problems.append(thin)
        if tool.twinOf is not None:
            twinsOf[tool.twinOf].append(name)
            if thin is None:
                others = sorted(
                    routeName
                    for routeName in routesByAction.get(actionOf(tool), [])
                    if routesByName[routeName] is not tool.twinOf)
                if others:
                    problems.append(
                        f'{name}: the route(s) {others} call its action '
                        f'{actionOf(tool).__name__} too, but its twin is '
                        f'{tool.twinOf.__name__}: {_TWIN_FIXES}')
            continue
        if name in routesByName:
            problems.append(f'{name}: a route has this name: declare it '
                            f'(twinOf=<route>) or rename it')
        if thin is None and actionOf(tool) in routesByAction:
            problems.append(
                f'{name}: the route(s) '
                f'{sorted(routesByAction[actionOf(tool)])} '
                f'call its action {actionOf(tool).__name__} too: '
                f'{_TWIN_FIXES}')
    for route, names in twinsOf.items():
        if len(names) > 1:
            problems.append(f'{route.__name__}: the twin of more than one '
                            f'tool ({sorted(names)}): a route has at most one')
    return problems


# ---- The lookalike detector ----

def _localNames(node: ast.FunctionDef) -> set[str]:
    """Its params, and every name bound inside it."""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.Name) and not isinstance(child.ctx,
                                                            ast.Load):
            names.add(child.id)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and child is not node):
            names.add(child.name)
        elif isinstance(child, ast.alias):
            names.add((child.asname or child.name).split('.')[0])
    return names


def normalisedTokens(source: str) -> list[str]:
    """The body (docstring dropped) as AST tokens in source order: locals
    and params V, constants their type, attribute, keyword and global names
    kept."""
    node = functionDef(source)
    local = _localNames(node)
    tokens = []

    def visit(n):
        if isinstance(n, ast.expr_context):
            return
        if isinstance(n, ast.Name):
            tokens.append('V' if n.id in local else n.id)
            return
        if isinstance(n, ast.Constant):
            tokens.append(type(n.value).__name__)
            return
        tokens.append(type(n).__name__)
        if isinstance(n, ast.Attribute):
            tokens.append(n.attr)
        elif isinstance(n, ast.keyword) and n.arg:
            tokens.append(n.arg)
        elif isinstance(n, (ast.arg, ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            tokens.append('V')
        for child in ast.iter_child_nodes(n):
            visit(child)

    for statement in _withoutDocstring(node.body):
        visit(statement)
    return tokens


def calledNames(source: str) -> set[str]:
    """The names the body calls: `f(...)` -> f, `x.f(...)` -> f."""
    names = set()
    for statement in _withoutDocstring(functionDef(source).body):
        for child in ast.walk(statement):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    names.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    names.add(child.func.attr)
    return names


def similarity(sourceA: str, sourceB: str) -> tuple[float, float, int, int]:
    """(A, B, tokens of a, tokens of b)."""
    a, b = normalisedTokens(sourceA), normalisedTokens(sourceB)
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    callsA, callsB = calledNames(sourceA), calledNames(sourceB)
    union = callsA | callsB
    calls = len(callsA & callsB) / len(union) if union else 0.0
    return ratio, calls, len(a), len(b)


def isLookalike(ratio: float, calls: float, tokensA: int,
                tokensB: int) -> bool:
    return (ratio >= LOOKALIKE_RATIO and calls >= LOOKALIKE_CALLS
            and min(tokensA, tokensB) >= LOOKALIKE_MIN_TOKENS)


def lookalikeScores(routeSources: dict[str, str],
                    actionSources: dict[str, str]) -> list[tuple]:
    """(route, tool, A, B, tokens, tokens) for every pair, highest A
    first."""
    scores = [(route, tool) + similarity(routeSource, actionSource)
              for route, routeSource in routeSources.items()
              for tool, actionSource in actionSources.items()]
    return sorted(scores, key=lambda score: (score[2], score[3]),
                  reverse=True)


def lookalikes(routeSources: dict[str, str],
               actionSources: dict[str, str]) -> set[tuple[str, str]]:
    return {(route, tool)
            for route, tool, *score in lookalikeScores(routeSources,
                                                       actionSources)
            if isLookalike(*score)}


def staleNotTwins(notTwins: dict, flagged: set) -> list[tuple[str, str]]:
    return sorted(pair for pair in notTwins if pair not in flagged)


def realDetectorSources() -> tuple[dict[str, str], dict[str, str]]:
    """Routes no tool names as its twin, and the actions of the twinOf=None
    tools (thin ones only: a tool that is not thin fails on its own)."""
    allTools = tools()
    twinned = {tool.twinOf for tool in allTools.values()}
    routeSources = {name: sourceOf(route) for name, route in routes().items()
                    if route not in twinned}
    actionSources = {name: sourceOf(actionOf(tool))
                     for name, tool in allTools.items()
                     if tool.twinOf is None and thinProblem(tool) is None}
    return routeSources, actionSources


# ---- The real checks ----

def _twins() -> list:
    return [tool for tool in tools().values() if tool.twinOf is not None]


def test_theTwinsAreTheDeclaredOnes():
    assert sorted(tool.__name__ for tool in _twins()) == [
        'getMyStorePolicy', 'getUserStore']


def test_theRealCounts_arePinned():
    """36 tools serve the AI, 19 routes the people (ApiRequests): a tool or
    route added or removed is a decision, not an accident."""
    assert len(tools()) == 36
    assert len(routes()) == 19


@pytest.mark.parametrize('tool', _twins(), ids=lambda tool: tool.__name__)
def test_eachTwinPair_sharesOneAction(tool):
    problems = pairProblems(tool, routes())
    assert not problems, '\n'.join(problems)


def test_everyTool_isThin_andNoUndeclaredTwin():
    problems = toolProblems(tools(), routes())
    assert not problems, '\n'.join(problems)


def test_noRouteLooksLikeAToolsAction():
    flagged = lookalikes(*realDetectorSources())
    unexplained = sorted(flagged - set(NOT_TWINS))
    assert not unexplained, (
        f'{unexplained}: each route reads like the action of a twinOf=None '
        'tool. Make them twins (one action, twinOf=<route>), or add the pair '
        'to NOT_TWINS with why they are two jobs')


def test_notTwins_isNotStale():
    allRoutes, allTools = routes(), tools()
    unknown = sorted(pair for pair in NOT_TWINS
                     if pair[0] not in allRoutes or pair[1] not in allTools)
    assert not unknown, f'{unknown}: NOT_TWINS names a missing route or tool'
    stale = staleNotTwins(NOT_TWINS, lookalikes(*realDetectorSources()))
    assert not stale, (f'{stale}: no longer flagged by the detector: remove '
                       'them from NOT_TWINS')


# ---- Fixtures ----

class _FxAnswer(ResponseModel):
    ok: bool


class _FxOtherAnswer(ResponseModel):
    ok: bool


_TEXTS = ['Doing one thing', 'Doing another thing', 'Doing a third thing']


def fxGetThing(model, userId: str) -> dict:
    return {'ok': True}


def fxOtherThing(model, userId: str) -> dict:
    return {'ok': False}


def fxOwnThing(model, userId: str) -> dict:
    return {'ok': True}


def fxTwoArgs(model, userId: str, flag: str | None) -> dict:
    return {'ok': flag is None}


def fxIffyThing(model, userId: str) -> dict:
    return {'ok': True}


def fxSignatureThing(model, userId: str) -> dict:
    return {'ok': True}


def fxThirdThing(model, userId: str) -> dict:
    return {'ok': True}


def _tool(twinOf, responseModel=_FxAnswer):
    return mcp_tool('admin', thinkingText=_TEXTS,
                    responseModel=responseModel, twinOf=twinOf)


def _route(responseModel=_FxAnswer):
    return route_config(httpMethod='POST', jwtRequired=True,
                        responseModel=responseModel)


# A tool and a route a body might call in place of an action (each its
# own function: route_config stamps the function it decorates).
@_tool(twinOf=None)
def fxToolAsAction(model, userId: str) -> dict:
    return {'ok': True}


@_route()
def fxRouteAsAction(model, userId: str) -> dict:
    return {'ok': True}


class _FxRoutes():

    @_route()
    def fxGetThing(self, userId: str) -> dict:
        """The route's docstring."""
        return fxGetThing(_FxAnswer, userId)

    @_route()
    def fxIffy(self, userId: str) -> dict:
        if not userId:
            raise ValueError('no user')
        return fxIffyThing(_FxAnswer, userId)

    @_route()
    def fxTwoArgs(self, userId: str) -> dict:
        return fxTwoArgs(_FxAnswer, userId, None)

    @_route()
    def fxSignature(self, userId: str) -> dict:
        return fxSignatureThing(_FxAnswer, userId)

    @_route()
    def fxOtherAction(self, userId: str) -> dict:
        return fxThirdThing(_FxAnswer, userId)

    @_route(_FxOtherAnswer)
    def fxOwnModel(self, userId: str) -> dict:
        return fxOwnThing(_FxOtherAnswer, userId)


_FX_ROUTES = _members(_FxRoutes, 'httpMethod')


class _FxTools():

    @_tool(twinOf=_FxRoutes.fxGetThing)
    def fxGetThing(self, userId: str) -> dict:
        """The tool's docstring."""
        return fxGetThing(_FxAnswer, userId)

    @_tool(twinOf=_FxRoutes.fxIffy)
    def fxIffy(self, userId: str) -> dict:
        return fxIffyThing(_FxAnswer, userId)

    @_tool(twinOf=_FxRoutes.fxTwoArgs)
    def fxTwoArgs(self, userId: str) -> dict:
        return fxTwoArgs(_FxAnswer, userId, 'x')

    @_tool(twinOf=_FxRoutes.fxSignature)
    def fxSignature(self, userId: str, storeId: str | None = None) -> dict:
        return fxSignatureThing(_FxAnswer, userId)

    @_tool(twinOf=_FxRoutes.fxOtherAction)
    def fxOtherAction(self, userId: str) -> dict:
        return fxOtherThing(_FxAnswer, userId)

    @_tool(twinOf=_FxRoutes.fxGetThing)
    def fxRenamed(self, userId: str) -> dict:
        return fxGetThing(_FxAnswer, userId)

    @_tool(twinOf=None)
    def fxSolo(self, userId: str) -> dict:
        return fxOtherThing(_FxAnswer, userId)

    @_tool(twinOf=None)
    def fxSharesTheRoutesAction(self, userId: str) -> dict:
        return fxGetThing(_FxAnswer, userId)

    @_tool(twinOf=None)
    def fxThick(self, userId: str) -> dict:
        answer = fxOtherThing(_FxAnswer, userId)
        return answer


class _FxNamedLikeARoute():

    @_tool(twinOf=None)
    def fxOwnModel(self, userId: str) -> dict:
        return fxOtherThing(_FxAnswer, userId)


class _FxSecondTwin():

    @_tool(twinOf=_FxRoutes.fxGetThing)
    def fxGetThing(self, userId: str) -> dict:
        return fxGetThing(_FxAnswer, userId)


class _FxMoreRoutes():

    @_route()
    def fxCheckedFirst(self, userId: str) -> dict:
        if not userId:
            raise ValueError('no user')
        answer = fxOtherThing(_FxAnswer, userId)
        return answer

    @_route()
    def fxGetThingAgain(self, userId: str) -> dict:
        return fxGetThing(_FxAnswer, userId)


_FX_MORE_ROUTES = _members(_FxMoreRoutes, 'httpMethod')


def _problems(tool) -> str:
    return '\n'.join(pairProblems(tool, _FX_ROUTES))


def test_fixture_aValidPair_passes():
    assert pairProblems(_FxTools.fxGetThing, _FX_ROUTES) == []
    assert toolProblems({'fxGetThing': _FxTools.fxGetThing,
                         'fxSolo': _FxTools.fxSolo}, _FX_ROUTES) == []


def test_fixture_twinOfNotARoute_fails():
    assert 'is not an ApiRequests route' in pairProblems(
        _FxTools.fxGetThing, {})[0]


def test_fixture_d_aTwinNamedUnlikeItsRoute_fails():
    assert 'name the tool as its route' in _problems(_FxTools.fxRenamed)


def test_fixture_anExtraIf_fails():
    assert 'must be exactly `return <action>' in _problems(_FxTools.fxIffy)


def test_fixture_differentArgs_fail():
    assert 'different arguments after the model' in _problems(
        _FxTools.fxTwoArgs)


def test_fixture_differentSignatures_fail():
    assert 'differ in signature' in _problems(_FxTools.fxSignature)


def test_fixture_differentActions_fail():
    assert 'twins call the same action' in _problems(_FxTools.fxOtherAction)


def test_fixture_aModelNotItsOwn_fails():
    @_tool(twinOf=None)
    def fxWrongModel(self, userId: str) -> dict:
        return fxGetThing(_FxOtherAnswer, userId)

    assert 'must be its own responseModel' in thinProblem(fxWrongModel)


def test_fixture_aRouteOrToolAsTheAction_fails():
    @_tool(twinOf=None)
    def fxCallsATool(self, userId: str) -> dict:
        return fxToolAsAction(_FxAnswer, userId)

    @_tool(twinOf=None)
    def fxCallsARoute(self, userId: str) -> dict:
        return fxRouteAsAction(_FxAnswer, userId)

    assert 'must be a module-level action' in thinProblem(fxCallsARoute)
    assert 'must be a module-level action' in thinProblem(fxCallsATool)


def test_fixture_aThickTool_fails():
    assert 'must be exactly `return <action>' in '\n'.join(toolProblems(
        {'fxThick': _FxTools.fxThick}, _FX_ROUTES))


def test_fixture_a_aSoloToolNamedLikeARoute_fails():
    problems = toolProblems({'fxOwnModel': _FxNamedLikeARoute.fxOwnModel},
                            _FX_ROUTES)
    assert any('a route has this name' in p for p in problems), problems


def test_fixture_b_aSoloToolWhoseActionAThinRouteCalls_fails():
    problems = toolProblems(
        {'fxSharesTheRoutesAction': _FxTools.fxSharesTheRoutesAction},
        _FX_ROUTES)
    assert any("call its action fxGetThing too" in p
               for p in problems), problems


def test_fixture_b_aRouteCallingASoloToolsAction_pastItsFirstStatement_fails():
    """A route that checks first and calls the action mid-body is still an
    undeclared twin."""
    problems = toolProblems({'fxSolo': _FxTools.fxSolo}, _FX_MORE_ROUTES)
    assert any("['fxCheckedFirst'] call its action fxOtherThing too" in p
               for p in problems), problems
    assert any('give the route its own action (as getWaitingOnYou does), or '
               'make it a single-statement declared twin' in p
               for p in problems), problems


def test_fixture_b_aSecondRouteCallingATwinnedToolsAction_fails():
    problems = toolProblems({'fxGetThing': _FxTools.fxGetThing},
                            {**_FX_ROUTES, **_FX_MORE_ROUTES})
    assert any("['fxGetThingAgain'] call its action fxGetThing too, but its "
               'twin is fxGetThing' in p for p in problems), problems
    assert any('give the route its own action (as getWaitingOnYou does), or '
               'make it a single-statement declared twin' in p
               for p in problems), problems


def test_fixture_actionsCalled_findsEveryActionTheBodyCalls():
    assert actionsCalled(_FxMoreRoutes.fxCheckedFirst) == {fxOtherThing}
    assert actionsCalled(_FxRoutes.fxGetThing) == {fxGetThing}


def test_fixture_c_aRouteTwinOfTwoTools_fails():
    problems = toolProblems({'fxGetThing': _FxTools.fxGetThing,
                             'fxRenamed': _FxTools.fxRenamed}, _FX_ROUTES)
    assert any('the twin of more than one tool' in p
               for p in problems), problems
    problems = toolProblems({'one': _FxTools.fxGetThing,
                             'two': _FxSecondTwin.fxGetThing}, _FX_ROUTES)
    assert any('the twin of more than one tool' in p
               for p in problems), problems


# ---- Fixtures: the decorator refuses ----

def _undecorated(self, userId: str) -> dict:
    return fxGetThing(_FxAnswer, userId)


@pytest.mark.parametrize('twinOf, problem', [
    ('fxGetThing', 'twinOf is the route function'),
    (42, 'twinOf is the route function'),
    (_undecorated, 'without @route_config'),
    (_FxTools.fxSolo, 'names a tool, not a route'),
])
def test_fixture_mcpTool_refusesABadTwinOf(twinOf, problem):
    with pytest.raises(ValueError, match=problem):
        _tool(twinOf=twinOf)(_undecorated)


def test_fixture_mcpTool_needsTwinOf():
    with pytest.raises(TypeError, match='twinOf'):
        mcp_tool('admin', thinkingText=_TEXTS, responseModel=_FxAnswer)


def test_fixture_mcpTool_refusesStackingOverARoute():
    with pytest.raises(ValueError, match='a tool is no route'):
        _tool(twinOf=None)(_FxRoutes.fxGetThing)


def test_fixture_mcpTool_stampsTheTwin():
    assert _FxTools.fxGetThing.twinOf is _FxRoutes.fxGetThing
    assert _FxTools.fxSolo.twinOf is None
    assert _FxTools.fxGetThing.responseModel is _FxAnswer


# ---- Fixtures: the lookalike detector ----

_ROUTE_SOURCE = '''
def payTheOrder(self, userId: str, storeId: str, orderId: str) -> dict:
    """Pay the order."""
    requireCustomer(userId)
    order = db.read({'_id': orderId, 'storeId': storeId}, ORDERS,
                    findOne=True)
    if order is None:
        raise ValueError(ORDER_NOT_FOUND)
    if order['status'] != 'pending':
        raise ValueError('Only a pending order can be paid')
    updated = db.update({'_id': order['_id'], '_version': order['_version']},
                        {'status': 'paid'}, ORDERS)
    return projectedCopy(OrderRow, updated)
'''

# The same body, its locals and params renamed and the model a param.
_RENAMED_COPY = '''
def payOrder(model, customer: str, shop: str, theOrder: str) -> dict:
    requireCustomer(customer)
    found = db.read({'_id': theOrder, 'storeId': shop}, ORDERS,
                    findOne=True)
    if found is None:
        raise ValueError(ORDER_NOT_FOUND)
    if found['status'] != 'pending':
        raise ValueError('Not pending')
    saved = db.update({'_id': found['_id'], '_version': found['_version']},
                      {'status': 'paid'}, ORDERS)
    return projectedCopy(model, saved)
'''

_UNRELATED = '''
def listVoices(model, userId: str) -> dict:
    requireAdmin(userId)
    voices = []
    for voice in voiceModel.fetchVoices(timeout=10):
        if voice.get('category') in ALLOWED:
            voices.append({'voiceId': voice['voice_id'],
                           'name': voice['name']})
    voices.sort(key=lambda v: v['name'])
    return {'voices': voices[:MAX_VOICES]}
'''

_SHORT_ROUTE = '''
def a(self, userId: str) -> dict:
    return thing(Model, userId)
'''

_SHORT_ACTION = '''
def b(model, userId: str) -> dict:
    return thing(Model, userId)
'''


def test_fixture_tokens_dropTheDocstring_andNormalise():
    tokens = normalisedTokens(_ROUTE_SOURCE)
    assert 'Pay the order.' not in tokens
    assert 'order' not in tokens and 'userId' not in tokens
    assert {'V', 'str', 'db', 'read', 'ORDERS', 'findOne',
            'requireCustomer'} <= set(tokens)


def test_fixture_aRenamedCopyPaste_isCaught():
    ratio, calls, a, b = similarity(_ROUTE_SOURCE, _RENAMED_COPY)
    assert isLookalike(ratio, calls, a, b), (ratio, calls, a, b)
    assert lookalikes({'payTheOrder': _ROUTE_SOURCE},
                      {'payOrder': _RENAMED_COPY}) == {
                          ('payTheOrder', 'payOrder')}


def test_fixture_anUnrelatedPair_isNotCaught():
    assert lookalikes({'payTheOrder': _ROUTE_SOURCE},
                      {'listVoices': _UNRELATED}) == set()


def test_fixture_shortBodies_areBelowTheFloor():
    ratio, calls, a, b = similarity(_SHORT_ROUTE, _SHORT_ACTION)
    assert ratio >= LOOKALIKE_RATIO and calls >= LOOKALIKE_CALLS
    assert min(a, b) < LOOKALIKE_MIN_TOKENS
    assert not isLookalike(ratio, calls, a, b)


def test_fixture_aStaleNotTwinsEntry_fails():
    flagged = lookalikes({'payTheOrder': _ROUTE_SOURCE},
                         {'payOrder': _RENAMED_COPY,
                          'listVoices': _UNRELATED})
    notTwins = {('payTheOrder', 'payOrder'): 'two jobs',
                ('payTheOrder', 'listVoices'): 'no longer alike'}
    assert staleNotTwins(notTwins, flagged) == [
        ('payTheOrder', 'listVoices')]
