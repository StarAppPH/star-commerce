"""Keeps hand-written source files small enough to read and navigate.

Every source file may have at most MAX_LINES lines. While some are over, this
test fails with a brief for the SMALLEST oversized file: what is in it, how to
split that kind of file, the rules every split follows, and how to check the
split is done. Split that one file, rerun, and the brief moves on to the next,
until the test passes.

Not checked: the client folder, files AppCreator generates (their paths come
from AppCreator.py, so this file is identical in every Star App repo), the
server/objects_<publisher>.py files pubSubModels.py generates (only a file
carrying its header counts, and test_pubSubModels.py fails on one nothing
generates), files whose name starts with `test`, and anything inside a
virtualenv, node_modules, .next or __pycache__ folder, even when git tracks it.
"""
import ast
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest

import AppCreator
import pubSubModels

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_LINES = 1500
SOURCE_SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.css'}
SKIPPED_DIR_NAMES = {'venv', '.venv', 'node_modules', '.next', '__pycache__'}


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


CLIENT_DIR = _repo_relative(AppCreator.CLIENT_DIR) + '/'

# Written by AppCreator.py: nobody edits or reads these by hand.
GENERATED_FILES = {
    _repo_relative(AppCreator.SERVER_DIR / 'app.py'),
    _repo_relative(AppCreator.SERVER_DIR / 'appPubSub.py'),
} | {
    _repo_relative(AppCreator.CLIENT_API_DIR / name)
    for name in ('Server.ts', 'ApiResponse.ts', 'ServerRequests.ts')
} | set(pubSubModels.generatedModelFiles(REPO_ROOT))
GENERATED_DIRS = tuple(
    _repo_relative(directory) + '/'
    for directory in (AppCreator.SERVER_SCHEMAS_DIR,
                      AppCreator.CLIENT_SCHEMAS_DIR,
                      getattr(AppCreator, 'CLIENT_GENERATED_DIR', None))
    if directory is not None)

# The outline lists each top-level block of at least this many lines on its
# own row and folds runs of smaller ones into one row. The threshold doubles
# until the outline fits in OUTLINE_MAX_ROWS rows.
OUTLINE_MIN_BLOCK_LINES = 40
OUTLINE_MAX_ROWS = 70

# How to split a file, by path. The first matching pattern wins.
SPLIT_GUIDANCE = [
    ('server/ApiRequests.py', """\
Use the layout accountingSystem already has: ApiRequests.py holds only its
imports and `class ApiRequests(<Name>Actions, ...)`, and each mixin lives in
server/actions/<Name>Actions.py (server/actions/ has an __init__.py).
AppCreator imports only the ApiRequests class and reads the routes from its
methods, so it needs no change as long as ApiRequests still inherits every
route.
- Move each *Actions class to server/actions/<Name>Actions.py. A class over
  the limit becomes several mixins split by topic (OrderActions ->
  OrderPaymentActions, OrderDeliveryActions, ...), each added to the bases of
  ApiRequests.
- Move module-level helpers, constants and exception classes next to their
  callers: into the one actions module that uses them, or, when several do,
  into server/actions/<Feature>Helper.py.
- Other code imports names from ApiRequests (grep "from ApiRequests import"
  and "ApiRequestsModule."): update those imports to the new modules.
- Move one class or feature at a time and run its tests before the next; the
  outline above gives the line ranges."""),
    ('server/actions/*Actions.py', """\
This is one of the mixins that `class ApiRequests(...)` in server/ApiRequests.py
combines. AppCreator reads the routes from that class's methods, so splitting
the mixin needs no other change.
- Split it by topic into several mixins, server/actions/<Name><Topic>Actions.py
  (SupplierActions -> SupplierPaymentActions, ...), and add each to the bases
  of ApiRequests.
- Helpers, constants and classes used by only one new mixin move with it; ones
  several share go to server/actions/<Feature>Helper.py.
- Other code imports names from this module (grep its module name in imports
  and patches): update those imports to the new modules."""),
    ('server/objects.py', """\
objects.py becomes the package server/objects/ with one module per domain
object (server/objects/salesOrder.py holds SalesOrder and its View, Response
and line models). Delete objects.py once the package holds everything.
- server/objects/__init__.py re-exports every name other files import, private
  ones included (grep "from objects import"), so the `from objects import ...`
  lines elsewhere stay unchanged.
- After the re-exports, __init__.py sets __module__ to 'objects' on every
  class a submodule defines:
      for _value in list(globals().values()):
          if isinstance(_value, type) and _value.__module__.startswith('objects.'):
              _value.__module__ = 'objects'
  AppCreator (_classes_from_module) only generates schemas for classes whose
  __module__ is 'objects', and test_route_response_models.py asserts it.
- Submodules import from each other directly
  (from objects.customer import Customer), never `from objects import ...`,
  which would be circular.
- A string (forward) annotation resolves in its defining module: import the
  model it names there, or call model_rebuild() in __init__.py once
  everything is imported.
- Update every doc that names objects.py
  (grep -rl "objects.py" CLAUDE.md server/*.md .claude)."""),
    ('*.py', """\
Split it by feature into sibling modules, one responsibility per file, and
import them where the code was used."""),
    ('*', """\
Split it by feature into sibling files, one responsibility per file, and
import them where the code was used."""),
]

RULES = """\
- Move code only: no behaviour changes, renames or new features. Every caller
  keeps working.
- Every new file must be at or under the limit; aim well below so it has room
  to grow.
- Never raise MAX_LINES, add an exclusion or a skip to this test, or squeeze
  lines (joined statements, deleted comments or blank lines) to get under it.
- Never modify an assert in any test. Fix the code or the test setup instead.
- Patched names: a patch must target the module where the moved code looks the
  name up. After moving code out of a module, grep the tests for
  patch('<module>.<name>'), patch.object(<module>Module, '<name>') and
  monkeypatch.setattr(<module>Module, ...), and retarget each one to the new
  module. A patch left on the old module silently stops applying, so a test
  could reach the real database or services.
- Source-reading tests: some tests parse a file's source (grep the tests for
  ast.parse and __file__). Point them at the new files so they still cover
  the moved code.
- Never hand-edit generated files (app.py, appPubSub.py, ServerRequests.ts,
  Server.ts, ApiResponse.ts, schemas/)."""

DONE_WHEN = """\
Use the pytest and AppCreator commands from this repo's CLAUDE.md (Commands):
they give the environment, the test database, the sibling services that must
be running and any files to ignore.
Before you start, from server/ record the test count:
    pytest --collect-only -q | tail -1
The split is done when all of these hold:
1. This test passes or names a different file.
2. The generated code is unchanged: rerun AppCreator, then from the repo root
    git diff --exit-code -- {generated}
3. The test count matches the one you recorded, and the full suite passes.
   Never run another repo's suite at the same time: they share the local
   MongoDB.
4. The split is committed on its own: "Split {file} into <n> modules"."""


@dataclass
class _Block:
    label: str | None  # None for imports, constants and other statements
    start: int
    end: int
    children: list['_Block'] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def _source_files():
    # Untracked-but-not-ignored files too, so a new file is checked before it is committed.
    out = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
        cwd=REPO_ROOT, capture_output=True, check=True).stdout
    for rel in out.decode('utf-8').split('\0'):
        path = PurePosixPath(rel)
        if (rel and path.suffix in SOURCE_SUFFIXES
                and not rel.startswith(CLIENT_DIR)
                and not path.name.startswith('test')
                and not SKIPPED_DIR_NAMES.intersection(path.parts[:-1])
                and rel not in GENERATED_FILES
                and not rel.startswith(GENERATED_DIRS)
                and (REPO_ROOT / rel).is_file()):
            yield rel


def _read_lines(rel: str) -> list[str]:
    return (REPO_ROOT / rel).read_bytes().decode('utf-8', 'replace').splitlines()


def _python_block(node: ast.stmt) -> _Block:
    decorators = getattr(node, 'decorator_list', [])
    start = min([node.lineno] + [d.lineno for d in decorators])
    if isinstance(node, ast.ClassDef):
        methods = sum(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for child in node.body)
        return _Block(f'class {node.name} ({methods} methods)', start,
                      node.end_lineno,
                      [_python_block(child) for child in node.body])
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _Block(f'def {node.name}', start, node.end_lineno)
    return _Block(None, start, node.end_lineno)


def _row(indent: str, start: int, end: int, text: str) -> str:
    return f'{indent}{start:>6}-{end:<6}{end - start + 1:>6} lines  {text}'


def _summary(blocks: list[_Block]) -> str:
    names = [block.label for block in blocks if block.label]
    if not names:
        return 'other statements'
    shown = ', '.join(names[:3])
    if len(names) > 3:
        shown += f' and {len(names) - 3} more'
    return shown


def _rows(blocks: list[_Block], threshold: int, indent: str = '') -> list[str]:
    rows, small = [], []

    def flushSmall():
        if small:
            rows.append(_row(indent, small[0].start, small[-1].end, _summary(small)))
            small.clear()

    for block in blocks:
        if block.size < threshold:
            small.append(block)
            continue
        flushSmall()
        rows.append(_row(indent, block.start, block.end,
                         block.label or 'other statements'))
        if block.children and block.size > MAX_LINES:
            rows.extend(_rows(block.children, threshold, indent + '    '))
    flushSmall()
    return rows


def _outline(rel: str, lines: list[str]) -> str:
    if PurePosixPath(rel).suffix != '.py':
        return '(no outline for this file type: read it in chunks)'
    try:
        tree = ast.parse('\n'.join(lines))
    except SyntaxError as error:
        return f'(could not parse it: {error})'
    blocks = [_python_block(node) for node in tree.body]
    if not blocks:
        return '(empty file)'
    note = 'Top-level blocks (lines start-end):'
    threshold = OUTLINE_MIN_BLOCK_LINES
    rows = _rows(blocks, threshold)
    while len(rows) > OUTLINE_MAX_ROWS and threshold < len(lines):
        threshold *= 2
        rows = _rows(blocks, threshold)
    return '\n'.join([note] + rows)


def _guidance(rel: str) -> str:
    return next(text for pattern, text in SPLIT_GUIDANCE if fnmatchcase(rel, pattern))


def _section(title: str, body: str) -> str:
    return f'== {title} ==\n{body}'


def _brief(oversized: list[tuple[int, str]]) -> str:
    lineCount, rel = oversized[0]
    queue = oversized[1:]
    task = (f'{rel} has {lineCount} lines; the limit is {MAX_LINES}. '
            'Split this one file now.')
    if queue:
        task += (f' {len(queue)} more oversized file(s) wait after it (QUEUE '
                 'below): leave them for later runs.')
    else:
        task += ' It is the last oversized file.'
    generated = ' '.join(sorted(GENERATED_FILES) + [d.rstrip('/') for d in GENERATED_DIRS])
    sections = [
        f'FILE TOO BIG\n{task}',
        _section('WHAT IS IN IT', _outline(rel, _read_lines(rel))),
        _section('HOW TO SPLIT IT', _guidance(rel)),
        _section('RULES FOR EVERY SPLIT', RULES),
        _section('DONE WHEN', DONE_WHEN.format(file=rel, generated=generated)),
    ]
    if queue:
        sections.append(_section('QUEUE (smallest first)', '\n'.join(
            f'{count:>6}  {path}' for count, path in queue)))
    return '\n\n'.join(sections)


def test_source_files_stay_small_enough_to_read():
    oversized = sorted(
        (len(lines), rel) for rel in _source_files()
        if len(lines := _read_lines(rel)) > MAX_LINES)
    if oversized:
        pytest.fail(_brief(oversized), pytrace=False)
