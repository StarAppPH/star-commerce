"""Only pubSubMockDataGenerator.py may write the shared pubSubMockDb.

All services share one local MongoDB database, pubSubMockDb. Each repo's
pubSubMockDataGenerator.py fills its own collection with reference messages,
and the OTHER repos' test_pubSubContracts.py replay them. Any other writer (a
test publishing with publishToPubSubMockDb=True, or a _wipe() that deletes the
collection) destroys fixtures that other repos depend on.

The check walks the AST of every .py file under this folder, so strings and
docstrings that merely mention the flag never trigger it. It needs no project
imports and no database, and the same file is shipped to every service.
"""
import ast
import os
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).parent
SKIPPED_DIRS = {'venv', '.venv', '__pycache__', 'node_modules'}
GENERATOR_FILE = 'pubSubMockDataGenerator.py'
PUBSUB_FILE = 'pubSub.py'
PUBSUB_MODULE = 'pubSub'
# service.py's publish decorator forwards a caller's publishToPubSubMockDb flag
# to createMockData, so like pubSub.py it is part of the write implementation.
SERVICE_FILE = 'service.py'
# The allow-lists hold paths relative to this folder, so a nested file that
# merely shares a name (e.g. tools/pubSub.py) is still scanned.
MOCK_DB_NAME = 'pubSubMockDb'
PUBLISH_FLAG = 'publishToPubSubMockDb'
CREATE_MOCK_DATA = 'createMockData'
WRITE_METHODS = {
    'create', 'createMany', 'update', 'bulkUpdate', 'findOneAndUpdate',
    'delete', 'deleteMany', 'deleteAllDataInDatabaseForDevEnvironment',
}
PYMONGO_WRITE_METHODS = {
    'insert_one', 'insert_many', 'update_one', 'update_many', 'replace_one',
    'delete_one', 'delete_many', 'bulk_write', 'find_one_and_update',
    'find_one_and_replace', 'find_one_and_delete', 'drop', 'drop_collection',
}
FAILURE_SENTENCE = ('Only pubSubMockDataGenerator.py may write pubSubMockDb '
                    '(other repos replay it). Observe publishes without '
                    'writing the mock DB.')


def _isTrue(node):
    return isinstance(node, ast.Constant) and node.value is True


def _findMockDbPublishes(tree):
    """Lines with publishToPubSubMockDb=True, as a keyword, a dict entry or a
    parameter default (pubSub.py applies defaults, so a True default publishes
    on every call)."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arguments):
            positional = node.posonlyargs + node.args
            defaultPairs = list(zip(positional[len(positional) - len(node.defaults):],
                                    node.defaults))
            defaultPairs += [(arg, default) for arg, default
                             in zip(node.kwonlyargs, node.kw_defaults)
                             if default is not None]
            for arg, default in defaultPairs:
                if arg.arg == PUBLISH_FLAG and _isTrue(default):
                    lines.append(default.lineno)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == PUBLISH_FLAG and _isTrue(keyword.value):
                    lines.append(keyword.value.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == PUBLISH_FLAG
                        and _isTrue(value)):
                    lines.append(key.lineno)
    return lines


def _isMockDbReceiver(node):
    if isinstance(node, ast.Name):
        return node.id == MOCK_DB_NAME
    if isinstance(node, ast.Attribute):
        return node.attr == MOCK_DB_NAME
    return False


def _isMockDbPymongoHandle(node):
    """pubSubMockDb.db or pubSubMockDb.db[collection]."""
    if isinstance(node, ast.Subscript):
        node = node.value
    return (isinstance(node, ast.Attribute) and node.attr == 'db'
            and _isMockDbReceiver(node.value))


def _findDirectMockDbWrites(tree):
    """Lines calling a write method on pubSubMockDb or its raw pymongo handle
    (reads are allowed)."""
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        receiver = node.func.value
        if ((method in WRITE_METHODS and _isMockDbReceiver(receiver))
                or (method in PYMONGO_WRITE_METHODS
                    and _isMockDbPymongoHandle(receiver))):
            lines.append(node.lineno)
    return lines


def _findCreateMockDataCalls(tree):
    """Lines calling pubSub's createMockData, imported by name or via the module.

    Unrelated functions that happen to share the name (seeders defining their
    own createMockData) are not flagged.
    """
    importedNames = set()
    moduleNames = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == PUBSUB_MODULE:
            for alias in node.names:
                if alias.name in (CREATE_MOCK_DATA, '*'):
                    importedNames.add(alias.asname or CREATE_MOCK_DATA)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PUBSUB_MODULE:
                    moduleNames.add(alias.asname or PUBSUB_MODULE)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if ((isinstance(func, ast.Name) and func.id in importedNames)
                or (isinstance(func, ast.Attribute)
                    and func.attr == CREATE_MOCK_DATA
                    and isinstance(func.value, ast.Name)
                    and func.value.id in moduleNames)):
            lines.append(node.lineno)
    return lines


def _iterPythonFiles():
    for root, dirNames, fileNames in os.walk(SERVER_DIR):
        dirNames[:] = sorted(d for d in dirNames
                             if d not in SKIPPED_DIRS and not d.startswith('.'))
        for fileName in sorted(fileNames):
            if fileName.endswith('.py'):
                yield Path(root) / fileName


@pytest.fixture(scope='module')
def serverTrees():
    """(relative path, AST or the parse error) of every .py file under this
    folder, parsed once for the three scans below."""
    trees = []
    for path in _iterPythonFiles():
        relativePath = path.relative_to(SERVER_DIR).as_posix()
        try:
            # Parsing bytes lets ast honour a UTF-8 BOM or coding cookie.
            trees.append((relativePath, ast.parse(path.read_bytes(), filename=str(path))))
        except (SyntaxError, ValueError) as error:
            trees.append((relativePath, error))
    return trees


def _scanServer(serverTrees, finder, allowedRelativePaths):
    offenders = []
    for relativePath, tree in serverTrees:
        if isinstance(tree, Exception):
            offenders.append(f'{relativePath}: could not be parsed ({tree})')
            continue
        if relativePath in allowedRelativePaths:
            continue
        offenders.extend(f'{relativePath}:{line}'
                         for line in sorted(set(finder(tree))))
    return offenders


def _failureMessage(offenders):
    return FAILURE_SENTENCE + '\n' + '\n'.join(offenders)


def test_noMockDbPublishOutsideGenerator(serverTrees):
    offenders = _scanServer(serverTrees, _findMockDbPublishes, {GENERATOR_FILE})
    assert offenders == [], _failureMessage(offenders)


def test_noDirectMockDbWritesOutsideGenerator(serverTrees):
    offenders = _scanServer(serverTrees, _findDirectMockDbWrites,
                            {GENERATOR_FILE, PUBSUB_FILE})
    assert offenders == [], _failureMessage(offenders)


def test_noCreateMockDataOutsideGenerator(serverTrees):
    offenders = _scanServer(serverTrees, _findCreateMockDataCalls,
                            {GENERATOR_FILE, PUBSUB_FILE, SERVICE_FILE})
    assert offenders == [], _failureMessage(offenders)


def test_scannerSelfCheck():
    flaggedSource = '\n'.join([
        'publish(data, publishToPubSubMockDb=True)',
        "publish(data, **{'publishToPubSubMockDb': True})",
        'pubSubMockDb.delete({}, projectName)',
        'mongo.pubSubMockDb.create(doc, projectName)',
        'from pubSub import createMockData',
        'import pubSub',
        'createMockData(mockData, pubSubMockDb)',
        'pubSub.createMockData(mockData, pubSubMockDb)',
        'def handler(data, publishToPubSubMockDb: bool = True):',
        '    pass',
        'def handler(data, *, publishToPubSubMockDb=True):',
        '    pass',
        "pubSubMockDb.db['ecommerce'].delete_many({})",
        "pubSubMockDb.db.drop_collection('ecommerce')",
    ])
    # A UTF-8 BOM (bytes input) must parse like any other file.
    tree = ast.parse(b'\xef\xbb\xbf' + flaggedSource.encode('utf-8'))
    assert sorted(_findMockDbPublishes(tree)) == [1, 2, 9, 11]
    assert sorted(_findDirectMockDbWrites(tree)) == [3, 4, 13, 14]
    assert sorted(_findCreateMockDataCalls(tree)) == [7, 8]

    cleanSource = '\n'.join([
        '"""Publish with publishToPubSubMockDb=True in the generator."""',
        "note = 'publish(data, publishToPubSubMockDb=True)'",
        'publish(data, publishToPubSubMockDb=False)',
        "publish(data, **{'publishToPubSubMockDb': False})",
        'publish(data, publishToPubSubMockDb=publishToPubSubMockDb)',
        'pubSubMockDb.read({}, projectName)',
        'pubSubMockDb.getAllCollectionNames()',
        'otherDb.delete({}, projectName)',
        "text = 'createMockData(mockData, pubSubMockDb)'",
        'def createMockData():',
        '    pass',
        'createMockData()',
        'seeder.createMockData()',
        'def handler(data, publishToPubSubMockDb: bool = False):',
        '    pass',
        'def handler(data, *, publishToPubSubMockDb=False, other=True):',
        '    pass',
        "pubSubMockDb.db['ecommerce'].find({})",
        "self.mockDb.db['ecommerce'].create_index('consumeFunctionName')",
    ])
    tree = ast.parse(cleanSource)
    assert _findMockDbPublishes(tree) == []
    assert _findDirectMockDbWrites(tree) == []
    assert _findCreateMockDataCalls(tree) == []
