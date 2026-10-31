"""Keeps every route response down to the fields the client reads.

For every route in RouteResponseModels, client/scripts/unusedResponseFields.ts
(ts-morph) lists the property paths of its response type that no hand-written
client code reads by name, and the dynamic-access sites (Object.keys/entries/
values, JSON.stringify, a rest element, any flow into a loose type) that hide
which fields a response-typed value needs. Its docstring defines the rules;
this test passes it the generated paths (AppCreator's, as test_file_sizes.py
derives them), applies the exclusions and renders the brief.

While a route sends an unused field, a violation exists or the analyzer could
not judge something (a node it could not type, a route type with no schema
fields), this test fails with
a brief: the unused paths per route (most first), how to trim the top route,
and the violations with file:line. There is no baseline and no allowlist: it
stays red until the responses are trimmed.

Two rules exclude a route, because code outside client/ reads its response:
- it publishes to Pub/Sub (a `fn` in server/pubsub_models.json): its response
  IS the message payload its consumers read;
- it is defined in server/actions/ServiceToServiceActions.py, the routes
  another service calls over HTTP.
The brief lists each excluded route with its reason. An excluded route's
analyzer problems still fail the test.

The second rule is enforced by scanning the siblings for HTTP calls:
`<client>.request('<route>', ...)`, `request(<client>.path, '<route>', ...)`
or getServiceByName('<Cls>').request, where <client> is a sibling's
services/*.py object for this repo's projectName. A route a sibling calls
that is not in ServiceToServiceActions fails ("move it there"), and so does a
route there that no sibling calls (stale). The siblings are read as
pubSubMap.py reads them (the same committed ref, its reasons when one is
missing). A sibling that cannot be read fails the test. Only while there is
no fleet yet (the checked-in server/pubsub_map.json lists no repo but this
one) is a sibling that was never checked out and is not in that map skipped:
it is no sibling yet (a fresh scaffold's example service), and the brief
names it. Once the map lists another repo, a missing sibling fails ("clone it
or pass --repo"), mapped or not.

Needs node and client/node_modules (`cd client && npm install`); it fails, never
skips, without them.

The analyzer takes minutes, so test_everyResponseFieldIsReadByTheClient skips
for RERUN_AFTER (7 days) after it passed. A pass writes STAMP_PATH
(server/.testStamps/unusedResponseFields.json, gitignored, one per checkout or
worktree); a skip never does, and a run deletes it before the analyzer starts,
so a failed or interrupted run leaves none. A missing, unreadable or malformed
stamp, or one dated in the future (a wrong clock), runs it, and so does
RUN_UNUSED_RESPONSE_FIELDS=1 (or true, yes, on). Only that test skips: the
fast tests below always run.
"""
import ast
import datetime
import functools
import json
import os
import textwrap
from pathlib import Path

import pytest

import AppCreator
from AppConfig import Timezone
import pubSubModels
import test_file_sizes
from pubSubMap import freshSources, releaseTrees, serverDir
from pubSubMapScan import MapError, Repo, kwarg
from clientNode import capped, client_project_dir, install_hint, require_npx, run

REPO_ROOT = AppCreator.PROJECT_ROOT.resolve()
CLIENT_DIR = client_project_dir()
ANALYZER = 'scripts/unusedResponseFields.ts'
ANALYZER_TIMEOUT_SECONDS = 900
FIXTURE_TIMEOUT_SECONDS = 120
# Client tooling whose reads do not count, besides the generated files.
CLIENT_TOOLING = ('scripts/', 'e2e/', 'playwright.config.ts')
# How many unused paths the brief lists per route before "... and N more".
BRIEF_PATHS_PER_ROUTE = 25
# test_everyResponseFieldIsReadByTheClient skips for RERUN_AFTER after a pass
# (see the module docstring), unless FORCE_ENV_VAR is truthy.
STAMP_PATH = Path(__file__).resolve().parent / '.testStamps' / 'unusedResponseFields.json'
RERUN_AFTER = datetime.timedelta(days=7)
FORCE_ENV_VAR = 'RUN_UNUSED_RESPONSE_FIELDS'

SERVICE_TO_SERVICE = 'actions/ServiceToServiceActions.py'
SERVICE_TO_SERVICE_CLASS = 'ServiceToServiceActions'
SERVICE_TO_SERVICE_REASON = f'called by another service ({SERVICE_TO_SERVICE_CLASS})'
SERVICE_TO_SERVICE_BRIEF = f"""\
Before trimming a route below: is it called by ANOTHER SERVICE (a sibling repo,
a job or a script calling this route over HTTP) rather than by this repo's
client? The client is not its only reader then, so 'unused by the client' does
not mean unused. Do NOT remove its fields. Move the route, unchanged, into
server/{SERVICE_TO_SERVICE}. The gate skips every route defined there and
checks that each one really is called by another service. Only trim the routes
the client alone reads."""
SERVICE_TO_SERVICE_HOWTO = ('- Called by another service over HTTP? Do not trim it: move it, unchanged, '
                            f'into server/{SERVICE_TO_SERVICE}.')

TRIM_HOWTO = """\
How to trim {route} (response model {model}):
- Add a narrower client-facing response model in server/objects/ holding only
  the fields the client reads (the used paths above: everything not listed),
  and point the route's `responseModel` at it. Never shrink a stored *View
  model: DB documents and other routes rely on it.
- Read only those fields: a Mongo projection in the route (or its helper), so
  the DB reads less too, then build the narrower model from the result.
- Regenerate: `cd server && ENVIRONMENT=localdev python AppCreator.py`. Never
  edit the generated client/app/schemas/*.ts or client/app/api/*.ts.
- Prove it: `cd client && npx tsc --noEmit && npm run build` (no client code
  read a removed field), the route's tests (the localdev responseModel check
  catches a projection that drops a required field), then rerun this test.
- A field that only looks unused because the client reads it dynamically is
  a violation below: read it by name first.
""" + SERVICE_TO_SERVICE_HOWTO


# ---- Running the analyzer ---------------------------------------------------


def _client_relative(repoRelative: str) -> str | None:
    """A repo-relative path relative to the analyzer's root (CLIENT_DIR, which
    is client/salesapp/ where the client sits one level down), keeping a
    folder's trailing slash; None when it lies outside that root."""
    try:
        rel = (AppCreator.PROJECT_ROOT / repoRelative).resolve().relative_to(CLIENT_DIR).as_posix()
    except ValueError:
        return None
    return rel + '/' if repoRelative.endswith('/') else rel


def generated_client_paths() -> list[str]:
    """The generated files and folders under the analyzer root (as
    test_file_sizes.py derives them from AppCreator), relative to it."""
    generated = [_client_relative(p) for p in
                 (*test_file_sizes.GENERATED_DIRS, *sorted(test_file_sizes.GENERATED_FILES))]
    return [p for p in generated if p]


def analyzer_args() -> list[str]:
    """The real client's paths: the generated files and the client's
    tooling, each skipped where this project has it."""
    routeModels = (AppCreator.CLIENT_API_DIR / 'ServerRequests.ts').resolve().relative_to(CLIENT_DIR)
    schemas = AppCreator.CLIENT_SCHEMAS_DIR.resolve().relative_to(CLIENT_DIR)
    args = ['--schemas', schemas.as_posix(), '--route-models', routeModels.as_posix()]
    for skip in [*generated_client_paths(), *CLIENT_TOOLING]:
        if (CLIENT_DIR / skip).exists():
            args += ['--skip', skip]
    return args


def run_analyzer(args: list[str], timeout: int = ANALYZER_TIMEOUT_SECONDS) -> dict:
    """Runs the analyzer and returns its JSON result."""
    npx = require_npx(CLIENT_DIR, ('ts-morph', 'tsx'))
    hint = install_hint(CLIENT_DIR)
    # From the client, so npx resolves the pinned tsx; --no never downloads one.
    proc = run([npx, '--no', '--', 'tsx', ANALYZER, *args], CLIENT_DIR, timeout,
               f'if it hangs at start-up, {hint}')
    if proc.returncode != 0:
        pytest.fail(f'{ANALYZER} exited {proc.returncode} ({hint} if a module is '
                    f'missing):\n{proc.stderr[-3000:]}')
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f'{ANALYZER} printed no JSON ({error}).\nstdout starts:\n{proc.stdout[:1500]}\n'
                    f'stderr ends:\n{proc.stderr[-1500:]}')


# ---- Exclusions -------------------------------------------------------------


@functools.cache
def fleet() -> tuple:
    """(own projectName, {projectName: RepoSource}, warnings, the projectNames
    the checked-in map lists), read as pubSubMap.py reads them: the one
    discovery the map's tests share (pubSubMap.freshSources)."""
    ownName, sources, checkedIn, _, warnings = freshSources(REPO_ROOT)
    return ownName, sources, warnings, frozenset((checkedIn or {}).get('repos', {}))


@pytest.fixture(scope='module', autouse=True)
def _releaseParsedSiblings():
    """http_caller_routes parses every sibling's sources again: free those
    trees when this module is done."""
    yield
    if fleet.cache_info().currsize:
        releaseTrees(fleet()[1])


def publisher_routes() -> dict[str, str]:
    """fn -> reason, for every topic this repo publishes: its response is the
    message payload."""
    ownName = fleet()[0]
    manifest = pubSubModels.parseManifest(
        (serverDir(REPO_ROOT) / pubSubModels.MANIFEST).read_text(encoding='utf-8'), ownName,
        f'server/{pubSubModels.MANIFEST}')
    reasons: dict[str, list[str]] = {}
    for name, topic in manifest['topics'].items():
        reasons.setdefault(topic['fn'], []).append(
            f"publishes {name} (consumers: {', '.join(topic['consumers'])})")
    # A fn published to two topics keeps both reasons.
    return {fn: '; '.join(each) + '; its response is the message payload'
            for fn, each in reasons.items()}


def _names_client(node: ast.expr, variables: set[str], classes: set[str]) -> bool:
    """Whether `node` is the client: `x`, `obj.x` or getServiceByName('Cls')
    (matched as pubSubMapSection._adminProblems matches class names)."""
    if isinstance(node, ast.Name):
        return node.id in variables
    if isinstance(node, ast.Attribute):
        return node.attr in variables
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'getServiceByName' and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).lower() in classes)


def route_arg(call: ast.Call, variables: set[str], classes: set[str]) -> ast.expr | None:
    """The route-name argument when `call` sends an HTTP request to the client."""
    func = call.func
    # client.request('route', ...)
    if (isinstance(func, ast.Attribute) and func.attr == 'request'
            and _names_client(func.value, variables, classes)):
        return call.args[0] if call.args else kwarg(call, 'functionName')
    # request(client.path, 'route', ...), the services' shared helper
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if (name == 'request' and len(call.args) >= 2 and isinstance(call.args[0], ast.Attribute)
            and call.args[0].attr == 'path' and _names_client(call.args[0].value, variables, classes)):
        return call.args[1]
    return None


def client_calls(module, clientVars: set[str], classes: set[str]) -> list[tuple[int, ast.expr]]:
    """(line, route argument) for each HTTP request `module` sends to the
    client: through its services variable, an aliased import of it
    (`from services.x import x as eco`), getServiceByName('Cls'), or a loop
    over a list holding it or over serviceGetter.listOfServices."""
    names = set(clientVars) | {
        local for local, binding in module.names.items()
        if binding[0] == 'from' and binding[1].startswith('services') and binding[2] in clientVars}
    loopNames: dict[int, set[str]] = {}
    for loop in ast.walk(module.tree):
        if not (isinstance(loop, (ast.For, ast.AsyncFor)) and isinstance(loop.target, ast.Name)):
            continue
        over = loop.iter
        if ((isinstance(over, (ast.List, ast.Tuple, ast.Set))
             and any(_names_client(elt, names, classes) for elt in over.elts))
                or (isinstance(over, ast.Attribute) and over.attr == 'listOfServices')):
            for stmt in loop.body:
                for node in ast.walk(stmt):
                    loopNames.setdefault(id(node), set()).add(loop.target.id)
    found = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call):
            arg = route_arg(node, names | loopNames.get(id(node), set()), classes)
            if arg is not None:
                found.append((node.lineno, arg))
    return found


def _points_here(module, ownName: str, ownDir: str | None) -> bool:
    """Whether a services module names this repo (projectName or localRepoDirName)."""
    return any(isinstance(node, ast.keyword) and node.arg in ('projectName', 'localRepoDirName')
               and isinstance(node.value, ast.Constant) and node.value.value in (ownName, ownDir)
               for node in ast.walk(module.tree))


def http_caller_routes() -> tuple[dict[str, str], list[str], list[str]]:
    """(route -> reason, scanned siblings, problems) for the routes sibling
    services call over HTTP, read as pubSubMap.py reads them."""
    ownName, sources, warnings, mapped = fleet()
    ownDir = sources[ownName].dirName
    routes: dict[str, str] = {}
    scanned: list[str] = []
    notYet: list[str] = []
    problems: list[str] = [f'fleet discovery: {warning}' for warning in warnings]
    noFleetYet = not (mapped - {ownName})
    for name, src in sorted(sources.items()):
        if name == ownName:
            continue
        if src.section is None and src.absence == 'missing' and name not in mapped:
            if noFleetYet:
                notYet.append(name)
            else:
                problems.append(f'sibling {name} is not checked out ({src.reason}) and the fleet '
                                'exists (server/pubsub_map.json lists another repo): clone it or '
                                'pass --repo, or the gate cannot tell whether it calls a route '
                                'over HTTP')
            continue
        if src.section is None:
            problems.append(f'sibling {name} could not be read ({src.reason}): the gate cannot '
                            'tell whether it calls a route over HTTP. Check it out as pubSubMap.py '
                            'expects (see `python pubSubMap.py`).')
            continue
        # Matched on the canonical projectName, as the map matches consumers.
        clients = {var: svc for var, svc in src.section.services.items() if svc['projectName'] == ownName}
        classes = {svc['cls'].lower() for svc in clients.values() if svc.get('cls')}
        scanned.append(f"{name}@{src.ref or 'working tree'} (client: {', '.join(sorted(clients)) or 'none'})")
        for rel, module in src.section.repo.modules.items():
            try:
                if rel.startswith('services/') and _points_here(module, ownName, ownDir) \
                        and not any(svc['file'] == rel for svc in clients.values()):
                    problems.append(f'{name}/server/{rel} names {ownName} but declares no client the '
                                    'gate can read (`var = Cls(projectName=..., localRepoDirName=...)` '
                                    'at module level): its HTTP calls would go unseen')
                calls = client_calls(module, set(clients), classes) if clients else []
            except MapError as error:
                problems.append(f'{name}: {error}; the gate cannot check it for HTTP calls')
                continue
            for line, arg in calls:
                where = f'{name}/server/{rel}:{line}'
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    routes.setdefault(arg.value, f'{name} calls it over HTTP ({where})')
                else:
                    problems.append(f'{where} calls {ownName} with a non-literal route name: make it '
                                    'a string literal so the gate can tell which response it reads')
    if scanned and not any(not s.endswith('(client: none)') for s in scanned):
        problems.append(f'no sibling declares a services client for {ownName}, so the HTTP-caller '
                        'exclusion looked nowhere: check the siblings\' server/services/*.py '
                        f'(scanned: {"; ".join(scanned)})')
    scanned += [f'{name} skipped (never checked out, not in server/pubsub_map.json: no sibling yet)'
                for name in notYet]
    return routes, scanned, problems


def service_to_service_routes(source: str | None = None) -> list[str]:
    """The @route_config methods of ServiceToServiceActions, from its file's
    source (read from server/ when not given)."""
    if source is None:
        path = serverDir(REPO_ROOT) / SERVICE_TO_SERVICE
        if not path.is_file():
            pytest.fail(f'server/{SERVICE_TO_SERVICE} is missing: every project has it (a fresh '
                        f'scaffold writes it with an empty class {SERVICE_TO_SERVICE_CLASS}, combined '
                        'into ApiRequests), for the routes another service calls over HTTP')
        source = path.read_text(encoding='utf-8')
    classes = [node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == SERVICE_TO_SERVICE_CLASS]
    if not classes:
        pytest.fail(f'server/{SERVICE_TO_SERVICE} defines no class {SERVICE_TO_SERVICE_CLASS}')

    def isRoute(decorator: ast.expr) -> bool:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        return isinstance(target, ast.Name) and target.id == 'route_config'

    return [node.name for node in classes[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isRoute(d) for d in node.decorator_list)]


def service_to_service_problems(httpRoutes: dict[str, str], serviceRoutes: list[str],
                                routes: dict) -> list[str]:
    """A route a sibling calls over HTTP that is not in ServiceToServiceActions,
    and a route there that no sibling calls."""
    problems = [f'{route}: {reason}, but it is not in server/{SERVICE_TO_SERVICE}: move it there, '
                'unchanged (another service reads its response, so the gate must skip it)'
                for route, reason in sorted(httpRoutes.items())
                if route in routes and route not in serviceRoutes]
    problems += [f'{route} is in server/{SERVICE_TO_SERVICE} but no sibling calls it over HTTP: '
                 'if no other service calls it, move it back next to the client feature it serves '
                 'so the gate checks its fields'
                 for route in serviceRoutes if route not in httpRoutes]
    return problems


def failing_routes(routes: dict, excluded: dict) -> list[tuple[str, dict]]:
    """Routes with unused paths that no exclusion covers, most paths first."""
    return sorted(((route, r) for route, r in routes.items() if r['unused'] and route not in excluded),
                  key=lambda item: (-len(item[1]['unused']), item[0]))


# ---- The brief --------------------------------------------------------------


def exclusion_lines(excluded: dict[str, str], scanned: list[str]) -> list[str]:
    return [f'Excluded ({len(excluded)}), their response is read outside client/:',
            *(f'  {route}: {reason}' for route, reason in sorted(excluded.items())),
            f"HTTP callers searched in: {'; '.join(scanned) or 'none'}."]


def brief(failing: list[tuple[str, dict]], violations: list[dict], stats: dict) -> list[str]:
    lines = []
    if failing:
        total = sum(len(r['unused']) for _, r in failing)
        lines += [SERVICE_TO_SERVICE_BRIEF, '']
        lines.append(f'{len(failing)} route(s) send {total} field path(s) no client code reads '
                     '(most first; a listed object path covers its children):')
        for route, result in failing:
            paths = result['unused']
            nothingRead = all(p['status'] == 'unused' for p in result['paths'].values())
            note = '; NOTHING in client/ reads this response' if nothingRead else ''
            lines.append(f"  {route} -> {result['responseType']} ({len(paths)}{note})")
            lines.extend(textwrap.wrap(', '.join(capped(paths, BRIEF_PATHS_PER_ROUTE)), width=96,
                                       initial_indent='      ', subsequent_indent='      '))
        topRoute, top = failing[0]
        lines += ['', TRIM_HOWTO.format(route=topRoute, model=top['responseType'])]
    if violations:
        lines += ['', f'{len(violations)} dynamic-access violation(s): each hides which fields of a '
                  'response-typed value the client needs. Read the fields by name:']
        lines += [f"  client/{v['where']} {v['kind']} on {', '.join(v['types'])}: {v['code']}"
                  for v in violations]
    lines.append(f"(analyzer: {stats['seconds']:.0f}s over {stats['clientFiles']} client files and "
                 f"{stats['routes']} routes)")
    return lines


# ---- Skipping for a week after a pass ---------------------------------------


def _utcNow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def last_pass(stampPath: Path = STAMP_PATH) -> datetime.datetime | None:
    """When the stamp says the test last passed (timezone-aware), or None
    when it is missing, unreadable or malformed."""
    try:
        passedAt = datetime.datetime.fromisoformat(
            json.loads(stampPath.read_text(encoding='utf-8'))['lastPassedAt'])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return passedAt if passedAt.tzinfo is not None else None


def _localTime(instant: datetime.datetime) -> str:
    """instant in the project's timezone, e.g. '2026-10-14 12:00 UTC+8'."""
    local = instant.astimezone(Timezone().getTimezone())
    offsetHours = local.utcoffset().total_seconds() / 3600
    return f'{local:%Y-%m-%d %H:%M} UTC{offsetHours:+g}'


def skip_reason(now: datetime.datetime, stampPath: Path = STAMP_PATH) -> str | None:
    """Why the test skips (it passed less than RERUN_AFTER before now), or
    None: run it. It runs when forced, without a readable stamp, and with a
    stamp dated after now, so a wrong clock cannot skip it forever."""
    if os.getenv(FORCE_ENV_VAR, '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return None
    passedAt = last_pass(stampPath)
    if passedAt is None or not passedAt <= now < passedAt + RERUN_AFTER:
        return None
    return (f'passed at {_localTime(passedAt)}, less than '
            f'{RERUN_AFTER.days} days ago ({stampPath.name}); set {FORCE_ENV_VAR}=1 to run it now')


def record_pass(now: datetime.datetime, stampPath: Path = STAMP_PATH) -> None:
    stampPath.parent.mkdir(parents=True, exist_ok=True)
    stampPath.write_text(json.dumps(
        {'lastPassedAt': now.astimezone(datetime.timezone.utc).isoformat()}) + '\n', encoding='utf-8')


def clear_pass(stampPath: Path = STAMP_PATH) -> None:
    """Forget the last pass, so a run that fails, crashes or is interrupted
    leaves no stamp for the next run to skip on."""
    stampPath.unlink(missing_ok=True)


def test_everyResponseFieldIsReadByTheClient():
    skipReason = skip_reason(_utcNow())
    if skipReason:
        pytest.skip(skipReason)
    clear_pass()
    result = run_analyzer(analyzer_args())
    httpRoutes, scanned, problems = http_caller_routes()
    serviceRoutes = service_to_service_routes()
    # Every analyzer problem counts, an excluded route's included.
    problems = ([f'analyzer: {p}' for p in result['problems']] + problems
                + service_to_service_problems(httpRoutes, serviceRoutes, result['routes']))
    services = {route: f'{SERVICE_TO_SERVICE_REASON}: {httpRoutes[route]}' if route in httpRoutes
                else SERVICE_TO_SERVICE_REASON for route in serviceRoutes}
    # A publisher that is no route (PUBSUB_PAYLOAD_MODELS) has no response here.
    excluded = {route: reason for route, reason in {**publisher_routes(), **services}.items()
                if route in result['routes']}
    failing = failing_routes(result['routes'], excluded)
    violations = result['violations']
    exclusions = exclusion_lines(excluded, scanned)
    if problems or failing or violations:
        lines = [*problems, '' if problems else None]
        if failing or violations:
            lines += brief(failing, violations, result['stats']) + ['']
        pytest.fail('\n'.join(line for line in lines + exclusions if line is not None), pytrace=False)
    print('\n'.join(exclusions))
    record_pass(_utcNow())


# ---- Fast tests of the rules --------------------------------------------------


_NOW = datetime.datetime(2026, 10, 17, 4, 0, tzinfo=datetime.timezone.utc)


def _stampedDaysAgo(tmp_path, monkeypatch, days):
    """A temp stamp of a pass `days` before _NOW (negative: in the future), unforced."""
    monkeypatch.delenv(FORCE_ENV_VAR, raising=False)
    stamp = tmp_path / '.testStamps' / 'unusedResponseFields.json'
    record_pass(_NOW - datetime.timedelta(days=days), stamp)
    return stamp


def test_stampSkip_runsWithoutAStamp(tmp_path, monkeypatch):
    monkeypatch.delenv(FORCE_ENV_VAR, raising=False)
    assert skip_reason(_NOW, tmp_path / 'missing.json') is None


def test_stampSkip_skipsWithinAWeekOfAPass(tmp_path, monkeypatch):
    stamp = _stampedDaysAgo(tmp_path, monkeypatch, 3)
    assert json.loads(stamp.read_text(encoding='utf-8')) == {'lastPassedAt': '2026-10-14T04:00:00+00:00'}
    reason = skip_reason(_NOW, stamp)
    assert reason is not None
    passedAt = datetime.datetime(2026, 10, 14, 4, 0, tzinfo=datetime.timezone.utc)
    assert _localTime(passedAt) in reason and FORCE_ENV_VAR in reason


def test_stampSkip_aClearedPassRuns(tmp_path, monkeypatch):
    stamp = _stampedDaysAgo(tmp_path, monkeypatch, 3)
    clear_pass(stamp)
    assert not stamp.exists()
    assert skip_reason(_NOW, stamp) is None
    clear_pass(stamp)  # no stamp: nothing to do


def test_stampSkip_runsAWeekAfterAPass(tmp_path, monkeypatch):
    assert skip_reason(_NOW, _stampedDaysAgo(tmp_path, monkeypatch, 8)) is None


def test_stampSkip_runsWithAStampInTheFuture(tmp_path, monkeypatch):
    assert skip_reason(_NOW, _stampedDaysAgo(tmp_path, monkeypatch, -1)) is None


def test_stampSkip_runsWithAMalformedStamp(tmp_path, monkeypatch):
    monkeypatch.delenv(FORCE_ENV_VAR, raising=False)
    stamp = tmp_path / 'stamp.json'
    for garbage in ('not json', '[]', '{"lastPassedAt": 5}', '{"lastPassedAt": "yesterday"}',
                    '{"lastPassedAt": "2026-10-16T04:00:00"}'):
        stamp.write_text(garbage, encoding='utf-8')
        assert skip_reason(_NOW, stamp) is None, garbage
    stamp.write_bytes(b'\xff\xfe')
    assert skip_reason(_NOW, stamp) is None


def test_stampSkip_runsWhenForced(tmp_path, monkeypatch):
    stamp = _stampedDaysAgo(tmp_path, monkeypatch, 3)
    monkeypatch.setenv(FORCE_ENV_VAR, '1')
    assert skip_reason(_NOW, stamp) is None


def test_publisherRoutesAreTheManifestFns():
    manifest = json.loads((serverDir(REPO_ROOT) / pubSubModels.MANIFEST).read_text(encoding='utf-8'))
    routes = publisher_routes()
    assert set(routes) == {topic['fn'] for topic in manifest['topics'].values()}
    # Each route's reason names its topic.
    for name, topic in manifest['topics'].items():
        assert name in routes[topic['fn']]


def test_clientPathsAreRelativeToTheAnalyzerRoot(monkeypatch):
    # A client one level down (SalesApp's client/salesapp): paths are taken
    # relative to it, not to client/, and anything outside it is dropped.
    monkeypatch.setitem(globals(), 'CLIENT_DIR', REPO_ROOT / 'client' / 'salesapp')
    assert _client_relative('client/salesapp/app/schemas/') == 'app/schemas/'
    assert _client_relative('client/salesapp/app/api/Server.ts') == 'app/api/Server.ts'
    assert _client_relative('client/scripts/other.ts') is None
    assert _client_relative('server/app.py') is None


def test_everySkipIsAnExistingPathUnderTheAnalyzerRoot():
    args = analyzer_args()
    skips = [args[i + 1] for i, arg in enumerate(args) if arg == '--skip']
    for skip in skips:
        path = CLIENT_DIR / skip
        assert path.exists(), f'--skip {skip} names nothing under {CLIENT_DIR}'
        assert path.is_dir() == skip.endswith('/'), f'--skip {skip}: a folder ends in /, a file does not'
    # Every generated client file this project has is skipped, so its reads
    # never count; ServerRequests.ts always is.
    generated = generated_client_paths()
    assert any(p.endswith('ServerRequests.ts') for p in skips), (skips, generated)
    assert [p for p in generated if (CLIENT_DIR / p).exists()] == [p for p in skips if p in generated]


def _siblingSources(*siblings):
    from pubSubMap import RepoSource
    sources = {'me': RepoSource('me', 'me-repo')}
    for name, absence in siblings:
        sources[name] = RepoSource(name, f'{name}-repo')
        sources[name].absence, sources[name].reason = absence, f'{name} is {absence}'
    return sources


def test_onlyASiblingNeverSeenAnywhereIsSkipped_whileThereIsNoFleet(monkeypatch):
    # No fleet yet (the map lists only this repo): a fresh scaffold's example
    # service (no checkout, not in the map) is no sibling yet; an unreadable
    # one still fails.
    sources = _siblingSources(('example', 'missing'), ('broken', 'unreadable'))
    monkeypatch.setitem(globals(), 'fleet', lambda: ('me', sources, [], frozenset({'me'})))
    routes, scanned, problems = http_caller_routes()
    assert routes == {}
    assert [p.split(' could not be read')[0] for p in problems] == ['sibling broken']
    assert scanned == ['example skipped (never checked out, not in server/pubsub_map.json: no sibling yet)']

    # With no sibling at all, the gate has nowhere to look and nothing to fail on.
    monkeypatch.setitem(globals(), 'fleet', lambda: ('me', {'me': sources['me']}, [], frozenset({'me'})))
    assert http_caller_routes() == ({}, [], [])


def test_aMissingSibling_failsOnceTheFleetExists(monkeypatch):
    # The map lists another repo: a sibling that is not checked out fails,
    # whether the map lists it or not; none is skipped.
    sources = _siblingSources(('example', 'missing'), ('mapped', 'missing'))
    monkeypatch.setitem(globals(), 'fleet', lambda: ('me', sources, [], frozenset({'me', 'mapped'})))
    routes, scanned, problems = http_caller_routes()
    assert routes == {}
    assert scanned == []
    assert len(problems) == 2
    assert problems[0].startswith('sibling example is not checked out (example is missing)')
    assert 'clone it or pass --repo' in problems[0]
    assert problems[1].startswith('sibling mapped could not be read')


def test_serviceToServiceRoutesAreItsRouteMethods():
    source = textwrap.dedent("""\
        from route_config import route_config


        class ServiceToServiceActions():

            @route_config(httpMethod='POST', responseModel=Thing)
            def getThingForService(self, thingId: str) -> dict:
                return self._helper(thingId)

            @route_config
            def bareDecorator(self) -> dict:
                return {}

            def _helper(self, thingId: str) -> dict:
                return {}


        class OtherActions():

            @route_config(httpMethod='POST', responseModel=Thing)
            def notAServiceRoute(self) -> dict:
                return {}
        """)
    assert service_to_service_routes(source) == ['getThingForService', 'bareDecorator']
    # The scaffold's empty class has none.
    assert service_to_service_routes('class ServiceToServiceActions():\n    pass\n') == []


def test_serviceToServiceIsEnforcedBothWays():
    routes = {'calledAndMoved': {}, 'calledNotMoved': {}, 'movedNotCalled': {}, 'clientOnly': {}}
    httpRoutes = {'calledAndMoved': 'eco calls it over HTTP (a.py:1)',
                  'calledNotMoved': 'eco calls it over HTTP (a.py:2)',
                  'notARouteHere': 'eco calls it over HTTP (a.py:3)'}
    problems = service_to_service_problems(httpRoutes, ['calledAndMoved', 'movedNotCalled'], routes)
    assert [p.split(':')[0].split(' ')[0] for p in problems] == ['calledNotMoved', 'movedNotCalled']
    assert 'move it there' in problems[0] and 'no sibling calls it' in problems[1]


def test_briefAsksAboutOtherServicesBeforeTrimming():
    failing = [('getThing', {'unused': ['a'], 'responseType': 'Thing',
                             'paths': {'a': {'status': 'unused'}, 'b': {'status': 'used'}}})]
    text = '\n'.join(brief(failing, [], {'seconds': 1, 'clientFiles': 1, 'routes': 1}))
    # Near the top, before the per-route list; its short form in the how-to.
    assert text.index(SERVICE_TO_SERVICE_BRIEF) < text.index('route(s) send')
    assert 'Do NOT remove its fields' in SERVICE_TO_SERVICE_BRIEF
    assert SERVICE_TO_SERVICE_HOWTO in text.split('How to trim')[1]


def test_excludedRoutesAreDropped():
    routes = {'kept': {'unused': ['a']}, 'excluded': {'unused': ['a', 'b']}, 'clean': {'unused': []}}
    assert [route for route, _ in failing_routes(routes, {'excluded': 'publishes it'})] == ['kept']


def test_siblingHttpCallsAreFound():
    code = textwrap.dedent("""\
        from services.ecommerceServiceV2 import ecommerceServiceV2 as eco
        ecommerceServiceV2.request('getCategories', 'POST', {})
        services.ecommerceServiceV2.request(functionName='getUnits', method='POST', data={})
        serviceGetter.getServiceByName('EcommerceServiceV2').request('getLandingPageData', 'POST', {})
        request(ecommerceServiceV2.path, 'getProductById', 'POST', {})
        ecommerceServiceV2.request(routeName, 'POST', {})
        eco.request('viaAlias', 'POST', {})
        for svc in [warehouseManagementService, ecommerceServiceV2]:
            svc.request('viaListLoop', 'POST', {})
        for svc in serviceGetter.listOfServices:
            svc.request('viaEveryService', 'POST', {})
        ecommerceServiceV2.ping()
        accountingService.request('notThisRepo', 'POST', {})
        """)
    module = Repo({'actions/calls.py': code}).module('actions/calls.py')
    found = [(line, arg.value if isinstance(arg, ast.Constant) else None)
             for line, arg in client_calls(module, {'ecommerceServiceV2'}, {'ecommerceservicev2'})]
    assert sorted(found) == [(2, 'getCategories'), (3, 'getUnits'), (4, 'getLandingPageData'),
                             (5, 'getProductById'), (6, None), (7, 'viaAlias'), (9, 'viaListLoop'),
                             (11, 'viaEveryService')]


FIXTURE = {
    'tsconfig.json': json.dumps({
        'compilerOptions': {'target': 'es2020', 'lib': ['es2020'], 'strict': True, 'jsx': 'preserve',
                            'module': 'esnext', 'moduleResolution': 'bundler', 'noEmit': True},
        'include': ['**/*.ts', '**/*.tsx'],
    }),
    # A library: its generics are judged by their instantiated types.
    'node_modules/react/index.d.ts': """\
        export declare function useState<S>(initial: S): [S, (value: S) => void];
        export type DependencyList = readonly unknown[];
        export declare function useMemo<T>(factory: () => T, deps: DependencyList): T;
        """,
    'app/jsx.d.ts': """\
        declare namespace JSX {
          interface Element { readonly isElement: true; }
          interface IntrinsicElements { div: { children?: string | number | Element | (string | number | Element)[] }; }
          interface ElementChildrenAttribute { children: object; }
        }
        declare function structuredClone<T>(value: T): T;
        """,
    'app/schemas/WidgetSchema.ts': """\
        export interface Widget {
          widgetShown: string;
          widgetNeverRead: string;
          widgetDestructured: number;
          widgetRenamed: string;
          widgetByString: string;
          widgetFlowed: string;
          widgetItems: WidgetItem[];
          widgetNested: Nested | null;
          widgetSharedHolder: Shared;
        }
        export interface WidgetItem {
          itemLabel: string;
          itemNeverRead: string;
        }
        export interface Nested {
          nestedDeep: string;
        }
        export interface Shared {
          sharedLeaf: string;
          sharedOther: string;
        }
        """,
    'app/schemas/GadgetSchema.ts': """\
        import type { Shared } from './WidgetSchema';
        export interface Gadget { gadgetSpreadAway: string; }
        export interface Gizmo { gizmoEntries: string; }
        export interface Keyed { keyedField: string; }
        export interface Valued { valuedField: string; }
        export interface Strung { strungField: string; }
        export interface Rested { restedKept: string; restedRest: string; }
        export interface Holder { holderShared: Shared; }
        """,
    # A list with minItems is generated as a tuple: read through like an array.
    'app/schemas/TupleSchema.ts': """\
        export type TupleItems = [TupleItem, ...TupleItem[]];
        export interface TupleList { tupleItems: TupleItems; }
        export interface TupleItem { tupleRead: string; tupleSkip: string; }
        """,
    'app/schemas/FlowSchema.ts': """\
        export interface CastList { castItems: CastItem[]; }
        export interface CastItem { castRead: string; castSkip: string; }
        export interface StateList { stateItems: StateItem[]; }
        export interface StateItem { stateRead: string; stateSkip: string; }
        export interface PropItem { propRead: string; propSkip: string; }
        export interface Outer { outerInner: Inner; }
        export interface Inner { innerRead: string; innerSkip: string; }
        export interface GridItem { gridField: string; gridSkip: string; }
        export interface Bracket { bracketField: string; bracketSkip: string; }
        export interface LooseArg { looseArgField: string; }
        export interface GenericArg { genericArgField: string; }
        export interface IdentityArg { identityRead: string; identitySkip: string; }
        export interface Assigned { assignedField: string; }
        export interface Returned { returnedField: string; }
        export interface Casted { castedField: string; }
        export interface Snap { snapField: string; }
        export interface Echo { echoField: string; echoNested: EchoNested; }
        export interface EchoNested { echoLeaf: string; }
        export type Aliased = { aliasedRead: string; aliasedSkip: string; };
        export interface WithLiteral { literalHolder: { literalRead: string; literalSkip: string; }; }
        export interface WithOtherLiteral { otherHolder: { literalSkip: string; }; }
        export interface BothA { bothRead: string; }
        export interface BothB { bothSkip: string; }
        export interface MaybeArg { maybeArgField: string; }
        export interface AliasArg { aliasArgField: string; }
        export interface CastArg { castArgField: string; }
        export interface SpreadArg { spreadArgField: string; }
        export interface WrappedArg { wrappedArgField: string; }
        export interface ForIn { forInField: string; }
        export interface OwnKeys { ownKeysField: string; }
        export interface OwnNames { ownNamesField: string; }
        export interface OwnDescriptors { ownDescriptorsField: string; }
        export interface EachArg { eachArgField: string; }
        export interface ForOfArg { forOfArgField: string; }
        export interface DataArg { dataArgField: string; }
        export interface CloneArg { cloneArgField: string; }
        export interface HookArg { hookArgField: string; }
        export interface ClassArg { classArgField: string; }
        export interface NarrowArg { narrowRead: string; narrowSkip: string; }
        export interface WideArg { narrowRead: string; wideOnly: string; }
        // Each Wider type is a subtype of its Base, so TypeScript drops it
        // from a union holding both.
        export interface ReducedBase { reducedRead: string; reducedSkip: string; }
        export interface ReducedWider { reducedRead: string; reducedSkip: string; reducedWiderOnly?: string; }
        export interface PaidDoc { paidAmount: number; paidCheck?: string | null; paidSkip: string; }
        export interface PaidWallet { supplierPaid?: PaidNested | null; customerPaid?: PaidOther | null; }
        export interface PaidNested { paidAmount: number; paidCheck?: string | null; }
        export interface PaidOther { paidAmount: number; paidCheck?: string | null; paidOtherOnly?: string; }
        // Unrelated to the types above (no subtype either way), each with a read
        // field named like a field only a dropped member declares.
        export interface OtherWider { reducedRead: string; reducedWiderOnly: string; }
        export interface OtherPaid { paidCheck?: string | null; paidOtherOnly: string; }
        // RevWider's revShared is read, RevBase's never is.
        export interface RevBase { revRead: string; revShared: string; }
        export interface RevWider { revRead: string; revShared: string; revWiderOnly?: string; }
        export interface CallbackOld { callbackRead: boolean; callbackSkip: string; }
        export interface CallbackNew { callbackRead: boolean; callbackSkip: string; }
        export interface AndLeft { andRead: string; andSkip: string; }
        export interface Evolving { evolvingRead: string; evolvingSkip: string; }
        export interface DepRow { depRead: string; depSkip: string; }
        // The two arguments of a two-argument generated wrapper.
        export interface PairLeftNew { leftRead: string; crossName: string; }
        export interface PairRightNew { crossName: string; }
        export interface PairLeftOld { leftRead: string; }
        export interface PairRightOld { crossName: string; }
        """,
    'app/api/ApiResponse.ts': """        export interface ApiSuccess<T> { status: 200; data: T; }
        export interface ApiFailure { status: 400; message: string; }
        export type ApiResponse<T> = ApiSuccess<T> | ApiFailure;
        export interface Pair<L, R> { left: L; right: R; }
        """,
    'app/api/ServerRequests.ts': """\
        import type { Widget } from '../schemas/WidgetSchema';
        import type { Gadget, Gizmo, Keyed, Valued, Strung, Rested, Holder } from '../schemas/GadgetSchema';
        import type * as F from '../schemas/FlowSchema';
        import type { TupleList } from '../schemas/TupleSchema';
        import type { ApiResponse, Pair } from './ApiResponse';
        export interface RouteResponseModels {
          getWidget: Widget;
          getTupleList: TupleList;
          getGadget: Gadget;
          getGizmo: Gizmo;
          getKeyed: Keyed;
          getValued: Valued;
          getStrung: Strung;
          getRested: Rested;
          getHolder: Holder;
          getCastList: F.CastList;
          getStateList: F.StateList;
          getPropItem: F.PropItem;
          getOuter: F.Outer;
          getGrid: F.GridItem;
          getBracket: F.Bracket;
          getLooseArg: F.LooseArg;
          getGenericArg: F.GenericArg;
          getIdentityArg: F.IdentityArg;
          getAssigned: F.Assigned;
          getReturned: F.Returned;
          getCasted: F.Casted;
          getSnap: F.Snap;
          getEcho: F.Echo;
          getAliased: F.Aliased;
          getLiteral: F.WithLiteral;
          getOtherLiteral: F.WithOtherLiteral;
          getBoth: F.BothA & F.BothB;
          getMaybeArg: F.MaybeArg;
          getAliasArg: F.AliasArg;
          getCastArg: F.CastArg;
          getSpreadArg: F.SpreadArg;
          getWrappedArg: F.WrappedArg;
          getForIn: F.ForIn;
          getOwnKeys: F.OwnKeys;
          getOwnNames: F.OwnNames;
          getOwnDescriptors: F.OwnDescriptors;
          getEachArg: F.EachArg;
          getForOfArg: F.ForOfArg;
          getDataArg: F.DataArg;
          getCloneArg: F.CloneArg;
          getHookArg: F.HookArg;
          getClassArg: F.ClassArg;
          getNarrowArg: F.NarrowArg;
          getReducedBase: F.ReducedBase;
          getReducedWider: F.ReducedWider;
          getPaidDoc: F.PaidDoc;
          getPaidWallet: F.PaidWallet;
          getOtherWider: F.OtherWider;
          getOtherPaid: F.OtherPaid;
          getRevBase: F.RevBase;
          getRevWider: F.RevWider;
          getCallbackNew: F.CallbackNew;
          getAndLeft: F.AndLeft;
          getEvolving: F.Evolving;
          getDepRow: F.DepRow;
          getPairLeft: F.PairLeftNew;
          getPairRight: F.PairRightNew;
          getOpaque: object;
        }
        export interface RouteRequestBodies {
          postWidget: Widget;
        }
        export declare function call<K extends keyof RouteResponseModels>(route: K): RouteResponseModels[K];
        // A generated request method: what it is given goes back to the server.
        export declare function postEcho(body: F.Echo): void;
        // Generated request methods returning the generated ApiResponse wrapper.
        export declare const requests: {
          getReducedBase(): Promise<ApiResponse<F.ReducedBase>>;
          getReducedWider(): Promise<ApiResponse<F.ReducedWider>>;
          getOtherWider(): Promise<ApiResponse<F.OtherWider>>;
          getRevBase(): Promise<ApiResponse<F.RevBase>>;
          getRevWider(): Promise<ApiResponse<F.RevWider>>;
          getCallbackNew(): Promise<ApiResponse<F.CallbackNew>>;
          getEvolving(): Promise<ApiResponse<F.Evolving>>;
          getPair(): Promise<Pair<F.PairLeftNew, F.PairRightNew>>;
        };
        """,
    'app/page.ts': """\
        import { call } from './api/ServerRequests';
        import type { RouteRequestBodies } from './api/ServerRequests';
        import type { Widget } from './schemas/WidgetSchema';

        interface Row { widgetFlowed: string; }

        export function render(): string {
          const widget = call('getWidget');
          const { widgetDestructured, widgetRenamed: alias } = widget;
          const labels = widget.widgetItems.map((item) => item.itemLabel);
          const updated: Widget = { ...widget, widgetShown: 'x' };
          const maybe: Widget | null = widget;
          const deep = maybe?.widgetNested?.nestedDeep;
          const row: Row = widget;
          const column = 'widgetByString';
          const leaf = call('getHolder').holderShared.sharedLeaf;
          return `${widget.widgetShown}${widgetDestructured}${alias}${labels.join()}${updated.widgetShown}` +
            `${deep}${row.widgetFlowed}${column}${leaf}`;
        }

        export function send<K extends keyof RouteRequestBodies>(body: RouteRequestBodies[K]): string {
          return JSON.stringify(body);
        }

        export function leak(): string {
          const bag: Record<string, unknown> = { ...call('getGadget') }; // MARK gadget
          const counted = Object.keys(bag).length + Object.entries(call('getGizmo')).length; // MARK entries
          const keyed = Object.keys(call('getKeyed')).join(); // MARK keys
          const valued = Object.values(call('getValued')).join(); // MARK values
          const strung = JSON.stringify(call('getStrung')); // MARK stringify
          const { restedKept, ...others } = call('getRested'); // MARK rest
          return `${counted}${keyed}${valued}${strung}${restedKept}${String(others)}`;
        }
        """,
    'app/tuples.ts': """\
        import { call } from './api/ServerRequests';

        export function tupleLabels(): string {
          return call('getTupleList').tupleItems.map((item) => item.tupleRead).join();
        }
        """,
    'app/flows.tsx': """\
        import { useState, useMemo } from 'react';
        import { call, postEcho } from './api/ServerRequests';
        import type * as F from './schemas/FlowSchema';

        interface CastRow { castRead: string; }
        interface StateRow { stateRead: string; }
        interface PropRow { propRead: string; }
        interface CardProps { item: PropRow; }
        interface InnerRow { innerRead: string; }
        interface OuterRow { outerInner: InnerRow; }
        // A client alias of a library mapped type over another interface.
        type SharedView = Pick<F.WideArg, 'narrowRead'>;

        function Card(props: CardProps): JSX.Element {
          return <div>{props.item.propRead}</div>;
        }

        export const gridRows = call('getGrid');

        export function Flows(): JSX.Element {
          const casted = call('getCastList').castItems as CastRow[];
          const [rows, setRows] = useState<StateRow[]>([]);
          setRows(call('getStateList').stateItems);
          const outer: OuterRow = call('getOuter');
          const columns = [{ field: 'gridField' }];
          const key = 'bracketField';
          const bracket = call('getBracket');
          const total = useMemo(() => rows.length, [rows]);
          postEcho(call('getEcho'));
          const both = call('getBoth');
          const aliased = call('getAliased');
          const literal = call('getLiteral');
          const other = call('getOtherLiteral');
          const shared: SharedView = call('getNarrowArg');
          return (
            <div>
              {casted[0].castRead}
              {rows[0].stateRead}
              {outer.outerInner.innerRead}
              {columns.length}
              {bracket[key]}
              {total}
              {both.bothRead}
              {aliased.aliasedRead}
              {literal.literalHolder.literalRead}
              {other.otherHolder.literalSkip}
              {shared.narrowRead}
              <Card item={call('getPropItem')} />
            </div>
          );
        }
        """,
    'app/loose.ts': """\
        import { call } from './api/ServerRequests';
        import type * as F from './schemas/FlowSchema';

        function serialize(data: object): string {
          return JSON.stringify(data);
        }
        function persist<T>(value: T): string {
          return JSON.stringify(value);
        }
        function persistLater<U>(value: U): string {
          return persist(value);
        }
        function identity<T>(value: T): T {
          return value;
        }
        async function orFallback<T>(fetcher: () => Promise<T>, fallback: T): Promise<T> {
          try {
            return await fetcher();
          } catch {
            return fallback;
          }
        }
        declare const sessionStorage: { setItem(key: string, value: string): void };
        function persistMaybe<T>(key: string, value?: T): void {
          sessionStorage.setItem(key, JSON.stringify(value));
        }
        function persistAlias<T>(value: T): string {
          const v = value;
          return JSON.stringify(v);
        }
        function persistCast<T>(value: T): string {
          return JSON.stringify(value as T);
        }
        function persistSpread<T extends object>(value: T): string {
          const c = { ...value };
          return JSON.stringify(c);
        }
        function persistWrapped<T>(value: { data: T } | null): string {
          return JSON.stringify(value);
        }
        function persistEach<T>(items: T[]): void {
          items.forEach((i) => sessionStorage.setItem('k', JSON.stringify(i)));
        }
        function persistForOf<T>(items: readonly T[]): void {
          for (const i of items) sessionStorage.setItem('k', JSON.stringify(i));
        }
        function save<T>(w: { data: T }): string {
          return JSON.stringify(w.data);
        }
        function saveClone<T>(w: { data: T }): string {
          return JSON.stringify(structuredClone(w.data));
        }
        function useStored<T>(key: string) {
          const save = (v: T) => sessionStorage.setItem(key, JSON.stringify(v));
          return { save };
        }
        class Store<T> {
          save(v: T): string {
            return JSON.stringify(v);
          }
        }
        let sink: unknown;

        export function generics(): void {
          useStored<F.HookArg>('k').save(call('getHookArg')); // MARK hookArg
          new Store<F.ClassArg>().save(call('getClassArg')); // MARK classArg
          persistEach([call('getEachArg')]); // MARK eachArg
          persistForOf([call('getForOfArg')]); // MARK forOfArg
          save({ data: call('getDataArg') }); // MARK dataArg
          saveClone({ data: call('getCloneArg') }); // MARK cloneArg
          persistMaybe('k', call('getMaybeArg')); // MARK maybeArg
          persistAlias(call('getAliasArg')); // MARK aliasArg
          persistCast(call('getCastArg')); // MARK castArg
          persistSpread(call('getSpreadArg')); // MARK spreadArg
          persistWrapped({ data: call('getWrappedArg') }); // MARK wrappedArg
        }

        export function enumerate(): number {
          let n = 0;
          for (const k in call('getForIn')) n += k.length; // MARK forIn
          n += Reflect.ownKeys(call('getOwnKeys')).length; // MARK ownKeys
          n += Object.getOwnPropertyNames(call('getOwnNames')).length; // MARK ownNames
          n += Object.keys(Object.getOwnPropertyDescriptors(call('getOwnDescriptors'))).length; // MARK ownDescriptors
          return n;
        }

        export function loose(): string {
          const a = serialize(call('getLooseArg')); // MARK looseArg
          const b = persistLater(call('getGenericArg')); // MARK genericArg
          const kept = identity(call('getIdentityArg'));
          void orFallback(async () => call('getIdentityArg'), call('getIdentityArg'));
          sink = call('getAssigned'); // MARK assigned
          const c = call('getCasted') as unknown; // MARK casted
          const snap = { ...call('getSnap'), savedAt: 1 };
          const d = JSON.stringify(snap); // MARK snap
          return `${a}${b}${kept.identityRead}${String(sink)}${String(c)}${d}`;
        }

        export function give(): object {
          return call('getReturned'); // MARK returned
        }
        """,
    'app/reduced.ts': """        import { useMemo } from 'react';
        import { call, requests } from './api/ServerRequests';
        import type { ApiResponse, Pair } from './api/ApiResponse';
        import type * as F from './schemas/FlowSchema';

        const GETTERS = { base: 'getReducedBase', wider: 'getReducedWider', other: 'getOtherWider' } as const;
        const REV_GETTERS = { base: 'getRevBase', wider: 'getRevWider' } as const;

        // A call through a union of route methods: its result type drops
        // ReducedWider, which flows into ReducedBase only (it is no OtherWider).
        export async function viaUnionCall(kind: 'base' | 'wider' | 'other'): Promise<string> {
          const res = await requests[GETTERS[kind]]();
          return res.status === 200 ? res.data.reducedRead : '';
        }
        export function readOthers(): string {
          return call('getOtherWider').reducedWiderOnly + call('getOtherPaid').paidOtherOnly;
        }

        // RevWider flows into RevBase, never the other way: a read on RevWider
        // counts for RevWider alone.
        export async function viaReverse(kind: 'base' | 'wider'): Promise<string> {
          const res = await requests[REV_GETTERS[kind]]();
          return (res.status === 200 ? res.data.revRead : '') + call('getRevWider').revShared;
        }

        // An `in` narrowing, a conditional and ??: the result type keeps PaidNested
        // and OtherPaid; PaidDoc and PaidOther flow into PaidNested only.
        function receipt(doc: F.PaidDoc | F.PaidWallet, other?: F.OtherPaid): string {
          const payment = 'paidAmount' in doc ? doc : doc.supplierPaid ?? other ?? doc.customerPaid;
          return payment?.paidCheck ?? '';
        }
        export function viaNarrowing(wallet: boolean): string {
          return receipt(wallet ? call('getPaidWallet') : call('getPaidDoc'), call('getOtherPaid'));
        }

        // A route method behind a function-typed config, typed with another type.
        interface Callbacks {
          load: () => Promise<ApiResponse<F.CallbackOld>>;
          loadPair: () => Promise<Pair<F.PairLeftOld, F.PairRightOld>>;
        }
        const callbacks: Callbacks = {
          load: () => requests.getCallbackNew(),
          loadPair: () => requests.getPair(),
        };
        // A wrapper's arguments pair by position: PairLeftNew flows into
        // PairLeftOld only, so the right side's crossName read is no read of its own.
        export async function viaPair(): Promise<string> {
          const pair = await callbacks.loadPair();
          return pair.left.leftRead + pair.right.crossName;
        }
        export async function viaCallback(): Promise<boolean> {
          const res = await callbacks.load();
          return res.status === 200 && res.data.callbackRead;
        }

        // `a && b` never yields a's object, so the copy holds no AndLeft.
        export function storedCopy(): string {
          const doc = call('getAndLeft');
          const copy = doc && { andRead: doc.andRead };
          return JSON.stringify(copy);
        }

        // An untyped `let` takes the type of what is assigned to it: no loose value.
        export async function evolving(): Promise<string> {
          let res;
          res = await requests.getEvolving();
          return res.status === 200 ? res.data.evolvingRead : '';
        }

        // A dependency list drops DepRow (a subtype of Kept), but its elements are
        // only compared: no DepRow flows into Kept, which may be serialized.
        interface Kept { depRead: string; }
        export function depsOnly(kept: Kept): string {
          const row = call('getDepRow');
          const label = useMemo(() => row.depRead, [row, kept]);
          return label + JSON.stringify(kept);
        }
        """,
}


def _fixture_line(file: str, marker: str) -> str:
    lines = textwrap.dedent(FIXTURE[file]).splitlines()
    return f'{file}:{next(i for i, line in enumerate(lines, 1) if marker in line)}'


def test_analyzerRulesBite(tmp_path):
    for name, content in FIXTURE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding='utf-8')

    result = run_analyzer(['--root', str(tmp_path), '--schemas', 'app/schemas',
                           '--route-models', 'app/api/ServerRequests.ts', '--skip', 'app/api/'],
                          timeout=FIXTURE_TIMEOUT_SECONDS)
    status = {route: {p: v['status'] for p, v in r['paths'].items()} for route, r in result['routes'].items()}

    assert status['getWidget'] == {
        'widgetShown': 'used',
        'widgetNeverRead': 'unused',
        'widgetDestructured': 'used',
        'widgetRenamed': 'used',
        'widgetByString': 'usedByName',
        'widgetFlowed': 'used',
        'widgetItems': 'used',
        'widgetItems[].itemLabel': 'used',
        'widgetItems[].itemNeverRead': 'unused',
        'widgetNested': 'used',
        'widgetNested.nestedDeep': 'used',
        'widgetSharedHolder': 'unused',
        # Read through getHolder; still covered by the unused parent above.
        'widgetSharedHolder.sharedLeaf': 'used',
        'widgetSharedHolder.sharedOther': 'unused',
    }
    assert result['routes']['getWidget']['unused'] == [
        'widgetNeverRead', 'widgetItems[].itemNeverRead', 'widgetSharedHolder']
    # A tuple's elements, never its array methods.
    assert status['getTupleList'] == {'tupleItems': 'used', 'tupleItems[].tupleRead': 'used',
                                      'tupleItems[].tupleSkip': 'unused'}
    # Read through flows tsc cannot back up: a cast, a useState setter, a JSX
    # prop, nested pairing; by name through a DataGrid field and obj[key].
    assert status['getCastList'] == {'castItems': 'used', 'castItems[].castRead': 'used', 'castItems[].castSkip': 'unused'}
    assert status['getStateList'] == {'stateItems': 'used', 'stateItems[].stateRead': 'used',
                                      'stateItems[].stateSkip': 'unused'}
    assert status['getPropItem'] == {'propRead': 'used', 'propSkip': 'unused'}
    assert status['getOuter'] == {'outerInner': 'used', 'outerInner.innerRead': 'used', 'outerInner.innerSkip': 'unused'}
    # Read through a client alias of Pick<> over another interface.
    assert status['getNarrowArg'] == {'narrowRead': 'used', 'narrowSkip': 'unused'}
    assert status['getGrid'] == {'gridField': 'usedByName', 'gridSkip': 'unused'}
    assert status['getBracket'] == {'bracketField': 'usedByName', 'bracketSkip': 'unused'}
    # A generic that only hands T back reads by name; a request method's
    # argument goes back to the server whole.
    assert status['getIdentityArg'] == {'identityRead': 'used', 'identitySkip': 'unused'}
    assert status['getEcho'] == {'echoField': 'used', 'echoNested': 'used', 'echoNested.echoLeaf': 'used'}
    # Intersections, aliased and anonymous type literals (each literal its own type).
    assert status['getBoth'] == {'bothRead': 'used', 'bothSkip': 'unused'}
    assert status['getAliased'] == {'aliasedRead': 'used', 'aliasedSkip': 'unused'}
    assert status['getLiteral'] == {'literalHolder': 'used', 'literalHolder.literalRead': 'used',
                                    'literalHolder.literalSkip': 'unused'}
    # Reads on a union whose subtype members TypeScript dropped (a call through
    # a union of route methods, an `in` narrowing's conditional, ??) count for
    # the dropped members, and so does a route behind a function-typed config
    # (through the generated ApiResponse wrapper). A field only the dropped
    # member declares stays unused.
    assert status['getReducedBase'] == {'reducedRead': 'used', 'reducedSkip': 'unused'}
    assert status['getReducedWider'] == {'reducedRead': 'used', 'reducedSkip': 'unused',
                                         'reducedWiderOnly': 'unused'}
    assert status['getPaidDoc'] == {'paidAmount': 'usedByName', 'paidCheck': 'used', 'paidSkip': 'unused'}
    assert status['getPaidWallet'] == {
        'supplierPaid': 'used', 'supplierPaid.paidAmount': 'usedByName', 'supplierPaid.paidCheck': 'used',
        'customerPaid': 'used', 'customerPaid.paidAmount': 'usedByName', 'customerPaid.paidCheck': 'used',
        'customerPaid.paidOtherOnly': 'unused'}
    assert status['getCallbackNew'] == {'callbackRead': 'used', 'callbackSkip': 'unused'}
    assert status['getPairLeft'] == {'leftRead': 'used', 'crossName': 'unused'}
    assert status['getPairRight'] == {'crossName': 'used'}
    # A dropped member flows only into the kept members it is assignable to, and
    # a kept member never into a dropped one: reads of the same names on the
    # unrelated OtherWider/OtherPaid and on RevWider count for no one else.
    assert status['getOtherWider'] == {'reducedRead': 'used', 'reducedWiderOnly': 'used'}
    assert status['getOtherPaid'] == {'paidCheck': 'used', 'paidOtherOnly': 'used'}
    assert status['getRevBase'] == {'revRead': 'used', 'revShared': 'unused'}
    assert status['getRevWider'] == {'revRead': 'used', 'revShared': 'used', 'revWiderOnly': 'unused'}
    # Neither `doc && {...}`, an untyped `let` assigned a response nor a
    # dependency list is a violation (the list below has none of them).
    assert status['getAndLeft'] == {'andRead': 'used', 'andSkip': 'unused'}
    assert status['getEvolving'] == {'evolvingRead': 'used', 'evolvingSkip': 'unused'}
    assert status['getDepRow'] == {'depRead': 'used', 'depSkip': 'unused'}
    # Every value behind a violation is dynamic, never unused.
    for route, field in [('getGadget', 'gadgetSpreadAway'), ('getGizmo', 'gizmoEntries'),
                         ('getKeyed', 'keyedField'), ('getValued', 'valuedField'),
                         ('getStrung', 'strungField'), ('getLooseArg', 'looseArgField'),
                         ('getGenericArg', 'genericArgField'), ('getAssigned', 'assignedField'),
                         ('getReturned', 'returnedField'), ('getCasted', 'castedField'),
                         ('getSnap', 'snapField'), ('getMaybeArg', 'maybeArgField'),
                         ('getAliasArg', 'aliasArgField'), ('getCastArg', 'castArgField'),
                         ('getSpreadArg', 'spreadArgField'), ('getWrappedArg', 'wrappedArgField'),
                         ('getForIn', 'forInField'), ('getOwnKeys', 'ownKeysField'),
                         ('getOwnNames', 'ownNamesField'), ('getOwnDescriptors', 'ownDescriptorsField'),
                         ('getEachArg', 'eachArgField'), ('getForOfArg', 'forOfArgField'),
                         ('getDataArg', 'dataArgField'), ('getCloneArg', 'cloneArgField'),
                         ('getHookArg', 'hookArgField'), ('getClassArg', 'classArgField')]:
        assert status[route] == {field: 'dynamic'}, route
    # The field the destructuring names is read; the rest element hides the others.
    assert status['getRested'] == {'restedKept': 'used', 'restedRest': 'dynamic'}

    # The same-type spread, the request body `send` serializes, useMemo's
    # dependency array, `identity` and `orFallback` (safeFetch's shape) are no
    # violations; these are.
    assert sorted((v['where'], v['kind'], v['types'][0]) for v in result['violations']) == sorted([
        (_fixture_line('app/page.ts', 'MARK gadget'), 'spread into a loose value', 'Gadget'),
        (_fixture_line('app/page.ts', 'MARK entries'), 'Object.entries', 'Gizmo'),
        (_fixture_line('app/page.ts', 'MARK keys'), 'Object.keys', 'Keyed'),
        (_fixture_line('app/page.ts', 'MARK values'), 'Object.values', 'Valued'),
        (_fixture_line('app/page.ts', 'MARK stringify'), 'JSON.stringify', 'Strung'),
        (_fixture_line('app/page.ts', 'MARK rest'), 'rest element', 'Rested'),
        (_fixture_line('app/loose.ts', 'MARK looseArg'), 'passed to a loose parameter of serialize', 'LooseArg'),
        (_fixture_line('app/loose.ts', 'MARK genericArg'), 'passed to a loose parameter of persistLater', 'GenericArg'),
        (_fixture_line('app/loose.ts', 'MARK assigned'), 'assigned to a loose value', 'Assigned'),
        (_fixture_line('app/loose.ts', 'MARK casted'), 'cast to a loose type', 'Casted'),
        (_fixture_line('app/loose.ts', 'MARK snap'), 'JSON.stringify', 'app/loose.ts:__object'),
        (_fixture_line('app/loose.ts', 'MARK returned'), 'returned as a loose value', 'Returned'),
        (_fixture_line('app/loose.ts', 'MARK maybeArg'), 'passed to a loose parameter of persistMaybe', 'MaybeArg'),
        (_fixture_line('app/loose.ts', 'MARK aliasArg'), 'passed to a loose parameter of persistAlias', 'AliasArg'),
        (_fixture_line('app/loose.ts', 'MARK castArg'), 'passed to a loose parameter of persistCast', 'CastArg'),
        (_fixture_line('app/loose.ts', 'MARK spreadArg'), 'passed to a loose parameter of persistSpread', 'SpreadArg'),
        (_fixture_line('app/loose.ts', 'MARK wrappedArg'), 'passed to a loose parameter of persistWrapped', 'WrappedArg'),
        (_fixture_line('app/loose.ts', 'MARK forIn'), 'for...in', 'ForIn'),
        (_fixture_line('app/loose.ts', 'MARK ownKeys'), 'Reflect.ownKeys', 'OwnKeys'),
        (_fixture_line('app/loose.ts', 'MARK ownNames'), 'Object.getOwnPropertyNames', 'OwnNames'),
        (_fixture_line('app/loose.ts', 'MARK ownDescriptors'), 'Object.getOwnPropertyDescriptors', 'OwnDescriptors'),
        (_fixture_line('app/loose.ts', 'MARK eachArg'), 'passed to a loose parameter of persistEach', 'EachArg'),
        (_fixture_line('app/loose.ts', 'MARK forOfArg'), 'passed to a loose parameter of persistForOf', 'ForOfArg'),
        (_fixture_line('app/loose.ts', 'MARK dataArg'), 'passed to a loose parameter of save', 'DataArg'),
        (_fixture_line('app/loose.ts', 'MARK cloneArg'), 'passed to a loose parameter of saveClone', 'CloneArg'),
        (_fixture_line('app/loose.ts', 'MARK hookArg'), "passed to a loose parameter of useStored<F.HookArg>('k').save", 'HookArg'),
        (_fixture_line('app/loose.ts', 'MARK classArg'), 'passed to a loose parameter of new Store<F.ClassArg>().save', 'ClassArg'),
    ])
    # A route whose response type has no schema fields is a problem, not a pass.
    assert len(result['problems']) == 1 and result['problems'][0].startswith('route getOpaque:'), result['problems']
