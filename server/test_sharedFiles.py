"""Tests for sharedFileCheck.py.

The first four tests hold this repo's shared files to the create-star-app
checkout next to it, at its committed HEAD. Without one (Cloud Build, a lone
clone) they skip and say why. The rest are unit tests of the check, run on a
fake template repository and a fake project under tmp_path: no database, no
app import and no real checkout needed.
"""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import pytest

import sharedFileCheck
from sharedFileCheck import (CONFIG_PATH, GIT_REDIRECT_VARS, MANIFEST_PATH,
                             SharedFileError, TemplateUnavailable, checkAll,
                             checkEntry, loadContext, main, markerProblems,
                             parseCatFile, parseLsTree, parseManifest,
                             readProjectName, readText, syncEntry)

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
# The tools every repo carries as exact copies: the Pub/Sub map, the
# generated Pub/Sub message models and this check.
SHARED_TOOLS = [
    'server/pubSubMap.py', 'server/pubSubMapContract.py',
    'server/pubSubMapScan.py', 'server/pubSubMapSection.py',
    'server/pubSubModels.py', 'server/sharedFileCheck.py',
    'server/test_pubSubMap.py', 'server/test_pubSubModels.py',
    'server/test_sharedFiles.py'
]


def _realContext():
    """(context, skip reason, error) for this repo: exactly one is set."""
    if not (SERVER_DIR / 'AppConfig.py').is_file():
        return None, ('not inside a Star App server/ folder (no AppConfig.py '
                      'next to this test); drift not checked'), None
    try:
        return loadContext(PROJECT_ROOT), None, None
    except TemplateUnavailable as reason:
        return None, str(reason), None
    except SharedFileError as error:
        return None, None, str(error)


REAL_CONTEXT, REAL_SKIP_REASON, REAL_ERROR = _realContext()
REAL_PATHS = ([entry.path for entry in REAL_CONTEXT.applicable()]
              if REAL_CONTEXT else ['drift-not-checked'])


def realContext():
    """The check's context for this repo. Skips when there is no usable
    template checkout; fails when the manifest or this repo is broken."""
    if REAL_ERROR is not None:
        pytest.fail(REAL_ERROR)
    if REAL_CONTEXT is None:
        pytest.skip(REAL_SKIP_REASON)
    return REAL_CONTEXT


@pytest.mark.parametrize('path', REAL_PATHS)
def test_sharedFileMatchesTemplate(path):
    context = realContext()
    result = checkEntry(context, context.entry(path))
    assert result.status == 'ok', f'{result.details}\n\n{context.fixHint()}'


def test_markersAreWellFormed():
    context = realContext()
    problems = []
    for entry in context.applicable():
        path = PROJECT_ROOT / entry.path
        if entry.hasMarkers and path.is_file():
            problems += [
                f'{entry.path} {problem}'
                for problem in markerProblems(readText(path), entry.mode)
            ]
    assert not problems, (
        'Broken star-app markers. Each marker pair must be balanced and '
        'uniquely named, and a per-repo block may only sit inside a shared '
        'region or in an exact-mode file:\n  ' + '\n  '.join(problems))


def test_manifestEntriesExistInTemplate():
    context = realContext()
    missing = [
        f'{entry.path} <- {entry.source}' for entry in context.entries
        if context.template.read(entry.source) is None
    ]
    assert not missing, (
        f'{MANIFEST_PATH} at {context.template.label} names template sources '
        'that are not committed there:\n  ' + '\n  '.join(missing) +
        '\nCommit them in create-star-app, or fix the manifest.')


def test_sharedToolsAreInTheManifest():
    context = realContext()
    everywhere = {
        entry.path
        for entry in context.entries
        if entry.mode == 'exact' and entry.rendered and not entry.exceptProjects
        and not entry.onlyProjects
    }
    missing = [path for path in SHARED_TOOLS if path not in everywhere]
    assert not missing, (
        f'{MANIFEST_PATH} at {context.template.label} must list these as exact '
        'templates every repo has: ' + ', '.join(missing))


# ---------------------------------------------------------------------------
# Unit tests: a fake template repository and a fake project under tmp_path
# ---------------------------------------------------------------------------

FAKE_PROJECT = 'fakeShop'
FAKE_PORT = 6123
FLEET = [{'dir': 'fakeA', 'projectName': FAKE_PROJECT},
         {'dir': 'fakeB', 'projectName': 'otherFake'}]
TOOL_SOURCE = 'create_star_app/templates/tool.py.tmpl'
TOOL_PATH = 'server/tool.py'
SUITE_SOURCE = 'create_star_app/templates/test_suite.py.tmpl'
SUITE_PATH = 'server/test_suite.py'


def text(*lines: str) -> str:
    """LF text with one argument per line and a final newline."""
    return '\n'.join(lines) + '\n'


def opening(kind: str, name: str) -> str:
    return f'# >>> star-app {kind}: {name}'


def closing(kind: str, name: str) -> str:
    return f'# <<< star-app {kind}: {name}'


CONFIG_TEXT = text('from dataclasses import dataclass', '', '', '@dataclass',
                   'class ProjectConfig:', '    projectName: str',
                   '    port: int = 5000', "    gcpRegion: str = 'asia-east1'")
APP_CONFIG_TEXT = text('class Project:', '', '    def __init__(self):',
                       f"        self.projectName = '{FAKE_PROJECT}'")
TOOL = text('"""A shared tool."""', 'LIMIT = 10', '', '', 'def limit():',
            '    return LIMIT')
KNOWN = text('KNOWN = [', opening('per-repo', 'known'), "    'templateDefault',",
             closing('per-repo', 'known'), ']', 'LIMIT = 10')


def suite(header='# this project only', body="    return 'shared'",
          known="    'templateDefault',", footer='# this project only, too'):
    """A test file whose middle is the shared region "helpers"."""
    return text(header, '', opening('shared', 'helpers'), 'def helper():', body,
                '', 'KNOWN = [', opening('per-repo', 'known'), known,
                closing('per-repo', 'known'), ']', closing('shared', 'helpers'),
                '', footer)


def exact(path=TOOL_PATH, source=TOOL_SOURCE, **extra) -> dict:
    return {'path': path, 'source': source, 'mode': 'exact', **extra}


def regions(path=SUITE_PATH, source=SUITE_SOURCE) -> dict:
    return {
        'path': path,
        'source': source,
        'mode': 'regions',
        'regions': ['helpers']
    }


def _git(directory: Path, *args: str):
    """git for the fixtures, with the same redirect variables stripped as the
    check strips, so an exported GIT_DIR cannot point it at another repo."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_REDIRECT_VARS}
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                   cwd=directory, check=True, capture_output=True, env=env)


def writeFiles(root: Path, files: dict):
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8'))


def commitAll(root: Path):
    _git(root, 'add', '-A')
    _git(root, 'commit', '-q', '-m', 'template')


# Each distinct template checkout is written (and committed) once per module,
# then copied into every test's folder: no test shares one, and none pays the
# git spawns (20 ms or more each on Windows) of building it again.
_TEMPLATES = {}


@pytest.fixture(scope='module', autouse=True)
def _templateCheckouts(tmp_path_factory):
    _TEMPLATES.clear()
    _TEMPLATES[None] = tmp_path_factory.mktemp('templateCheckouts')
    yield
    _TEMPLATES.clear()


def makeTemplate(tmp_path: Path, sources: dict, files: list, commit=True,
                 repos=FLEET) -> Path:
    """A create-star-app git checkout holding config.py, the manifest
    (listing `repos` and `files`) and the given sources, all committed unless
    commit=False: a copy of this module's first one with the same contents
    (written straight in place when that folder exists already)."""
    root = tmp_path / 'create-star-app'
    manifest = json.dumps({'repos': repos, 'files': files}, indent=2)
    contents = {CONFIG_PATH: CONFIG_TEXT, MANIFEST_PATH: manifest, **sources}
    folder = _TEMPLATES.get(None)
    if folder is None or root.exists():
        return _writeTemplate(root, contents, commit)
    key = (json.dumps(contents, sort_keys=True), commit)
    if key not in _TEMPLATES:
        built = folder / str(len(_TEMPLATES)) / 'create-star-app'
        _TEMPLATES[key] = _writeTemplate(built, contents, commit)
    shutil.copytree(_TEMPLATES[key], root, symlinks=True)
    return root


def _writeTemplate(root: Path, contents: dict, commit: bool) -> Path:
    writeFiles(root, contents)
    _git(root, 'init', '-q')
    if commit:
        commitAll(root)
    return root


def makeProject(tmp_path: Path, files: dict, port=FAKE_PORT) -> Path:
    """A project next to the template, with AppConfig.py, AppCreator.py and
    the given files."""
    root = tmp_path / 'fakeRepo'
    portLine = f'PORT: int = {port}' if port is not None else 'HOST = None'
    writeFiles(root, {
        'server/AppConfig.py': APP_CONFIG_TEXT,
        'server/AppCreator.py': text(portLine),
        **files
    })
    return root


def check(projectRoot: Path, **options) -> dict:
    """Each checked repo path -> its EntryResult."""
    _, results = checkAll(projectRoot, **options)
    return {result.entry.path: result for result in results}


def test_exactCopyMatches(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    results = check(makeProject(tmp_path, {TOOL_PATH: TOOL}))
    assert {path: result.status for path, result in results.items()} == {TOOL_PATH: 'ok'}


def test_crlfOnlyDifferenceMatches(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: TOOL.replace('\n', '\r\n')})
    assert check(project)[TOOL_PATH].status == 'ok'


def test_oneCharacterChangeFailsAndShowsTheLine(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: TOOL.replace('LIMIT = 10', 'LIMIT = 11')})
    context, results = checkAll(project)
    result = results[0]
    assert result.status == 'drift'
    assert re.search(r'^-\s+2\s+LIMIT = 10$', result.details, re.MULTILINE)
    assert re.search(r'^\+\s+2\s+LIMIT = 11$', result.details, re.MULTILINE)
    assert TOOL_SOURCE in result.details
    assert context.template.label in result.details  # <branch>@<short sha>


def test_missingRepoCopyFails(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    result = check(makeProject(tmp_path, {}))[TOOL_PATH]
    assert result.status == 'drift'
    assert 'missing in this repo' in result.details


def test_diffIsCappedAt200Lines(tmp_path):
    long = text(*[f'VALUE_{index} = {index}' for index in range(300)])
    makeTemplate(tmp_path, {TOOL_SOURCE: long}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: long.replace(' = ', ' == ')})
    details = check(project)[TOOL_PATH].details
    diff = details.split('(columns: template line, repo line)\n', 1)[1].splitlines()
    assert len(diff) == sharedFileCheck.MAX_DIFF_LINES + 1
    assert diff[-1].endswith('more diff lines not shown')


def test_perRepoBlockContentsAreMasked(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: KNOWN}, [exact()])
    ours = KNOWN.replace("    'templateDefault',", "    'ours',\n    'alsoOurs',")
    assert check(makeProject(tmp_path, {TOOL_PATH: ours}))[TOOL_PATH].status == 'ok'


def test_changeOutsidePerRepoBlockStillFails(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: KNOWN}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: KNOWN.replace('LIMIT = 10', 'LIMIT = 9')})
    assert check(project)[TOOL_PATH].status == 'drift'


def test_removedMarkerFails(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: KNOWN}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: KNOWN.replace(closing('per-repo', 'known') + '\n', '')})
    result = check(project)[TOOL_PATH]
    assert result.status == 'drift'
    assert 'per-repo block "known" is never closed' in result.details


def test_regionChangeFails(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    project = makeProject(tmp_path, {SUITE_PATH: suite(body="    return 'changed'")})
    result = check(project)[SUITE_PATH]
    assert result.status == 'drift'
    assert 'region "helpers"' in result.details
    assert re.search(r"^\+\s+5\s+return 'changed'$", result.details, re.MULTILINE)


def test_changesOutsideRegionsAndInPerRepoBlocksPass(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    ours = suite(header='# our header\nimport os', known="    'ours',", footer='# our footer')
    assert check(makeProject(tmp_path, {SUITE_PATH: ours}))[SUITE_PATH].status == 'ok'


def test_missingRegionFails(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    noRegion = text('# this project only', 'def helper():', "    return 'shared'")
    result = check(makeProject(tmp_path, {SUITE_PATH: noRegion}))[SUITE_PATH]
    assert result.status == 'drift'
    assert 'region "helpers": its markers are missing' in result.details


def test_brokenTemplateMarkersAreAnError(tmp_path):
    broken = KNOWN.replace(closing('per-repo', 'known'), closing('per-repo', 'other'))
    makeTemplate(tmp_path, {TOOL_SOURCE: broken}, [exact()])
    result = check(makeProject(tmp_path, {TOOL_PATH: KNOWN}))[TOOL_PATH]
    assert result.status == 'error'
    assert 'broken markers' in result.details


def test_renderingMatchesScaffold(tmp_path):
    source = text("PORT = '$port'", "REGION = '$gcpRegion'", "NAME = '${projectName}'",
                  "PRICE = '$$5'")
    rendered = text(f"PORT = '{FAKE_PORT}'", "REGION = 'asia-east1'",
                    f"NAME = '{FAKE_PROJECT}'", "PRICE = '$5'")
    makeTemplate(tmp_path, {TOOL_SOURCE: source}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: rendered})
    context, results = checkAll(project)
    assert context.expected(context.entry(TOOL_PATH)) == rendered
    assert results[0].status == 'ok'


def test_fleetSourceIsTakenVerbatim(tmp_path):
    verbatim = text("PRICE = '$port'")
    makeTemplate(tmp_path, {'fleet/tool.py': verbatim}, [exact(source='fleet/tool.py')])
    assert check(makeProject(tmp_path, {TOOL_PATH: verbatim}))[TOOL_PATH].status == 'ok'


def test_unknownTemplateVariableIsAnError(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: text("X = '$notAVariable'")}, [exact()])
    result = check(makeProject(tmp_path, {TOOL_PATH: text("X = ''")}))[TOOL_PATH]
    assert result.status == 'error'
    assert 'notAVariable' in result.details


def test_portWithoutAPortLineIsAnError(tmp_path):
    """Never ProjectConfig's default port: the repo's own, or an error."""
    makeTemplate(tmp_path, {TOOL_SOURCE: text("PORT = '$port'")}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: text("PORT = '5000'")}, port=None)
    result = check(project)[TOOL_PATH]
    assert result.status == 'error'
    assert 'port' in result.details


def test_exceptProjectsFilter(tmp_path):
    otherSource = 'create_star_app/templates/other.py.tmpl'
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL, otherSource: TOOL}, [
        exact(exceptProjects=[FAKE_PROJECT]),
        exact('server/other.py', otherSource, exceptProjects=['someOtherProject'])
    ])
    results = check(makeProject(tmp_path, {'server/other.py': TOOL}))
    assert {path: result.status for path, result in results.items()} == {'server/other.py': 'ok'}


def test_onlyProjectsFilter(tmp_path):
    otherSource = 'create_star_app/templates/other.py.tmpl'
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL, otherSource: TOOL}, [
        exact(onlyProjects=['someOtherProject']),
        exact('server/other.py', otherSource, onlyProjects=[FAKE_PROJECT])
    ])
    results = check(makeProject(tmp_path, {'server/other.py': TOOL}))
    assert {path: result.status for path, result in results.items()} == {'server/other.py': 'ok'}


def test_exceptAndOnlyProjectsTogetherIsAnError():
    entry = {**exact(), 'exceptProjects': ['a'], 'onlyProjects': ['b']}
    with pytest.raises(SharedFileError, match='not both'):
        parseManifest(json.dumps({'files': [entry]}), 'test')


def test_writeRestoresAndKeepsPerRepoBlocksAndCrlf(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: KNOWN}, [exact()])
    drifted = KNOWN.replace("    'templateDefault',", "    'ours',").replace('LIMIT = 10', 'LIMIT = 99')
    project = makeProject(tmp_path, {TOOL_PATH: drifted.replace('\n', '\r\n')})
    context = loadContext(project)
    entry = context.entry(TOOL_PATH)
    assert syncEntry(context, entry).startswith(f'updated {TOOL_PATH}')
    written = (project / TOOL_PATH).read_bytes()
    assert written == KNOWN.replace("    'templateDefault',", "    'ours',").replace('\n', '\r\n').encode()
    assert checkEntry(context, entry).status == 'ok'
    assert syncEntry(context, entry) is None


def test_writeReplacesOnlyTheSharedRegions(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    project = makeProject(tmp_path, {
        SUITE_PATH: suite(header='# our header', body='    return None', known="    'ours',")
    })
    context = loadContext(project)
    assert syncEntry(context, context.entry(SUITE_PATH)) is not None
    assert (project / SUITE_PATH).read_bytes() == suite(header='# our header',
                                                        known="    'ours',").encode()


def test_writeCreatesAMissingFile(tmp_path):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    project = makeProject(tmp_path, {})
    context = loadContext(project)
    assert syncEntry(context, context.entry(TOOL_PATH)) == f'created {TOOL_PATH}'
    assert (project / TOOL_PATH).read_bytes() == TOOL.encode()


def test_writeRefusesARegionWithoutMarkers(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    project = makeProject(tmp_path, {SUITE_PATH: text('# no markers here')})
    context = loadContext(project)
    with pytest.raises(SharedFileError, match='have no markers'):
        syncEntry(context, context.entry(SUITE_PATH))


def test_uncommittedTemplateChangeIsIgnoredUnlessWorktree(tmp_path):
    template = makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    writeFiles(template, {TOOL_SOURCE: TOOL.replace('LIMIT = 10', 'LIMIT = 11')})
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    assert check(project)[TOOL_PATH].status == 'ok'
    assert check(project, worktree=True)[TOOL_PATH].status == 'drift'
    commitAll(template)
    assert check(project)[TOOL_PATH].status == 'drift'


def test_missingTemplateSkipsWithAReason(tmp_path, capsys):
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    with pytest.raises(TemplateUnavailable, match='not found at .*drift not checked'):
        loadContext(project)
    assert main([], projectRoot=project) == 0
    assert 'WARNING: create-star-app not found at' in capsys.readouterr().out


def test_templateThatIsNotAGitCheckoutSkips(tmp_path):
    writeFiles(tmp_path / 'create-star-app', {MANIFEST_PATH: json.dumps({'files': []})})
    project = makeProject(tmp_path, {})
    with pytest.raises(TemplateUnavailable, match='not a git checkout'):
        loadContext(project)


def test_manifestOnlyInTheWorkingTreeSkipsUnlessWorktree(tmp_path):
    template = makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()], commit=False)
    _git(template, 'add', CONFIG_PATH)
    _git(template, 'commit', '-q', '-m', 'config only')
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    with pytest.raises(TemplateUnavailable, match=f'no {MANIFEST_PATH} at HEAD'):
        loadContext(project)
    assert check(project, worktree=True)[TOOL_PATH].status == 'ok'


def test_cliExitCodes(tmp_path, capsys):
    template = makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    assert main([], projectRoot=project) == 0
    writeFiles(project, {TOOL_PATH: TOOL.replace('10', '12')})
    capsys.readouterr()
    assert main([], projectRoot=project) == 1
    out = capsys.readouterr().out
    assert 'LIMIT = 12' in out
    assert 'python sharedFileCheck.py --write` in fakeA and fakeB' in out
    assert main(['--write'], projectRoot=project) == 0
    assert main([], projectRoot=project) == 0
    writeFiles(template, {TOOL_SOURCE: text("X = '$notAVariable'")})
    commitAll(template)
    assert main([], projectRoot=project) == 2


@pytest.mark.parametrize('lines, mode, expected', [
    ([opening('per-repo', 'a'), closing('per-repo', 'a')], 'exact', []),
    ([opening('per-repo', 'a')], 'exact', ['never closed']),
    ([closing('per-repo', 'a')], 'exact', ['but nothing is open']),
    ([opening('per-repo', 'a'), closing('per-repo', 'a'),
      opening('per-repo', 'a'), closing('per-repo', 'a')], 'exact', ['a second per-repo block']),
    ([opening('shared', 'a'), opening('shared', 'b'), closing('shared', 'b'),
      closing('shared', 'a')], 'regions', ['opens inside shared block']),
    ([opening('per-repo', 'a'), closing('per-repo', 'a')], 'regions', ['outside every shared region']),
    ([opening('shared', 'a'), opening('per-repo', 'b'), closing('per-repo', 'b'),
      closing('shared', 'a')], 'regions', []),
    (['    ' + opening('per-repo', 'indented'), '    ' + closing('per-repo', 'indented')], 'exact', []),
    (['# >>> star-app shard: typo'], 'exact', ['malformed marker']),
])
def test_markerProblems(lines, mode, expected):
    problems = markerProblems(text(*lines), mode)
    assert len(problems) == len(expected)
    for problem, fragment in zip(problems, expected):
        assert fragment in problem


@pytest.mark.parametrize('files, error', [
    ([exact(path='server/../outside.py')], 'relative POSIX path'),
    ([exact(path='server\\tool.py')], 'relative POSIX path'),
    ([exact(path='.git/config')], 'relative POSIX path'),
    ([exact(path='.git./hooks/post-checkout')], 'relative POSIX path'),
    ([exact(path='.GIT/config')], 'relative POSIX path'),
    ([exact(path='server/a:b.py')], 'relative POSIX path'),
    ([exact(path='con')], 'relative POSIX path'),
    ([exact(path='server/nul.txt')], 'relative POSIX path'),
    ([exact(path='server/tools./x.py')], 'relative POSIX path'),
    ([exact(source='README.md')], 'must be a create_star_app/templates/'),
    ([exact(source='create_star_app/templates/tool.py')], 'must be a create_star_app/templates/'),
    ([exact(source='create_star_app/templates/../x.tmpl')], 'relative POSIX path'),
    ([exact(exceptProject=['x'])], 'unknown key'),
    ([exact(mode='mirror')], '"mode" must be one of'),
    ([exact(regions=['helpers'])], 'only applies to regions mode'),
    ([dict(regions(), regions=[])], 'distinct region names'),
    ([dict(regions(), path='server/tools/job.bat')], 'cannot use regions mode'),
    ([exact(), exact()], 'listed twice'),
])
def test_manifestIsValidated(files, error):
    with pytest.raises(SharedFileError, match=re.escape(error)):
        parseManifest(json.dumps({'files': files}), 'test')


@pytest.mark.parametrize('repo', [
    {'dir': 'fakeA'},
    {'dir': '../fakeA', 'projectName': 'x'},
    {'dir': '//host/share', 'projectName': 'x'},
    {'dir': 'C:/fakeA', 'projectName': 'x'},
    {'dir': 'fakeA.', 'projectName': 'x'},
    {'dir': 'fakeA', 'projectName': '../x'},
    {'dir': 'fakeA', 'projectName': 'x', 'extra': 1},
])
def test_manifestReposAreValidated(repo):
    with pytest.raises(SharedFileError, match=re.escape('repos[0]')):
        parseManifest(json.dumps({'repos': [repo], 'files': []}), 'test')


def test_manifestMustBeJson():
    with pytest.raises(SharedFileError, match='not valid JSON'):
        parseManifest('{"files": [', 'test')


def test_fleetFilesApplyOnlyToListedProjects(tmp_path):
    """A fresh scaffold beside create-star-app is never asked for a fleet/
    file; a project the manifest's repos list is."""
    sources = {TOOL_SOURCE: TOOL, 'fleet/fleetOnly.py': TOOL}
    files = [exact(), exact('server/fleetOnly.py', 'fleet/fleetOnly.py')]
    fresh = tmp_path / 'fresh'
    makeTemplate(fresh, sources, files, repos=[{'dir': 'fakeB', 'projectName': 'otherFake'}])
    results = check(makeProject(fresh, {TOOL_PATH: TOOL}))
    assert {path: result.status for path, result in results.items()} == {TOOL_PATH: 'ok'}
    listed = tmp_path / 'listed'
    makeTemplate(listed, sources, files)
    results = check(makeProject(listed, {TOOL_PATH: TOOL}))
    assert {path: result.status for path, result in results.items()} == {
        TOOL_PATH: 'ok', 'server/fleetOnly.py': 'drift'}


def test_redirectingGitVariablesAreIgnored(tmp_path, monkeypatch):
    makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    decoy = tmp_path / 'decoy'
    writeFiles(decoy, {'README.md': text('not the template')})
    _git(decoy, 'init', '-q')
    commitAll(decoy)
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    monkeypatch.setenv('GIT_DIR', str(decoy / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(decoy))
    monkeypatch.setenv('GIT_INDEX_FILE', str(decoy / '.git' / 'index'))
    assert check(project)[TOOL_PATH].status == 'ok'


def test_templateInsideAnotherRepoIsNotACheckout(tmp_path):
    """create-star-app must be a checkout itself, not a plain folder that
    happens to sit inside some other repository."""
    outer = tmp_path / 'outer'
    makeTemplate(outer, {TOOL_SOURCE: TOOL}, [exact()], commit=False)
    (outer / 'create-star-app' / '.git').rename(outer / '.git')
    commitAll(outer)
    project = makeProject(outer, {TOOL_PATH: TOOL})
    with pytest.raises(TemplateUnavailable, match='not a git checkout'):
        loadContext(project)


def test_lsTreeParseKeepsOddNamesAndSkipsLinks():
    regular, script, link, submodule = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40
    listing = b''.join([
        f'100644 blob {regular}\tserver/odd\nname.py\0'.encode(),
        f'100755 blob {script}\tserver/run.sh\0'.encode(),
        f'120000 blob {link}\tserver/link.py\0'.encode(),
        f'160000 commit {submodule}\tvendor\0'.encode(),
    ])
    assert parseLsTree(listing, 'test') == {'server/odd\nname.py': regular,
                                            'server/run.sh': script}


def test_catFileHeadersAreVerified():
    first, second = 'a' * 40, 'b' * 40
    good = f'{first} blob 3\nabc\n{second} blob 0\n\n'.encode()
    assert parseCatFile(good, [first, second], 'test') == {first: b'abc', second: b''}
    for bad in (f'{second} blob 3\nabc\n', f'{first} tree 3\nabc\n',
                f'{first} blob 9\nabc\n', f'{first} missing\n'):
        with pytest.raises(SharedFileError):
            parseCatFile(bad.encode(), [first], 'test')


@pytest.mark.parametrize('lines, expected', [
    pytest.param(['class Project:', '    def __init__(self):',
                  "        self.projectName = 'shop'"], 'shop', id='fleetShape'),
    pytest.param(['class Project:', '    def __init__(self):', '        if True:',
                  "            self.projectName = 'shop'", '    def forTests(self):',
                  "        self.projectName = 'shopTest'"], 'shop', id='firstInSourceOrder'),
    pytest.param(['try:', '    class Project:', "        projectName = 'shop'",
                  'finally:', '    pass'], 'shop', id='classInsideTry'),
    pytest.param(['class Project:', "    projectName = 'classLevel'", '    def __init__(self):',
                  "        self.projectName = 'method'"], 'classLevel', id='classLevelFirst'),
    pytest.param(['def make():', '    class Project:', "        projectName = 'hidden'"],
                 None, id='notInsideADef'),
])
def test_readProjectName(lines, expected):
    assert readProjectName(text(*lines)) == expected


def test_writeNeverTargetsTheGitFolder(tmp_path):
    project = makeProject(tmp_path, {})
    (project / '.git').mkdir()
    for relative in ('.git./hooks/post-checkout', '.GIT/config', 'server/../.git/x',
                     '../outside.py'):
        with pytest.raises(SharedFileError):
            sharedFileCheck._repoPath(project, relative)
    assert sharedFileCheck._repoPath(project, TOOL_PATH) == project / TOOL_PATH


def test_cliTemplateWorktreeAndTemplateFlags(tmp_path, capsys):
    template = makeTemplate(tmp_path, {TOOL_SOURCE: TOOL}, [exact()])
    writeFiles(template, {TOOL_SOURCE: TOOL.replace('LIMIT = 10', 'LIMIT = 11')})
    project = makeProject(tmp_path, {TOOL_PATH: TOOL})
    assert main([], projectRoot=project) == 0
    capsys.readouterr()
    assert main(['--template-worktree'], projectRoot=project) == 1
    assert '+ working tree' in capsys.readouterr().out
    moved = tmp_path / 'elsewhere'
    template.rename(moved)
    assert main([], projectRoot=project) == 0
    assert 'WARNING: create-star-app not found' in capsys.readouterr().out
    assert main(['--template', str(moved)], projectRoot=project) == 0
    assert '1 shared files match create-star-app' in capsys.readouterr().out


@pytest.mark.parametrize('source, drifted', [
    pytest.param(TOOL, TOOL.replace(text('LIMIT = 10'), text(opening('per-repo', 'mine'),
                                                             'LIMIT = 11',
                                                             closing('per-repo', 'mine'))),
                 id='repoOnlyBlock'),
    pytest.param(KNOWN, KNOWN.replace(': known', ': mine'), id='renamedBlock'),
    pytest.param(KNOWN, text('KNOWN = [', opening('per-repo', 'known'), "    'templateDefault',",
                             ']', 'LIMIT = 9', closing('per-repo', 'known')), id='stretchedBlock'),
])
def test_perRepoBlocksCannotHideAChange(tmp_path, source, drifted):
    makeTemplate(tmp_path, {TOOL_SOURCE: source}, [exact()])
    assert check(makeProject(tmp_path, {TOOL_PATH: drifted}))[TOOL_PATH].status == 'drift'


def test_repoOnlyBlockInARegionCannotHideAChange(tmp_path):
    makeTemplate(tmp_path, {SUITE_SOURCE: suite()}, [regions()])
    hidden = text(opening('per-repo', 'mine'), "    return 'changed'",
                  closing('per-repo', 'mine')).rstrip('\n')
    project = makeProject(tmp_path, {SUITE_PATH: suite(body=hidden)})
    assert check(project)[SUITE_PATH].status == 'drift'


# Twin of BACKSLASH and test_pep701CheckFindsOnlyNewSyntax in test_pubSubMap.py
# (copied verbatim, cases included); keep in step.
BACKSLASH = chr(92)


def pep701Problems(source: str) -> list[str]:
    """f-string syntax only Python 3.12+ parses (PEP 701), which
    ast.parse(feature_version=(3, 10)) does not reject on a 3.12 interpreter:
    a replacement field holding the outer quote, a backslash or a comment.
    [] before 3.12, where such source does not even tokenize."""
    # Twin of pep701Problems in test_pubSubMap.py; keep in step.
    if not hasattr(tokenize, 'FSTRING_START'):
        return []
    problems: list[str] = []
    frames: list[list] = []  # [quote, brace depth] per open f-string
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.FSTRING_END:
            enclosing = []  # the same quote as its start, checked there
        elif token.type == tokenize.FSTRING_MIDDLE:
            enclosing = frames[:-1]  # this f-string's own text
        else:
            enclosing = frames if frames and frames[-1][1] > 0 else []
        line = token.start[0]
        if any(quote in token.string for quote, _ in enclosing):
            problems.append(f'line {line}: the outer quote inside a replacement field')
        if enclosing and BACKSLASH in token.string:
            problems.append(f'line {line}: a backslash inside a replacement field')
        if enclosing and token.type == tokenize.COMMENT:
            problems.append(f'line {line}: a comment inside a replacement field')
        if token.type == tokenize.FSTRING_START:
            frames.append([token.string.lstrip('rRbBfFuU'), 0])
        elif token.type == tokenize.FSTRING_END:
            frames.pop()
        elif frames and token.type == tokenize.OP and token.string in ('{', '}'):
            frames[-1][1] += 1 if token.string == '{' else -1
    return problems


@pytest.mark.skipif(not hasattr(tokenize, 'FSTRING_START'),
                    reason='PEP 701 syntax only tokenizes on Python 3.12+')
@pytest.mark.parametrize('source, problems', [
    ("x = f'{'a'}'\n", 1),
    ('x = f"{f"{1}"}"\n', 1),
    ("x = f'{\"" + BACKSLASH + "n\"}'\n", 1),
    ("x = f'''{1 # note\n}'''\n", 1),
    ("x = f'''{'a'}'''\n", 0),
    ("x = f\"{f'{1}'}\"\n", 0),
    ("x = f'{\"a\"}' + '" + BACKSLASH + "n'\n", 0),
])
def test_pep701CheckFindsOnlyNewSyntax(source, problems):
    assert len(pep701Problems(source)) == problems


def test_toolSourcesParseOnPython310():
    for path in (Path(sharedFileCheck.__file__), Path(__file__)):
        ast.parse(path.read_bytes(), filename=str(path), feature_version=(3, 10))
        assert pep701Problems(path.read_text(encoding='utf-8')) == [], path
