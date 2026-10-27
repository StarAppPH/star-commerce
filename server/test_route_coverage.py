"""Every route the server serves is exercised by at least one test.

A route is a public method carrying @route_config metadata (httpMethod) on
ApiRequests or any class it inherits from, or on PubSubRequests (the push
handlers) — the same sets AppCreator turns into app.py and appPubSub.py
routes. It counts as tested when some test_*.py file calls it as
an attribute (`api.routeName(...)`, `ApiRequests().routeName(...)`) or
requests its URL path ('/routeName'), outside a `with pytest.raises(...)`
block: a call that only expects an error does not test the route.

This file is identical in every Star App repo.
"""
import ast
from pathlib import Path

import pytest

from ApiRequests import ApiRequests
from PubSubRequests import PubSubRequests

SERVER_DIR = Path(__file__).resolve().parent


def _routeNames() -> dict[str, str]:
    """Route name -> the class that defines it, in MRO order: every public
    @route_config method of ApiRequests (and its mixins) and of
    PubSubRequests (the push handlers)."""
    routes = {}
    for root in (ApiRequests, PubSubRequests):
        for cls in root.__mro__:
            for name, func in vars(cls).items():
                if (callable(func) and not name.startswith('_')
                        and hasattr(func, 'httpMethod')):
                    routes.setdefault(name, cls.__name__)
    return routes


def _isPytestRaises(item: ast.withitem) -> bool:
    call = item.context_expr
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == 'raises')


class _RouteUseFinder(ast.NodeVisitor):
    """Collects attribute calls and '/path' strings, skipping
    `with pytest.raises(...)` blocks."""

    def __init__(self):
        self.used: set[str] = set()

    def visit_With(self, node: ast.With):
        if any(_isPytestRaises(item) for item in node.items):
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute):
            self.used.add(node.func.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str) and node.value.startswith('/'):
            self.used.add(node.value.strip('/').split('?')[0])


@pytest.fixture(scope='module')
def testTrees():
    """(path, AST) of every test_*.py file, parsed once for this module."""
    return [(path, ast.parse(path.read_bytes(), filename=str(path)))
            for path in sorted(SERVER_DIR.glob('test_*.py'))]


def _usedNames(testTrees) -> set[str]:
    finder = _RouteUseFinder()
    for _, tree in testTrees:
        finder.visit(tree)
    return finder.used


def test_everyRoute_isCalledByATest(testTrees):
    used = _usedNames(testTrees)
    untested = [f'{name} ({owner})' for name, owner in _routeNames().items()
                if name not in used]
    assert not untested, (
        'Routes no test calls (as api.<route>(...) or a request to '
        '/<route>, outside pytest.raises):\n  ' + '\n  '.join(untested))


def _calledName(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ''


def _checksSomething(func: ast.AST) -> bool:
    """True when the test body asserts: an assert statement, a
    pytest.raises block, or a call to an assert helper — a mock's assert_*
    (assert_called_once_with, ...) or a test helper named assert*/_assert*
    (assertExactShape, _assertScanProgressPublishes, ...)."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.With) and any(
                _isPytestRaises(item) for item in node.items):
            return True
        if (isinstance(node, ast.Call)
                and _calledName(node).lstrip('_').lower().startswith('assert')):
            return True
    return False


def _callsARoute(func: ast.AST, routes: set[str]) -> bool:
    return any(isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute)
               and node.func.attr in routes
               for node in ast.walk(func))


def _testFunctions(tree: ast.Module):
    """Module-level test_* functions and test_* methods of Test* classes."""
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith('test_')):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith('test_')):
                    yield f'{node.name}.{item.name}', item


def test_everyTestThatCallsARoute_checksSomething(testTrees):
    """A test that calls a route but asserts nothing only proves the route
    does not crash. Every test_* function that calls a route directly must
    also assert, expect an error with pytest.raises, or check a mock with
    assert_*(). Helpers and fixtures are not checked; the test using them
    is."""
    routes = set(_routeNames())
    unchecked = []
    for path, tree in testTrees:
        for name, func in _testFunctions(tree):
            if _callsARoute(func, routes) and not _checksSomething(func):
                unchecked.append(f'{path.name}::{name} (line {func.lineno})')
    assert not unchecked, (
        'Tests that call a route but check nothing (add an assert on the '
        'result or the stored data):\n  ' + '\n  '.join(unchecked))


def test_routeNames_findsTheRoutes():
    """Guards the scan itself: an empty route set would pass vacuously."""
    routes = _routeNames()
    assert routes
    assert all(not name.startswith('_') for name in routes)
