"""Scan one Star App repo's server/ source into its section of the Pub/Sub map.

pubSubMap.py joins one section per repo into server/pubsub_map.json, the
checked-in map of every Pub/Sub flow across the fleet. This module builds a
section, by AST only (nothing is imported):

- the publishers: every @pubSubDecorator function, its topic, its consumers
  and its payload, i.e. what its return value looks like on the wire;
- the push handlers: every public @route_config method reachable from class
  PubSubRequests, and the message contract each one relies on: the payload
  paths it reads, how (required, optional, non-null, only if present, inside a
  lenient try) and which consumer model field each path feeds;
- the pydantic v2 models both sides use (fields along the C3 MRO, aliases,
  requiredness, nullability, Literal and Enum values).

A section is a pure function of one repo's source. Names resolve inside that
repo only (class names repeat across repos). The source is this repo's working
tree, or a sibling's committed git ref (never its working tree or index).

Pure stdlib and Python 3.10 compatible on purpose: it must run without the
app, its env vars or Mongo, under every repo's interpreter, and it is
byte-identical in every repo (create-star-app ships it).
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = frozenset({'venv', '.venv', 'node_modules', '__pycache__', '.git', 'schemas'})
# This tool's own modules: scanned like any source, but never a class owner.
TOOL_MODULES = frozenset({'pubSubMapScan.py', 'pubSubMapContract.py', 'pubSubMapSection.py', 'pubSubMap.py',
                          'pubSubModels.py'})
MAX_CALL_DEPTH = 5
MAX_PAYLOAD_DEPTH = 3
ROOT = '(root)'
# Variables a git hook (or a caller) may export that would point `git -C <dir>`
# at another repository than <dir>, or change how it finds one. Twin of
# GIT_REDIRECT_VARS in sharedFileCheck.py; keep in step.
GIT_REDIRECT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                     'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_COMMON_DIR', 'GIT_NAMESPACE',
                     'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM')
GIT_TIMEOUT = 60
_GIT_MEMOS = []  # memoizedGit's open memos, innermost last: {(root, args, stdin): stdout or None}
_BOM = '\ufeff'


class MapError(Exception):
    """A hard error: the map cannot be built (exit 2 in the CLI)."""


# ----------------------------------------------------------------------------
# Source access
# ----------------------------------------------------------------------------

def isScannedSource(relPath: str) -> bool:
    """True for a repo-relative POSIX path the map scans: server/**/*.py, minus
    tests, conftest and the vendored/generated folders."""
    parts = relPath.split('/')
    if len(parts) < 2 or parts[0] != 'server' or not relPath.endswith('.py'):
        return False
    if parts[-1].startswith('test') or parts[-1] == 'conftest.py':
        return False
    return not any(part in SKIP_DIRS for part in parts[1:-1])


def normaliseSource(data: bytes) -> str:
    """Decoded source with any BOM dropped and CRLF turned into LF, so a CRLF
    checkout and an LF one give the same AST and the same map bytes."""
    text = data.decode('utf-8', 'replace')
    if text.startswith(_BOM):
        text = text[1:]
    return text.replace('\r\n', '\n')


def gitEnv() -> dict:
    """os.environ minus GIT_REDIRECT_VARS, with optional locks and prompts off.
    Twin of _gitEnv in sharedFileCheck.py; keep in step."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_REDIRECT_VARS}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


@contextlib.contextmanager
def memoizedGit():
    """One build or run: inside it, runGit answers a read it already made
    from memory (never ls-files: the working tree may change). Never across
    runs, so a repo changed between two runs is read afresh."""
    _GIT_MEMOS.append(_GIT_MEMOS[-1] if _GIT_MEMOS else {})
    try:
        yield
    finally:
        _GIT_MEMOS.pop()


def runGit(root: Path, args: list[str], stdin: bytes | None = None) -> bytes | None:
    """stdout of `git -C root <args>` (no shell, no fsmonitor, no CRLF
    conversion, GIT_TIMEOUT), or None when git fails, times out or is missing.
    Twin of _git in sharedFileCheck.py; keep in step."""
    memo = _GIT_MEMOS[-1] if _GIT_MEMOS and args[0] != 'ls-files' else {}
    key = (str(root), tuple(args), stdin)
    if key not in memo:
        try:
            proc = subprocess.run(['git', '-C', str(root), '-c', 'core.fsmonitor=false',
                                   '-c', 'core.autocrlf=false', *args], input=stdin,
                                  capture_output=True, timeout=GIT_TIMEOUT, env=gitEnv())
        except (OSError, subprocess.SubprocessError):
            return None
        memo[key] = proc.stdout if proc.returncode == 0 else None
    return memo[key]


def isRepoRoot(directory: Path) -> bool:
    """True when `directory` is itself the top of a git checkout (a folder
    inside some other repo is not). Twin of _isOwnCheckout in
    sharedFileCheck.py; keep in step."""
    if not Path(directory).is_dir():
        return False
    top = runGit(directory, ['rev-parse', '--show-toplevel'])
    if top is None:
        return False
    try:
        return os.path.samefile(top.decode('utf-8', 'replace').strip(), directory)
    except OSError:
        return False


def readWorkingTree(root: Path) -> dict[str, str]:
    """{server-relative path: source} from root's working tree: tracked files
    plus untracked ones git does not ignore (os.walk when there is no git)."""
    listing = runGit(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard',
                            '--', 'server'])
    rels = None if listing is None else [rel for rel in listing.decode('utf-8', 'replace').split('\0') if rel]
    return readServerDir(root / 'server', rels)


def readServerDir(server: Path, rels: list | None = None) -> dict[str, str]:
    """{server-relative path: source} of the scanned sources in `server`, a
    repo's server/ folder whatever its name: those among `rels` (repo-relative
    paths under server/, as git lists them), else every one os.walk finds,
    as readWorkingTree does without git and checkPubSubTopics.py does in the
    image (server/ at its root, no git)."""
    if rels is None:
        rels = []
        for base, dirs, names in os.walk(server):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            rels.extend('server/' + (Path(base) / name).relative_to(server).as_posix() for name in names)
    files = {}
    for rel in sorted(set(rels)):
        path = server / rel[len('server/'):]
        if isScannedSource(rel) and path.is_file():
            files[rel[len('server/'):]] = normaliseSource(path.read_bytes())
    return files


def lsTreeRecords(listing: bytes) -> list:
    """(mode, type, object id, path) of each `git ls-tree -z` record
    (`<mode> <type> <id>` TAB `<path>` NUL, so a newline in a path cannot
    shift the parse); a record of any other shape is left out."""
    out = []
    for record in listing.split(b'\0'):
        meta, tab, rel = record.partition(b'\t')
        fields = meta.split(b' ')
        if tab and len(fields) == 3:
            out.append((fields[0], fields[1], fields[2], rel.decode('utf-8', 'replace')))
    return out


def isRegularBlob(mode: bytes, kind: bytes) -> bool:
    """A regular file in a git tree: a symlink or a submodule counts as no file."""
    return kind == b'blob' and mode in (b'100644', b'100755')


def readGitRef(root: Path, ref: str) -> dict[str, str] | None:
    """{server-relative path: source} as committed at `ref`: regular blobs
    listed by `ls-tree -r -z` (lsTreeRecords), then read by object id with
    one `cat-file --batch`, each header checked. None when the ref cannot be
    read. Twin of _lsTreeBlobs + _catFileBlobs (parsers parseLsTree /
    parseCatFile) in sharedFileCheck.py; keep in step."""
    listing = runGit(root, ['ls-tree', '-r', '-z', ref, '--', 'server'])
    if listing is None:
        return None
    wanted = sorted((name, oid) for mode, kind, oid, name in lsTreeRecords(listing)
                    if isRegularBlob(mode, kind) and isScannedSource(name))
    if not wanted:
        return {}
    data = runGit(root, ['cat-file', '--batch'], stdin=b''.join(oid + b'\n' for _, oid in wanted))
    if data is None:
        return None
    files, pos = {}, 0
    for name, oid in wanted:
        end = data.find(b'\n', pos)
        header = data[pos:end].split(b' ') if end >= 0 else []
        if len(header) != 3 or header[0] != oid or header[1] != b'blob' or not header[2].isdigit():
            raise MapError(f'unexpected git cat-file output for {name} in {root} at {ref}')
        size, pos = int(header[2]), end + 1
        if data[pos + size:pos + size + 1] != b'\n':
            raise MapError(f'git cat-file output for {name} in {root} at {ref} is cut short')
        files[name[len('server/'):]] = normaliseSource(data[pos:pos + size])
        pos += size + 1
    return files


def readProjectName(source: str, where: str = 'server/AppConfig.py') -> str | None:
    """The projectName class Project assigns in AppConfig.py: its first
    class-level `projectName = '...'`, else the first (in source order)
    `self.projectName = '...'` in its methods. None when it assigns none.
    Twin of readProjectName in sharedFileCheck.py; keep in step."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise MapError(f'{where} does not parse: {e}') from None
    project = _projectClass(tree.body)
    if project is None:
        return None
    for statement in project.body:
        value = _assignedString(statement, lambda target: isinstance(target, ast.Name)
                                and target.id == 'projectName')
        if value is not None:
            return value
    found = []
    for method in project.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(method):
                value = _assignedString(node, _isSelfProjectName)
                if value is not None:
                    found.append((node.lineno, node.col_offset, value))
    return min(found)[2] if found else None


def _projectClass(nodes):
    """The first class Project in source order, at module level or inside
    module-level if/try/with/for/while blocks, never inside a def or another
    class. Twin of _projectClass in sharedFileCheck.py; keep in step."""
    for node in nodes:
        if isinstance(node, ast.ClassDef) and node.name == 'Project':
            return node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        found = _projectClass(ast.iter_child_nodes(node))
        if found is not None:
            return found
    return None


def _isSelfProjectName(target: ast.AST) -> bool:
    return (isinstance(target, ast.Attribute) and target.attr == 'projectName'
            and isinstance(target.value, ast.Name) and target.value.id == 'self')


def _assignedString(node: ast.AST, isTarget) -> str | None:
    """The string constant `node` assigns to a target isTarget accepts."""
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        return None
    if (any(isTarget(target) for target in targets) and isinstance(value, ast.Constant)
            and isinstance(value.value, str)):
        return value.value
    return None


def moduleNameFor(rel: str) -> str:
    """Import name of a server-relative path ('actions/X.py' -> 'actions.X')."""
    parts = rel[:-3].split('/')
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def segKeys(seg) -> tuple:
    """The payload keys a path segment accepts. A model field read by alias
    or name (populate_by_name) is a tuple segment: (primary, *alternatives)."""
    return seg if isinstance(seg, tuple) else (seg,)


def pathText(path: tuple) -> str:
    """A payload path as the map prints it: res.item.category, items[].unit."""
    out = ''
    for seg in path:
        key = segKeys(seg)[0]
        out += key if key == '[]' else (('.' if out else '') + key)
    return out or ROOT


def pathAltKeys(path: tuple) -> list:
    """Per segment, the alternative keys beyond the printed one ([] when a
    path has none at all, which is the usual case)."""
    alts = [list(segKeys(seg)[1:]) for seg in path]
    return alts if any(alts) else []


def sourceText(module: 'Module', node: ast.AST) -> str:
    """The node's source text with whitespace collapsed (stable across
    interpreter versions, unlike ast.unparse)."""
    return ' '.join((ast.get_source_segment(module.text, node) or '?').split())


def finalName(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def decoratorCall(node, name: str):
    """The decorator of `node` named `name` (Name, Attribute or Call form):
    the Call node, True for a bare decorator, or None."""
    for dec in getattr(node, 'decorator_list', []):
        if finalName(dec) == name:
            return dec if isinstance(dec, ast.Call) else True
    return None


def kwarg(call, name: str):
    if isinstance(call, ast.Call):
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
    return None


def constStr(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def ownStatements(body: list) -> list:
    """Every statement and expression under `body`, skipping nested defs,
    classes and lambdas (they are not the function's own code)."""
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    out, stack = [], [n for n in reversed(body) if not isinstance(n, defs)]
    while stack:
        node = stack.pop()
        out.append(node)
        for child in reversed(list(ast.iter_child_nodes(node))):
            if not isinstance(child, defs):
                stack.append(child)
    return out


# ----------------------------------------------------------------------------
# Module and import index
# ----------------------------------------------------------------------------

class Module:
    """One parsed source file and its top-level names."""

    def __init__(self, repo: 'Repo', rel: str, text: str):
        self.repo, self.rel, self.text = repo, rel, text
        self.name = moduleNameFor(rel)
        self.isPackage = rel == '__init__.py' or rel.endswith('/__init__.py')
        self._tree = None
        self._names = None
        self.stars: list[str] = []

    @property
    def tree(self) -> ast.Module:
        if self._tree is None:
            try:
                self._tree = ast.parse(self.text, filename=self.rel)
            except SyntaxError as e:
                raise MapError(
                    f'Could not parse server/{self.rel} (line {e.lineno}: {e.msg}) with Python '
                    f'{sys.version.split()[0]} at {sys.executable}') from None
        return self._tree

    def _absolute(self, module: str | None, level: int) -> str:
        if not level:
            return module or ''
        base = self.name.split('.') if self.isPackage else self.name.split('.')[:-1]
        base = base[:len(base) - (level - 1)] if level > 1 else base
        return '.'.join([*base, *([module] if module else [])])

    @property
    def names(self) -> dict:
        """{name: binding} for the module's top level (also inside top-level
        if/try blocks); the last binding wins, as in Python."""
        if self._names is None:
            self._names = {}
            self._collect(self.tree.body)
        return self._names

    def _collect(self, body: list) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._names[stmt.name] = ('def', stmt)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self._names[target.id] = ('value', stmt.value)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
                self._names[stmt.target.id] = ('value', stmt.value)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        self._names[alias.asname] = ('import', alias.name)
                    else:
                        self._names[alias.name.split('.')[0]] = ('import', alias.name.split('.')[0])
            elif isinstance(stmt, ast.ImportFrom):
                source = self._absolute(stmt.module, stmt.level)
                for alias in stmt.names:
                    if alias.name == '*':
                        self.stars.append(source)
                    else:
                        self._names[alias.asname or alias.name] = ('from', source, alias.name)
            elif isinstance(stmt, ast.If):
                self._collect(stmt.body)
                self._collect(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                for block in (stmt.body, *[h.body for h in stmt.handlers], stmt.orelse, stmt.finalbody):
                    self._collect(block)


class ClassInfo:
    """A class definition in the repo, with its anchor `path::Class`."""

    def __init__(self, module: Module, node: ast.ClassDef, qual: str):
        self.module, self.node, self.name = module, node, node.name
        self.key = (module.rel, qual)
        self.anchor = f'{module.rel}::{qual}'
        self.methods = {s.name: s for s in node.body
                        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.rootParam = None


# Field(...) arguments and type names that constrain a field's value beyond
# its type (pydantic, annotated-types, validators inside Annotated).
_CONSTRAINT_KWARGS = frozenset({'min_length', 'max_length', 'pattern', 'regex', 'gt', 'ge', 'lt', 'le',
                                'multiple_of', 'min_items', 'max_items', 'max_digits', 'decimal_places',
                                'allow_inf_nan', 'strict', 'unique_items', 'const'})
_CONSTRAINED_TYPE = re.compile(
    r'con(str|int|float|list|set|frozenset|decimal|date|bytes)|(Strict|Positive|Negative|NonNegative|NonPositive'
    r'|Finite|Future|Past|Aware|Naive|Secret|IPv)\w*|\w+(Url|Dsn|Validator)|EmailStr|NameEmail|UUID\d|Json'
    r'|StringConstraints|MinLen|MaxLen|Len|Gt|Ge|Lt|Le|Interval|MultipleOf|Predicate')


class FieldInfo:
    """One pydantic field as the map needs it. `constrained`: something
    beyond its type limits its value (a Field constraint, a constrained
    type, an after validator, a model validator)."""

    def __init__(self, name, ftype, required, hasDefault, inKeys, outAlias, unverifiable,
                 exclude, computed, beforeValidators, constrained=False):
        self.name, self.type, self.required = name, ftype, required
        self.hasDefault = hasDefault
        self.inKeys, self.outAlias = inKeys, outAlias
        self.unverifiable, self.exclude, self.computed = unverifiable, exclude, computed
        # the defs of its before/wrap/plain validators (they see the raw value)
        self.beforeValidators = tuple(beforeValidators)
        self.validatorBefore = bool(self.beforeValidators)
        self.nullable, self.constrained = isNullable(ftype), constrained

    def outKey(self, byAlias: bool) -> str:
        return self.outAlias if byAlias and self.outAlias else self.name


class ModelInfo:
    def __init__(self, ci: ClassInfo, fields: dict, extra, modelValidated: bool, rootType):
        self.ci, self.fields, self.extra = ci, fields, extra
        self.modelValidated, self.rootType = modelValidated, rootType  # any model/root validator

    def acceptedKeys(self) -> tuple:
        return tuple(sorted({k for f in self.fields.values() if not f.computed for k in f.inKeys}))


# ---- structured types: tuples, printed by printType (never ast.unparse) ----
ANY = ('any',)
NONE = ('none',)
_PRIMS = {'str': ('str',), 'int': ('int',), 'float': ('float',), 'bool': ('bool',),
          'bytes': ('str',), 'object': ANY, 'complex': ('float',)}
_EXT_TYPES = {'typing.Any': ANY, 'datetime.datetime': ('datetime',), 'datetime.date': ('date',),
              'datetime.time': ('str',), 'decimal.Decimal': ('float',), 'uuid.UUID': ('str',),
              'bson.ObjectId': ('str',), 'bson.objectid.ObjectId': ('str',),
              'pydantic.EmailStr': ('str',), 'pydantic.HttpUrl': ('str',), 'pydantic.AnyUrl': ('str',)}
_LISTS = {'list', 'List', 'set', 'Set', 'tuple', 'Tuple', 'frozenset', 'FrozenSet', 'Sequence',
          'Iterable', 'MutableSequence', 'Collection', 'conlist'}
_DICTS = {'dict', 'Dict', 'Mapping', 'MutableMapping', 'DefaultDict', 'OrderedDict'}


def union(members) -> tuple:
    flat = []
    for member in members:
        flat.extend(member[1] if member[0] == 'union' else [member])
    if any(m == ANY for m in flat):
        return ANY
    unique = {printType(m): m for m in flat}
    ordered = [unique[k] for k in sorted(unique, key=lambda k: (k == 'None', k))]
    return ordered[0] if len(ordered) == 1 else ('union', tuple(ordered))


def isNullable(t) -> bool:
    return t == ANY or t == NONE or (t[0] == 'union' and any(isNullable(m) for m in t[1]))


def nonNull(t) -> tuple:
    if t[0] == 'union':
        return union([m for m in t[1] if m != NONE]) if any(m != NONE for m in t[1]) else NONE
    return t


def printType(t) -> str:
    kind = t[0]
    if kind == 'none':
        return 'None'
    if kind in ('list', 'dict'):
        inner = printType(t[1])
        return kind if inner == 'any' else (f'list[{inner}]' if kind == 'list' else f'dict[str, {inner}]')
    if kind == 'union':
        return ' | '.join(printType(m) for m in t[1])
    if kind == 'lit':  # no values known (an auto() Enum, say): [...]
        return 'Literal[' + (', '.join(repr(v) for v in t[1]) or '...') + ']'
    if kind == 'enum':
        return t[1] + '[' + (', '.join(repr(v) for v in t[2]) or '...') + ']'
    if kind == 'model':
        return t[2]
    return kind


def literalValues(t):
    """The set of Literal/Enum values `t` allows (None excluded), or None when
    `t` allows anything of its base type or its values are not known (an
    auto() Enum)."""
    members = t[1] if t[0] == 'union' else (t,)
    values = set()
    for m in members:
        if m == NONE:
            continue
        known = m[1] if m[0] == 'lit' else m[2] if m[0] == 'enum' else ()
        if not known:
            return None
        values.update(known)
    return values


# ----------------------------------------------------------------------------
# Repo: name resolution, MRO, models
# ----------------------------------------------------------------------------

class Repo:
    """Every scanned module of one repo, with per-repo name resolution."""

    def __init__(self, files: dict[str, str]):
        self.modules = {rel: Module(self, rel, text) for rel, text in sorted(files.items())}
        self.byName = {m.name: m for m in self.modules.values()}
        self._resolved, self._classes, self._mro, self._models = {}, {}, {}, {}
        self._byClassName = None
        self.heuristic, self.ambiguous = set(), set()

    def module(self, rel: str) -> Module | None:
        return self.modules.get(rel)

    def classInfo(self, module: Module, node: ast.ClassDef, qual: str | None = None) -> ClassInfo:
        key = (module.rel, qual or node.name)
        if key not in self._classes:
            self._classes[key] = ClassInfo(module, node, qual or node.name)
        return self._classes[key]

    def resolveName(self, module: Module, name: str, _seen: frozenset = frozenset()):
        """What `name` means at module top level: ('class', ClassInfo),
        ('func', Module, node, owner), ('value', Module, expr),
        ('module', Module), ('ext', dotted) or None."""
        key = (module.rel, name)
        if key in self._resolved:
            return self._resolved[key]
        if key in _seen:
            return None
        seen = _seen | {key}
        found = None
        entry = module.names.get(name)
        if entry is None:
            for star in module.stars:
                target = self.byName.get(star)
                found = self.resolveName(target, name, seen) if target else None
                if found:
                    break
            if found is None and module.isPackage and f'{module.name}.{name}' in self.byName:
                found = ('module', self.byName[f'{module.name}.{name}'])
        elif entry[0] == 'def':
            node = entry[1]
            found = (('class', self.classInfo(module, node)) if isinstance(node, ast.ClassDef)
                     else ('func', module, node, None))
        elif entry[0] == 'value':
            found = ('value', module, entry[1])
            if isinstance(entry[1], (ast.Name, ast.Attribute)):
                found = self.resolveExpr(module, entry[1], seen) or found
        elif entry[0] == 'import':
            target = self.byName.get(entry[1])
            found = ('module', target) if target else ('ext', entry[1])
        else:
            source, attr = entry[1], entry[2]
            target, sub = self.byName.get(source), self.byName.get(f'{source}.{attr}')
            if target is not None and (attr in target.names or target.stars or sub is None):
                found = self.resolveName(target, attr, seen)
            if found is None and sub is not None:
                found = ('module', sub)
            if found is None and target is None:
                found = ('ext', f'{source}.{attr}')
        self._resolved[key] = found
        return found

    def resolveExpr(self, module: Module, node, _seen: frozenset = frozenset()):
        """resolveName for a Name or a dotted Attribute chain (Subscript bases
        such as RootModel[T] resolve to their value)."""
        if isinstance(node, ast.Name):
            return self.resolveName(module, node.id, _seen)
        if isinstance(node, ast.Subscript):
            return self.resolveExpr(module, node.value, _seen)
        if not isinstance(node, ast.Attribute):
            return None
        base = self.resolveExpr(module, node.value, _seen)
        if base is None:
            return None
        if base[0] == 'module':
            return self.resolveName(base[1], node.attr, _seen)
        if base[0] == 'ext':
            return ('ext', f'{base[1]}.{node.attr}')
        if base[0] == 'class':
            return self.member(base[1], node.attr)
        return None

    def classByName(self, name: str):
        """Fallback: a class name that is unique in the repo (flagged as a
        heuristic); ambiguous names stay unresolved and are recorded."""
        if self._byClassName is None:
            self._byClassName = {}
            pattern = re.compile(r'^class\s+(\w+)', re.MULTILINE)
            for module in self.modules.values():
                if module.rel in TOOL_MODULES:
                    continue
                for match in pattern.finditer(module.text):
                    self._byClassName.setdefault(match.group(1), set()).add(module.rel)
        rels = self._byClassName.get(name, set())
        candidates = []
        for rel in sorted(rels):
            entry = self.modules[rel].names.get(name)
            if entry and entry[0] == 'def' and isinstance(entry[1], ast.ClassDef):
                candidates.append(self.classInfo(self.modules[rel], entry[1]))
        if len(candidates) == 1:
            self.heuristic.add(f'{name} -> {candidates[0].anchor}')
            return candidates[0]
        if len(candidates) > 1:
            self.ambiguous.add(f"{name}: {', '.join(c.anchor for c in candidates)}")
        return None

    def resolveClass(self, module: Module, node) -> ClassInfo | None:
        found = self.resolveExpr(module, node)
        if found and found[0] == 'class':
            return found[1]
        if found is None and isinstance(node, ast.Name) and node.id not in _BUILTIN_NAMES:
            return self.classByName(node.id)
        return None

    def member(self, ci: ClassInfo, name: str):
        """A class attribute along the MRO: a method or a nested class."""
        for c in self.mro(ci):
            if not isinstance(c, ClassInfo):
                continue
            if name in c.methods:
                return ('func', c.module, c.methods[name], c)
            for stmt in c.node.body:
                if isinstance(stmt, ast.ClassDef) and stmt.name == name:
                    return ('class', self.classInfo(c.module, stmt, f'{c.key[1]}.{name}'))
        return None

    def bases(self, ci: ClassInfo) -> list:
        out = []
        for base in ci.node.bases:
            found = self.resolveExpr(ci.module, base)
            if found is None and isinstance(base, ast.Name) and base.id not in _BUILTIN_NAMES:
                c = self.classByName(base.id)
                found = ('class', c) if c else None
            if found and found[0] == 'class' and found[1] is not ci:
                out.append(found[1])
            elif found and found[0] == 'ext':
                ext = _normaliseExt(found[1])
                out.append(ext)
                if ext == 'pydantic.RootModel' and isinstance(base, ast.Subscript):
                    ci.rootParam = base.slice
        return out

    def mro(self, ci: ClassInfo, _stack: frozenset = frozenset()) -> list:
        """C3 linearisation; external bases (pydantic.BaseModel, enum.Enum)
        appear as dotted strings."""
        if ci.key in self._mro:
            return self._mro[ci.key]
        if ci.key in _stack:
            return [ci]
        seqs = []
        bases = self.bases(ci)
        for base in bases:
            seqs.append(list(self.mro(base, _stack | {ci.key})) if isinstance(base, ClassInfo) else [base])
        seqs.append(list(bases))
        result = [ci]
        while True:
            seqs = [s for s in seqs if s]
            if not seqs:
                break
            for seq in seqs:
                head = seq[0]
                if not any(head in s[1:] for s in seqs):
                    break
            else:
                head = seqs[0][0]
            result.append(head)
            seqs = [s[1:] if s[0] == head else [x for x in s if x != head] for s in seqs]
        self._mro[ci.key] = result
        return result

    def isModel(self, ci: ClassInfo) -> bool:
        return any(c in ('pydantic.BaseModel', 'pydantic.RootModel') for c in self.mro(ci))

    def isEnum(self, ci: ClassInfo) -> bool:
        return any(isinstance(c, str) and c.startswith('enum.') for c in self.mro(ci))

    def methodOf(self, ci: ClassInfo, name: str):
        found = self.member(ci, name)
        return found if found and found[0] == 'func' else None

    # ---- types -------------------------------------------------------------
    def annType(self, module: Module, node, depth: int = 0) -> tuple:
        """Structured type of an annotation expression in `module`."""
        if node is None or depth > 12:
            return ANY
        if isinstance(node, ast.Constant):
            if node.value is None:
                return NONE
            if isinstance(node.value, str):
                try:
                    return self.annType(module, ast.parse(node.value, mode='eval').body, depth + 1)
                except SyntaxError:
                    return ANY
            return ANY
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return union([self.annType(module, node.left, depth + 1),
                          self.annType(module, node.right, depth + 1)])
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._namedType(module, node, depth)
        if isinstance(node, ast.Subscript):
            head = finalName(node.value)
            args = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            if head == 'Optional':
                return union([self.annType(module, args[0], depth + 1), NONE])
            if head == 'Union':
                return union([self.annType(module, a, depth + 1) for a in args])
            if head in ('Annotated', 'SkipJsonSchema', 'ClassVar', 'Final', 'Required', 'NotRequired'):
                return self.annType(module, args[0], depth + 1)
            if head == 'Literal':
                values = []
                for a in args:
                    if isinstance(a, ast.Constant):
                        values.append(a.value)
                    else:
                        inner = self.annType(module, a, depth + 1)
                        values.extend(inner[1] if inner[0] == 'lit' else [])
                lits = tuple(sorted({v for v in values if v is not None}, key=repr))
                return union([('lit', lits)] + ([NONE] if None in values else []))
            if head in _LISTS:
                inner = [self.annType(module, a, depth + 1) for a in args
                         if not (isinstance(a, ast.Constant) and a.value is Ellipsis)]
                return ('list', union(inner) if inner else ANY)
            if head in _DICTS:
                return ('dict', self.annType(module, args[1], depth + 1) if len(args) > 1 else ANY)
            if head in ('Type', 'type', 'Callable'):
                return ANY
            return self._namedType(module, node.value, depth)
        return ANY

    def _namedType(self, module: Module, node, depth: int) -> tuple:
        found = self.resolveExpr(module, node)
        name = finalName(node)
        if found is None:
            if name in _PRIMS:
                return _PRIMS[name]
            if name in _LISTS:
                return ('list', ANY)
            if name in _DICTS:
                return ('dict', ANY)
            if name == 'None':
                return NONE
            ci = self.classByName(name) if isinstance(node, ast.Name) and name not in _BUILTIN_NAMES else None
            return self.classType(ci) if ci else ANY
        if found[0] == 'ext':
            dotted = found[1]
            if dotted in _EXT_TYPES:
                return _EXT_TYPES[dotted]
            last = dotted.rsplit('.', 1)[-1]
            if last in _LISTS:
                return ('list', ANY)
            if last in _DICTS:
                return ('dict', ANY)
            if last in ('constr', 'StrictStr', 'NameEmail', 'SecretStr'):
                return ('str',)
            if last in ('conint', 'StrictInt', 'PositiveInt', 'NonNegativeInt'):
                return ('int',)
            if last in ('confloat', 'StrictFloat', 'PositiveFloat', 'NonNegativeFloat'):
                return ('float',)
            return ANY
        if found[0] == 'class':
            return self.classType(found[1])
        if found[0] == 'value':
            return self.annType(found[1], found[2], depth + 1)
        return ANY

    def classType(self, ci: ClassInfo) -> tuple:
        if self.isEnum(ci):
            values = []
            for stmt in ci.node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    values.append(stmt.value.value)
            return ('enum', ci.name, tuple(sorted(set(values), key=repr)))
        if self.isModel(ci):
            info = self.modelInfo(ci)
            label = printType(info.rootType) if info.rootType is not None else ci.name
            return ('model', ci.key, label)
        return ANY

    def rootTypes(self, t, depth: int = 0) -> tuple:
        """`t` with every RootModel replaced by its root type (for Literal
        and numeric comparisons)."""
        if depth > 8 or not isinstance(t, tuple):
            return t
        if t[0] == 'model':
            mi = self.modelOfType(t)
            return self.rootTypes(mi.rootType, depth + 1) if mi and mi.rootType is not None else t
        if t[0] == 'union':
            members = [self.rootTypes(m, depth + 1) for m in t[1]]
            return union(members) if all(isinstance(m, tuple) for m in members) else ('union', tuple(members))
        if t[0] in ('list', 'dict'):
            return (t[0], self.rootTypes(t[1], depth + 1))
        return t

    def modelOfType(self, t) -> ModelInfo | None:
        t = nonNull(t)
        if t[0] == 'model' and t[1] in self._classes:
            return self.modelInfo(self._classes[t[1]])
        return None

    # ---- models ------------------------------------------------------------
    def modelInfo(self, ci: ClassInfo) -> ModelInfo:
        if ci.key in self._models:
            return self._models[ci.key]
        self._models[ci.key] = ModelInfo(ci, {}, None, False, None)  # cycle guard
        classes = [c for c in self.mro(ci) if isinstance(c, ClassInfo)]
        raw, config, before, computed, after = {}, {}, {}, {}, set()
        modelValidated = False
        for c in reversed(classes):
            config.update(_configOf(c))
            for stmt in c.node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    name = stmt.target.id
                    if name.startswith('_') or name == 'model_config' or finalName(
                            stmt.annotation.value if isinstance(stmt.annotation, ast.Subscript)
                            else stmt.annotation) == 'ClassVar':
                        continue
                    raw[name] = (c, stmt)
                    computed.pop(name, None)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if decoratorCall(stmt, 'computed_field'):
                        computed[stmt.name] = (c, stmt)
                    for decName in ('field_validator', 'validator'):
                        dec = decoratorCall(stmt, decName)
                        if isinstance(dec, ast.Call):
                            mode = constStr(kwarg(dec, 'mode')) or 'after'
                            pre = kwarg(dec, 'pre')
                            if mode in ('before', 'wrap', 'plain') or (
                                    isinstance(pre, ast.Constant) and pre.value is True):
                                for target in filter(None, map(constStr, dec.args)):
                                    before.setdefault(target, []).append(stmt)
                            else:
                                after.update(filter(None, map(constStr, dec.args)))
                    modelValidated = modelValidated or any(
                        decoratorCall(stmt, n) for n in ('model_validator', 'root_validator'))
        byName = bool(config.get('populate_by_name') or config.get('validate_by_name')
                      or config.get('allow_population_by_field_name'))
        generated = 'alias_generator' in config
        fields = {}
        rootType = None
        for name, (c, stmt) in raw.items():
            info = self._fieldInfo(c, stmt, byName, generated, before.get('*', []) + before.get(name, []),
                                   modelValidated or bool({'*', name} & after))
            if name == 'root' and 'pydantic.RootModel' in self.mro(ci):
                rootType = info.type
                continue
            fields[name] = info
        if rootType is None and ci.rootParam is not None:
            rootType = self.annType(ci.module, ci.rootParam)
        for name, (c, node) in computed.items():
            dec = decoratorCall(node, 'computed_field')
            alias = constStr(kwarg(dec, 'alias')) if isinstance(dec, ast.Call) else None
            fields[name] = FieldInfo(name, self.annType(c.module, node.returns), False, False, (),
                                     alias, False, False, True, ())
        info = ModelInfo(ci, fields, config.get('extra'), modelValidated, rootType)
        self._models[ci.key] = info
        return info

    def _fieldInfo(self, c: ClassInfo, stmt: ast.AnnAssign, byName: bool, generated: bool,
                   beforeValidators, checked: bool) -> FieldInfo:
        annotation, value = stmt.annotation, stmt.value
        fieldCall = value if isinstance(value, ast.Call) and finalName(value) == 'Field' else None
        if isinstance(annotation, ast.Subscript) and finalName(annotation.value) == 'Annotated':
            meta = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else []
            for m in meta[1:]:
                if isinstance(m, ast.Call) and finalName(m) == 'Field' and fieldCall is None:
                    fieldCall = m
        if value is not None and value is not fieldCall:
            required, hasDefault = False, not _isNoneConst(value)
        elif fieldCall is not None:
            default = fieldCall.args[0] if fieldCall.args else kwarg(fieldCall, 'default')
            if kwarg(fieldCall, 'default_factory') is not None:
                required, hasDefault = False, True
            elif default is None or (isinstance(default, ast.Constant) and default.value is Ellipsis):
                required, hasDefault = True, False
            else:
                required, hasDefault = False, not _isNoneConst(default)
        else:
            required, hasDefault = True, False
        alias = constStr(kwarg(fieldCall, 'alias'))
        validationAlias = kwarg(fieldCall, 'validation_alias')
        serialAlias = constStr(kwarg(fieldCall, 'serialization_alias'))
        unverifiable = generated or (validationAlias is not None and constStr(validationAlias) is None)
        primary = constStr(validationAlias) or alias or stmt.target.id
        inKeys = (primary,) + ((stmt.target.id,) if byName and primary != stmt.target.id else ())
        exclude = kwarg(fieldCall, 'exclude')
        constrained = checked or any(_CONSTRAINED_TYPE.fullmatch(finalName(n) or '') or (
            isinstance(n, ast.Call) and finalName(n) == 'Field'
            and any(kw.arg in _CONSTRAINT_KWARGS for kw in n.keywords))
            for n in [*ast.walk(annotation), *(ast.walk(value) if value is not None else ())])
        return FieldInfo(stmt.target.id, self.annType(c.module, annotation), required, hasDefault,
                         inKeys, serialAlias or alias, unverifiable,
                         isinstance(exclude, ast.Constant) and exclude.value is True, False,
                         beforeValidators, constrained)


_BUILTIN_NAMES = frozenset(dir(builtins)) | {'self', 'cls'}


def _isNoneConst(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _normaliseExt(dotted: str) -> str:
    last = dotted.rsplit('.', 1)[-1]
    if dotted.startswith('pydantic') and last in ('BaseModel', 'RootModel'):
        return f'pydantic.{last}'
    if dotted.startswith('enum.') or dotted in ('enum',):
        return dotted
    return dotted


def _configOf(c: ClassInfo) -> dict:
    """model_config = ConfigDict(...) / {...}, or a legacy class Config."""
    out = {}
    for stmt in c.node.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if not any(isinstance(t, ast.Name) and t.id == 'model_config' for t in targets):
                continue
            value = stmt.value
            pairs = []
            if isinstance(value, ast.Call):
                pairs = [(kw.arg, kw.value) for kw in value.keywords if kw.arg]
            elif isinstance(value, ast.Dict):
                pairs = [(constStr(k), v) for k, v in zip(value.keys, value.values) if constStr(k)]
            for key, node in pairs:
                out[key] = _configValue(node)
        elif isinstance(stmt, ast.ClassDef) and stmt.name == 'Config':
            for inner in stmt.body:
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if isinstance(t, ast.Name):
                            out[t.id] = _configValue(inner.value)
    return out


def _configValue(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return True


# ----------------------------------------------------------------------------
# Publisher payloads: what a @pubSubDecorator function's return looks like
# ----------------------------------------------------------------------------

UNKNOWN = ('unknown',)
ALWAYS, MAYBE = 'always', 'maybe'
_DUMP_MAYBE_KWARGS = ('exclude_none', 'exclude_unset', 'exclude_defaults', 'include', 'exclude')
PAYLOAD_TAGS = ('modelDump', 'persistedDump', 'dictLiteral')


class Obj:
    """A JSON object on the publisher side: {key: (shape, presence,
    publisherField)}. A model dump or a literal dict is `closed` (no other
    keys); a responseModel or declared shape is open (the validation that
    vouches for it ignores extra keys). `mode` is how nested model types
    expand: ('dump', byAlias, allMaybe) or ('input',)."""
    __slots__ = ('fields', 'closed', 'label', 'mode')

    def __init__(self, fields: dict, closed: bool, label: str | None, mode):
        self.fields, self.closed, self.label, self.mode = fields, closed, label, mode


def shapeUnion(shapes) -> object:
    """Union of publisher shapes. Objs merge key by key (always only when
    every branch always sends the key); anything unknown makes it unknown."""
    flat = []
    for s in shapes:
        flat.extend(s[1] if isinstance(s, tuple) and s[0] == 'union' else [s])
    if any(s == UNKNOWN for s in flat):
        return UNKNOWN
    objs = [s for s in flat if isinstance(s, Obj)]
    rest = [s for s in flat if not isinstance(s, Obj)]
    merged = objs[0] if objs else None
    for other in objs[1:]:
        fields = {}
        for key in sorted(set(merged.fields) | set(other.fields)):
            a, b = merged.fields.get(key), other.fields.get(key)
            if a and b:
                fields[key] = (shapeUnion([a[0], b[0]]), ALWAYS if a[1] == b[1] == ALWAYS else MAYBE,
                               a[2] or b[2])
            else:
                one = a or b
                fields[key] = (one[0], MAYBE, one[2])
        merged = Obj(fields, merged.closed and other.closed,
                     merged.label if merged.label == other.label else None,
                     merged.mode if merged.mode == other.mode else ('input',))
    members = ([merged] if merged else []) + ([union(rest)] if rest else [])
    return members[0] if len(members) == 1 else ('union', tuple(members))


def printShape(s) -> str:
    if isinstance(s, Obj):
        return s.label or 'dict'
    if s == UNKNOWN:
        return 'unknown'
    if s[0] == 'union':
        parts = sorted({printShape(m) for m in s[1]}, key=lambda p: (p == 'None', p))
        return ' | '.join(parts)
    if s[0] == 'list':
        inner = printShape(s[1])
        return 'list' if inner == 'any' else f'list[{inner}]'
    return printType(s)


class PayloadResolver:
    """Classifies a publisher's own return statements (nested defs, lambdas,
    'idempotent' and None are skipped): a model dump, a literal dict, a
    persisted dump (db.create/update of a model dump), or a call to a repo
    function (followed to MAX_PAYLOAD_DEPTH). A route_config responseModel
    supplies the types and, where a return is unknown, the presence too
    (required -> always, defaulted -> maybe)."""

    def __init__(self, repo: Repo):
        self.repo = repo
        self._objs, self._fnMemo = {}, {}

    def modelObj(self, ci: ClassInfo, mode) -> object:
        key = (ci.key, mode)
        if key not in self._objs:
            info = self.repo.modelInfo(ci)
            if info.rootType is not None:
                self._objs[key] = info.rootType
            else:
                fields = {}
                for f in info.fields.values():
                    if mode[0] == 'dump':
                        if not f.exclude:
                            fields[f.outKey(mode[1])] = (f.type, MAYBE if mode[2] else ALWAYS,
                                                         f'{ci.anchor}.{f.name}')
                    elif not f.computed:
                        fields[f.inKeys[0]] = (f.type, ALWAYS if f.required else MAYBE,
                                               f'{ci.anchor}.{f.name}')
                self._objs[key] = Obj(fields, mode[0] == 'dump' and info.extra != 'allow', ci.name, mode)
        return self._objs[key]

    def expand(self, shape, mode):
        """`shape` with a model type replaced by its Obj in `mode`."""
        if isinstance(shape, tuple) and shape[0] == 'model' and shape[1] in self.repo._classes:
            return self.modelObj(self.repo._classes[shape[1]], mode or ('input',))
        return shape

    def resolve(self, module: Module, node, owner, declared: ClassInfo | None = None):
        """(source, model anchor or None, shape) of a publisher's payload."""
        rm = None
        rc = decoratorCall(node, 'route_config')
        if isinstance(rc, ast.Call) and kwarg(rc, 'responseModel') is not None:
            rm = self.repo.resolveClass(module, kwarg(rc, 'responseModel'))
            rm = rm if rm is not None and self.repo.isModel(rm) else None
        results = self._fnReturns(module, node, owner, 0)
        known = [r for r in results if r[1] in PAYLOAD_TAGS]
        if not results or len(known) < len(results):
            if rm is not None:
                return 'responseModel', rm.anchor, self.modelObj(rm, ('input',))
            if declared is not None:
                return 'declared', declared.anchor, self.modelObj(declared, ('input',))
            return 'unknown', None, UNKNOWN
        shape, source, model = _combine(known)
        typed = rm or declared
        if typed is not None:
            shape = self._overlay(shape, ('model', typed.key, typed.name))
            model = model or typed.anchor
        return source, model, shape

    def _overlay(self, shape, rmType):
        """Fill the unknown values of a literal-dict shape with the
        responseModel's (else the declared model's) types (input mode), key
        by key."""
        if shape == UNKNOWN or shape == ANY:
            return self.expand(rmType, ('input',))
        base = nonNull(rmType) if isinstance(rmType, tuple) else rmType
        if isinstance(shape, tuple) and shape[0] == 'list' and isinstance(base, tuple) and base[0] == 'list':
            return ('list', self._overlay(shape[1], base[1]))
        if isinstance(shape, tuple) and shape[0] == 'union':
            return ('union', tuple(m if m == NONE else self._overlay(m, rmType) for m in shape[1]))
        if isinstance(shape, Obj) and shape.label is None:
            target = self.expand(nonNull(rmType), ('input',))
            if not isinstance(target, Obj):
                return shape
            fields = {}
            for key, (sub, presence, anchor) in shape.fields.items():
                rf = target.fields.get(key)
                fields[key] = ((self._overlay(sub, rf[0]) if rf else sub), presence,
                               anchor or (rf[2] if rf else None))
            return Obj(fields, shape.closed, None, ('input',))
        return shape

    def _fnReturns(self, module, node, owner, depth) -> list:
        key = (module.rel, node.lineno, node.name)
        if key in self._fnMemo:
            return self._fnMemo[key]
        self._fnMemo[key] = [(UNKNOWN, 'unknown', None)]  # recursion guard
        fc = _FnCtx(self.repo, module, node, owner)
        out = []
        for stmt in ownStatements(node.body):
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                v = stmt.value
                if isinstance(v, ast.Constant) and (v.value is None or isinstance(v.value, str)):
                    continue
                fc.at = stmt.lineno
                out.append(self._classify(v, fc, depth))
        self._fnMemo[key] = out
        return out

    def _classify(self, e, fc, depth) -> tuple:
        """(shape, tag, model anchor) of an expression a publisher returns."""
        if isinstance(e, ast.Dict):
            fields, closed = {}, True
            for k, v in zip(e.keys, e.values):
                if k is None:
                    spread = self._classify(v, fc, depth)[0]
                    if isinstance(spread, Obj):
                        fields.update(spread.fields)
                        closed = closed and spread.closed
                    else:
                        closed = False
                elif constStr(k) is not None:
                    fields[constStr(k)] = (self._classify(v, fc, depth)[0], ALWAYS, None)
                else:
                    closed = False
            return Obj(fields, closed, None, ('input',)), 'dictLiteral', None
        if isinstance(e, ast.Name):
            options = fc.reaching(e.id)
            if not options:
                t = fc.paramType(e.id)
                return (t if t is not None and t[0] not in ('model', 'any', 'dict') else UNKNOWN), 'value', None
            if e.id in fc.visiting:
                return UNKNOWN, 'unknown', None
            fc.visiting.add(e.id)
            results = [self._classify(b[1], fc, depth) for b in options if b[0] == 'assign']
            stores = fc.storesAfter(e.id, min(b[2] for b in options))
            fc.visiting.discard(e.id)
            if not results or len(results) < len(options):
                return UNKNOWN, 'unknown', None
            if stores:
                results = [(_withChanges(r[0], stores, lambda x: self._classify(x, fc, depth)[0]),) + r[1:]
                           for r in results]
            if all(r[1] in PAYLOAD_TAGS for r in results):
                return _combine(results)
            return shapeUnion([r[0] for r in results]), 'value', None
        if isinstance(e, ast.Call):
            return self._classifyCall(e, fc, depth)
        if isinstance(e, ast.Constant):
            return _constType(e.value), 'value', None
        if isinstance(e, (ast.List, ast.Tuple, ast.Set)):
            return ('list', shapeUnion([self._classify(x, fc, depth)[0] for x in e.elts]) if e.elts
                    else ANY), 'value', None
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return ('list', self._classify(e.elt, fc.withComprehension(e), depth)[0]), 'value', None
        if isinstance(e, ast.IfExp):
            return shapeUnion([self._classify(e.body, fc, depth)[0],
                               self._classify(e.orelse, fc, depth)[0]]), 'value', None
        if isinstance(e, ast.BoolOp):
            return shapeUnion([self._classify(x, fc, depth)[0] for x in e.values]), 'value', None
        if isinstance(e, ast.JoinedStr):
            return ('str',), 'value', None
        if isinstance(e, ast.Compare) or (isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not)):
            return ('bool',), 'value', None
        t = fc.typeOf(e, 0)
        if t is not None and t[0] not in ('model', 'any'):
            return t, 'value', None
        return UNKNOWN, 'unknown', None

    def _classifyCall(self, e, fc, depth) -> tuple:
        repo, func = self.repo, e.func
        if isinstance(func, ast.Attribute) and func.attr in ('model_dump', 'dict'):
            mi = repo.modelOfType(fc.typeOf(func.value, 0) or ANY)
            if mi is None:
                return UNKNOWN, 'unknown', None
            allMaybe = any(kwarg(e, k) is not None and not (
                isinstance(kwarg(e, k), ast.Constant) and not kwarg(e, k).value) for k in _DUMP_MAYBE_KWARGS)
            byAlias = isinstance(kwarg(e, 'by_alias'), ast.Constant) and kwarg(e, 'by_alias').value is True
            return self.modelObj(mi.ci, ('dump', byAlias, allMaybe)), 'modelDump', mi.ci.anchor
        if (isinstance(func, ast.Attribute) and func.attr in ('create', 'update')
                and isinstance(func.value, ast.Name) and func.value.id == 'db'):
            index = 0 if func.attr == 'create' else 1
            if len(e.args) > index:
                shape, tag, model = self._classify(e.args[index], fc, depth)
                if tag == 'modelDump' and isinstance(shape, Obj):
                    return _persisted(shape), 'persistedDump', model
            return UNKNOWN, 'unknown', None
        if isinstance(func, ast.Name) and func.id in _BUILTIN_RESULTS:
            return _BUILTIN_RESULTS[func.id], 'value', None
        callee = fc.callee(e)
        if callee is not None and callee[0] == 'func' and depth < MAX_PAYLOAD_DEPTH:
            results = self._fnReturns(callee[1], callee[2], callee[3], depth + 1)
            if results and all(r[1] in PAYLOAD_TAGS for r in results):
                return _combine(results)
        t = fc.typeOf(e, 0)
        if t is not None and t[0] not in ('model', 'any'):
            return t, 'value', None
        return UNKNOWN, 'unknown', None


def _persisted(shape: Obj) -> Obj:
    """A stored document: the db layer always writes _id and _version, so
    they are non-null whatever the model's own defaults say (the fleet's
    StoredDocumentKeys convention); other stored keys may exist (open)."""
    fields = dict(shape.fields)
    for key, t in (('_id', ('str',)), ('_version', ('int',))):
        if key in fields:
            fields[key] = (t, ALWAYS, fields[key][2])
    return Obj(fields, False, shape.label, shape.mode)


def _withChanges(shape, changes, classify):
    """A dict shape (a literal or a model dump) after the changes made to it
    before the return: `x['k'] = v` adds k, `del x['k']` / `x.pop('k')`
    removes it, update()/setdefault()/`|=` leave it open. A change inside a
    branch or loop only makes the key maybe-present."""
    if not isinstance(shape, Obj):
        return shape
    fields, closed = dict(shape.fields), shape.closed
    for kind, key, value, _, topLevel in changes:
        old = fields.get(key)
        if kind == 'set':
            fields[key] = (classify(value), ALWAYS if topLevel or (old and old[1] == ALWAYS) else MAYBE, None)
        elif kind == 'del' and old is not None:
            if topLevel:
                del fields[key]
            else:
                fields[key] = (old[0], MAYBE, old[2])
        elif kind == 'open':
            closed = False
            fields = {k: (f[0], MAYBE, f[2]) for k, f in fields.items()}
    return Obj(fields, closed, shape.label, shape.mode)


def _combine(results) -> tuple:
    """(union shape, the strongest tag, the model when all agree)."""
    tags = {r[1] for r in results}
    models = {r[2] for r in results}
    return (shapeUnion([r[0] for r in results]), next(t for t in PAYLOAD_TAGS if t in tags),
            next(iter(models)) if len(models) == 1 else None)


_BUILTIN_RESULTS = {'str': ('str',), 'int': ('int',), 'float': ('float',), 'bool': ('bool',),
                    'len': ('int',), 'round': ('float',), 'sum': ('float',)}


def _constType(value) -> tuple:
    if value is None:
        return NONE
    for kind in (bool, int, float, str):
        if isinstance(value, kind):
            return (kind.__name__,)
    return ANY


class _FnCtx:
    """Local name bindings and types inside one publisher-side function."""

    def __init__(self, repo: Repo, module: Module, node, owner):
        self.repo, self.module, self.node, self.owner = repo, module, node, owner
        self.extra, self.bindings, self.stores, self.visiting = {}, {}, {}, set()
        self.at = None
        self.params = {a.arg: a.annotation for a in [*node.args.posonlyargs, *node.args.args,
                                                     *node.args.kwonlyargs]}
        self._collect(node.body, True)
        for stmt in ownStatements(node.body):
            if isinstance(stmt, ast.comprehension):
                for name in _targetNames(stmt.target):
                    self._bind(name, 'iter', stmt.iter, stmt.iter.lineno, False)
            elif isinstance(stmt, ast.NamedExpr):
                self._bind(stmt.target.id, 'assign', stmt.value, stmt.lineno, False)
            elif isinstance(stmt, ast.ExceptHandler) and stmt.name:
                self._bind(stmt.name, 'other', stmt, stmt.lineno, False)

    def _bind(self, name, kind, expr, line, topLevel):
        self.bindings.setdefault(name, []).append((kind, expr, line, topLevel))

    def _collect(self, body, topLevel):
        """Bindings of the function's own statements, with their line and
        whether they sit directly in the function body (not in a branch)."""
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            self._changes(stmt, topLevel)
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                            and constStr(target.slice) is not None:
                        self.stores.setdefault(target.value.id, []).append(
                            ('set', constStr(target.slice), stmt.value, stmt.lineno, topLevel))
                    kind = 'assign' if isinstance(target, ast.Name) else 'other'
                    for name in _targetNames(target):
                        self._bind(name, kind, stmt.value, stmt.lineno, topLevel)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                self._bind(stmt.target.id, 'assign' if stmt.value is not None else 'ann',
                           stmt.value if stmt.value is not None else stmt.annotation, stmt.lineno, topLevel)
            elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
                self._bind(stmt.target.id, 'other', stmt, stmt.lineno, topLevel)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                for name in _targetNames(stmt.target):
                    self._bind(name, 'iter', stmt.iter, stmt.lineno, False)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    for name in _targetNames(item.optional_vars):
                        self._bind(name, 'other', item.context_expr, stmt.lineno, False)
            for block in ('body', 'orelse', 'finalbody'):
                if isinstance(getattr(stmt, block, None), list) and not isinstance(stmt, ast.ExceptHandler):
                    self._collect(getattr(stmt, block), False)
            for handler in getattr(stmt, 'handlers', []):
                self._collect(handler.body, False)

    def _changes(self, stmt, topLevel):
        """del x['k'], x.pop('k'), and the mutations that leave x open."""
        def add(name, kind, key):
            self.stores.setdefault(name, []).append((kind, key, None, stmt.lineno, topLevel))
        if isinstance(stmt, ast.Delete):
            for t in stmt.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and constStr(t.slice):
                    add(t.value.id, 'del', constStr(t.slice))
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            add(stmt.target.id, 'open', None)
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return)):
            for n in ast.walk(stmt):
                f = getattr(n, 'func', None)
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    if f.attr == 'pop' and n.args and constStr(n.args[0]):
                        add(f.value.id, 'del', constStr(n.args[0]))
                    elif f.attr in ('update', 'setdefault', 'clear', 'popitem'):
                        add(f.value.id, 'open', None)

    def reaching(self, name) -> list:
        """The bindings of `name` that can reach the return at self.at: those
        before it, from the last one directly in the function body on."""
        options = self.bindings.get(name, [])
        if self.at is None:
            return options
        before = [b for b in options if b[2] <= self.at] or options
        tops = [b[2] for b in before if b[3]]
        return [b for b in before if b[2] >= max(tops)] if tops else before

    def storesAfter(self, name, line) -> list:
        return [s for s in self.stores.get(name, []) if s[3] > line and (self.at is None or s[3] <= self.at)]

    def withComprehension(self, comp) -> '_FnCtx':
        out = _FnCtx.__new__(_FnCtx)
        out.__dict__.update(self.__dict__)
        out.extra, out.bindings, out.visiting = dict(self.extra), dict(self.bindings), set()
        out.stores = self.stores
        for gen in comp.generators:
            it = out.typeOf(gen.iter, 0)
            elem = nonNull(nonNull(it)[1]) if it is not None and nonNull(it)[0] == 'list' else None
            for name in _targetNames(gen.target):
                out.extra[name] = elem
                out.bindings.pop(name, None)
        return out

    def paramType(self, name):
        if name in self.extra:
            return self.extra[name]
        if self.params.get(name) is not None:
            return self.repo.annType(self.module, self.params[name])
        return None

    def callee(self, call):
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in ('self', 'cls') and self.owner is not None:
            return self.repo.member(self.owner, func.attr)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
            ci = self.repo.resolveClass(self.module, func.value.func)
            return self.repo.member(ci, func.attr) if ci is not None else None
        if isinstance(func, (ast.Name, ast.Attribute)):
            return self.repo.resolveExpr(self.module, func)
        return None

    def typeOf(self, e, depth) -> tuple | None:
        """The structured type of a local expression, when it can be told."""
        if depth > 6 or e is None:
            return None
        if isinstance(e, ast.Name):
            if e.id in self.extra or (e.id in self.params and e.id not in self.bindings):
                return self.paramType(e.id)
            if e.id in self.visiting:
                return None
            self.visiting.add(e.id)
            try:
                return self._nameType(e.id, depth)
            finally:
                self.visiting.discard(e.id)
        return self._exprType(e, depth)

    def _nameType(self, name, depth):
        repo = self.repo
        if True:
            for kind, x, _, _ in self.bindings.get(name, []):
                t = None
                if kind == 'assign':
                    t = self.typeOf(x, depth + 1)
                elif kind == 'ann':
                    t = repo.annType(self.module, x)
                elif kind == 'iter':
                    it = self.typeOf(x, depth + 1)
                    t = nonNull(it)[1] if it is not None and nonNull(it)[0] == 'list' else None
                if t is not None and t != ANY:
                    return t
            return None

    def _exprType(self, e, depth):
        repo = self.repo
        if isinstance(e, ast.Call):
            func = e.func
            if isinstance(func, ast.Attribute) and func.attr in ('model_copy', 'copy'):
                return self.typeOf(func.value, depth + 1)
            if isinstance(func, ast.Attribute) and func.attr in ('model_validate', 'parse_obj',
                                                                 'model_construct'):
                ci = repo.resolveClass(self.module, func.value)
                return repo.classType(ci) if ci is not None else None
            found = self.callee(e)
            if found is None and isinstance(func, ast.Name) and func.id not in _BUILTIN_NAMES:
                ci = repo.classByName(func.id)
                found = ('class', ci) if ci else None
            if found is not None and found[0] == 'class':
                return repo.classType(found[1])
            if found is not None and found[0] == 'func' and found[2].returns is not None:
                return repo.annType(found[1], found[2].returns)
            return None
        if isinstance(e, ast.Attribute):
            base = self.typeOf(e.value, depth + 1)
            mi = repo.modelOfType(base) if base is not None else None
            if mi is not None:
                if e.attr == 'root' and mi.rootType is not None:
                    return mi.rootType
                f = mi.fields.get(e.attr)
                return f.type if f is not None else None
            return base if base is not None and base[0] == 'list' and e.attr == 'root' else None
        if isinstance(e, ast.Subscript):
            base = self.typeOf(e.value, depth + 1)
            return nonNull(base)[1] if base is not None and nonNull(base)[0] == 'list' else None
        if isinstance(e, ast.Constant):
            return _constType(e.value)
        return None


def _targetNames(target) -> list:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _targetNames(elt)]
    return []
