"""Keeps the documents a test's fake database returns in step with the models.

A unit test that patches a db module (patch('ApiRequests.db') and the like)
and sets `mockDb.read.return_value = {...}` to a hand-written dict tests the
code against that dict. Nothing checks it against the model: when a model
renames or removes a field, the fake keeps the old shape, and the test goes
on passing against a document the real database would never return (mock
drift). So a fake document is built through its model with
`fakeDoc(<Model>, ...)` from conftest.py, which validates it and refuses any
key the model does not declare, nested models included. A value the model
rewrites ('' -> None, a stripped string) arrives rewritten: to feed a raw
value, set it on the dict fakeDoc returns.

This test scans every git-listed test file under server/ (and conftest.py)
for a db fake whose return_value or side_effect is a raw document: a dict
literal or dict(...) call, a list, tuple or iter(...) of them, or a name the
same function (else the module) binds to one. A fake is an assignment to
`<...>.<db method>...<return_value|side_effect>`, a
`<...>.<db method> = MagicMock(return_value=...)`, a
`patch('<...>.db.<db method>', return_value=...)` or a
`patch.object(<...>, '<db method>', return_value=...)`. A deliberate raw
document, such as a legacy shape a normalization test feeds in, carries a
`# raw-doc: <reason>` comment (a reason of at least MIN_REASON_CHARS
characters) on the fake's statement or on the statement binding the name.

Known limits (convert these by hand): documents returned from the body of a
`def` used as a side_effect, and documents passed into a helper as
parameters.

Not scanned: anything inside a virtualenv, node_modules or __pycache__ folder.
"""
import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
from pydantic import BaseModel, ConfigDict, Field

from conftest import fakeDoc

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = 'server'
SKIPPED_DIR_NAMES = {'venv', '.venv', 'node_modules', '__pycache__'}

# The db methods (and the pymongo collection methods a test reaches through
# mockDb.db[...]) whose fake return value stands in for a stored document.
DB_READ_METHODS = {
    'read', 'readMany', 'find', 'find_one', 'aggregate', 'update', 'create',
    'findOneAndUpdate', 'find_one_and_update'
}
MOCK_RESULT_ATTRS = {'return_value', 'side_effect'}
MOCK_FACTORIES = {'MagicMock', 'Mock'}
PATCH_TARGET = re.compile(r'\.db\.(' + '|'.join(sorted(DB_READ_METHODS)) + r')$')
RAW_DOC_MARKER = '# raw-doc:'
MIN_REASON_CHARS = 10

FIX = f"""\
Build each flagged fake document through its model with fakeDoc from
conftest.py, or with a factory there that wraps it (such as unitDoc or
userDoc) where the repo has one:

    from conftest import fakeDoc
    from objects import <Model>
    mockDb.read.return_value = fakeDoc(<Model>, _id='doc-001', <field>=...)

fakeDoc(Model, **fields) validates the fields through the model, refuses any
key the model does not declare (at any depth), and returns the model's dump
by alias, so it looks like a stored document (_id, _version). Use the model
of the collection the code under test reads: read the production code path
to find it. For an aggregate whose pipeline projects a response shape, use
that response's model. A value the model rewrites ('' -> None, a stripped
string) arrives rewritten: to feed a raw value, set it on the returned dict.

If the test deliberately feeds a document no model describes (a legacy shape
a normalization test repairs, a malformed record), keep the raw dict and end
the statement's line with `{RAW_DOC_MARKER} <why it must stay raw>` (at least
{MIN_REASON_CHARS} characters).

Rules:
- Change test setup only. Never modify an assert or an expected value.
- If a test fails once its fake is built through the model, the old fake was
  hiding a real mismatch between the code and the model: report it, do not
  edit the assert or quietly reshape the fake to make it pass.
- Never add an exclusion or a skip to this test."""


@dataclass(frozen=True)
class Finding:
    rel: str
    line: int
    source: str


def _testFiles():
    # Untracked-but-not-ignored files too, so a new test is checked before it is committed.
    out = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
         '--', SERVER_DIR],
        cwd=REPO_ROOT, capture_output=True, check=True).stdout
    for rel in out.decode('utf-8').split('\0'):
        path = PurePosixPath(rel)
        if (rel and path.suffix == '.py'
                and (path.name.startswith('test') or path.name == 'conftest.py')
                and not SKIPPED_DIR_NAMES.intersection(path.parts[:-1])
                and (REPO_ROOT / rel).is_file()):
            yield rel


def _leaves(value: ast.expr):
    """What a fake's value hands out: the elements of a list, tuple or set,
    and of an iter()/list()/tuple() call, recursively; else the value."""
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        for element in value.elts:
            yield from _leaves(element)
    elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
          and value.func.id in ('iter', 'list', 'tuple') and value.args):
        yield from _leaves(value.args[0])
    else:
        yield value


def _isDictExpr(node: ast.expr) -> bool:
    """A dict literal, or a dict(...) call with keywords or a dict literal."""
    if isinstance(node, ast.Dict):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'dict'
            and (bool(node.keywords)
                 or any(isinstance(arg, ast.Dict) for arg in node.args)))


def _isDocLiteral(value: ast.expr) -> bool:
    return any(_isDictExpr(leaf) for leaf in _leaves(value))


def _names(value: ast.expr) -> list[str]:
    """The names a fake's value refers to directly: `doc`, `[doc1, doc2]`,
    `iter(docs)`."""
    return [leaf.id for leaf in _leaves(value) if isinstance(leaf, ast.Name)]


def _ownNodes(scope: ast.AST):
    """Every node inside `scope`, but not inside a nested function, lambda
    or class (scopes of their own)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _hasMarker(lines: list[str], node: ast.AST) -> bool:
    for line in lines[node.lineno - 1:node.end_lineno]:
        _, marker, reason = line.partition(RAW_DOC_MARKER)
        if marker and len(reason.strip()) >= MIN_REASON_CHARS:
            return True
    return False


def _bindings(scope: ast.AST) -> dict[str, list[ast.stmt]]:
    """Every plain `name = ...` statement directly in `scope`."""
    found: dict[str, list[ast.stmt]] = {}
    for node in _ownNodes(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node)
    return found


def _scopes(tree: ast.Module):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _hasDbMethod(node: ast.expr) -> bool:
    """A db method somewhere in an attribute chain (`mockDb.read`,
    `mockDb.db.__getitem__.return_value.aggregate`)."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        if isinstance(node, ast.Attribute) and node.attr in DB_READ_METHODS:
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _callName(call: ast.Call) -> str:
    """`patch`, `patch.object`, `MagicMock` (`mock.` prefixes dropped)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr == 'object' and isinstance(func.value, (ast.Name, ast.Attribute)):
            return 'patch.object'
        return func.attr
    return ''


def _isDbPatch(call: ast.Call) -> bool:
    """patch('<...>.db.<db method>', ...) or patch.object(<...>, '<db method>', ...)."""
    name = _callName(call)
    if name == 'patch' and call.args:
        target = call.args[0]
        return (isinstance(target, ast.Constant) and isinstance(target.value, str)
                and bool(PATCH_TARGET.search(target.value)))
    if name == 'patch.object' and len(call.args) > 1:
        attribute = call.args[1]
        return (isinstance(attribute, ast.Constant)
                and attribute.value in DB_READ_METHODS)
    return False


def _mockResults(call: ast.Call) -> list[ast.expr]:
    return [keyword.value for keyword in call.keywords
            if keyword.arg in MOCK_RESULT_ATTRS]


def _fakeValues(node: ast.AST) -> list[ast.expr]:
    """The values a db fake in `node` returns, or [] when `node` is no db fake."""
    if isinstance(node, ast.Assign):
        values = []
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr in MOCK_RESULT_ATTRS and _hasDbMethod(target.value):
                values.append(node.value)
            elif (target.attr in DB_READ_METHODS
                  and isinstance(node.value, ast.Call)
                  and _callName(node.value) in MOCK_FACTORIES):
                values += _mockResults(node.value)
        return values
    if isinstance(node, ast.Call) and _isDbPatch(node):
        return _mockResults(node)
    return []


def findRawDocFakes(source: str, rel: str = '<source>') -> list[Finding]:
    """Every db fake in `source` returning a raw document without a
    `# raw-doc:` reason."""
    tree = ast.parse(source)
    lines = source.splitlines()
    moduleBindings = _bindings(tree)
    findings = []
    for scope in _scopes(tree):
        scopeBindings = moduleBindings if scope is tree else _bindings(scope)

        def isRaw(value: ast.expr) -> bool:
            if _isDocLiteral(value):
                return True
            for name in _names(value):
                binds = scopeBindings.get(name) or moduleBindings.get(name, [])
                if any(_isDocLiteral(bind.value) and not _hasMarker(lines, bind)
                       for bind in binds):
                    return True
            return False

        for node in _ownNodes(scope):
            values = _fakeValues(node)
            if values and not _hasMarker(lines, node) and any(map(isRaw, values)):
                findings.append(
                    Finding(rel, node.lineno, lines[node.lineno - 1].strip()))
    return sorted(set(findings), key=lambda finding: finding.line)


def _brief(findings: list[Finding]) -> str:
    rows = '\n'.join(f'  {finding.rel}:{finding.line}  {finding.source}'
                     for finding in findings)
    return (f'MOCK DRIFT\n{len(findings)} db fake(s) return a hand-written '
            'document that no model checks. When a model renames or removes '
            'a field, such a fake keeps the old shape and its test passes '
            'against data the real database would never return.\n\n'
            f'== FAKES (file:line) ==\n{rows}\n\n== HOW TO FIX ==\n{FIX}')


def test_db_fakes_build_documents_through_their_model():
    findings = []
    for rel in _testFiles():
        source = (REPO_ROOT / rel).read_text(encoding='utf-8')
        findings += findRawDocFakes(source, rel)
    if findings:
        pytest.fail(_brief(findings), pytrace=False)


def test_detector_flags_raw_documents_and_allows_marked_ones():
    flagged = '''
RAW = {'_id': 'm'}
def test_a():
    mockDb.read.return_value = {'_id': 'x'}
    mockDb.db.__getitem__.return_value.aggregate.return_value = iter([{'a': 1}])
    doc = {'_id': 'y'}
    mockDb.read.side_effect = [None, doc]
    docs = [{'_id': 'z'}]
    mockCollection.find.return_value = docs
    mockDb.update.return_value = {**doc, '_version': 2}
    mockDb.read.return_value = dict(_id='x', name='n')
    mockDb.read.return_value = RAW
    with patch('ApiRequests.db.read', return_value={'_id': 'x'}):
        pass
    with patch.object(ApiRequestsModule.db, 'aggregate', return_value=[{'a': 1}]):
        pass
    mockDb.read = MagicMock(return_value={'_id': 'x'})
'''
    assert [f.line for f in findRawDocFakes(flagged)] == [
        4, 5, 7, 9, 10, 11, 12, 13, 15, 17
    ]

    allowed = '''
from conftest import fakeDoc
LEGACY = {'_id': 'l'}  # raw-doc: legacy shape the normalizer repairs
doc = {'_id': 'm'}
def test_b():
    mockDb.read.return_value = fakeDoc(Doc, _id='x')
    mockDb.read.return_value = {'_id': 'x'}  # raw-doc: malformed record
    mockDb.read.return_value = LEGACY
    mockDb.read.side_effect = [None, 0]
    mockDb.read.return_value = None
    mockDb.aggregate.return_value = iter([])
    mockService.request.return_value = {'status': 'ok'}
    mockDb.read.return_value = dict(existing)
    with patch('ApiRequests._send_email', return_value={'sent': True}):
        pass
    doc = fakeDoc(Doc, _id='y')
    mockDb.read.return_value = doc
    mockDb.read.return_value = {'_id': 'x'}  # raw-doc:
    mockDb.read.return_value = {'_id': 'x'}  # raw-doc: too short
'''
    # A local fakeDoc binding shadows the raw module-level `doc`. Only the
    # last two fakes are flagged: a marker needs a reason of 10+ characters.
    assert [f.line for f in findRawDocFakes(allowed)] == [18, 19]


class _Part(BaseModel):
    size: int = 0


class _Doc(BaseModel):
    """A stand-in stored model: an _id alias, a required field, a default
    and nested submodels (alone and in a list)."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias='_id')
    name: str
    isForSale: bool = True
    part: _Part | None = None
    parts: list[_Part] = Field(default_factory=list)


def test_fake_doc_validates_and_refuses_unknown_keys():
    doc = fakeDoc(_Doc, _id='d1', name='Widget', part={'size': 2})
    # A stored document: aliased keys and every default.
    assert doc == {'_id': 'd1', 'name': 'Widget', 'isForSale': True,
                   'part': {'size': 2}, 'parts': []}
    # A renamed or removed field fails loudly, though the model ignores extras.
    with pytest.raises(AssertionError, match='docName'):
        fakeDoc(_Doc, _id='d1', name='Widget', docName='Widget')
    # So does one inside a nested model, or a list of them.
    with pytest.raises(AssertionError, match=r'part\.sise'):
        fakeDoc(_Doc, _id='d1', name='Widget', part={'sise': 2})
    with pytest.raises(AssertionError, match=r'parts\[1\]\.sise'):
        fakeDoc(_Doc, _id='d1', name='Widget', parts=[{'size': 1}, {'sise': 2}])
    # A missing required field fails validation.
    with pytest.raises(ValueError, match='name'):
        fakeDoc(_Doc, _id='d1')
