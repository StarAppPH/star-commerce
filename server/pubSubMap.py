"""The fleet's Pub/Sub map: server/pubsub_map.json and server/PUBSUB_MAP.md.

Every Star App repo checks in the same two files: pubsub_map.json (the source
of truth) and PUBSUB_MAP.md (rendered from the JSON alone). They list every
@pubSubDecorator publisher in the fleet, the push handler each consumer has
for it, and whether the payload satisfies what that handler reads.

    cd server && python pubSubMap.py            # check: exit 1 when stale or failing
    cd server && python pubSubMap.py --write    # regenerate both files
    cd server && python pubSubMap.py --write-all  # ... here and in every sibling checkout
    cd server && python pubSubMap.py --explain createUnit_accounting

`--write-all` regenerates this repo's map, then each sibling checkout's with
this process's own writeMap (a sibling's code is never run; its sources are
parsed, as always). A sibling is skipped, with the reason, when its folder
is missing or not its own repo root (or holds another project), it is on
another branch than this repo (or either is on a detached HEAD), it lacks one
of the tool modules (TOOL_MODULES: pubSubMap*.py, pubSubModels.py), or they
differ from this repo's (a half-done rollout would write maps that its own
tests call stale). An error in one repo is reported and the others still
run; no write follows a symlink or leaves the repo.
Nothing is staged or committed: it prints the git commands to run.

AppCreator.py runs writeMap() at the end of a full generation, so the usual
"change a publisher or a handler, run AppCreator" step refreshes the map;
`--write` is for map-only refreshes (e.g. after a sibling commit).

How the map is built (pubSubMapScan.py, pubSubMapContract.py and
pubSubMapSection.py do the per-repo work):
- This repo is read from its working tree. Siblings are found transitively
  through every repo's server/services/*.py (`localRepoDirName`, resolved as
  <projectRoot>/../<dir>), then through the checked-in snapshot, and read at
  a committed git ref, never their working tree: this repo's branch name when
  the sibling has it, else main. So other sessions' uncommitted work never
  changes this repo's map; only commits do. `--repo name=path[@ref]`
  overrides one sibling and `--worktree-siblings` reads working trees.
- A sibling that is absent (no folder, not a git checkout, no such ref, or
  its AppConfig declares another projectName) keeps its section from the
  checked-in snapshot, with the reason reported (and a gitignored
  server/services/<service>.notFound.md note); the freshness test skips it.
  Its flows are re-checked against whatever side is live. A snapshot-only
  repo that no repo declares any more is dropped ("retired").
- The output is deterministic (sorted keys and lists, POSIX paths, no
  timestamps, SHAs or absolute paths, LF, one line per row), so the four
  repos' copies are byte-identical when they see the same commits.

Compat issues (`undeclared`, `nullable`, `literal`, `numericNarrowing`,
`forbiddenExtra`) always fail, in the tests and check mode of both repos of
the flow; nothing suppresses one. Fix the payload or the handler (`--explain`
shows both sides). The retired PUBSUB_CONTRACT_GAPS is a hatch error.
Static, type-level only: value-dependent logic (e.g. mapping type=='services'
to None, then dereferencing it) is not modelled; ok means the declared types
line up at every path read. test_pubSubContracts replay is the ground truth.

Pure stdlib, Python 3.10 compatible, byte-identical in every repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pubSubMapContract import ROW_IDENTITY_KEYS, Requirement, parsePath, rowIdentity
from pubSubMapScan import (ALWAYS, MAYBE, NONE, TOOL_MODULES, UNKNOWN, MapError, Obj, PayloadResolver, Repo,
                           isNullable, isRepoRoot, literalValues, memoizedGit, nonNull, pathText, printShape,
                           printType, readGitRef, readWorkingTree, runGit, segKeys)
from pubSubMapSection import (COMPAT_ISSUES, Section, buildSection, flag, isSafeDirName,
                              isSafeProjectName, projectNameOf)

MAP_JSON = 'pubsub_map.json'
MAP_MD = 'PUBSUB_MAP.md'
FORMAT_VERSION = 3
REGENERATE = ('Then run `cd server && python pubSubMap.py --write` and commit '
              'server/pubsub_map.json and server/PUBSUB_MAP.md.')
BLIND_SPOT = ('Static, type-level only: value-dependent logic (e.g. mapping type==\'services\' to '
              'None, then dereferencing it) is not modelled; ok means the declared types line up at '
              'every path read. test_pubSubContracts replay is the ground truth.')
_SCALARS = frozenset({'str', 'int', 'float', 'bool', 'datetime', 'date', 'lit', 'enum'})
_TYPE_ISSUES = ('literal', 'numericNarrowing', 'forbiddenExtra')
INFOS = ('deadBranch', 'deadRead', 'maybe', 'unverified')


def serverDir(projectRoot: Path) -> Path:
    return Path(projectRoot) / 'server'


def loadCheckedInMap(projectRoot: Path) -> dict | None:
    """The checked-in pubsub_map.json, or None when absent or unreadable."""
    path = serverDir(projectRoot) / MAP_JSON
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def readLF(path: Path) -> str | None:
    """A file's text with CRLF turned into LF (git may check the map files
    out with CRLF), or None when it cannot be read."""
    try:
        return path.read_bytes().decode('utf-8').replace('\r\n', '\n')
    except (OSError, UnicodeDecodeError):
        return None


def renderJson(data: dict) -> str:
    """json.dumps(sort_keys=True, indent=2) layout, except that every row (a
    publisher entry, a handler requirement, a flow's non-ok row, a service,
    a problem entry) is ONE line, so a diff shows whole rows."""
    return _render(data, 0, ()) + '\n'


def _isRow(path: tuple) -> bool:
    return (path[:1] in (('orphanHandlers',), ('unknownSourceHandlers',)) and len(path) == 2) or (
        len(path) == 4 and path[0] == 'repos' and path[2] in ('publishers', 'services',
                                                             'unattributedPublishSites')) or (
        len(path) == 6 and path[0] == 'repos' and path[2] == 'handlers' and path[4] == 'requirements') or (
        len(path) == 5 and path[0] == 'flows' and path[2] == 'compat' and path[3] == 'rows') or (
        len(path) == 4 and path[0] == 'flows' and path[3] == 'counts')


def _render(obj, level: int, path: tuple) -> str:
    if _isRow(path) or not isinstance(obj, (dict, list)) or not obj:
        return json.dumps(obj, sort_keys=True, ensure_ascii=True)
    pad, inner = '  ' * level, '  ' * (level + 1)
    if isinstance(obj, dict):
        items = [f'{inner}{json.dumps(k, ensure_ascii=True)}: {_render(obj[k], level + 1, path + (k,))}'
                 for k in sorted(obj)]
        return '{\n' + ',\n'.join(items) + '\n' + pad + '}'
    items = [inner + _render(x, level + 1, path + ('[]',)) for x in obj]
    return '[\n' + ',\n'.join(items) + '\n' + pad + ']'


# ----------------------------------------------------------------------------
# Fleet discovery
# ----------------------------------------------------------------------------

class RepoSource:
    """Where one project's section came from: 'worktree' (this repo, or a
    sibling under --worktree-siblings), 'ref' (a sibling's commit) or
    'snapshot' (absent sibling; `reason` says why, `absence` is its kind:
    undeclared, missing, notRepo, noRef, unreadable or otherProject)."""

    def __init__(self, projectName: str, dirName: str | None):
        self.projectName, self.dirName = projectName, dirName
        self.root = self.ref = self.reason = None
        self.origin = 'snapshot'
        self.section: Section | None = None
        self.snapshotSection = None
        self.refsTried: list = []
        self.declaredBy: list = []
        self.override = False
        self.reachedRefs = False
        self.absence = None


def _currentBranch(root: Path) -> str:
    return _branchName(root) or 'main'


def _branchName(root: Path) -> str | None:
    """The branch checked out in `root`, or None on a detached HEAD."""
    out = runGit(root, ['symbolic-ref', '--short', '-q', 'HEAD'])
    return (out.decode('utf-8', 'replace').strip() or None) if out else None


def _hasRef(root: Path, ref: str) -> bool:
    return runGit(root, ['rev-parse', '--verify', '--quiet', ref + '^{commit}']) is not None


def _topWithRef(root: Path, ref: str) -> str | None:
    """`ref` when root is the top of its own checkout (isRepoRoot) and has
    that commit (_hasRef), asked in one git call; None means ask each."""
    out = runGit(root, ['rev-parse', '--show-toplevel', '--verify', '--quiet', ref + '^{commit}'])
    top = out.decode('utf-8', 'replace').split('\n')[0].strip() if out else ''
    try:
        return ref if top and os.path.samefile(top, root) else None
    except OSError:
        return None


def _parseOverrides(values) -> dict:
    out = {}
    for value in values or []:
        name, _, rest = value.partition('=')
        path, at, ref = rest.rpartition('@')
        if not at or Path(rest).is_dir():  # a folder whose name holds '@', no ref
            path, ref = rest, ''
        if not name or not path:
            raise MapError(f'--repo expects name=path[@ref], got {value!r}')
        out[name] = (Path(path), ref or None)
    return out


def _dirConflict(name: str, dirs: dict) -> MapError:
    return MapError(f'{name} is declared with two localRepoDirNames: ' + '; '.join(
        f'{d!r} by {", ".join(dirs[d])}' for d in sorted(dirs)))


def _snapshotDir(snapshot: dict | None, name: str, warnings: list) -> str | None:
    dirName = ((snapshot or {}).get('repos', {}).get(name) or {}).get('localRepoDirName')
    if dirName is not None and not isSafeDirName(dirName):
        warnings.append(f'the snapshot gives {name} the localRepoDirName {dirName!r}, which is not a '
                        'plain folder name; ignored')
        return None
    return dirName


def discoverFleet(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False,
                  snapshot: dict | None = None, warnings: list | None = None):
    """(own projectName, {projectName: RepoSource}) for every repo reachable
    from this one's services, its siblings' services and the snapshot.
    Names that could not be a project or a folder are reported in
    `warnings` and never used as paths."""
    projectRoot = Path(projectRoot).resolve()
    overrides, warnings = overrides or {}, warnings if warnings is not None else []
    own = buildSection(readWorkingTree(projectRoot))
    ownName = own.projectName
    sources = {ownName: RepoSource(ownName, None)}
    sources[ownName].section, sources[ownName].origin = own, 'worktree'
    sources[ownName].root = projectRoot
    declared = {}  # projectName -> {dirName: [source labels]}
    branch = _currentBranch(projectRoot)

    def declare(section: Section, label: str):
        for var, svc in flag(section.data, 'services').items():
            if svc['localRepoDirName']:
                declared.setdefault(svc['projectName'], {}).setdefault(
                    svc['localRepoDirName'], []).append(f'{label} ({var})')

    declare(own, 'server/services')
    snapshotRepos = []
    for name in sorted((snapshot or {}).get('repos', {})):
        if isSafeProjectName(name):
            snapshotRepos.append(name)
        else:
            warnings.append(f'the snapshot has a repo named {name!r}, which is not a plain name; ignored')
    while True:
        pending = [p for p in sorted(declared) if p not in sources] or [
            p for p in snapshotRepos if p not in sources]
        if not pending:
            break
        name = pending[0]
        dirs = declared.get(name, {})
        if len(dirs) > 1:
            raise _dirConflict(name, dirs)
        dirName = sorted(dirs)[0] if dirs else _snapshotDir(snapshot, name, warnings)
        src = sources[name] = RepoSource(name, dirName)
        src.declaredBy = sorted(label for labels in dirs.values() for label in labels) or [
            'the checked-in snapshot']
        section = _loadSibling(src, projectRoot, overrides.get(name), branch, worktreeSiblings)
        if section is not None:
            declare(section, f'{dirName}/server/services@{src.ref or "worktree"}')
    for name, dirs in declared.items():
        if len(dirs) > 1:
            raise _dirConflict(name, dirs)
    ownDirs = sorted(declared.get(ownName, {}))
    sources[ownName].dirName = ownDirs[0] if ownDirs else _snapshotDir(snapshot, ownName, warnings)
    for name in sorted(overrides):
        if name == ownName or name not in sources:
            warnings.append(f'--repo {name}=... ignored: ' + ('that is this repo' if name == ownName else
                            'no services file or snapshot names that project'))
    return ownName, sources


def _loadSibling(src: RepoSource, projectRoot: Path, override, branch: str, worktree: bool):
    src.override = override is not None
    src.refsTried = [override[1]] if override and override[1] else list(
        dict.fromkeys([f'refs/heads/{branch}', 'refs/heads/main']))
    root = override[0] if override else (projectRoot.parent / src.dirName if src.dirName else None)
    if root is None:
        src.reason, src.absence = 'no localRepoDirName is declared for it', 'undeclared'
        return None
    root = Path(root)
    src.root = root
    if not root.is_dir():
        src.reason, src.absence = f'no checkout at {root}', 'missing'
        return None
    ref = None if worktree else _topWithRef(root, src.refsTried[0])
    if ref is None and not isRepoRoot(root):
        src.reason, src.absence = f'{root} is not the top of a git checkout', 'notRepo'
        return None
    src.reachedRefs = True
    if worktree:
        files, src.origin, src.refsTried = readWorkingTree(root), 'worktree', ['its working tree']
    else:
        ref = ref or next((r for r in src.refsTried if _hasRef(root, r)), None)
        if ref is None:
            src.reason, src.absence = f'{root} has none of ' + ', '.join(src.refsTried), 'noRef'
            return None
        files = readGitRef(root, ref)
        if files is None:
            src.reason, src.absence = f'could not read {root} at {ref}', 'unreadable'
            return None
        src.ref, src.origin = ref, 'ref'
    where = f'{src.dirName or root}@{src.ref or "working tree"}'
    try:
        declaredName = projectNameOf(Repo(files))
        if declaredName != src.projectName:
            src.reason = (f'{root}/server/AppConfig.py declares projectName {declaredName!r}, '
                          f'not {src.projectName!r}')
            src.origin, src.absence = 'snapshot', 'otherProject'
            return None
        src.section = buildSection(files, src.projectName)
    except MapError as e:
        raise MapError(f'{where}: {e}') from None
    return src.section


# ----------------------------------------------------------------------------
# Compat: walk each requirement path through the publisher's payload shape
# ----------------------------------------------------------------------------

def _members(shape):
    if isinstance(shape, tuple) and shape[0] == 'union':
        return [m for m in shape[1] if m != NONE]
    return [shape]


def _shapeNullable(shape) -> bool:
    if isinstance(shape, Obj) or shape == UNKNOWN:
        return False
    if shape[0] == 'union':
        return any(m == NONE or (not isinstance(m, Obj) and isNullable(m)) for m in shape[1])
    return shape == NONE


class _View:
    __slots__ = ('status', 'shape', 'presence', 'nullable', 'field')

    def __init__(self, status, shape=None, presence=None, nullable=False, field=None):
        self.status, self.shape, self.presence = status, shape, presence
        self.nullable, self.field = nullable, field


def navigate(section: Section, shape, path: tuple) -> list:
    """The publisher's views at `path`, one per union branch: found (with its
    type, presence, nullability and publisher field), absent (a closed shape
    without the key), ancestorAbsent, or unverified (an open shape, any).
    An alias segment matches any of the keys pydantic accepts for it."""
    branches = [(_View('found', shape, ALWAYS, False, None), ('input',))]
    last = len(path) - 1
    for i, seg in enumerate(path):
        keys, nxt = segKeys(seg), []
        for view, mode in branches:
            if view.status != 'found':
                nxt.append((view if view.status != 'absent' else _View('ancestorAbsent'), mode))
                continue
            for member in _members(view.shape):
                member = section.payloads.expand(member, mode)
                if isinstance(member, Obj):
                    if keys[0] == '[]':
                        nxt.append((_View('unverified'), mode))
                        continue
                    hit = next((k for k in keys if k in member.fields and k not in section.secretKeys), None)
                    if hit is not None:
                        sub, presence, field = member.fields[hit]
                        nxt.append((_View('found', sub, presence, _shapeNullable(sub), field),
                                    member.mode or mode))
                    elif member.closed or keys[0] in section.secretKeys:
                        nxt.append((_View('absent' if i == last else 'ancestorAbsent'), mode))
                    else:
                        nxt.append((_View('unverified'), mode))
                elif isinstance(member, tuple) and member[0] == 'list' and keys[0] == '[]':
                    nxt.append((_View('found', member[1], ALWAYS, _shapeNullable(member[1]),
                                      view.field), mode))
                elif isinstance(member, tuple) and member[0] in _SCALARS:
                    # a key (or element) of a str/number/bool: that read never happens
                    nxt.append((_View('ancestorAbsent'), mode))
                else:
                    nxt.append((_View('unverified'), mode))
        branches = nxt
    return [v for v, _ in branches]


def _typeIssues(pub, con) -> set:
    """literal / numericNarrowing between a publisher type and a consumer
    type (lists compared element-wise; dicts, models and any are skipped)."""
    issues = set()
    if isinstance(pub, Obj) or pub == UNKNOWN or con is None:
        return issues
    pubMembers = [m for m in _members(pub) if not isinstance(m, Obj)]
    conMembers = _members(nonNull(con))
    if any(m[0] == 'any' for m in conMembers) or not pubMembers:
        return issues
    cv, pv = literalValues(con), literalValues(('union', tuple(pubMembers)))
    if cv is not None and pv is not None and not pv <= cv:
        issues.add('literal')
    kinds = {m[0] for m in conMembers}
    if 'int' in kinds and 'float' not in kinds and any(m[0] == 'float' for m in pubMembers):
        issues.add('numericNarrowing')
    pubLists = [m for m in pubMembers if m[0] == 'list']
    conLists = [m for m in conMembers if m[0] == 'list']
    if pubLists and conLists:
        for p in pubLists:
            issues |= _typeIssues(p[1], conLists[0][1])
    return issues


def checkRequirement(section: Section, shape, req, consumerRepo=None) -> dict:
    """The publisher side of one requirement and its result: ok, issue
    (with `issue`), or an info result (deadBranch, deadRead, maybe,
    unverified). A read inside a lenient try is no exception: its issue is
    an issue, since the handler would drop the message silently. A
    requirement from the map whose type cannot be checked
    (typeUnknown) gives unverified, whatever else holds: a skipped check
    outranks maybe and the other info results."""
    views = navigate(section, shape, req.path)
    found = [v for v in views if v.status == 'found']
    statuses = {v.status for v in views}
    presence = ('unknown' if 'unverified' in statuses else
                'absent' if not found else
                ALWAYS if len(found) == len(views) and all(v.presence == ALWAYS for v in found)
                else MAYBE)
    row = {'presence': presence,
           'publisherType': ' | '.join(sorted({printShape(v.shape) for v in found})) if found else ''}
    fields = sorted({v.field for v in found if v.field})
    if fields:
        row['publisherField'] = ', '.join(fields)
    if req.unverifiable:
        row['result'] = 'unverified'
        return row
    if req.kind == 'dynamicKey':
        row['result'] = 'dynamicKey'
        return row
    if req.deadBranch:
        for disc in req.deadBranch.split(', '):
            dviews = navigate(section, shape, parsePath(disc))
            if dviews and all(v.status == 'found' and v.presence == ALWAYS for v in dviews):
                row['result'] = 'deadBranch'
                return row
    issues, infos = set(), set()
    for v in views:
        if v.status == 'unverified':
            infos.add('unverified')
        elif v.status == 'ancestorAbsent':
            infos.add('deadRead')
        elif v.status == 'absent':
            if (req.required or (req.nonNull and not req.hasDefault)) and not req.ifPresent:
                issues.add('undeclared')
            else:
                infos.add('deadRead')
        else:
            if req.nonNull and v.nullable and not req.ifPresent and not req.noneToValue:
                issues.add('nullable')
            if v.presence == MAYBE and not req.ifPresent and (
                    req.required or (req.nonNull and not req.hasDefault)):
                infos.add('maybe')
            if req.typeUnknown:
                row['typeUnchecked'] = True
                continue
            pubType = section.repo.rootTypes(v.shape)
            conType = consumerRepo.rootTypes(req.ctype) if consumerRepo and req.ctype else req.ctype
            issues |= _typeIssues(pubType, conType)
            if req.forbidKeys is not None:
                for member in _members(v.shape):
                    member = section.payloads.expand(member, ('input',))
                    if isinstance(member, Obj) and member.closed:
                        extra = sorted(set(member.fields) - set(req.forbidKeys) - section.secretKeys)
                        if extra:
                            issues.add('forbiddenExtra')
                            row['extraKeys'] = extra
                    elif isinstance(member, Obj):
                        infos.add('unverified')
    if issues:
        issue = next(i for i in COMPAT_ISSUES if i in issues)
        row['result'], row['issue'] = 'issue', issue
    elif row.get('typeUnchecked'):
        row['result'] = 'unverified'
    else:
        row['result'] = next((i for i in INFOS if i in infos), 'ok')
    return row


def consumerRequirements(data: dict, flow: dict) -> dict:
    """{row identity: the handler's requirement} for a flow, so a flow row
    (publisher side) can be shown with its consumer type and flags."""
    handler = data['repos'][flow['consumer']]['handlers'][flow['handler']]
    return {rowIdentity(r): r for r in handler.get('requirements', [])}


_PUBLISHER_KEYS = ('presence', 'publisherType', 'publisherField', 'issue', 'extraKeys')


def _publisherSide(row: dict) -> tuple:
    return tuple(row.get(k) or None for k in ('presence', 'publisherType', 'publisherField'))


def _evaluateFlow(pub: RepoSource, fn: str, con: RepoSource, handler: str, snapshotFlow):
    """(compat, live rows) of one flow. `compat` is what the map stores: the
    verdict, the count of each result and the non-ok rows; an ok row stores
    nothing. The live rows pair every requirement with its full result, for
    --explain.

    With one side absent, its snapshot section stands in. An absent
    consumer's recorded requirements (Requirement.fromJson: paths, flags,
    consumer types, extra='forbid' key sets) are re-checked against the live
    payload, so an unchanged fleet gives the same map. An absent publisher's
    recorded results are reused only for requirements that are unchanged
    here; the others are unverified."""
    hdata = (con.section.data if con.section else con.snapshotSection)['handlers'][handler]
    snapshot = (snapshotFlow or {}).get('compat') or {}
    if pub.section is None and con.section is None:
        return snapshot or {'verdict': 'unverified', 'counts': {}}, []
    snapRows = {rowIdentity(r): r for r in snapshot.get('rows', [])}
    snapReqs = {rowIdentity(r): r for r in (flag(con.snapshotSection or {}, 'handlers').get(
        handler) or {}).get('requirements', [])}
    reqs = (con.section.requirements[handler] if con.section is not None
            else [Requirement.fromJson(r) for r in hdata.get('requirements', [])])
    rows, unmatched = [], False
    for req in reqs:
        row = {k: v for k, v in req.toJson().items() if k in ROW_IDENTITY_KEYS}
        old = snapRows.get(rowIdentity(row))
        if pub.section is not None:
            row.update(checkRequirement(pub.section, pub.section.shapes[fn], req,
                                        con.section.repo if con.section else None))
            if row.get('typeUnchecked') and old and old.get('issue') in _TYPE_ISSUES \
                    and _publisherSide(row) == _publisherSide(old) and row['result'] == 'unverified':
                row.update({k: old[k] for k in ('issue', 'extraKeys') if k in old}, result='issue')
        elif snapReqs.get(rowIdentity(row)) == req.toJson():
            row.update({k: old[k] for k in _PUBLISHER_KEYS if old and k in old})
            row['result'] = old['result'] if old else 'ok'
        else:
            unmatched = True
            row.update({'presence': 'unknown', 'result': 'unverified'})
        rows.append((req, row))
    payloadSource = (pub.section.data if pub.section else pub.snapshotSection)['publishers'][fn][
        'payload']['source']
    results = [row['result'] for _, row in rows]
    unchecked = any(r.get('typeUnchecked') and r['result'] == 'unverified' for _, r in rows)
    if 'issue' in results:
        verdict = 'issues'
    elif payloadSource == 'unknown' or unmatched or unchecked:
        verdict = 'unverified'
    else:
        verdict = 'ok'
    compat = {'verdict': verdict, 'counts': {r: results.count(r) for r in sorted(set(results))}}
    notOk = sorted((_storedRow(row) for _, row in rows if row['result'] != 'ok'), key=rowIdentity)
    if notOk:
        compat['rows'] = notOk
    return compat, rows


def _storedRow(row: dict) -> dict:
    """A non-ok row as the map stores it: the requirement's identity, the
    result, and the publisher side (empty values left out)."""
    keep = ROW_IDENTITY_KEYS + _PUBLISHER_KEYS + ('result',)
    return {k: v for k, v in row.items() if k in keep and v not in (None, '', [], False)}


# ----------------------------------------------------------------------------
# The join
# ----------------------------------------------------------------------------

class FleetMap:
    """A freshly built map: `data` (what pubsub_map.json holds), the rendered
    `json` and `markdown`, the live `sources` behind it, and what the build
    noticed beyond the data: `warnings`, `retired` repos dropped from the
    snapshot, and `blockers` (why --write must refuse)."""

    def __init__(self, ownName: str, sources: dict, data: dict, liveRows: dict | None = None,
                 warnings=(), retired=(), blockers=()):
        self.ownName, self.sources, self.data = ownName, sources, data
        self.liveRows = liveRows or {}
        self.warnings, self.retired, self.blockers = list(warnings), list(retired), list(blockers)
        self.json = renderJson(data)
        self.markdown = renderMarkdown(data)

    def outputs(self) -> tuple:
        return ((MAP_JSON, self.json), (MAP_MD, self.markdown))

    def failures(self) -> list:
        """The problems that fail this repo's real-fleet tests."""
        return ownFailures(self.problems(), self.ownName)

    def problems(self) -> list:
        return problems(self.data) + [
            _problem('retiredRepo', [], f'{name} was dropped from the map: it is only in the snapshot and '
                                        'no repo declares it in server/services any more.')
            for name in self.retired]


def _declares(data: dict, project: str) -> bool:
    return any(svc['projectName'] == project for svc in flag(data, 'services').values())


def fleetSources(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False,
                 warnings: list | None = None) -> tuple:
    """(own projectName, {projectName: RepoSource}, the checked-in map, the
    snapshot): discoverFleet, with the checked-in map as the snapshot when
    this version of the tool wrote it."""
    raw = loadCheckedInMap(projectRoot)
    snapshot = raw if (raw or {}).get('formatVersion') == FORMAT_VERSION else None
    ownName, sources = discoverFleet(projectRoot, overrides, worktreeSiblings, snapshot, warnings)
    return ownName, sources, raw, snapshot


def buildMap(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False,
             discovered: tuple | None = None) -> FleetMap:
    """Discover the fleet (or take freshSources' `discovered` one), build
    every present repo's section, reuse the snapshot for absent ones, and
    join publishers to handlers."""
    if discovered is None:
        warnings = []
        with memoizedGit():
            ownName, sources, raw, snapshot = fleetSources(projectRoot, overrides, worktreeSiblings, warnings)
    else:
        ownName, sources, raw, snapshot, warnings = discovered
    for name, src in sources.items():
        src.snapshotSection = (snapshot or {}).get('repos', {}).get(name)
    kept = {name for name, src in sources.items() if src.section is not None}
    while True:  # a snapshot-only repo stays while a kept section still declares it
        more = {name for name, src in sources.items() if name not in kept and src.snapshotSection
                and any(_declares(sources[k].section.data if sources[k].section else
                                  sources[k].snapshotSection, name) for k in kept)}
        if not more:
            break
        kept |= more
    retired = sorted(name for name, src in sources.items() if name not in kept and src.snapshotSection)
    repos = {}
    for name in sorted(kept):
        src = sources[name]
        entry = dict(src.section.data if src.section is not None else src.snapshotSection)
        if src.dirName:
            entry['localRepoDirName'] = src.dirName
        repos[name] = entry
    snapFlows = {(f['publisher'], f['fn'], f['consumer']): f for f in (snapshot or {}).get('flows', [])}
    flows, liveRows = [], {}
    for pubName, entry in sorted(repos.items()):
        for fn, pub in sorted(flag(entry, 'publishers').items()):
            for conName in flag(pub, 'consumers'):
                handler = f'{fn}_{pubName}'
                flow = {'publisher': pubName, 'fn': fn, 'topic': pub['topic'], 'consumer': conName}
                if conName not in repos:
                    flow['status'] = 'repoUnknown'
                elif handler not in flag(repos[conName], 'handlers'):
                    flow['status'] = 'missingHandler'
                else:
                    flow['handler'], flow['status'] = handler, 'ok'
                    flow['compat'], liveRows[(pubName, fn, conName)] = _evaluateFlow(
                        sources[pubName], fn, sources[conName], handler,
                        snapFlows.get((pubName, fn, conName)))
                flows.append(flow)
    orphans, unknownSources = [], []
    for conName, entry in sorted(repos.items()):
        for handler, h in sorted(flag(entry, 'handlers').items()):
            source = repos.get(h['source'])
            publisher = flag(source, 'publishers').get(h['topicFn']) if source else None
            if source is None:
                unknownSources.append({'repo': conName, 'handler': handler, 'source': h['source']})
            elif publisher is None:
                orphans.append({'repo': conName, 'handler': handler,
                                'reason': f"{h['source']} has no publisher {h['topicFn']}"})
            elif conName not in flag(publisher, 'consumers'):
                orphans.append({'repo': conName, 'handler': handler,
                                'reason': f"{h['source']}'s {h['topicFn']} does not list {conName} "
                                          'in projectNameConsumers'})
    data = {'formatVersion': FORMAT_VERSION, 'repos': repos, 'flows': flows}
    if orphans:
        data['orphanHandlers'] = orphans
    if unknownSources:
        data['unknownSourceHandlers'] = unknownSources
    return FleetMap(ownName, sources, data, liveRows, warnings, retired,
                    _blockers(ownName, sources, set(repos) | set(retired), raw))


def _blockers(ownName: str, sources: dict, settled: set, raw: dict | None) -> list:
    """Siblings the map would lose or misstate if written now: neither in
    the map nor retired (`settled`), yet in the checked-in map (any format)
    or addressed by this repo's publishers or handlers."""
    own, out = sources[ownName].section.data, []
    for name, src in sorted(sources.items()):
        if name in settled:
            continue
        involved = (name in (raw or {}).get('repos', {})
                    or any(name in flag(p, 'consumers') for p in flag(own, 'publishers').values())
                    or any(h['source'] == name for h in flag(own, 'handlers').values()))
        if involved:
            out.append(_missingRepoMessage(name, src.dirName) + f' ({src.reason})')
    return out


def _missingRepoMessage(name: str, dirName: str | None) -> str:
    where = f'../{dirName}' if dirName else 'its checkout'
    return (f'{name} is not in the map: no checkout at {where} and no snapshot section; clone it or '
            f'pass --repo {name}=<path>, then run `python pubSubMap.py --write`')


_FRESH, _DISCOVERED = {}, {}  # resolved root -> freshMap's FleetMap / freshSources' tuple


def freshSources(projectRoot: Path) -> tuple:
    """fleetSources(projectRoot) plus its warnings, discovered once per
    process: freshMap builds on it and tests share it (their teardown
    releases the trees they parse again, releaseTrees)."""
    key = str(Path(projectRoot).resolve())
    if key not in _DISCOVERED:
        warnings = []
        with memoizedGit():
            _DISCOVERED[key] = (*fleetSources(projectRoot, warnings=warnings), warnings)
    return _DISCOVERED[key]


def freshMap(projectRoot: Path) -> FleetMap:
    """buildMap(projectRoot) on freshSources, built once per process (the
    tests share it); its sibling notes are synced like every other build's.
    It keeps each section's sources but not their parse trees."""
    key = str(Path(projectRoot).resolve())
    if key not in _FRESH:
        _FRESH[key] = buildMap(projectRoot, discovered=freshSources(projectRoot))
        syncSiblingNotes(projectRoot, _FRESH[key])
        releaseTrees(_FRESH[key].sources)
    return _FRESH[key]


def releaseTrees(sources: dict) -> None:
    """Give each live section an unparsed Repo over the same sources, so a
    long-lived map or discovery holds text, not millions of AST nodes (and
    the caches built on them); a later reader parses again, on demand."""
    for src in sources.values():
        if src.section is not None:
            repo = Repo({rel: module.text for rel, module in src.section.repo.modules.items()})
            src.section.repo, src.section.payloads = repo, PayloadResolver(repo)


def incidentView(data: dict, project: str) -> dict:
    """A project's section plus the flows that touch it (freshness check)."""
    return {'repo': data['repos'].get(project),
            'flows': [f for f in data['flows'] if project in (f['publisher'], f['consumer'])]}


def siblingView(data: dict, project: str, own: str) -> dict:
    """A sibling's section plus its flows that do not touch `own`."""
    return {'repo': data['repos'].get(project),
            'flows': [f for f in data['flows'] if project in (f['publisher'], f['consumer'])
                      and own not in (f['publisher'], f['consumer'])]}


def differingAnchors(old: dict, new: dict, limit: int = 10) -> list:
    """Up to `limit` publishers, handlers or topics whose entries differ."""
    out = []
    oldRepo, newRepo = old.get('repo') or {}, new.get('repo') or {}
    for kind in ('publishers', 'handlers'):
        a, b = oldRepo.get(kind, {}), newRepo.get(kind, {})
        for name in sorted(set(a) | set(b)):
            if a.get(name) != b.get(name):
                out.append((b.get(name) or a.get(name) or {}).get('anchor', name))
    flowsA = {(f['topic'], f['consumer']): f for f in old.get('flows', [])}
    flowsB = {(f['topic'], f['consumer']): f for f in new.get('flows', [])}
    for key in sorted(set(flowsA) | set(flowsB)):
        if flowsA.get(key) != flowsB.get(key):
            out.append(f'{key[0]} -> {key[1]}')
    for kind in sorted(set(oldRepo) | set(newRepo)):
        if kind not in ('publishers', 'handlers') and oldRepo.get(kind) != newRepo.get(kind):
            out.append(f'section {kind}')
    return out[:limit]


# ----------------------------------------------------------------------------
# Problems: one structured list behind the tests, the CLI, writeMap and the MD
# ----------------------------------------------------------------------------

# kind -> MD heading; the order is the MD's. 'retiredRepo' is information only.
PROBLEM_KINDS = {
    'missingHandler': 'Missing handlers', 'repoUnknown': 'Consumers not in the map',
    'compatIssue': 'Compat issues',
    'orphanHandler': 'Orphan handlers', 'unknownSource': 'Unknown-source handlers',
    'unresolvedContract': 'Unresolved handler contracts',
    'nonLiteralConsumers': 'Non-literal consumer lists', 'unresolvedConsumers': 'Unknown consumers',
    'unscannedPublisher': 'Publishers outside the scanned folders',
    'pubSubAdminInvisible': 'Publishers pubSubAdmin.py cannot read',
    'unattributedPublishSite': 'Hand-publish sites (unattributed)', 'hatchError': 'Escape-hatch errors',
    'retiredRepo': 'Retired repos',
}
INFO_KINDS = frozenset({'retiredRepo'})

# The problem kinds each real-fleet test in test_pubSubMap.py fails on, and
# which end of a flow the repo must be to act (None: any). Check mode fails on
# exactly these, so `python pubSubMap.py` stands in for those tests where
# pytest cannot run. retiredRepo is information only.
FLEET_CHECKS = {
    'ownPublishers': (('missingHandler', 'repoUnknown'), 'publisher'),
    'addressedHere': (('missingHandler',), 'consumer'),
    'handlerSources': (('orphanHandler', 'unknownSource'), None),
    'contracts': (('unresolvedContract',), None),
    'pubSubAdmin': (('nonLiteralConsumers', 'unresolvedConsumers', 'unscannedPublisher',
                     'pubSubAdminInvisible'), None),
    'handPublish': (('unattributedPublishSite',), None),
    'hatches': (('hatchError',), None),
    'compat': (('compatIssue',), None),
}


def ownFailures(found: list, project: str, check: str | None = None) -> list:
    """The problems in `found` that fail one of `project`'s real-fleet
    tests (only the test `check`, when given), each once."""
    out = []
    for name, (kinds, role) in FLEET_CHECKS.items():
        out += [p for p in found if check in (None, name) and p['kind'] in kinds and project in p['projects']
                and (role is None or p.get(role) == project) and p not in out]
    return out


def _problem(kind: str, projects, message: str, **extra) -> dict:
    return {'kind': kind, 'projects': sorted(set(projects)), 'message': message, **extra}


def problems(data: dict) -> list:
    """Every problem in the map, as {kind, projects (the repos that must act),
    message (with the fix), publisher/consumer for flow problems}."""
    out, repos = [], data['repos']
    for f in data['flows']:
        pub = repos[f['publisher']]['publishers'][f['fn']]
        ends = {'publisher': f['publisher'], 'consumer': f['consumer']}
        compat = f.get('compat')
        if f['status'] == 'missingHandler':
            conDir = repos[f['consumer']].get('localRepoDirName') or f['consumer']
            out.append(_problem('missingHandler', ends.values(), (
                f"{pub['anchor']} publishes {f['topic']} to {f['consumer']}, but {conDir}/server/"
                f"PubSubRequests.py has no {f['fn']}_{f['publisher']}. Add a @route_config handler there, "
                f"or drop {f['consumer']} from projectNameConsumers."), **ends))
        elif f['status'] == 'repoUnknown':
            dirName = next((s['localRepoDirName'] for s in flag(repos[f['publisher']], 'services').values()
                            if s['projectName'] == f['consumer']), None)
            out.append(_problem('repoUnknown', [f['publisher']], f"{f['topic']}: "
                                + _missingRepoMessage(f['consumer'], dirName) + '.', **ends))
        elif compat:
            out += [_problem('compatIssue', ends.values(), compatMessage(data, f, r), **ends)
                    for r in compat.get('rows', []) if r['result'] == 'issue']
    for o in data.get('orphanHandlers', []):
        out.append(_problem('orphanHandler', [o['repo']], (
            f"{o['repo']} handler {o['handler']} has no publisher: {o['reason']}. Remove the handler, or "
            "add this repo to the publisher's projectNameConsumers."), consumer=o['repo']))
    for u in data.get('unknownSourceHandlers', []):
        out.append(_problem('unknownSource', [u['repo']], (
            f"{u['repo']} handler {u['handler']}: {u['source']!r} is not a known projectName (name "
            'handlers <publisherFunction>_<publisherProjectName>).'), consumer=u['repo']))
    for name, entry in sorted(repos.items()):
        for h, hd in sorted(flag(entry, 'handlers').items()):
            if not flag(hd, 'resolved'):
                out.append(_problem('unresolvedContract', [name], (
                    f"{name}: could not tell what {h} reads from its message. Parse it with its "
                    'generated <Fn>Message (python pubSubModels.py --pull writes objects_<publisher>.py).'),
                    consumer=name))
        for p in flag(entry, 'publishers').values():
            where = f"{name} {p['anchor']}"
            if not flag(p, 'consumersLiteral'):
                out.append(_problem('nonLiteralConsumers', [name], (
                    f"{where}: projectNameConsumers must be a literal list of services (pubSubAdmin.py "
                    f"reads it from source), not {p.get('rawConsumers')}."), publisher=name))
            if flag(p, 'unresolvedConsumers'):
                out.append(_problem('unresolvedConsumers', [name], (
                    f"{where}: {flag(p, 'unresolvedConsumers')} are not services/*.py variables."),
                    publisher=name))
            if not flag(p, 'inScannedDir'):
                out.append(_problem('unscannedPublisher', [name], (
                    f"{where} is not directly in server/ or server/actions/ (pubSubAdmin only scans those), "
                    f"so pubSubAdmin.py never creates topic {p['topic']}. Move it."), publisher=name))
            if flag(p, 'pubSubAdminProblems'):
                out.append(_problem('pubSubAdminInvisible', [name], (
                    f"{where}: pubSubAdmin.py cannot create topic {p['topic']}: "
                    + '; '.join(flag(p, 'pubSubAdminProblems')) + '.'), publisher=name))
        for site in flag(entry, 'unattributedPublishSites'):
            out.append(_problem('unattributedPublishSite', [name], (
                f"{name} {site['anchor']} calls publishMessage with topic {site['topic']}. Use "
                "f'{decoratedPublisher.__name__}_{AppConfig().projectName}' of a @pubSubDecorator "
                'function so the topic has an owner pubSubAdmin.py creates.'), publisher=name))
        for err in flag(entry, 'hatchErrors'):
            out.append(_problem('hatchError', [name], f'{name}: {err}'))
    unique = []  # two requirements at one path can make the same line
    for p in out:
        if p not in unique:
            unique.append(p)
    return unique


def compatMessage(data: dict, f: dict, r: dict) -> str:
    con = data['repos'][f['consumer']]
    h = con['handlers'][f['handler']]
    consumerType = consumerRequirements(data, f).get(rowIdentity(r), {}).get('consumerType')
    pubField = r.get('publisherField') or data['repos'][f['publisher']]['publishers'][f['fn']]['anchor']
    conField = r.get('forwardedTo') or h['anchor']
    extra = f" (extra keys {r['extraKeys']})" if r.get('extraKeys') else ''
    return (f"{f['topic']} -> {f['consumer']}.{f['handler']}: {r['path']} is {r['issue']}{extra} "
            f"(publisher {pubField}: {r.get('publisherType') or '-'}; consumer {conField}: {consumerType or 'any'}). "
            f"Fix the payload or the handler (python pubSubMap.py --explain {f['handler']}).")


# ----------------------------------------------------------------------------
# PUBSUB_MAP.md, rendered from the JSON alone
# ----------------------------------------------------------------------------

def _cell(text) -> str:
    return str(text).replace('|', '\\|').replace('\n', ' ')


def _flags(r: dict) -> str:
    names = [n for n in Requirement.FLAGS + ('extraForbid',) if r.get(n)]
    if r.get('deadBranch'):
        names.append(f"deadBranch({r['deadBranch']})")
    return ', '.join(names) or 'optional'


def renderMarkdown(data: dict) -> str:
    repos, flows = data['repos'], data['flows']
    lines = [
        '# Pub/Sub map', '',
        '> Generated by server/pubSubMap.py from pubsub_map.json. Do not edit. Regenerate: '
        '`cd server && python pubSubMap.py --write` (AppCreator also runs it). '
        '`python pubSubMap.py --explain <topic>` prints live file:line.', '',
        '**Legend.** A flow is one publisher -> consumer edge. *Payload* is how the publisher\'s '
        'return value was typed: `modelDump`, `dictLiteral`, `persistedDump` (a db.create/update '
        'of a model dump), `responseModel` (the route_config model), `declared` '
        '(PUBSUB_PAYLOAD_MODELS) or `unknown`; a typed return takes the unknown types of its values '
        'from the responseModel, else the PUBSUB_PAYLOAD_MODELS model. *Compat*: `ok`, `issues` (each fails the tests of '
        'both repos of the flow; nothing suppresses one) or `unverified` (payload unknown, or a side '
        'read from the snapshot could not be re-checked). Issues: `undeclared` (a required key the '
        'publisher never sends), `nullable` (the publisher may send null where the consumer needs a '
        'value), `literal`, `numericNarrowing` (float into int), `forbiddenExtra`. Info: '
        '`deadBranch`, `deadRead`, `maybe` (the publisher may omit a key the consumer needs), '
        '`unverified`, `dynamicKey` (the handler reads a key of this path that the map cannot '
        'resolve statically; not checked). *Reads* flags include `lenient` (read inside a '
        '`try/except Exception` that does not raise again; its issues still fail, as the handler '
        'would drop the message silently) and `noneToValue` (every before validator of the field '
        'provably turns None into a value, so null is accepted). A row that is not `ok` shows the '
        'publisher field, type and presence. Paths: `a.b` is a key, `a[]` a list element. '
        + BLIND_SPOT, '',
        '## Summary', '',
        '| Repo | Dir | Publishers | Push handlers | Utility handlers | Flows out | Flows in | Compat issues in |',
        '|---|---|---|---|---|---|---|---|']
    for name, entry in sorted(repos.items()):
        out = [f for f in flows if f['publisher'] == name]
        inn = [f for f in flows if f['consumer'] == name]
        issues = sum(_count(f, 'issue') for f in inn)
        lines.append(f"| {name} | {entry.get('localRepoDirName') or '-'} | {len(flag(entry, 'publishers'))} | "
                     f"{len(flag(entry, 'handlers'))} | {len(flag(entry, 'utilityHandlers'))} | {len(out)} | "
                     f"{len(inn)} | {issues} |")
    lines += ['', '## Topics', '', '| Topic | Publisher | Payload | Consumers | Compat |', '|---|---|---|---|---|']
    byTopic = {}
    for f in flows:
        byTopic.setdefault((f['publisher'], f['fn']), []).append(f)
    for (pubName, fn), group in sorted(byTopic.items()):
        p = repos[pubName]['publishers'][fn]
        publisher = (f"{pubName} · `{p['anchor']}` · {p['kind']} · "
                     f"{'prod' if flag(p, 'prodPublish') else 'mock-only'}")
        payload = p['payload']['source'] + (f" `{p['payload']['model'].split('::')[-1]}`"
                                            if p['payload']['model'] else '')
        consumers = '<br>'.join(f"{f['consumer']}: " + (f"`{f['handler']}`" if f.get('handler') else
                                                        f"**{f['status'].upper()}**") for f in group)
        compat = '<br>'.join(f"{f['consumer']}: " + (_verdict(f['compat']) if f.get('compat') else '-')
                             for f in group)
        lines.append(f"| `{p['topic']}` | {publisher} | {payload} | {consumers} | {compat} |")
    lines += ['', '## Flows', '']
    for f in flows:
        if not f.get('compat'):
            continue
        h = repos[f['consumer']]['handlers'][f['handler']]
        notOk = {rowIdentity(r): r for r in f['compat'].get('rows', [])}
        counts = ', '.join(f'{n} {r}' for r, n in sorted(f['compat'].get('counts', {}).items()))
        lines += [f"### {f['topic']} -> {f['consumer']}", '',
                  f"Handler `{h['anchor']}` · compat {_verdict(f['compat'])}", '',
                  f"<details><summary>{len(h.get('requirements', []))} requirements ({counts})</summary>",
                  '', '| Path | Reads | Consumer field / type | Result |', '|---|---|---|---|']
        for req in h.get('requirements', []):
            what = req.get('model', '').split('::')[-1] if req.get('kind') == 'model' else (
                (req.get('forwardedTo', '').split('::')[-1] + ': ' if req.get('forwardedTo') else '')
                + req.get('consumerType', ''))
            lines.append(f"| `{req['path']}` | {_cell(_flags(req))} | {_cell(what) or '-'} | "
                         f"{_cell(_resultCell(notOk.get(rowIdentity(req))))} |")
        lines += ['', '</details>', '']
    lines += ['## Problems and unknowns', '']
    lines += _problemSections(data)
    return '\n'.join(lines).rstrip('\n') + '\n'


def _resultCell(row: dict | None) -> str:
    if row is None:
        return 'ok'
    text = row['result'] + (f" ({row['issue']})" if row.get('issue') else '')
    if row.get('publisherType') or row.get('presence'):
        field = row.get('publisherField', '').split('::')[-1]
        text += f" - publisher {field + ': ' if field else ''}{row.get('publisherType') or '-'}, " \
                f"{row.get('presence', 'unknown')}"
    if row.get('extraKeys'):
        text += f" (extra keys {', '.join(row['extraKeys'])})"
    return text


def _count(flow: dict, result: str) -> int:
    return ((flow.get('compat') or {}).get('counts') or {}).get(result, 0)


def _verdict(compat: dict) -> str:
    n = (compat.get('counts') or {}).get('issue', 0)
    return compat['verdict'] + (f' ({n} issue{"s" if n != 1 else ""})' if n else '')


def _problemSections(data: dict) -> list:
    repos, flows = sorted(data['repos'].items()), data['flows']
    publishers = [(n, p) for n, e in repos for p in flag(e, 'publishers').values()]
    handlers = [(n, h, d) for n, e in repos for h, d in sorted(flag(e, 'handlers').items())]
    found = problems(data)
    sections = [(title, [p['message'] for p in found if p['kind'] == kind])
                for kind, title in PROBLEM_KINDS.items() if kind not in INFO_KINDS]
    sections += [
        ('Utility handlers', [f"{n} `{h}`" for n, e in repos for h in flag(e, 'utilityHandlers')]),
        ('Truncated analyses (depth limit)', [f"{n} `{h}`" for n, h, d in handlers if flag(d, 'truncated')]),
        ('Unknown payloads', [f"{n} `{p['anchor']}`" for n, p in publishers
                              if p['payload']['source'] == 'unknown']),
        ('Hand-publish sites (attributed, payload not checked)', [
            f"{n} `{s}` -> `{p['topic']}`" for n, p in publishers for s in flag(p, 'extraPublishSites')]),
        ('Mock-only publishers', [f"{n} `{p['topic']}`" for n, p in publishers if not flag(p, 'prodPublish')]),
        ('Dead branches and dead reads', [f"`{f['topic']}` -> {f['consumer']}: `{r['path']}` {r['result']}"
                                          for f in flows if f.get('compat') for r in f['compat'].get('rows', [])
                                          if r['result'] in ('deadBranch', 'deadRead')]),
        ('Reads with a key the map cannot resolve (not checked)', [
            f"{n} `{h}`: a key of `{r['path']}`" for n, e in repos for h, d in sorted(flag(e, 'handlers').items())
            for r in d.get('requirements', []) if r.get('kind') == 'dynamicKey']),
        ('Ambiguous names', [f'{n}: `{a}`' for n, e in repos for a in flag(e, 'ambiguousNames')]),
        ('Heuristic name resolutions', [f'{n}: `{a}`' for n, e in repos for a in flag(e, 'heuristicNames')]),
    ]
    lines = []
    for title, items in sections:
        lines += [f'### {title}', '']
        lines += [f'- {_cell(item)}' for item in items] if items else ['None.']
        lines.append('')
    lines += ['### Sibling sources', '',
              'Each repo generates this file from its own working tree and its siblings\' committed '
              'refs (or from this snapshot for an absent sibling). The generating repo\'s view is not '
              'recorded, so every copy stays byte-identical; `python pubSubMap.py` prints it.', '']
    return lines


# ----------------------------------------------------------------------------
# Writing, checking, explaining
# ----------------------------------------------------------------------------

def _write(path: Path, text: str) -> bool:
    """Write `text` (LF) unless the file already holds it, ignoring CRLF."""
    if readLF(path) == text:
        return False
    path.write_bytes(text.encode('utf-8'))
    return True


def _guardTarget(projectRoot: Path, path: Path) -> None:
    """Raise MapError unless `path` may be written: neither it nor its folder
    is a symlink, and it resolves inside the repo, so a symlinked (or
    junctioned) server/ or services/ cannot redirect a write elsewhere."""
    root = Path(projectRoot).resolve()
    if path.is_symlink() or path.parent.is_symlink():
        raise MapError(f'refusing to write {path}: it or its folder is a symlink')
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise MapError(f'refusing to write {path}: it resolves outside {root}') from None


def writeMap(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False) -> list:
    """Regenerate pubsub_map.json and PUBSUB_MAP.md (each written only when
    its content changed) and sync the sibling notes. Returns the changes,
    then the map's problems, as human lines. Raises MapError instead of
    writing a map that would lose a sibling (see FleetMap.blockers)."""
    written, notes, found = _writeFleet(projectRoot, buildMap(projectRoot, overrides, worktreeSiblings))
    return [f'wrote server/{name}' for name in written] + notes + found


def _writeFleet(projectRoot: Path, fleet: FleetMap) -> tuple:
    """Write a built map: (map files written, note changes, problem lines).
    Refuses (MapError) when writing would lose a sibling or a target is
    unsafe (_guardTarget)."""
    _refuseIfBlocked(projectRoot, fleet)
    server, written = serverDir(projectRoot), []
    for name, text in fleet.outputs():
        _guardTarget(projectRoot, server / name)
        if _write(server / name, text):
            written.append(name)
    for cache in (_FRESH, _DISCOVERED):
        cache.pop(str(Path(projectRoot).resolve()), None)
    return (written, syncSiblingNotes(projectRoot, fleet),
            [p['message'] for p in fleet.problems() if p['kind'] not in INFO_KINDS])


@memoizedGit()
def writeAll(projectRoot: Path, overrides: dict | None = None) -> tuple:
    """--write-all (see the module docs): write this repo's map, then every
    sibling checkout's that passes the gates. Any error in one repo is
    reported and the run goes on. Returns (lines, exit code): 2 when a repo
    errored, else 0. Never stages or commits anything."""
    projectRoot = Path(projectRoot).resolve()
    fleet = buildMap(projectRoot, overrides)
    branch, lines, changed, failed = _branchName(projectRoot), [], [], False

    def write(label, root, build):
        nonlocal failed
        try:
            written, notes, found = build()
        except Exception as e:  # one repo's failure must not stop the others
            failed = True
            lines.append(errorLine(label, e))
            return
        lines.append(f'{label}: ' + ('wrote ' + ', '.join(f'server/{n}' for n in written) if written
                                     else 'unchanged'))
        lines.extend(f'  {c}' for c in notes)
        lines.extend(f'  WARNING: {m}' for m in found)
        if written:
            changed.append(root)

    write(projectRoot.name, projectRoot, lambda: _writeFleet(projectRoot, fleet))
    for name, src in sorted(fleet.sources.items()):
        if name == fleet.ownName:
            continue
        label = src.dirName or name
        try:
            reason = _writeAllSkip(src, label, branch)
        except Exception as e:
            failed = True
            lines.append(errorLine(label, e))
            continue
        if reason:
            lines.append(f'{label}: skipped ({reason})')
        else:
            root = Path(src.root).resolve()
            write(label, root, lambda root=root: _writeFleet(root, buildMap(root, overrides)))
    if changed:
        lines += commitLines(projectRoot, 'the regenerated map',
                             [(root, [f'server/{MAP_JSON}', f'server/{MAP_MD}']) for root in changed])
        lines.append('(Paths are from the root of this repo. Each repo reads its siblings at their committed '
                     'refs, so after committing a source change, run --write-all again until every copy matches.)')
    else:
        lines.append('No map changed; nothing to commit.')
    return lines, 2 if failed else 0


def errorLine(label: str, e: Exception) -> str:
    """A repo's error line in a multi-repo run (a MapError says what to do;
    anything else is named with its type)."""
    detail = str(e) if isinstance(e, MapError) else f'{type(e).__name__}: {e}'
    return f'{label}: error ({detail})'


def commitLines(projectRoot: Path, what: str, changed: list) -> list:
    """`Commit <what> in: ...` and one `git -C <repo> add <paths> && git -C
    <repo> commit` line per (repo root, changed paths), with a sibling of
    this repo given as ../<dir>. Nothing is staged or committed here."""
    lines = [f'Commit {what} in: ' + ', '.join(Path(root).name for root, _ in changed)]
    for root, paths in changed:
        root = Path(root)
        where = f'../{root.name}' if root.parent == Path(projectRoot).parent else str(root)
        lines.append(f'  git -C {where} add {" ".join(paths)} && git -C {where} commit')
    return lines


def _writeAllSkip(src: RepoSource, label: str, branch: str | None) -> str | None:
    """Why --write-all (or pubSubModels.py --push) must not write this
    sibling, or None. Discovery already rejected a sibling it could not use
    (src.reason): no folder, not its own repo root, no such ref, another
    project. Adopted means it has every TOOL_MODULES file, identical."""
    if src.section is None:
        return src.reason
    root = Path(src.root)
    theirs = _branchName(root)
    if branch is None:
        return 'this repo is on a detached HEAD'
    if theirs is None:
        return f'{label} is on a detached HEAD'
    if theirs != branch:
        return (f'{label} is on {theirs}, this repo is on {branch}; switch it, or run '
                '`python pubSubMap.py --write` there')
    missing = next((name for name in sorted(TOOL_MODULES) if not (root / 'server' / name).is_file()), None)
    if missing:
        return f'not adopted yet: no server/{missing}'
    here = Path(__file__).resolve().parent
    if any(readLF(root / 'server' / name) != readLF(here / name) for name in sorted(TOOL_MODULES)):
        return (f"{label}'s pubSubMap tools differ from this repo's; run `python sharedFileCheck.py --write` "
                'there first')
    return None


def _refuseIfBlocked(projectRoot: Path, fleet: FleetMap) -> None:
    """Raise MapError when writing would lose a sibling (after syncing the
    sibling notes, which say how to get it back)."""
    if fleet.blockers:
        syncSiblingNotes(projectRoot, fleet)
        raise MapError('refusing to write the map: ' + '; '.join(fleet.blockers))


# ----------------------------------------------------------------------------
# "Sibling not found" notes (machine-specific, gitignored, never compared)
# ----------------------------------------------------------------------------

NOTE_SUFFIX = '.notFound.md'


def siblingNotes(projectRoot: Path, fleet: FleetMap) -> dict:
    """{file name: Markdown} that server/services/ should hold: one note per
    sibling this build could not use, named after its services file
    (<stem>.notFound.md) or, for a sibling known only transitively, after
    its projectName; a retired repo gets none. Written for an AI agent
    reading the repo later."""
    own = fleet.sources[fleet.ownName].section
    notes = {}
    for name, src in sorted(fleet.sources.items()):
        if name == fleet.ownName or src.section is not None or name in fleet.retired:
            continue
        services = sorted((var, svc) for var, svc in own.services.items() if svc['projectName'] == name)
        service = services[0] if services else None
        stem = Path(service[1]['file']).stem if service else name
        notes[stem + NOTE_SUFFIX] = _siblingNote(Path(projectRoot), fleet, src, service)
    return notes


def syncSiblingNotes(projectRoot: Path, fleet: FleetMap) -> list:
    """Write each note whose content changed and delete the notes of siblings
    that are usable again. Touches nothing but server/services/*.notFound.md:
    a name that is not a plain file name there is skipped and reported.
    Returns one line per file written, removed or skipped."""
    folder = serverDir(projectRoot) / 'services'
    try:
        _guardTarget(projectRoot, folder / ('x' + NOTE_SUFFIX))
    except MapError as e:
        return [f'skipped the sibling notes: {e}']
    notes, changes = siblingNotes(projectRoot, fleet), []
    for path in (sorted(folder.glob('*' + NOTE_SUFFIX)) if folder.is_dir() else []):
        if path.is_file() and path.name not in notes:
            path.unlink()
            changes.append(f'removed server/services/{path.name} (the sibling is usable again)')
    for name, text in sorted(notes.items()):
        target = folder / name
        if Path(name).name != name or target.resolve().parent != folder.resolve():
            changes.append(f'skipped a sibling note named {name!r}: not a plain file name in server/services')
            continue
        try:
            _guardTarget(projectRoot, target)
        except MapError as e:
            changes.append(f'skipped a sibling note: {e}')
            continue
        folder.mkdir(parents=True, exist_ok=True)
        if _write(target, text):
            changes.append(f'wrote server/services/{name} (a sibling checkout is missing; read it)')
    return changes


def _siblingNote(projectRoot: Path, fleet: FleetMap, src: RepoSource, service) -> str:
    name, own = src.projectName, fleet.sources[fleet.ownName].section
    var, svc = service if service else (None, None)
    title = Path(svc['file']).stem if svc else name
    expected = f'../{src.dirName}' if src.dirName else None
    absolute = projectRoot.resolve().parent / src.dirName if src.dirName else None
    folderMissing = absolute is None or not absolute.is_dir()
    publishes = sorted(p['topic'] for p in flag(own.data, 'publishers').values() if name in flag(p, 'consumers'))
    consumes = sorted(h for h, d in flag(own.data, 'handlers').items() if d['source'] == name)
    lines = [f'# {title} ({name}): local checkout not found', '',
             'Generated by `server/pubSubMap.py` on this machine. Gitignored, and deleted automatically '
             'once the checkout is usable; do not edit or commit it.', '', '## Facts', '']
    if svc:
        lines.append(f"- Service file: `server/{svc['file']}` (variable `{var}`); projectName `{name}`.")
    else:
        lines.append(f"- No services file here: this repo knows `{name}` only through "
                     f"{', '.join(src.declaredBy)}.")
    if expected:
        lines.append(f'- Expected checkout: `{expected}` (`{absolute}`).')
    if src.override:
        lines.append(f'- Checked instead, from `--repo {name}=...`: `{src.root}`.')
    lines.append(f'- Reason: {src.reason}.')
    if src.ref:
        lines.append(f'- Ref read: `{src.ref}`.')
    elif src.reachedRefs:
        lines.append('- Ref tried: ' + ', then '.join(f'`{r}`' for r in src.refsTried) + '.')
    else:
        lines.append('- Ref tried: none, the checkout was not reached (it would be read at '
                     + ', else '.join(f'`{r}`' for r in src.refsTried or ['refs/heads/main']) + ').')
    lines += ['', '## What this affects in this repo', '']
    topics = ('This repo publishes to it: ' + (', '.join(f'`{t}`' for t in publishes) or 'nothing')
              + '. It publishes to this repo (handlers here): '
              + (', '.join(f'`{h}`' for h in consumes) or 'nothing') + '.')
    if name in fleet.data['repos']:
        lines.append(f'- The Pub/Sub map uses the checked-in snapshot (`server/pubsub_map.json`) for `{name}`. '
                     f'{topics} Changes committed in that repo since the snapshot are invisible here: '
                     'handlers added or removed, consumer lists, payload or message models.')
        lines.append(f'- `test_pubSubMap.py::test_mapIsFreshForSiblings[{name}]` skips it. Compat for '
                     "the topics this repo publishes to it re-checks the snapshot's recorded handler "
                     "contracts (paths, flags, consumer types and extra='forbid' key sets) against "
                     "this repo's live payloads. Compat for the topics it publishes here reuses the "
                     "snapshot's results only for requirements unchanged here; the others are "
                     'unverified.')
    else:
        lines.append(f'- The checked-in snapshot has no section for `{name}` either, so the Pub/Sub map '
                     f'leaves it out: topics this repo publishes to it show as repoUnknown, and '
                     f'`python pubSubMap.py --write` refuses while that loses information. {topics}')
    if svc and folderMissing:
        lines.append(f"- AppCreator's servicePortCheck cannot verify the dev port of `server/{svc['file']}` "
                     '(a warning, generation goes on).')
        lines.append(f'- `tools/devPubSub.py` cannot resolve this consumer\'s repo without '
                     f'`--repo {name}=<path>`.')
    tests = (['the replay tests in `test_pubSubContracts.py`'] if consumes else []) + (
        ['`test_ifAllServicesMatchConsumerProjectName.py`'] if svc else [])
    if tests:
        lines.append(f'- pubSubMockDb messages from `{name}` come only from its own '
                     f"`pubSubMockDataGenerator.py`, so {' and '.join(tests)} "
                     f"{'depends' if len(tests) == 1 else 'depend'} on that generator having "
                     'run on this machine (its rows expire after 3 days).')
    if svc and svc.get('dev'):
        lines.append(f"- Local HTTP calls to its dev URL `{svc['dev']}` (from `server/{svc['file']}`) need "
                     'that service running.')
    lines += ['', '## How to fix', '', '- ' + _howToFix(src, expected, absolute)]
    lines.append('- Then run `cd server && python pubSubMap.py`; this note disappears once the sibling '
                 'is read.')
    return '\n'.join(lines) + '\n'


def _howToFix(src: RepoSource, expected, absolute) -> str:
    """The fix for why `src` is absent: clone only into a missing folder."""
    name, repo = src.projectName, f'`python pubSubMap.py --repo {src.projectName}=<path>[@<ref>]`'
    where = f'`{src.root}` (from `--repo {name}=...`)' if src.override else f'`{expected}` (`{absolute}`)'
    refs = ', '.join(f'`{r}`' for r in src.refsTried)
    return {
        'undeclared': f'Declare `localRepoDirName` for `{name}` in a services file, or pass {repo}.',
        'missing': (f'Nothing is at {where}: fix the path'
                    + (f', or drop `--repo` to use `{expected}`.' if expected else '.')
                    if src.override else
                    f'Clone the `{src.dirName}` repository (its remote is not recorded here) into {where}, '
                    f'or point the map at an existing checkout with {repo}.'),
        'notRepo': (f'{where} is a folder but not the top of a git checkout: make it a checkout of '
                    f'`{name}`, or point the map at one with {repo}.'),
        'noRef': (f'{where} has none of {refs}: fetch that branch there (or check it out), or name a '
                  f'ref it has with {repo}.'),
        'unreadable': f'Repair the checkout at {where} ({src.reason}), or use {repo}.',
        'otherProject': (f'{where} holds another project ({src.reason}): put the `{name}` checkout '
                         f'there, or point the map at it with {repo}.'),
    }.get(src.absence, f'Point the map at a `{name}` checkout with {repo}.')


def describeSources(fleet: FleetMap) -> list:
    lines = []
    for name, src in sorted(fleet.sources.items()):
        if name == fleet.ownName:
            lines.append(f'{name}: this repo (working tree)')
        elif src.section is not None:
            lines.append(f'{name}: {src.dirName} at {src.ref or "working tree"}')
        else:
            lines.append(f'{name}: snapshot ({src.reason})')
    return lines


def explain(fleet: FleetMap, term: str) -> list:
    """Live path:line for the publisher and every consumer of a topic,
    handler or publisher function, then each consumer requirement with its
    result and provenance (how the handler reaches the read, with file:line)."""
    data, out = fleet.data, []
    matches = [f for f in data['flows'] if term in (f['topic'], f['fn'], f.get('handler'))]
    for pubName, entry in sorted(data['repos'].items()):
        for fn, p in flag(entry, 'publishers').items():
            if term in (fn, p['topic']) and not any(m['publisher'] == pubName and m['fn'] == fn
                                                     for m in matches):
                out.append(f"{p['topic']} has no consumers.")
    if not matches:
        return out or [f'No topic, publisher or handler named {term!r} in the map.']

    def where(project, kind, name):
        src = fleet.sources.get(project)
        loc = src.section.lines.get((kind, name)) if src and src.section else None
        if loc is None:
            return '(source not available)'
        prefix = '' if project == fleet.ownName else f'{src.dirName}/'
        suffix = '' if project == fleet.ownName or src.ref is None else f' (at {src.ref})'
        return f'{prefix}server/{loc[0]}:{loc[1]}{suffix}'

    for (pubName, fn) in sorted({(m['publisher'], m['fn']) for m in matches}):
        p = data['repos'][pubName]['publishers'][fn]
        out.append(f"{p['topic']}: published by {pubName} {where(pubName, 'publisher', fn)} "
                   f"({p['kind']}, {'prod' if flag(p, 'prodPublish') else 'mock-only'}; payload "
                   f"{p['payload']['source']} {p['payload']['model'] or ''})".rstrip())
        for site in flag(p, 'extraPublishSites'):
            out.append(f'  also hand-published by {site}')
        for f in [m for m in matches if m['publisher'] == pubName and m['fn'] == fn]:
            if not f.get('handler'):
                out.append(f"  -> {f['consumer']}: {f['status']}")
                continue
            out.append(f"  -> {f['consumer']} {f['handler']} {where(f['consumer'], 'handler', f['handler'])}: "
                       f"compat {_verdict(f['compat'])}")
            live = fleet.liveRows.get((pubName, fn, f['consumer']))
            if not live:
                out += [f"     {r['path']}: {_resultCell(r)} (snapshot; no provenance)"
                        for r in f['compat'].get('rows', [])]
                continue
            for req, row in live:
                text = _resultCell(row if row['result'] != 'ok' else None)
                ctype = printType(req.ctype) if req.ctype is not None else (req.model or '').split('::')[-1]
                out.append(f"     {pathText(req.path)} [{_flags(req.toJson())}] {ctype or 'any'} -> {text}"
                           f"; via {', '.join(sorted(req.via)) or '(snapshot)'}")
    return out


def main(argv=None, projectRoot: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--write', action='store_true', help='regenerate the map files')
    parser.add_argument('--write-all', action='store_true',
                        help="regenerate the map here and in every sibling checkout on this branch")
    parser.add_argument('--explain', metavar='NAME', help='a topic, handler or publisher function')
    parser.add_argument('--repo', action='append', metavar='NAME=PATH[@REF]',
                        help="read a sibling from PATH (at REF) instead of ../<localRepoDirName>")
    parser.add_argument('--worktree-siblings', action='store_true',
                        help="read siblings' working trees instead of their committed refs")
    args = parser.parse_args(argv)
    projectRoot = Path(projectRoot or Path(__file__).resolve().parent.parent)
    try:
        return _run(args, projectRoot)
    except MapError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    except Exception as e:  # anything else is a bug, not a stale map
        print(f'ERROR: unexpected {type(e).__name__}: {e}', file=sys.stderr)
        return 2


def _run(args, projectRoot: Path) -> int:
    if args.write_all:
        if args.worktree_siblings:
            raise MapError('--write-all cannot be combined with --worktree-siblings: each repo is regenerated '
                           "from its own working tree already, and reading every sibling's uncommitted changes "
                           'into every map would mix them')
        if args.explain:
            raise MapError('--write-all cannot be combined with --explain')
        lines, code = writeAll(projectRoot, _parseOverrides(args.repo))
        print('\n'.join(lines))
        return code
    fleet = buildMap(projectRoot, _parseOverrides(args.repo), args.worktree_siblings)
    for line in describeSources(fleet):
        print(f'source: {line}')
    for line in fleet.warnings:
        print(f'WARNING: {line}')
    if args.explain:
        for line in syncSiblingNotes(projectRoot, fleet):
            print(line)
        print('\n'.join(explain(fleet, args.explain)))
        return 0
    stale, failures = False, [] if args.write else fleet.failures()
    if args.write:
        written, notes, _ = _writeFleet(projectRoot, fleet)
        for line in [f'wrote server/{name}' for name in written] + notes or ['map files unchanged']:
            print(line)
    else:
        for line in syncSiblingNotes(projectRoot, fleet):
            print(line)
        server = serverDir(projectRoot)
        for name, text in fleet.outputs():
            if readLF(server / name) != text:
                stale = True
                print(f'STALE: server/{name} differs from a fresh generation')
        if stale:
            checked = loadCheckedInMap(projectRoot) or {'repos': {}, 'flows': []}
            for name in sorted(set(checked.get('repos', {})) | set(fleet.data['repos'])):
                diffs = differingAnchors(incidentView(checked, name), incidentView(fleet.data, name))
                if diffs:
                    print(f'  {name}: ' + ', '.join(diffs))
    for p in fleet.problems():  # FAIL: this repo's to fix; WARNING: another repo's
        label = 'FAIL' if p in failures else 'note' if p['kind'] in INFO_KINDS else 'WARNING'
        print(f"{label}: {p['message']}")
    issues = sum(_count(f, 'issue') for f in fleet.data['flows'])
    if issues:
        print(f'note: {issues} compat issue(s) in the fleet; each fails the tests of both repos of its flow')
    if failures:
        print(f'FAILED: {len(failures)} problem(s) for {fleet.ownName} (FAIL above); fix each as it '
              'says. ' + REGENERATE)
    elif stale and fleet.blockers:
        print('STALE: `--write` would refuse until the map has every sibling: ' + '; '.join(fleet.blockers))
    elif stale:
        print('STALE: run `cd server && python pubSubMap.py --write` and commit server/pubsub_map.json '
              'and server/PUBSUB_MAP.md.')
    return 1 if stale or failures else 0


if __name__ == '__main__':
    sys.exit(main())
