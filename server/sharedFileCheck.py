"""Keep the files the Star App repos share in step with create-star-app.

Several server files are meant to be identical in every Star App repo: the
generic tests (test_file_sizes.py, test_route_coverage.py, ...), small tools
(servicePortCheck.py, tools/devPubSub.py, the Cloud Run job scripts) and this
file itself. create-star-app is their source of truth: its templates render
them for a new project, and its fleet/sharedFiles.json manifest lists which
files the existing repos keep in step with it, and how. This module compares
each listed file with the template's version and, with --write, copies the
template's version in. Hand-copying a fix into one repo is how these files
drifted apart; with this check that drift fails a test instead.

Where the template is: the create-star-app checkout next to this repo
(../create-star-app, the side-by-side layout servicePortCheck.py relies on
too), or --template <dir>.

Which revision: the template's committed HEAD, read with git cat-file, never
its working tree, so half-finished template work cannot fail every repo's
suite. --template-worktree reads the working tree instead, to preview a
template edit before committing it.

What a repo's copy must equal: a `.tmpl` source rendered exactly as
create-star-app's scaffold.py renders it,
string.Template(raw).safe_substitute(vars), where vars are the ProjectConfig
field defaults in create_star_app/config.py (read by AST, at the same
revision) plus this repo's projectName (assigned in class Project of
server/AppConfig.py) and port (the `PORT: int = <n>` line of
server/AppCreator.py). A source that uses any other variable is an error. A
fleet/<file> source is taken verbatim, and only the projects the manifest's
`repos` lists have it: fleet files are not templates, so a fresh scaffold
never does. CRLF and LF compare equal.

Each manifest entry has a mode:
  exact    the whole file must match;
  regions  only the named shared regions must match; the rest of the file is
           this repo's own.
Marker comment lines (in .py and .ps1 files; .bat and .cmd files are
exact-only) delimit the regions and the per-repo blocks:
  `# >>> star-app shared: <name>` ... `# <<< star-app shared: <name>`
  `# >>> star-app per-repo: <name>` ... `# <<< star-app per-repo: <name>`
A per-repo block holds what legitimately differs per repo (a list of
exceptions, say). Its contents are masked on both sides before comparing (the
marker lines themselves must match), and --write keeps them. It may sit
anywhere in an exact file, and inside a shared region of a regions file.

Usage, from server/ (no environment, database or app import needed):
  python sharedFileCheck.py                      check; exit 1 on drift, 2 on errors
  python sharedFileCheck.py --write              take the template's version
  python sharedFileCheck.py --template-worktree  compare with uncommitted template work
  python sharedFileCheck.py --template <dir>     use another template checkout

No usable template (no folder, not a git checkout, no manifest at HEAD) is a
WARNING and exit 0: Cloud Build and lone clones have no sibling checkouts.
test_sharedFiles.py runs the same check in the suite, and skips there.

Pure stdlib and Python 3.10-compatible on purpose: it must run without the
app, its env vars or Mongo, on every interpreter the repos deploy with.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from string import Template

from servicePortCheck import readDeclaredPort

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR_NAME = 'create-star-app'
MANIFEST_PATH = 'fleet/sharedFiles.json'
CONFIG_PATH = 'create_star_app/config.py'
APP_CONFIG_PATH = 'server/AppConfig.py'
APP_CREATOR_PATH = 'server/AppCreator.py'
TEMPLATES_PREFIX = 'create_star_app/templates/'
RENDERED_SUFFIX = '.tmpl'
FLEET_PREFIX = 'fleet/'
MODES = ('exact', 'regions')
# Files whose comment lines start with '#', so they can carry markers. Every
# other file (.bat and .cmd included) is compared whole, markers or not.
MARKER_SUFFIXES = {'.py', '.ps1'}
MAX_DIFF_LINES = 200
DIFF_CONTEXT = 3

# Any comment line that starts like a marker. MARKER then checks its form, so
# a typo is reported instead of quietly not being a marker.
MARKER_LIKE = re.compile(r'^[ \t]*#[ \t]*(?:>>>|<<<)[ \t]*star-app')
MARKER = re.compile(
    r'^[ \t]*# (>>>|<<<) star-app (shared|per-repo): (\S(?:.*\S)?)[ \t]*\Z')
# Manifest paths: relative, POSIX, plain characters only (no ':', no space).
# They name the files --write writes, so _isSafePath also refuses the names
# Windows reads as something else.
SAFE_PATH = re.compile(r'^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z')
WINDOWS_DEVICE_NAMES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    f'{device}{number}' for device in ('COM', 'LPT') for number in range(1, 10)}
# A manifest repo: its folder next to create-star-app, and its projectName.
# Twin of _DIR_NAME / _PROJECT_NAME in pubSubMapSection.py; keep in step.
REPO_DIR_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
PROJECT_NAME = re.compile(r'^[A-Za-z0-9_-]+\Z')
MANIFEST_KEYS = {'description', 'repos', 'files'}
REPO_KEYS = {'dir', 'projectName'}
ENTRY_KEYS = {'path', 'source', 'mode', 'regions', 'exceptProjects', 'onlyProjects'}
GIT_TIMEOUT_SECONDS = 60
# Twin of GIT_REDIRECT_VARS in pubSubMapScan.py; keep in step. Variables a
# git hook (or a caller) may export that would point `git -C <dir>` at some
# other repository than <dir>, or send it looking beyond <dir> for one.
GIT_REDIRECT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                     'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                     'GIT_COMMON_DIR', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES',
                     'GIT_DISCOVERY_ACROSS_FILESYSTEM')


class SharedFileError(Exception):
    """Something the check cannot work around: a malformed manifest, an
    unreadable AppConfig.py, a template that uses an unknown variable. Exit
    code 2 from the CLI, and a failing test."""


class TemplateUnavailable(Exception):
    """No usable create-star-app checkout: the check is skipped, not failed."""


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetRepo:
    """A repo the manifest lists: its folder next to create-star-app (named
    in the fix hint) and its projectName (which has the fleet/ files)."""
    dir: str
    projectName: str


@dataclass(frozen=True)
class ManifestEntry:
    """One shared file: this repo's copy, its template source and the mode."""
    path: str  # repo-relative, e.g. server/servicePortCheck.py
    source: str  # template-relative, e.g. create_star_app/templates/x.tmpl
    mode: str  # 'exact' or 'regions'
    regions: tuple[str, ...] = ()  # the shared region names (regions mode)
    exceptProjects: tuple[str, ...] = ()  # projectNames that do not have this file
    # When set, the only projectNames that have this file (a fresh scaffold
    # never does): a file that sits elsewhere in one repo's layout.
    onlyProjects: tuple[str, ...] = ()

    @property
    def rendered(self) -> bool:
        """A template, rendered like scaffold.py does, rather than a verbatim
        fleet/ file."""
        return self.source.startswith(TEMPLATES_PREFIX)

    def appliesTo(self, projectName: str, fleetProjects: set[str]) -> bool:
        """Whether the project has this file: not when exceptProjects names
        it, only when onlyProjects (if any) names it, and a fleet/ file only
        when the manifest's repos list the project (a fresh scaffold has the
        templates, never the fleet files)."""
        if projectName in self.exceptProjects:
            return False
        if self.onlyProjects and projectName not in self.onlyProjects:
            return False
        return self.rendered or projectName in fleetProjects

    @property
    def hasMarkers(self) -> bool:
        return PurePosixPath(self.path).suffix in MARKER_SUFFIXES

    def describeMode(self) -> str:
        if self.mode == 'regions':
            return 'regions ' + ', '.join(f'"{name}"' for name in self.regions)
        return 'exact'


def parseManifest(text: str, where: str) -> tuple[list[ManifestEntry], list[FleetRepo]]:
    """The manifest's entries and its repos. Raises SharedFileError on
    anything malformed."""
    prefix = f'{MANIFEST_PATH} at {where}'
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SharedFileError(f'{prefix} is not valid JSON ({error})') from None
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        raise SharedFileError(f'{prefix}: expected an object with a "files" list')
    unknown = sorted(set(data) - MANIFEST_KEYS)
    if unknown:
        raise SharedFileError(f'{prefix}: unknown key(s) {unknown}')
    if not isinstance(data.get('repos', []), list):
        raise SharedFileError(f'{prefix}: "repos" must be a list')
    repos = [_parseRepo(raw, f'{prefix}: repos[{index}]')
             for index, raw in enumerate(data.get('repos', []))]
    entries: list[ManifestEntry] = []
    for index, raw in enumerate(data['files']):
        entry = _parseEntry(raw, f'{prefix}: files[{index}]')
        if any(other.path == entry.path for other in entries):
            raise SharedFileError(f'{prefix}: files[{index}]: {entry.path} is listed twice')
        entries.append(entry)
    return entries, repos


def _parseRepo(raw: object, label: str) -> FleetRepo:
    if not isinstance(raw, dict) or set(raw) != REPO_KEYS:
        raise SharedFileError(f'{label}: expected an object with exactly "dir" and "projectName"')
    folder, projectName = raw['dir'], raw['projectName']
    # Twin of isSafeDirName / isSafeProjectName in pubSubMapSection.py; keep in step.
    if not isinstance(folder, str) or not REPO_DIR_NAME.match(folder) or folder.endswith('.'):
        raise SharedFileError(f'{label}: "dir" {folder!r} must be a plain folder name')
    if not isinstance(projectName, str) or not PROJECT_NAME.match(projectName):
        raise SharedFileError(f'{label}: "projectName" {projectName!r} must be letters, '
                              'digits, "_" and "-"')
    return FleetRepo(folder, projectName)


def _parseEntry(raw: object, label: str) -> ManifestEntry:
    if not isinstance(raw, dict):
        raise SharedFileError(f'{label}: expected an object')
    unknown = sorted(set(raw) - ENTRY_KEYS)
    if unknown:
        raise SharedFileError(f'{label}: unknown key(s) {unknown}')
    for key in ('path', 'source', 'mode'):
        if not isinstance(raw.get(key), str):
            raise SharedFileError(f'{label}: "{key}" is required (a string)')
        if key != 'mode' and not _isSafePath(raw[key]):
            raise SharedFileError(
                f'{label}: "{key}" {raw[key]!r} must be a relative POSIX path of '
                'letters, digits, "_", "." and "-", with no part that ends in ".", '
                'names a .git folder or is a Windows device name')
    source = raw['source']
    if not (source.startswith(TEMPLATES_PREFIX) and source.endswith(RENDERED_SUFFIX)
            or source.startswith(FLEET_PREFIX)):
        raise SharedFileError(f'{label}: "source" {source!r} must be a {TEMPLATES_PREFIX}'
                              f'<name>{RENDERED_SUFFIX} template or a {FLEET_PREFIX}<file>')
    if raw['mode'] not in MODES:
        raise SharedFileError(f'{label}: "mode" must be one of {list(MODES)}, not {raw["mode"]!r}')
    regions = raw.get('regions', [])
    exceptProjects = raw.get('exceptProjects', [])
    onlyProjects = raw.get('onlyProjects', [])
    if not all(_isStringList(value) for value in (regions, exceptProjects, onlyProjects)):
        raise SharedFileError(f'{label}: "regions", "exceptProjects" and "onlyProjects" must '
                              'be lists of strings')
    if exceptProjects and onlyProjects:
        raise SharedFileError(f'{label}: give "exceptProjects" or "onlyProjects", not both')
    entry = ManifestEntry(raw['path'], raw['source'], raw['mode'], tuple(regions),
                          tuple(exceptProjects), tuple(onlyProjects))
    if entry.mode == 'exact':
        if regions:
            raise SharedFileError(f'{label}: "regions" only applies to regions mode')
        return entry
    if not entry.hasMarkers:
        raise SharedFileError(
            f'{label}: {entry.path} cannot use regions mode: only '
            f'{", ".join(sorted(MARKER_SUFFIXES))} files carry markers')
    if (not regions or len(set(regions)) != len(regions)
            or any(not name or name != name.strip() for name in regions)):
        raise SharedFileError(f'{label}: regions mode needs a list of distinct region names')
    return entry


def _isStringList(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _isSafePath(value: str) -> bool:
    """SAFE_PATH, and no part Windows would read as another name: one ending
    in '.' ('.', '..', and '.git.', which Windows opens as '.git'), a .git
    folder in any case, or a device name (CON, nul.txt)."""
    return bool(SAFE_PATH.match(value)) and not any(
        part.endswith('.') or _isGitDirName(part)
        or part.split('.')[0].upper() in WINDOWS_DEVICE_NAMES for part in value.split('/'))


def _isGitDirName(part: str) -> bool:
    return part.rstrip('. ').lower() == '.git'


# ---------------------------------------------------------------------------
# Reading the template and this repo
# ---------------------------------------------------------------------------


def normalise(data: bytes, where: str) -> str:
    """UTF-8 text with CRLF line endings turned into LF."""
    try:
        return data.decode('utf-8').replace('\r\n', '\n')
    except UnicodeDecodeError as error:
        raise SharedFileError(f'{where} is not UTF-8 text ({error})') from None


def readText(path: Path) -> str:
    return normalise(path.read_bytes(), str(path))


def _gitEnv() -> dict[str, str]:
    """os.environ without GIT_REDIRECT_VARS, with optional locks and the
    credential prompt off. Built per call, so a test's setenv is seen."""
    # Twin of gitEnv in pubSubMapScan.py; keep in step.
    env = {key: value for key, value in os.environ.items() if key not in GIT_REDIRECT_VARS}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    return env


def _git(directory: Path, *args: str,
         stdin: bytes | None = None) -> subprocess.CompletedProcess | None:
    """git in `directory`, never through a shell: no fsmonitor hook (it would
    run that checkout's configured program), no line-ending conversion, and
    a timeout. None when git could not run or timed out."""
    # Twin of runGit in pubSubMapScan.py; keep in step.
    try:
        return subprocess.run(['git', '-C', str(directory), '-c', 'core.fsmonitor=false',
                               '-c', 'core.autocrlf=false', *args],
                              input=stdin, capture_output=True, env=_gitEnv(),
                              timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gitText(result: subprocess.CompletedProcess | None) -> str:
    """A successful call's stdout, stripped; '' for a failed one."""
    if result is None or result.returncode != 0:
        return ''
    return result.stdout.decode('utf-8', errors='replace').strip()


def _gitError(result: subprocess.CompletedProcess | None) -> str:
    if result is None:
        return 'git could not run, or timed out'
    return result.stderr.decode('utf-8', errors='replace').strip()


def _isOwnCheckout(directory: Path) -> bool:
    """Whether `directory` is the top level of a git checkout itself, not a
    plain folder inside some other repository."""
    # Twin of isRepoRoot in pubSubMapScan.py; keep in step.
    topLevel = _gitText(_git(directory, 'rev-parse', '--show-toplevel'))
    try:
        return bool(topLevel) and os.path.samefile(topLevel, directory)
    except OSError:
        return False


def _lsTreeBlobs(root: Path, revision: str) -> dict[str, str]:
    """path -> blob id of every regular file at `revision`."""
    # Twin of readGitRef's ls-tree half in pubSubMapScan.py; keep in step.
    result = _git(root, 'ls-tree', '-r', '-z', revision)
    if result is None or result.returncode != 0:
        raise SharedFileError(f'git ls-tree {revision} failed in {root}: {_gitError(result)}')
    return parseLsTree(result.stdout, str(root))


def parseLsTree(listing: bytes, where: str) -> dict[str, str]:
    """`ls-tree -r -z` records (`<mode> <type> <id>` TAB `<path>` NUL) as
    path -> blob id, regular files only: a symlink or a submodule counts as
    no file. NUL-separated, so no file name can throw the parse off."""
    blobs: dict[str, str] = {}
    for record in listing.split(b'\0'):
        if not record:
            continue
        meta, tab, path = record.partition(b'\t')
        fields = meta.decode('ascii', errors='replace').split(' ')
        if not tab or len(fields) != 3:
            raise SharedFileError(f'unexpected git ls-tree output in {where}: {record[:80]!r}')
        mode, kind, oid = fields
        if kind == 'blob' and mode in ('100644', '100755'):
            blobs[path.decode('utf-8', errors='surrogateescape')] = oid
    return blobs


def _catFileBlobs(root: Path, oids: list[str]) -> dict[str, bytes]:
    """Each blob's content by id, through one `git cat-file --batch`."""
    # Twin of readGitRef's cat-file half in pubSubMapScan.py; keep in step.
    wanted = list(dict.fromkeys(oids))
    if not wanted:
        return {}
    request = ''.join(f'{oid}\n' for oid in wanted).encode('ascii')
    result = _git(root, 'cat-file', '--batch', stdin=request)
    if result is None or result.returncode != 0:
        raise SharedFileError(f'git cat-file failed in {root}: {_gitError(result)}')
    return parseCatFile(result.stdout, wanted, str(root))


def parseCatFile(out: bytes, oids: list[str], where: str) -> dict[str, bytes]:
    """`cat-file --batch` output for `oids`, in order. Every object's header
    (`<id> <type> <size>`) must name the id asked for, type blob and a size,
    and its content must end where the size says."""
    blobs: dict[str, bytes] = {}
    position = 0
    for oid in oids:
        end = out.find(b'\n', position)
        header = out[position:end].decode('ascii', errors='replace').split(' ') if end >= 0 else []
        if len(header) != 3 or header[0] != oid or header[1] != 'blob' or not header[2].isdigit():
            raise SharedFileError(f'unexpected git cat-file output in {where} for {oid}: '
                                  f'{" ".join(header)!r}')
        start = end + 1
        size = int(header[2])
        if out[start + size:start + size + 1] != b'\n':
            raise SharedFileError(f'git cat-file output in {where} is cut short at {oid}')
        blobs[oid] = out[start:start + size]
        position = start + size + 1
    return blobs


class TemplateSource:
    """create-star-app's files at one commit, or in its working tree, as LF
    text. Read through openTemplate()."""

    def __init__(self, root: Path, commit: str, label: str, worktree: bool):
        self.root = root
        self.commit = commit
        self.label = label  # e.g. main@1a2b3c4, or main@1a2b3c4 + working tree
        self.worktree = worktree
        self._texts: dict[str, str | None] = {}
        self._blobIds: dict[str, str] | None = None  # path -> blob id at commit

    def read(self, path: str) -> str | None:
        """The file's text, or None when the template has no such file."""
        self.prefetch([path])
        return self._texts[path]

    def prefetch(self, paths: list[str]) -> None:
        """Reads every path not read yet: from the working tree, or at the
        commit with one git cat-file call (by blob id, from one ls-tree)."""
        wanted = [path for path in dict.fromkeys(paths) if path not in self._texts]
        if not wanted:
            return
        if self.worktree:
            for path in wanted:
                file = self.root / path
                self._texts[path] = readText(file) if file.is_file() else None
            return
        if self._blobIds is None:
            self._blobIds = _lsTreeBlobs(self.root, self.commit)
        ids = {path: self._blobIds.get(path) for path in wanted}
        blobs = _catFileBlobs(self.root, [oid for oid in ids.values() if oid])
        for path, oid in ids.items():
            self._texts[path] = (None if oid is None else
                                 normalise(blobs[oid], f'{path} at {self.label}'))


def openTemplate(templateDir: Path, worktree: bool = False) -> TemplateSource:
    """The template checkout at templateDir. Raises TemplateUnavailable,
    saying why, when there is no usable one."""
    if not templateDir.is_dir() or not _isOwnCheckout(templateDir):
        raise TemplateUnavailable(f'{TEMPLATE_DIR_NAME} not found at {templateDir} '
                                  '(or not a git checkout); drift not checked')
    # One call for the commit and its short form ('--': never a file name).
    names = _gitText(_git(templateDir, 'rev-parse', 'HEAD^{commit}', '--short', 'HEAD^{commit}', '--')).split()
    if not names:
        raise TemplateUnavailable(
            f'{TEMPLATE_DIR_NAME} at {templateDir} has no commit yet; drift not checked')
    commit, short = names[0], names[1] if len(names) > 1 else names[0][:7]
    branch = _gitText(_git(templateDir, 'symbolic-ref', '--quiet', '--short', 'HEAD'))
    label = f'{branch or "detached HEAD"}@{short}' + (' + working tree' if worktree else '')
    template = TemplateSource(templateDir, commit, label, worktree)
    if template.read(MANIFEST_PATH) is None:
        where = 'in its working tree' if worktree else f'at HEAD ({label})'
        raise TemplateUnavailable(f'{TEMPLATE_DIR_NAME} at {templateDir} has no '
                                  f'{MANIFEST_PATH} {where}; drift not checked')
    return template


def _parsePython(source: str, where: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise SharedFileError(f'{where} does not parse: {error}') from None


def readProjectName(source: str, where: str = APP_CONFIG_PATH) -> str | None:
    """The projectName class Project assigns in AppConfig.py: its first
    class-level `projectName = '...'`, else the first (in source order)
    `self.projectName = '...'` in its methods. None when it assigns none."""
    # Twin of readProjectName in pubSubMapScan.py; keep in step.
    project = _projectClass(_parsePython(source, where).body)
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


def _projectClass(nodes) -> ast.ClassDef | None:
    """The first class Project in source order, at module level or inside
    module-level if/try/with/for/while blocks, never inside a def or another
    class."""
    # Twin of _projectClass in pubSubMapScan.py; keep in step.
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


def readConfigDefaults(source: str, where: str = CONFIG_PATH) -> dict[str, object]:
    """ProjectConfig's field defaults in create_star_app/config.py: every
    annotated field whose default is a literal."""
    defaults: dict[str, object] = {}
    for node in _parsePython(source, where).body:
        if not (isinstance(node, ast.ClassDef) and node.name == 'ProjectConfig'):
            continue
        for statement in node.body:
            if (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
                    and statement.value is not None):
                try:
                    defaults[statement.target.id] = ast.literal_eval(statement.value)
                except (ValueError, TypeError, SyntaxError):
                    continue  # field(default_factory=...) and the like
    return defaults


def render(raw: str, variables: dict[str, object], source: str) -> str:
    """The source rendered exactly as scaffold.py renders it. Unlike
    scaffold.py, a variable missing from `variables` is an error: left
    unsubstituted, it would make every repo's copy look drifted."""
    used = {match.group('named') or match.group('braced')
            for match in Template.pattern.finditer(raw)}
    unknown = sorted(name for name in used if name and name not in variables)
    if unknown:
        raise SharedFileError(
            f'{source} uses the template variable(s) {", ".join(unknown)}, but the '
            f'check only knows {", ".join(sorted(variables))} (the literal ProjectConfig '
            f'defaults in {CONFIG_PATH}, plus projectName from {APP_CONFIG_PATH} and '
            f'port from {APP_CREATOR_PATH}). Give the field a literal default, or '
            'write a literal dollar sign as $$.')
    return Template(raw).safe_substitute(variables)


@dataclass
class Context:
    """One run's inputs: this repo, the template revision, the manifest and
    the template variables. Built by loadContext()."""
    projectRoot: Path
    projectName: str
    template: TemplateSource
    entries: list[ManifestEntry]
    repos: list[FleetRepo]
    variables: dict[str, object]
    _expected: dict[str, str] = field(default_factory=dict)

    def applicable(self) -> list[ManifestEntry]:
        """The entries this project has (see ManifestEntry.appliesTo)."""
        fleetProjects = {repo.projectName for repo in self.repos}
        return [entry for entry in self.entries
                if entry.appliesTo(self.projectName, fleetProjects)]

    def entry(self, path: str) -> ManifestEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)

    def expected(self, entry: ManifestEntry) -> str:
        """This repo's copy as the template has it. Raises SharedFileError
        when the template lacks the source or cannot render it."""
        if entry.path not in self._expected:
            raw = self.template.read(entry.source)
            if raw is None:
                raise SharedFileError(
                    f'its source {entry.source} is not in {TEMPLATE_DIR_NAME} at '
                    f'{self.template.label}. Add and commit it there, or fix {MANIFEST_PATH}.')
            self._expected[entry.path] = (render(raw, self.variables, entry.source)
                                          if entry.rendered else raw)
        return self._expected[entry.path]

    def fixHint(self) -> str:
        folders = [repo.dir for repo in self.repos]
        if not folders:
            where = 'every repo that shares these files'
        elif len(folders) == 1:
            where = folders[0]
        else:
            where = ', '.join(folders[:-1]) + ' and ' + folders[-1]
        return (f'Either fold the change into the template, commit it in {TEMPLATE_DIR_NAME}, '
                f'then run `cd server && python sharedFileCheck.py --write` in {where}; '
                'or run `python sharedFileCheck.py --write` here to restore the '
                "template's version. Never edit one repo's copy alone.")


def loadContext(projectRoot: Path = PROJECT_ROOT, templateDir: Path | None = None,
                worktree: bool = False) -> Context:
    """Everything a check or a --write needs. Raises TemplateUnavailable (the
    check is skipped) or SharedFileError (it fails)."""
    template = openTemplate(templateDir or projectRoot.parent / TEMPLATE_DIR_NAME, worktree)
    entries, repos = parseManifest(template.read(MANIFEST_PATH), template.label)
    template.prefetch([CONFIG_PATH] + [entry.source for entry in entries])
    config = template.read(CONFIG_PATH)
    variables = readConfigDefaults(config) if config is not None else {}
    appConfig = projectRoot / APP_CONFIG_PATH
    if not appConfig.is_file():
        raise SharedFileError(f'{appConfig} not found, so the project is unknown')
    projectName = readProjectName(readText(appConfig))
    if projectName is None:
        raise SharedFileError(f'{APP_CONFIG_PATH} assigns no projectName in class Project')
    variables['projectName'] = projectName
    appCreator = projectRoot / APP_CREATOR_PATH
    port = readDeclaredPort(appCreator) if appCreator.is_file() else None
    if port is None:
        # Never render ProjectConfig's default as this repo's port: a
        # template that uses $port then fails, naming it.
        variables.pop('port', None)
    else:
        variables['port'] = port
    return Context(projectRoot, projectName, template, entries, repos, variables)


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """A marker pair, by the 0-based indexes of its two marker lines."""
    kind: str  # 'shared' or 'per-repo'
    name: str
    start: int
    end: int = -1
    inShared: bool = False  # a per-repo block inside a shared region


def scanMarkers(lines: list[str]) -> tuple[list[Block], list[str]]:
    """The marker blocks in `lines`, sorted by position, and every problem
    with them: a malformed marker, an unbalanced or misnested pair, a name
    used twice. Problems name 1-based line numbers."""
    blocks: list[Block] = []
    problems: list[str] = []
    stack: list[Block] = []
    for index, line in enumerate(lines):
        if not MARKER_LIKE.match(line):
            continue
        match = MARKER.match(line)
        if match is None:
            problems.append(
                f'line {index + 1}: malformed marker {line.strip()!r} (expected '
                '"# >>> star-app shared: <name>", "# <<< star-app per-repo: <name>" and so on)')
            continue
        direction, kind, name = match.groups()
        if direction == '>>>':
            parent = stack[-1] if stack else None
            if parent is not None and (kind == 'shared' or parent.kind == 'per-repo'):
                problems.append(f'line {index + 1}: {kind} block "{name}" opens inside '
                                f'{parent.kind} block "{parent.name}"')
            stack.append(Block(kind, name, index,
                               inShared=parent is not None and parent.kind == 'shared'))
        elif not stack or (stack[-1].kind, stack[-1].name) != (kind, name):
            openNow = f'{stack[-1].kind} block "{stack[-1].name}"' if stack else 'nothing'
            problems.append(f'line {index + 1}: closes {kind} block "{name}", but {openNow} is open')
        else:
            block = stack.pop()
            block.end = index
            blocks.append(block)
    problems += [f'line {block.start + 1}: {block.kind} block "{block.name}" is never closed'
                 for block in stack]
    blocks.sort(key=lambda block: block.start)
    seen = set()
    for block in blocks:
        if (block.kind, block.name) in seen:
            problems.append(f'line {block.start + 1}: a second {block.kind} block named "{block.name}"')
        seen.add((block.kind, block.name))
    return blocks, problems


# Per-repo blocks whose contents are validated, not only masked: the job .ps1
# tools are run against production, so their region block may hold nothing
# but the one $Region line, and create_or_update_job.ps1's scheduler block
# nothing but the one $SchedulerServiceAccount line: empty, the project's App
# Engine default account, or an account the project created (no other code
# can hide in a masked block).
VALIDATED_BLOCKS = {
    'region': (re.compile(r'^\$Region = "[a-z]+-[a-z]+[0-9]+"\r?$'),
               'exactly one line $Region = "<gcp region>", e.g. $Region = "asia-east1"'),
    'scheduler service account': (
        re.compile(r'^\$SchedulerServiceAccount = "(?:'
                   r'|\$Project@appspot\.gserviceaccount\.com'
                   r'|[a-z][a-z0-9-]*@[a-z][a-z0-9-]*\.iam\.gserviceaccount\.com)"\r?$'),
        'exactly one line $SchedulerServiceAccount = "<account>": empty, '
        '"$Project@appspot.gserviceaccount.com" or "<name>@<project>.iam.gserviceaccount.com"'),
}


def blockContentProblems(lines: list[str], blocks: list[Block]) -> list[str]:
    """A validated per-repo block (VALIDATED_BLOCKS) whose contents are not
    what it may hold."""
    problems = []
    for block in blocks:
        rule = VALIDATED_BLOCKS.get(block.name) if block.kind == 'per-repo' else None
        if rule is None:
            continue
        contents = lines[block.start + 1:block.end]
        if len(contents) != 1 or not rule[0].match(contents[0]):
            problems.append(f'line {block.start + 1}: per-repo block "{block.name}" may hold only '
                            f'{rule[1]}')
    return problems


def _modeProblems(blocks: list[Block], mode: str) -> list[str]:
    """A regions-mode file compares only its shared regions, so a per-repo
    block outside them would mean nothing."""
    if mode != 'regions':
        return []
    return [f'line {block.start + 1}: per-repo block "{block.name}" sits outside every '
            'shared region' for block in blocks if block.kind == 'per-repo' and not block.inShared]


def markerProblems(text: str, mode: str) -> list[str]:
    """Every marker problem in a file of the given mode."""
    blocks, problems = scanMarkers(text.split('\n'))
    return problems + _modeProblems(blocks, mode)


def _shared(blocks: list[Block], name: str) -> Block | None:
    return next((block for block in blocks if block.kind == 'shared' and block.name == name), None)


def _templateBlocks(entry: ManifestEntry, lines: list[str], label: str) -> list[Block]:
    """The template's blocks. Broken template markers, or a manifest region
    the template lacks, are a SharedFileError: no repo copy can be right."""
    blocks, problems = scanMarkers(lines)
    problems += _modeProblems(blocks, entry.mode)
    problems += [f'no shared region "{name}"' for name in entry.regions
                 if _shared(blocks, name) is None]
    if problems:
        raise SharedFileError(f'the template source {entry.source} at {label} has broken '
                              'markers: ' + '; '.join(problems))
    return blocks


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def _numbered(lines: list[str], blocks: list[Block] | None = None,
              region: Block | None = None) -> list[tuple[int, str]]:
    """(line number, text) for every line, or every line inside `region`,
    leaving out the contents of the per-repo blocks (their marker lines
    stay)."""
    first, last = (region.start + 1, region.end) if region else (0, len(lines))
    hidden = set()
    for block in blocks or []:
        if block.kind == 'per-repo':
            hidden.update(range(block.start + 1, block.end))
    return [(index + 1, lines[index]) for index in range(first, last) if index not in hidden]


def numberedDiff(expected: list[tuple[int, str]], actual: list[tuple[int, str]]) -> list[str]:
    """A unified diff, template (-) against repo (+), of at most
    MAX_DIFF_LINES lines. Each line shows its template and repo line numbers."""
    matcher = difflib.SequenceMatcher(None, [text for _, text in expected],
                                      [text for _, text in actual], autojunk=False)
    blank = ' ' * 5
    lines: list[str] = []
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        _, first, _, firstActual, _ = group[0]
        lines.append(f'@@ template line {_lineAt(expected, first)}, '
                     f'repo line {_lineAt(actual, firstActual)} @@')
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                lines += [f'  {a:>5} {b:>5}  {text}'
                          for (a, text), (b, _) in zip(expected[i1:i2], actual[j1:j2])]
                continue
            lines += [f'- {a:>5} {blank}  {text}' for a, text in expected[i1:i2]]
            lines += [f'+ {blank} {b:>5}  {text}' for b, text in actual[j1:j2]]
    if len(lines) > MAX_DIFF_LINES:
        hidden = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES] + [f'... {hidden} more diff lines not shown']
    return lines


def _lineAt(numbered: list[tuple[int, str]], index: int) -> int:
    if index < len(numbered):
        return numbered[index][0]
    return numbered[-1][0] + 1 if numbered else 1


def _section(entry: ManifestEntry, label: str, title: str, expected: list[tuple[int, str]],
             actual: list[tuple[int, str]]) -> list[str]:
    """[a heading and the numbered diff], or [] when the two are equal."""
    if [text for _, text in expected] == [text for _, text in actual]:
        return []
    return ['\n'.join([f'{title}:', f'--- template {entry.source} ({label})',
                       f'+++ repo     {entry.path}',
                       '    (columns: template line, repo line)',
                       *numberedDiff(expected, actual)])]


def compareEntry(entry: ManifestEntry, expected: str, actual: str, label: str) -> list[str]:
    """What differs between the template's text and this repo's: one section
    (a heading and a numbered diff) per mismatch, none when they match.
    Raises SharedFileError when the template's own markers are broken."""
    expectedLines, actualLines = expected.split('\n'), actual.split('\n')
    if not entry.hasMarkers:
        return _section(entry, label, 'whole file', _numbered(expectedLines),
                        _numbered(actualLines))
    templateBlocks = _templateBlocks(entry, expectedLines, label)
    repoBlocks, problems = scanMarkers(actualLines)
    problems += _modeProblems(repoBlocks, entry.mode)
    if not problems:
        problems += blockContentProblems(actualLines, repoBlocks)
    if problems:
        broken = 'broken markers in this repo copy:\n' + '\n'.join(f'  {p}' for p in problems)
        return [broken] + _section(entry, label, 'whole file, nothing masked',
                                   _numbered(expectedLines), _numbered(actualLines))
    if entry.mode == 'exact':
        masked = any(block.kind == 'per-repo' for block in templateBlocks + repoBlocks)
        return _section(entry, label,
                        'whole file, per-repo block contents masked' if masked else 'whole file',
                        _numbered(expectedLines, templateBlocks),
                        _numbered(actualLines, repoBlocks))
    sections: list[str] = []
    for name in entry.regions:
        repoRegion = _shared(repoBlocks, name)
        if repoRegion is None:
            sections.append(f'region "{name}": its markers are missing in this repo copy. '
                            f'Copy the region, markers included, from {entry.source}.')
            continue
        sections += _section(entry, label, f'region "{name}"',
                             _numbered(expectedLines, templateBlocks, _shared(templateBlocks, name)),
                             _numbered(actualLines, repoBlocks, repoRegion))
    return sections


@dataclass
class EntryResult:
    entry: ManifestEntry
    status: str  # 'ok', 'drift' or 'error'
    details: str = ''  # what differs and where, unless ok


def checkEntry(context: Context, entry: ManifestEntry) -> EntryResult:
    """This repo's copy of one manifest entry against the template."""
    try:
        expected = context.expected(entry)
    except SharedFileError as error:
        return EntryResult(entry, 'error', f'{entry.path}: {error}')
    path = context.projectRoot / entry.path
    if not path.is_file():
        return EntryResult(entry, 'drift', (
            f'{entry.path} is missing in this repo (template source: {entry.source} at '
            f'{context.template.label}). Run `python sharedFileCheck.py --write` to create it.'))
    try:
        sections = compareEntry(entry, expected, readText(path), context.template.label)
    except SharedFileError as error:
        return EntryResult(entry, 'error', f'{entry.path}: {error}')
    if not sections:
        return EntryResult(entry, 'ok')
    heading = [f'{entry.path} differs from {TEMPLATE_DIR_NAME}.',
               f'  template source:   {entry.source}',
               f'  template revision: {context.template.label}',
               f'  mode:              {entry.describeMode()}', '']
    return EntryResult(entry, 'drift', '\n'.join(heading + sections))


def checkAll(projectRoot: Path = PROJECT_ROOT, templateDir: Path | None = None,
             worktree: bool = False) -> tuple[Context, list[EntryResult]]:
    """Every manifest entry this project has, checked. Raises
    TemplateUnavailable or SharedFileError like loadContext()."""
    context = loadContext(projectRoot, templateDir, worktree)
    return context, [checkEntry(context, entry) for entry in context.applicable()]


# ---------------------------------------------------------------------------
# --write
# ---------------------------------------------------------------------------


def syncEntry(context: Context, entry: ManifestEntry) -> str | None:
    """Makes this repo's copy match the template, keeping its per-repo block
    contents, (regions mode) everything outside the shared regions, and its
    line-ending style. Returns what it did, or None when the copy already
    matched. Raises SharedFileError when it cannot do that safely."""
    expected = context.expected(entry)
    path = _repoPath(context.projectRoot, entry.path)
    if not path.is_file():
        _writeText(path, expected, crlf=False)
        return f'created {entry.path}'
    raw = path.read_bytes()
    actual = normalise(raw, entry.path)
    notes: list[str] = []
    updated = expected
    if entry.hasMarkers:
        updated, notes = _merge(entry, expected, actual, context.template.label)
    if updated == actual:
        return None
    _writeText(path, updated, crlf=b'\r\n' in raw)
    return f'updated {entry.path} ({entry.describeMode()})' + ''.join(f'; {n}' for n in notes)


def _merge(entry: ManifestEntry, expected: str, actual: str, label: str) -> tuple[str, list[str]]:
    """The template's text with this repo's per-repo block contents put back
    (exact), or this repo's text with each shared region replaced by the
    template's (regions). Also returns notes on blocks it could not keep."""
    templateLines, repoLines = expected.split('\n'), actual.split('\n')
    templateBlocks = _templateBlocks(entry, templateLines, label)
    repoBlocks, problems = scanMarkers(repoLines)
    if problems:
        raise SharedFileError(
            'cannot keep its per-repo blocks, because its markers are broken ('
            + '; '.join(problems) + '). Fix them by hand, or delete the file and rerun '
            '--write to recreate it from the template.')
    invalid = {problem.split('"')[1] for problem in blockContentProblems(repoLines, repoBlocks)}
    repoContents = {block.name: repoLines[block.start + 1:block.end]
                    for block in repoBlocks if block.kind == 'per-repo' and block.name not in invalid}
    if entry.mode == 'exact':
        merged, fromTemplate = _withRepoContents(templateLines, templateBlocks, repoContents,
                                                 0, len(templateLines))
        templateNames = {block.name for block in templateBlocks if block.kind == 'per-repo'}
        dropped = sorted(set(repoContents) - templateNames)
        notes = [f'per-repo block "{name}" is not in the template and was dropped'
                 for name in dropped]
    else:
        missing = [name for name in entry.regions if _shared(repoBlocks, name) is None]
        if missing:
            raise SharedFileError(
                f'region(s) {", ".join(missing)} have no markers in this repo copy. Copy '
                f'each region, markers included, from {entry.source} by hand first.')
        merged, fromTemplate = list(repoLines), []
        # Bottom-up, so replacing one region leaves the indexes above it valid.
        for repoRegion in sorted((_shared(repoBlocks, name) for name in entry.regions),
                                 key=lambda block: block.start, reverse=True):
            templateRegion = _shared(templateBlocks, repoRegion.name)
            inner, missingBlocks = _withRepoContents(templateLines, templateBlocks, repoContents,
                                                     templateRegion.start + 1, templateRegion.end)
            merged[repoRegion.start + 1:repoRegion.end] = inner
            fromTemplate += missingBlocks
        notes = []
    notes += [f'per-repo block "{name}" was not in this copy, so it has the template\'s '
              'contents: check them' for name in fromTemplate if name not in invalid]
    notes += [f'per-repo block "{name}" held more than it may, so it has the template\'s '
              'contents: check them' for name in sorted(invalid)]
    return '\n'.join(merged), notes


def _withRepoContents(templateLines: list[str], templateBlocks: list[Block],
                      repoContents: dict[str, list[str]], first: int,
                      last: int) -> tuple[list[str], list[str]]:
    """templateLines[first:last] with each per-repo block's contents
    replaced by this repo's, by name. Also returns the blocks this repo has
    no contents for, which keep the template's."""
    merged: list[str] = []
    missing: list[str] = []
    position = first
    for block in templateBlocks:
        if block.kind != 'per-repo' or not first <= block.start < last:
            continue
        merged += templateLines[position:block.start + 1]
        if block.name in repoContents:
            merged += repoContents[block.name]
        else:
            merged += templateLines[block.start + 1:block.end]
            missing.append(block.name)
        position = block.end
    merged += templateLines[position:last]
    return merged, missing


def _repoPath(projectRoot: Path, relative: str) -> Path:
    """projectRoot/relative, refusing a path that resolves (through a
    symlink, say) outside the repo or into a .git folder: --write never
    writes anywhere else."""
    path = projectRoot / relative
    try:
        parts = path.resolve().relative_to(projectRoot.resolve()).parts
    except ValueError:
        raise SharedFileError(f'{relative} resolves outside {projectRoot}; not writing it') from None
    if any(_isGitDirName(part) for part in parts):
        raise SharedFileError(f'{relative} resolves into a .git folder; not writing it')
    return path


def _writeText(path: Path, text: str, crlf: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _check(context: Context) -> int:
    results = [checkEntry(context, entry) for entry in context.applicable()]
    drifted = [result for result in results if result.status == 'drift']
    errors = [result for result in results if result.status == 'error']
    for result in drifted + errors:
        print(result.details, end='\n\n')
    revision = f'{TEMPLATE_DIR_NAME} {context.template.label}'
    if not drifted and not errors:
        skipped = len(context.entries) - len(results)
        note = f' ({skipped} not used by {context.projectName})' if skipped else ''
        print(f'{len(results)} shared files match {revision}{note}.')
        return 0
    if drifted:
        print(f'{len(drifted)} of {len(results)} shared files differ from {revision}.')
    if errors:
        print(f'{len(errors)} of {len(results)} shared files could not be checked against {revision}.')
    if drifted:
        print(context.fixHint())
    return 2 if errors else 1


def _write(context: Context) -> int:
    entries = context.applicable()
    changed: list[str] = []
    errors: list[str] = []
    for entry in entries:
        try:
            line = syncEntry(context, entry)
        except SharedFileError as error:
            errors.append(f'{entry.path}: {error}')
            continue
        if line:
            changed.append(line)
    for line in changed:
        print(line)
    for line in errors:
        print(f'ERROR: {line}')
    unchanged = len(entries) - len(changed) - len(errors)
    print(f'{len(changed)} file(s) written, {unchanged} already matched '
          f'{TEMPLATE_DIR_NAME} {context.template.label}.')
    return 2 if errors else 0


def _escapeUnprintable() -> None:
    """A diff can hold characters the Windows console's code page cannot
    print: escape them instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')


def main(argv: list[str] | None = None, projectRoot: Path = PROJECT_ROOT) -> int:
    parser = argparse.ArgumentParser(
        prog='sharedFileCheck.py',
        description=f'Compare the files this repo shares with {TEMPLATE_DIR_NAME} '
        "(listed in its fleet/sharedFiles.json), or take the template's version.")
    parser.add_argument('--write', action='store_true',
                        help="write the template's version of every differing file, keeping "
                        'per-repo blocks and the text outside shared regions')
    parser.add_argument('--template-worktree', action='store_true',
                        help="read the template's working tree instead of its committed HEAD")
    parser.add_argument('--template', type=Path, metavar='DIR',
                        help=f'the template checkout (default: ../{TEMPLATE_DIR_NAME})')
    args = parser.parse_args(argv)
    _escapeUnprintable()
    templateDir = args.template.resolve() if args.template else None
    try:
        context = loadContext(projectRoot, templateDir, args.template_worktree)
    except TemplateUnavailable as reason:
        print(f'WARNING: {reason}')
        return 0
    except SharedFileError as error:
        print(f'ERROR: {error}')
        return 2
    return _write(context) if args.write else _check(context)


if __name__ == '__main__':
    sys.exit(main())
