"""
Pub/Sub contract and integration tests for starCommerce.

The canonical narrative (flow diagram, file map, rules, the "adding a new
publisher / consumer handler" checklists) is ARCHITECTURE.md, section
"Pub/Sub mock messages & contract tests". This docstring covers what THIS
file needs you to know.

WHY THIS EXISTS
---------------
Services in this architecture talk to each other through Google Cloud Pub/Sub:
an ApiRequests.py method decorated with @pubSubDecorator publishes its RETURN
DICT as a message, and each target repo receives it in a PubSubRequests.py
handler named `<function>_<publisherProjectName>`. Nothing at runtime checks
that the payload a publisher emits is the payload a consumer expects, so this
suite replays real recorded messages through the real handlers instead, and
statically checks that publishers, mock data and handlers line up 1:1.

HOW A MOCK MESSAGE TRAVELS
--------------------------

  PUBLISHER REPO                                CONSUMER REPO (this one)
  ------------------------------------------    -------------------------------------
  ApiRequests.createOrder(..., publishToProdPubSub=False,
                               publishToPubSubMockDb=True)
      decorated with
      @pubSubDecorator(projectNameConsumers=[<service pointing at this repo>])
            |
            |  pubSubMockDataGenerator.py calls it ONCE
            v
  MockData row in the SHARED local MongoDB database `pubSubMockDb`,
  collection `<publisher projectName>`, with
      projectNameConsumers = [each consumer service's getProjectName()]
      consumeFunctionName  = 'createOrder_<publisher projectName>'
      index                = position in the publisher's emit order
            |
            |  this file's test_pubSub_replayToThisProject reads every row
            |  whose projectNameConsumers contains Project().projectName,
            |  sorted by `index`, and for each one runs
            |      seed(payload)                                  (REPLAY_STEPS)
            |      mockDataConsumer.consumeMockData(publisherProjectName,
            |          PubSubRequests().<consumeFunctionName>)
            |      check(payload, res)                            (REPLAY_STEPS)
            |      redelivery of the same payload            (REDELIVERY_STEPS)
            v
  PubSubRequests.createOrder_<publisher>(data)  <-- the real handler, writing
                                                    to the real local test DB
            |
            v
  test_atExitValidation: FAILS if any row addressed to this project was never
  consumed in this process (a message with no handler, or a handler nobody
  replayed).

THE FIVE TESTS
--------------
  test_ifAllApiRequestTested
      Every public PubSubRequests method (except UNTESTED_PUBSUB_FUNCTIONS) is
      referenced from some test_*.py -- which is why REPLAY_STEPS is keyed
      through `PubSubRequests().<handler>.__name__` -- and every
      consumeFunctionName addressed to this project exists as a handler.
  test_ifAllPubSubFunctionsGenerated
      Every @pubSubDecorator function in the top-level server/*.py files
      (non-recursive) is called in pubSubMockDataGenerator.py with the
      literal `publishToPubSubMockDb=True`, and every call site of one in
      those files (PubSubRequests.py excepted) passes the publishToProdPubSub
      keyword explicitly, so a test can never accidentally publish to
      production. The flag RULES themselves -- both parameters present,
      publishToProdPubSub without a default, never both True -- are enforced
      by pubSubDecorator's ValueError at import / call time, not here: a file
      that violates them is skipped by this scan with a warning.
  test_ifAllPubSubMockDataHasFunctions
      The set of handlers (minus UNTESTED_PUBSUB_FUNCTIONS) equals the set of
      consumeFunctionNames addressed to this project: a handler nobody
      publishes to is dead code, a message nobody handles is a lost event.
  test_pubSub_replayToThisProject
      The replay described above, then a wipe.
  test_atExitValidation
      The unconsumed-row check. Must run LAST and in the same process.

THINGS THAT BITE
----------------
  - pubSubMockDb is ONE local MongoDB database shared by every repo on this
    machine. Run every publisher's pubSubMockDataGenerator.py BEFORE this
    suite (a publisher's generator deletes and rebuilds only its own
    collection) and never run two repos' suites or generators concurrently.
  - The generator must have run at least once on this machine: with an empty
    pubSubMockDb the two tests that read it (test_ifAllApiRequestTested,
    test_ifAllPubSubMockDataHasFunctions) refuse to run. This repo's own
    generator creates its `starCommerce` collection even with zero
    publishers, so `python pubSubMockDataGenerator.py` is enough to bootstrap.
  - Rows older than 3 days are rejected (MockData.checkIfValid): re-run the
    publisher's generator, do not touch the row.
  - wipeDatabase() (imported from pubSubMockDataGenerator.py, the one list
    TEST_COLLECTIONS) empties this repo's local test database before and
    after the replay, localdev only. Other repos' suites do the same to their
    own databases and may delete their own pubSubMockDb collection while
    running -- regenerate afterwards.
  - Consumers are addressed by services/<name>.py's getProjectName(), which is
    `projectName=` if set, else the filename. If the target repo's
    AppConfig().projectName differs from the filename, set projectName= on the
    service or its messages can never be consumed;
    test_ifAllServicesMatchConsumerProjectName.py guards this drift.
  - The "Shared AST helpers" and "Static contract tests" regions (between
    their `star-app shared` marker lines) are identical in every repo, and
    test_sharedFiles.py fails when one differs from create-star-app's
    templates/test_pubSubContracts.py.tmpl. Fix the template, commit it,
    then run `python sharedFileCheck.py --write` in every repo -- never
    patch one repo's copy alone. Only the text outside the regions
    (wipeDatabase() and the replay section) is per-repo.
"""
import os
import ast
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from mongoDb import db, pubSubMockDb
from pubSub import mockDataConsumer, MockData
from PubSubRequests import PubSubRequests
from AppConfig import Project
from pubSubMockDataGenerator import TEST_COLLECTIONS, wipeDatabase

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PUBSUB_REQUESTS_FILE = os.path.join(SERVER_DIR, 'PubSubRequests.py')
MOCK_DATA_GENERATOR_FILE = os.path.join(SERVER_DIR,
                                        'pubSubMockDataGenerator.py')

# Consumer functions that intentionally have no mock data / test coverage.
# A handler belongs here ONLY when no publisher will ever address it: `ping`
# is a liveness probe, and `createDeadLetter` (the dead-letter receiver
# AppCreator recognises, if you add one) is fed by GCP's dead-letter topic,
# not by a @pubSubDecorator function. Listing a real handler here hides a
# missing generator run, a missing REPLAY_STEPS entry and a dead handler all
# at once -- do not.
UNTESTED_PUBSUB_FUNCTIONS = ['ping']

# The two `star-app shared` regions below are identical in every repo, and
# test_sharedFiles.py holds them to create-star-app's
# templates/test_pubSubContracts.py.tmpl. Fix them there, commit, then run
# `cd server && python sharedFileCheck.py --write` in every repo.

# >>> star-app shared: Shared AST helpers
# ---------------------------------------------------------------------------
# Shared AST helpers
# ---------------------------------------------------------------------------


def _list_class_methods(filename: str) -> List[str]:
    """
    Parses the given Python file and returns a list of all class method names
    (excluding private and inner functions).
    """
    with open(filename, 'r', encoding='utf-8') as f:
        source = f.read()
    tree = ast.parse(source, filename)
    func_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith('_'):
                        func_names.append(item.name)
    return func_names


def _list_referenced_functions(filename: str) -> List[str]:
    """
    Returns every function name that is called or referenced (attribute load)
    in the file, ignoring anything inside a `with pytest.raises(...)` block.
    """
    with open(filename, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename)
    called: Set[str] = set()

    def get_full_name(node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            parent = get_full_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        else:
            return None

    class _ReferencedNameCollector(ast.NodeVisitor):

        def __init__(self):
            self.inside_pytest_raises = 0

        def visit_With(self, node):
            # Check if this is a 'with pytest.raises(...)' block
            for item in node.items:
                ctx_expr = item.context_expr
                if (isinstance(ctx_expr, ast.Call)
                        and isinstance(ctx_expr.func, ast.Attribute)
                        and isinstance(ctx_expr.func.value, ast.Name)
                        and ctx_expr.func.value.id == 'pytest'
                        and ctx_expr.func.attr == 'raises'):
                    self.inside_pytest_raises += 1
                    for n in node.body:
                        self.visit(n)
                    self.inside_pytest_raises -= 1
                    break
            else:
                self.generic_visit(node)

        def visit_Call(self, node):
            if self.inside_pytest_raises == 0:
                name = get_full_name(node.func)
                if name:
                    called.add(name)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            # Only add if not inside pytest.raises
            if self.inside_pytest_raises == 0:
                parent = getattr(node, 'ctx', None)
                if isinstance(parent, ast.Load):
                    name = get_full_name(node)
                    if name:
                        called.add(name)
            self.generic_visit(node)

    _ReferencedNameCollector().visit(tree)
    return sorted(called)


def _dotted_name(node: ast.AST) -> str:
    """Return a dotted name for a call target (e.g., self.foo, pkg.mod.func)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append("<call>")  # handle nested calls like factory()().foo
    parts.reverse()
    return ".".join(parts) if parts else "<unknown>"


def _function_name(node: ast.AST) -> str:
    """Return only the final function name being called."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return "<unknown>"


def _literal_or_repr(node: ast.AST) -> Any:
    """Try to turn AST node into literal value; fallback to code string."""
    try:
        return ast.literal_eval(node)
    except Exception:
        try:
            return ast.unparse(node)  # Python 3.9+
        except Exception:
            return None


class _CallDetailsCollector(ast.NodeVisitor):
    """Collects every call in a file with its positional/keyword arguments."""

    def __init__(self, only_within: Optional[str] = None):
        self.only_within = only_within
        self._stack: List[str] = []
        self.calls: List[Dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._stack.append(node.name)
        if self.only_within is None or node.name == self.only_within:
            self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        return self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call):
        if self.only_within is not None and (not self._stack or self._stack[-1]
                                             != self.only_within):
            return

        target = node.func
        call_name = _dotted_name(target)
        func_name = _function_name(target)

        pos_args = [_literal_or_repr(a) for a in node.args]
        kw_args = {
            kw.arg: _literal_or_repr(kw.value)
            for kw in node.keywords if kw.arg is not None
        }
        has_kwargs = any(kw.arg is None
                         for kw in node.keywords)  # detects **kwargs

        self.calls.append({
            "call": call_name,  # full dotted path (e.g., "itemA.createUnit")
            "functionCalled":
            func_name,  # just the function (e.g., "createUnit")
            "positional_args": pos_args,
            "keyword_args": kw_args,
            "has_var_kwargs": has_kwargs,
            "lineno": getattr(node, "lineno", None),
        })

        self.generic_visit(node)


def get_called_functions(
        file_path: Union[str, Path],
        within_function: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return all function/method calls in the file (or inside a given function).
    """
    p = Path(file_path)
    if not p.exists() or p.suffix != ".py":
        raise ValueError("Please provide a valid Python (.py) file path")

    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src)
    collector = _CallDetailsCollector(only_within=within_function)
    collector.visit(tree)
    return collector.calls


def getAllFunctionsThatHasPubSubDecorator(
    file_path: str, ) -> List[Tuple[str, str]]:
    """
    Returns a list of function names for all functions decorated with
    @pubSubDecorator, including class methods. Raises if any of them is
    missing the publishToPubSubMockDb / publishToProdPubSub parameters, or if
    publishToProdPubSub has a default value.
    """
    with open(file_path, "r", encoding="utf-8") as file:
        node = ast.parse(file.read(), filename=file_path)

    functions = []

    def visit_body(body, class_prefix="", file_path=''):
        for item in body:
            if isinstance(item, ast.FunctionDef):
                for decorator in item.decorator_list:

                    if (isinstance(decorator, ast.Name)
                            and decorator.id == "pubSubDecorator") or (
                                isinstance(decorator, ast.Call)
                                and isinstance(decorator.func, ast.Name)
                                and decorator.func.id == "pubSubDecorator"):

                        foundArgPublishToPubSubMockDb = False
                        for x in item.args.args:
                            if x.arg == "publishToPubSubMockDb":
                                foundArgPublishToPubSubMockDb = True
                                break

                        if not foundArgPublishToPubSubMockDb:
                            raise AssertionError(
                                f"Function {item.name} does not have the argument publishToPubSubMockDb and is a pubSubDecorator."
                            )

                        for arg, default in zip(
                                item.args.args[-len(item.args.defaults):],
                                item.args.defaults):
                            if arg.arg == 'publishToProdPubSub':
                                raise Exception(
                                    f"in file {file_path}, function {item.name} publishToProdPubSub should have no default value so that we can properly set it."
                                )

                        foundArgPublishProductionPubSub = False
                        for x in item.args.args:
                            if x.arg == "publishToProdPubSub":
                                foundArgPublishProductionPubSub = True
                                break

                        if not foundArgPublishProductionPubSub:
                            raise AssertionError(
                                f"Function {item.name} does not have the argument publishToProdPubSub and is a pubSubDecorator."
                            )

                        functions.append(item.name)
            elif isinstance(item, ast.ClassDef):
                visit_body(item.body,
                           class_prefix=item.name,
                           file_path=file_path)

    visit_body(node.body, file_path=file_path)
    return functions


def get_all_called_functions_with_flag(file_path: str) -> List[str]:
    """
    Returns a list of all function names called in a file that include
    the keyword argument `publishToPubSubMockDb=True`.

    Ignores object/module prefixes. E.g., object.createUnit() becomes createUnit.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        node = ast.parse(f.read(), filename=file_path)

    matched_calls = []

    class CallVisitor(ast.NodeVisitor):

        def visit_Call(self, call_node: ast.Call):
            has_flag = False

            for kw in call_node.keywords:
                if kw.arg == "publishToPubSubMockDb" and isinstance(
                        kw.value, ast.Constant):
                    if kw.value.value is True:
                        has_flag = True
                        break

            if has_flag:
                func = call_node.func
                if isinstance(func, ast.Name):
                    matched_calls.append(func.id)
                elif isinstance(func, ast.Attribute):
                    matched_calls.append(func.attr)

            self.generic_visit(call_node)

    CallVisitor().visit(node)
    return matched_calls


# Folders under server/ that hold no app code.
_NON_SOURCE_DIRS = {
    'venv', '.venv', 'node_modules', '__pycache__', '.git', 'schemas'
}


def _walk_python_files(directory: str) -> List[str]:
    """
    Returns every Python file path under the given directory, recursively
    (so publishers in actions/ and other packages are scanned too), skipping
    _NON_SOURCE_DIRS. Sorted so the scan order is stable.
    """
    python_files = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _NON_SOURCE_DIRS)
        for file in sorted(files):
            if file.endswith(".py"):
                python_files.append(os.path.join(root, file))
    return python_files


def get_all_python_files(directory: str) -> List[str]:
    """
    Returns a list of all non-test Python file paths under the given
    directory, recursively.
    """
    return [
        file for file in _walk_python_files(directory)
        if not os.path.basename(file).startswith("test_")
    ]


def get_all_pubsub_functions_from_workspace() -> List[str]:
    """
    Scans all Python files in the server directory and returns a list of all
    functions decorated with @pubSubDecorator.
    """
    all_pubsub_functions = []
    python_files = get_all_python_files(SERVER_DIR)

    for file_path in python_files:
        try:
            pubsub_functions = getAllFunctionsThatHasPubSubDecorator(file_path)
            all_pubsub_functions.extend(pubsub_functions)
        except Exception as e:
            print(f"Warning: Could not parse {file_path}: {e}")
            continue

    return all_pubsub_functions


def _getConsumedMockFunctionNames() -> Set[str]:
    """
    Reads every collection in pubSubMockDb and returns the set of
    consumeFunctionName values for messages this project consumes.
    Raises if the mock DB has no collections at all.
    """
    allCollections = pubSubMockDb.getAllCollections()

    if len(allCollections) == 0:
        raise Exception(
            "No collections found in pubSubMockDb. Please add mock data to the database."
        )

    projectName = Project().projectName
    functionNames: Set[str] = set()
    for collection in allCollections:
        # '*_devData' are passive capture logs, not consumable mock messages
        if collection.endswith('_devData'):
            continue
        for data in pubSubMockDb.read({'projectNameConsumers': projectName},
                                      collection,
                                      projection={
                                          'consumeFunctionName': 1,
                                          '_id': 0
                                      }):
            functionNames.add(data['consumeFunctionName'])
    return functionNames
# <<< star-app shared: Shared AST helpers


# >>> star-app shared: Static contract tests
# ---------------------------------------------------------------------------
# Static contract tests
# ---------------------------------------------------------------------------


def test_ifAllApiRequestTested():
    """
    Test to ensure that all pubsub functions are tested.
    """
    pubSubFunctions = _list_class_methods(PUBSUB_REQUESTS_FILE)

    allTestFunctions = []
    for testFile in _walk_python_files(SERVER_DIR):
        x = os.path.relpath(testFile, SERVER_DIR)
        if os.path.basename(x).startswith('test_'):
            testFunctions = _list_referenced_functions(testFile)
            print("Test functions found in", x, ":", testFunctions)
            allTestFunctions.extend(testFunctions)

    for x in pubSubFunctions:
        if x in UNTESTED_PUBSUB_FUNCTIONS:
            continue
        if x not in allTestFunctions:
            raise Exception(
                f"PubSub function '{x}' is not tested in any test file. Please add a test for it."
            )

    for x in _getConsumedMockFunctionNames():
        if x not in pubSubFunctions:
            raise Exception(
                f"PubSub function '{x}' is not in PubSubRequests.py Please add the function to receive this message."
            )


def test_ifAllPubSubFunctionsGenerated():
    # Get pubSub functions from all Python files instead of just one file
    pubsubFunctions = get_all_pubsub_functions_from_workspace()
    mockDataGeneratorFunctions = get_all_called_functions_with_flag(
        MOCK_DATA_GENERATOR_FILE)

    for x in pubsubFunctions:
        if x not in mockDataGeneratorFunctions:
            raise AssertionError(
                f"Function {x} is not called in pubSubMockDataGenerator.py or it does not have the parameter publishToPubSubMockDb=True."
            )

    py_files = [
        os.path.relpath(f, SERVER_DIR) for f in _walk_python_files(SERVER_DIR)
    ]

    for file in py_files:
        allFunctions = get_called_functions(os.path.join(SERVER_DIR, file))
        for func in allFunctions:
            if func['functionCalled'] in pubsubFunctions:
                found = False
                for key, value in func['keyword_args'].items():
                    if key == 'publishToProdPubSub':
                        found = True
                        break

                if not found and file != 'PubSubRequests.py':
                    raise AssertionError(
                        f"Function {func['call']} in {file} does not have the parameter publishToProdPubSub."
                    )


def test_ifAllPubSubMockDataHasFunctions():
    pubSubFunctions = list(set(_list_class_methods(PUBSUB_REQUESTS_FILE)))

    for x in UNTESTED_PUBSUB_FUNCTIONS:
        pubSubFunctions.remove(x)

    functionsToTest = list(_getConsumedMockFunctionNames())

    for x in functionsToTest:
        if x not in pubSubFunctions:
            raise Exception(
                f"PubSub function '{x}' is not defined in PubSubRequests.py. Please add it."
            )

    if len(pubSubFunctions) != len(functionsToTest):
        raise Exception("Not all PubSub functions are covered by tests.")


def test_ifAllPubSubMockDataParsesWithGeneratedModels():
    """Every pubSubMockDb message addressed to this repo parses with its
    topic's generated objects_<publisher>.<Fn>Message, both as stored and
    as the publisher sends it on the wire (pubSub.convertToJson, then
    json.loads: dates become ISO strings). A failure names the field."""
    import importlib
    import json
    import pytest
    from pydantic import ValidationError
    from pubSub import pubSub as wire

    projectName = Project().projectName
    failures, checked = [], 0
    for collection in pubSubMockDb.getAllCollections():
        # '*_devData' are passive capture logs, not consumable mock messages
        if collection.endswith('_devData'):
            continue
        for row in pubSubMockDb.read({'projectNameConsumers': projectName}, collection):
            checked += 1
            topic = row['consumeFunctionName']
            source = topic.rsplit('_', 1)[-1]
            try:
                module = importlib.import_module(f'objects_{source}')
            except ImportError:
                failures.append(f'{topic}: server/objects_{source}.py does not import (python pubSubModels.py --pull)')
                continue
            name = module.TOPIC_MESSAGES.get(topic)
            if name is None:
                failures.append(f'{topic}: server/objects_{source}.py has no message model for it')
                continue
            payload = row['data']['body']['message']['data']
            for form, value in (('as stored', payload),
                                ('on the wire', json.loads(wire.convertToJson(payload)))):
                try:
                    getattr(module, name).model_validate(value)
                except ValidationError as e:
                    failures.extend(f"{topic} {form}: {'.'.join(str(p) for p in err['loc']) or '(root)'}: "
                                    f"{err['msg']}" for err in e.errors())
    if not checked:
        pytest.skip(f"no pubSubMockDb message is addressed to {projectName}: run the publishers' "
                    'pubSubMockDataGenerator.py')
    assert not failures, 'Mock messages that fail their generated model:\n' + '\n'.join(failures)
# <<< star-app shared: Static contract tests


# ---------------------------------------------------------------------------
# Integration test: replay every mock message addressed to this project
# ---------------------------------------------------------------------------
#
# Every publisher's generator emits its messages in its own order, so the
# order is discovered at runtime (consumeMockData enforces it). Each handler
# gets a seed step (the prerequisite docs it reads or updates that no earlier
# message creates -- a handler that answers {'updated': 0} when its target is
# missing would make the replay vacuous) and an assert step verifying the DB
# state it must leave behind. A handler's idempotent short-circuit
# ({'updated': 0}, {'deleted': False}, the already-stored doc) is never
# accepted as the outcome of a first delivery; the redelivery step below is
# where those shapes are asserted.


def _pendingMessages(serviceProjectName: str) -> list[dict]:
    """Mock messages from `serviceProjectName` addressed to this project, in
    generator (index) order."""
    return sorted(
        pubSubMockDb.read({'projectNameConsumers': Project().projectName},
                          serviceProjectName),
        key=lambda x: x['index'])


def _noop(payload: dict, res: dict = None):
    return None


pubSubRequests = PubSubRequests()

# consumeFunctionName -> (seed, assert). Every handler that receives mock data
# must have an entry (_replayService asserts it): a replay without seed/assert
# steps would be vacuous. Use _noop for a seed when the handler creates
# everything it needs or an earlier message already did. Extend THIS
# definition (the worked example below shows an entry) -- do not define a
# second REPLAY_STEPS further down, it would silently rebind the name.
REPLAY_STEPS: Dict[str, Tuple[Callable[[dict], None], Callable[[dict, dict],
                                                                None]]] = {}

# WORKED EXAMPLE (commented out: a fresh scaffold has no createOrder_accounting
# handler). Suppose the `accounting` repo publishes ApiRequests.createOrder
# (the commented example in this repo's ApiRequests.py, seen from the other
# side) with this repo's service in projectNameConsumers, so its payload is
# the created order: {'_id', '_version', 'customerId', 'items': [{'unitId',
# 'quantity'}], 'createdBy'}. This repo's handler, the createOrder_accounting
# example in PubSubRequests.py, returns the existing order if the _id is
# already stored, raises if the customer is unknown (genuine data fault), and
# otherwise inserts the 'Orders' document. Its replay entry would be:
#
# def _seedCreateOrder(payload: dict):
#     """Seed only what the handler READS and no earlier message creates (the
#     customer -- use your test factories once you have them). Never seed the
#     document the handler itself creates: the assert below cannot tell a
#     real insert from the existing-doc short-circuit, so the guarantee comes
#     from wipeDatabase() plus this rule."""
#     if db.read({'_id': payload['customerId']}, 'Customers',
#                findOne=True) is None:
#         db.create(
#             {
#                 '_id': payload['customerId'],
#                 '_version': 0,
#                 'name': f"seeded {payload['customerId']}"
#             }, 'Customers')
#
#
# def _assertCreateOrder(payload: dict, res: dict):
#     """Assert the outcome by reading the DB back; the returned dict alone
#     proves nothing. With the seed rule above honoured, a stored order here
#     can only have come from this delivery."""
#     assert res.get('_id') == payload['_id']
#     order = db.read({'_id': payload['_id']}, 'Orders', findOne=True)
#     assert order is not None
#     assert order['customerId'] == payload['customerId']
#     assert [(x['unitId'], x['quantity']) for x in order['items']] == [
#         (x['unitId'], x['quantity']) for x in payload['items']]
#
#
# REPLAY_STEPS[PubSubRequests().createOrder_accounting.__name__] = (
#     _seedCreateOrder, _assertCreateOrder)
#
# ... and add 'Customers' and 'Orders' to TEST_COLLECTIONS in
# pubSubMockDataGenerator.py.
#
# The key MUST be written as `PubSubRequests().createOrder_accounting.__name__`
# and not `pubSubRequests.createOrder_accounting.__name__` or the string
# 'createOrder_accounting': test_ifAllApiRequestTested proves every handler is
# referenced by scanning the test files' AST (_list_referenced_functions), and
# that scan records the BARE method name only when the attribute's receiver is
# a call expression. `pubSubRequests.createOrder_accounting` is recorded as
# the dotted 'pubSubRequests.createOrder_accounting', a plain string is not a
# reference at all, and the handler would be reported as untested either way.


# --- redelivery ---------------------------------------------------------------
#
# GCP redelivers any message whose handler did not return 200, so a handler
# that writes must be a no-op the second time it sees the same payload. Each
# entry names the collection + the document the handler owns (resolved from
# the payload) and what the second call must return; the owned document and
# the collection counts must be untouched. `_version` is ignored in the
# comparison: mongoDb.update bumps it even when the write changes nothing.
# Run IMMEDIATELY after the first delivery (not after the whole replay): a
# later message may legitimately change or delete the same document, and the
# original payload would then no longer describe a redelivery.
_COUNTED_COLLECTIONS = tuple(TEST_COLLECTIONS)

# Handlers whose second delivery is deliberately NOT a no-op, with the reason.
# Their redelivery step asserts the documented behaviour (one more document in
# the owned collection) instead of the no-op. A handler belongs here only when
# BOTH hold: the payload carries no natural key (nothing the handler could look
# up to recognise the event -- e.g. {unitId, quantity} for a stock adjustment,
# as opposed to an order with an _id), so it cannot tell a redelivery from a
# genuine second event; AND you accept that the generated route's messageId
# de-dupe (appPubSub.py) is the only guard -- it catches most redeliveries,
# but PubSubMessages is written AFTER the handler, so a crash between the
# handler and the 200 replays the same messageId and books the event twice.
# Anything that carries an _id belongs in the no-op set. Extend THIS
# definition; the commented entry shows the shape.
KNOWN_NON_IDEMPOTENT_ON_REDELIVERY: Dict[str, str] = {
    # PubSubRequests().adjustStock_warehouse.__name__:
    # 'payload {unitId, quantity} has no natural key; the route de-dupes a '
    # 'redelivery of the same messageId, and a re-publish with a new '
    # 'messageId is a genuine second adjustment',
}


# Generic id/check helpers most redelivery entries reuse.
def _payloadId(payload: dict) -> str:
    return payload['_id']


def _sameId(payload: dict, res: dict):
    assert res['_id'] == payload['_id']


# consumeFunctionName -> (owned collection, payload -> owned doc _id,
# check(payload, second res)). Every handler in REPLAY_STEPS needs an entry.
# Extend THIS definition -- never define a second REDELIVERY_STEPS below.
REDELIVERY_STEPS: Dict[str, Tuple[str, Callable[[dict], str],
                                  Callable[[dict, dict], None]]] = {}

# Worked example continued: createOrder_accounting owns the 'Orders' document
# whose _id is payload['_id']; redelivered, it must return that existing order
# (same _id) and leave 'Orders' -- and every counted collection -- untouched:
#
# REDELIVERY_STEPS[PubSubRequests().createOrder_accounting.__name__] = (
#     'Orders', _payloadId, _sameId)


def _withoutVersion(doc: dict) -> dict:
    return {k: v for k, v in (doc or {}).items() if k != '_version'}


def _assertRedeliveryDuplicates(functionName: str, payload: dict):
    collection = REDELIVERY_STEPS[functionName][0]
    countBefore = len(db.read({}, collection))
    getattr(pubSubRequests, functionName)(payload)
    assert len(db.read({}, collection)) == countBefore + 1, (
        f'{functionName} is listed in KNOWN_NON_IDEMPOTENT_ON_REDELIVERY but '
        f'its redelivery no longer books a second {collection} document - '
        f'move it to the no-op set')


def _assertRedeliveryIsNoop(functionName: str, payload: dict):
    collection, docId, check = REDELIVERY_STEPS[functionName]
    docBefore = db.read({'_id': docId(payload)}, collection, findOne=True)
    countsBefore = {c: len(db.read({}, c)) for c in _COUNTED_COLLECTIONS}

    res = getattr(pubSubRequests, functionName)(payload)

    check(payload, res)
    docAfter = db.read({'_id': docId(payload)}, collection, findOne=True)
    assert _withoutVersion(docAfter) == _withoutVersion(docBefore)
    assert {c: len(db.read({}, c))
            for c in _COUNTED_COLLECTIONS} == countsBefore


def _replayService(serviceProjectName: str) -> int:
    """Consume every pending message from one publisher in index order.
    Returns the number of messages replayed."""
    pending = _pendingMessages(serviceProjectName)
    for mockMessage in pending:
        functionName = mockMessage['consumeFunctionName']
        assert functionName in REPLAY_STEPS, (
            f'{functionName} has no seed/assert step in REPLAY_STEPS')
        handler = getattr(pubSubRequests, functionName)
        payload = MockData(**mockMessage).getData()
        seed, check = REPLAY_STEPS[functionName]
        seed(payload)
        res = mockDataConsumer.consumeMockData(serviceProjectName, handler)
        check(payload, res['res'])
        assert functionName in REDELIVERY_STEPS, (
            f'{functionName} has no redelivery step in REDELIVERY_STEPS')
        if functionName in KNOWN_NON_IDEMPOTENT_ON_REDELIVERY:
            _assertRedeliveryDuplicates(functionName, payload)
        else:
            _assertRedeliveryIsNoop(functionName, payload)
    return len(pending)


# Publishers (their AppConfig().projectName) that MUST have addressed this
# project for the replay to mean anything. Add one as soon as any of its
# @pubSubDecorator functions lists this repo's service as a consumer, so a
# forgotten generator run fails loudly instead of silently replaying nothing.
# e.g. EXPECTED_PUBLISHERS = ['accounting']
EXPECTED_PUBLISHERS: List[str] = []


def test_pubSub_replayToThisProject():
    try:
        wipeDatabase()

        for serviceProjectName in EXPECTED_PUBLISHERS:
            replayed = _replayService(serviceProjectName)
            assert replayed > 0, (
                f"No {serviceProjectName} mock messages address this project. "
                f"Either run that repo's pubSubMockDataGenerator.py first, or "
                f"its services/<thisRepo>.py addresses a different name: its "
                f"getProjectName() must equal '{Project().projectName}'.")

        # Any other publisher that addresses this project. A fresh scaffold has
        # none, so the loop replays zero messages -- which is what keeps this
        # test green until the first publisher exists. Own collection skipped:
        # a project never consumes its own messages.
        for serviceProjectName in mockDataConsumer.toConsumeIndex:
            if (serviceProjectName in EXPECTED_PUBLISHERS
                    or serviceProjectName == Project().projectName):
                continue
            _replayService(serviceProjectName)
    finally:
        wipeDatabase()


# Depends on test_pubSub_replayToThisProject having run first in the same
# process: mockDataConsumer is a Singleton whose consumed-index bookkeeping is
# what this validates. Run alone (or after a replay that stopped early) it
# FAILS whenever pubSubMockDb still holds messages addressed to this project,
# because none of them has been consumed in this process. Keep it the last
# test in this file.
def test_atExitValidation():
    mockDataConsumer.atExitValidation()


if __name__ == "__main__":
    test_ifAllApiRequestTested()
    test_ifAllPubSubFunctionsGenerated()
    test_ifAllPubSubMockDataHasFunctions()
    test_pubSub_replayToThisProject()
    test_atExitValidation()
