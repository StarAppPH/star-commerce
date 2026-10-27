"""testServices: what a lazily started sibling test server is derived from
(its services/ object), when one is started, swept or left alone, and that
every sibling call goes through the hook in services/__request.py. The
process side (start, Job Object, tree kill) is exercised by every run that
calls accounting."""
import ast
import json
import os
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import testServices
from testServices import (DATABASES_ENV, OWNER_ENV, SHELL_KEYS_ENV, ServiceConfig,
                          databaseName, devPort, envName, orphans, serverEnv,
                          serviceConfig)

SERVER = Path(__file__).resolve().parent


def _service(name='accountingService', projectName='accounting',
             repo='accountingSystem', dev='http://127.0.0.1:5000/'):
    return SimpleNamespace(name=name, localRepoDirName=repo,
                           path=SimpleNamespace(dev=dev, prod=None),
                           getProjectName=lambda: projectName)


# --- derived from the service object ---------------------------------------------


@pytest.mark.parametrize('name, expected', [
    ('accountingService', 'ACCOUNTING_SERVICE'), ('salesApp', 'SALES_APP'),
    ('warehouseManagementService', 'WAREHOUSE_MANAGEMENT_SERVICE')])
def test_envName(name, expected):
    assert envName(name) == expected


def test_devPort():
    assert devPort('http://127.0.0.1:5000/') == 5000
    assert devPort('http://127.0.0.1/') is None
    assert devPort(None) is None


def test_databaseName_derivedFromBothProjectNames():
    assert databaseName(_service(), 'ecommerceV2', {}) == 'testaccountingForEcommerceV2Scratch'
    assert databaseName(_service('salesApp', 'salesApp'), 'accounting', {}) == \
        'testsalesAppForAccountingScratch'


def test_databaseName_theOverrideMapWins():
    assert databaseName(_service(), 'ecommerceV2',
                        {'accountingService': 'testaccountingForEcomScratch'}) == \
        'testaccountingForEcomScratch'


def test_serviceConfig_fromTheServiceMetadata():
    project = Path('C:/w/xdist/ecommerce')
    environ = {DATABASES_ENV: json.dumps({'accountingService': 'testaccountingForEcomScratch'})}

    config = serviceConfig(_service(), 'ecommerceV2', environ, project)

    root = Path('C:/w/xdist/accountingSystem')
    assert config.name == 'accountingService'
    assert config.port == 5000
    assert config.root == root
    assert config.python.parts[:6] == root.parts + ('server', 'venv')
    assert config.databaseName == 'testaccountingForEcomScratch'


def test_serviceConfig_rootAndPythonOverrides():
    environ = {'PYTEST_SERVICE_ROOT_SALES_APP': 'D:/sales',
               'PYTEST_SERVICE_PYTHON_SALES_APP': 'D:/py312/python.exe'}

    config = serviceConfig(_service('salesApp', 'salesApp', 'SalesApp',
                                    'http://127.0.0.1:7000/'),
                           'ecommerceV2', environ, Path('C:/w/ecommerce'))

    assert (config.port, config.root, config.python, config.databaseName) == (
        7000, Path('D:/sales'), Path('D:/py312/python.exe'),
        'testsalesAppForEcommerceV2Scratch')


def test_everySiblingGetsAScratchTestDatabase():
    """Each registered service's test database is conftest's override or the
    derived name, a Scratch one either way."""
    from AppConfig import AppConfig
    from conftest import TEST_SERVICE_DATABASES
    from services.AllServices import serviceGetter

    assert set(TEST_SERVICE_DATABASES) <= {
        service.name for service in serviceGetter.listOfServices}
    for service in serviceGetter.listOfServices:
        config = serviceConfig(service, AppConfig().projectName,
                               {DATABASES_ENV: json.dumps(TEST_SERVICE_DATABASES)})

        assert config.databaseName == TEST_SERVICE_DATABASES.get(
            service.name, databaseName(service, AppConfig().projectName, {}))
        assert config.databaseName.endswith('Scratch'), service.name
        assert config.root.name == service.localRepoDirName


def test_everyServiceIsFoundByItsDevUrl():
    """findService resolves every registered service from its dev URL, and
    every one has a dev port: a call to it can start it."""
    from services.AllServices import serviceGetter
    modules = testServices.serviceModules()
    assert not any(name.startswith('__') for name in modules)
    for service in serviceGetter.listOfServices:
        assert service.name in modules
        assert devPort(service.path.dev), service.name
        assert testServices.findService(service.path.dev) is service
    assert testServices.findService('http://127.0.0.1:1/') is None


def test_serverEnv_theShellsVariablesAndTheServers():
    environ = {'PATH': 'p', 'FROM_OUR_DOTENV': 'x', 'PYTEST_XDIST_WORKER': 'gw0',
               'PYTHONPATH': 'y', 'STAR_TEST_SERVICES_AUTOSTART': '1',
               'databaseName': 'testecommerce_gw0Scratch',
               SHELL_KEYS_ENV: json.dumps(['PATH', 'PYTEST_XDIST_WORKER', 'PYTHONPATH',
                                           'STAR_TEST_SERVICES_AUTOSTART', 'databaseName'])}
    config = ServiceConfig('accountingService', 5000, Path('r'), Path('p'),
                           'testaccountingForEcomScratch')

    assert serverEnv(environ, config, 4242) == {
        'PATH': 'p', 'ENVIRONMENT': 'localdev',
        'databaseName': 'testaccountingForEcomScratch', OWNER_ENV: '4242'}


def test_serverEnv_outsidePytestTakesTheWholeEnvironment():
    config = ServiceConfig('accountingService', 5000, Path('r'), Path('p'), 'db')

    assert serverEnv({'PATH': 'p', 'OTHER': 'o'}, config, 1) == {
        'PATH': 'p', 'OTHER': 'o', 'ENVIRONMENT': 'localdev', 'databaseName': 'db',
        OWNER_ENV: '1'}


def test_serverEnv_noOtherCasingShadowsTheServersOwn():
    """Windows reads variable names case-insensitively: a DATABASENAME or an
    Environment from the shell must not reach the server beside its own."""
    environ = {'PATH': 'p', 'DATABASENAME': 'testecommerce', 'Environment': 'cloudprod',
               'star_test_server_owner': '1', 'VIRTUAL_ENV': 'C:/venv',
               'COV_CORE_SOURCE': 'x', 'cov_core_config': 'y', 'Pytest_Addopts': '-x'}
    config = ServiceConfig('accountingService', 5000, Path('r'), Path('p'), 'testaScratch')

    assert serverEnv(environ, config, 7) == {
        'PATH': 'p', 'ENVIRONMENT': 'localdev', 'databaseName': 'testaScratch',
        OWNER_ENV: '7'}


# --- knobs ---------------------------------------------------------------------


@pytest.mark.parametrize('value, expected', [(None, True), ('', True), ('0', True),
                                             ('1', False), (' 1 ', False)])
def test_autostart_optOut(value, expected):
    environ = {} if value is None else {'PYTEST_NO_AUTOSTART_SERVICES': value}

    assert testServices.autostart(environ) is expected


def test_serverGB_knob():
    assert testServices.serverGB({}) == testServices.SERVER_GB
    assert testServices.serverGB({'PYTEST_SERVER_GB': '0.5'}) == 0.5
    with pytest.raises(ValueError, match='PYTEST_SERVER_GB'):
        testServices.serverGB({'PYTEST_SERVER_GB': 'lots'})


# --- start, use or leave alone --------------------------------------------------------


@pytest.fixture
def _records(monkeypatch, tmp_path):
    """The record file, the lock files and the per-process caches in tmp_path,
    and one fake sibling, accountingService on :5000, as the only service
    (the same in every repo, whichever siblings it has)."""
    monkeypatch.setattr(testServices, 'TEMP_DIR', tmp_path)
    monkeypatch.setattr(testServices, '_servicesByUrl', {'http://127.0.0.1:5000/': _service()})
    monkeypatch.setattr(testServices, '_answered', set())
    monkeypatch.setattr(testServices, '_failed', {})
    monkeypatch.setattr(testServices, '_warned', set())
    return tmp_path


@pytest.fixture
def _noStart(monkeypatch, _records):
    """ensureRunning with no real start."""
    starts = []
    monkeypatch.setattr(testServices, '_start', lambda config, environ: starts.append(config))
    return starts


def _checkout(root: Path) -> Path:
    """A checkout with a server/app.py and a python file; returns the python."""
    (root / 'server').mkdir(parents=True)
    (root / 'server' / 'app.py').write_text('', encoding='utf-8')
    python = root / 'python.exe'
    python.write_text('', encoding='utf-8')
    return python


def test_ensureRunning_usesWhatAnswersAndRemembersIt(monkeypatch, _noStart):
    probes = []
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: probes.append(port) or True)

    testServices.ensureRunning('http://127.0.0.1:5000/')
    testServices.ensureRunning('http://127.0.0.1:5000')

    assert probes == [5000]
    assert _noStart == []


def test_ensureRunning_notesAServerItReusesOnce(monkeypatch, _noStart):
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: True)
    environ = {OWNER_ENV: str(os.getpid())}

    testServices.ensureRunning('http://127.0.0.1:5000/', environ)
    testServices._recordReuse('accountingService', 5000, environ)

    records = testServices.ownedServers()
    assert [(record['kind'], record['port'], record['startedBy']) for record in records] == [
        ('reused', 5000, None)]
    assert testServices.summaryLines(records) == [
        'using the accountingService server already on :5000 (not started by this '
        'run; left running)']


def test_reuse_ofAnotherRunsServerSaysWhoStopsIt(_records):
    testServices.writeRecords([{'name': 'accountingService', 'pid': 20, 'port': 5000,
                                'ownerPid': 99, 'startTime': 1}])

    testServices._recordReuse('accountingService', 5000, {OWNER_ENV: str(os.getpid())})

    assert testServices.summaryLines(testServices.ownedServers()) == [
        'using the accountingService server already on :5000 (started by test run '
        '99; it stops when that run ends)']


def test_ensureRunning_leavesAnUnknownUrlAlone(monkeypatch, _noStart):
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: False)

    testServices.ensureRunning('http://127.0.0.1:1/')

    assert _noStart == []


def test_ensureRunning_startsTheServiceWhoseDevUrlItIs(monkeypatch, _noStart, tmp_path):
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: False)
    python = _checkout(tmp_path / 'acct')
    environ = {DATABASES_ENV: json.dumps({'accountingService': 'testaccountingForEcomScratch'}),
               'PYTEST_SERVICE_ROOT_ACCOUNTING_SERVICE': str(tmp_path / 'acct'),
               'PYTEST_SERVICE_PYTHON_ACCOUNTING_SERVICE': str(python)}

    testServices.ensureRunning('http://127.0.0.1:5000/', environ)

    assert [(config.name, config.port, config.databaseName) for config in _noStart] == [
        ('accountingService', 5000, 'testaccountingForEcomScratch')]


@pytest.mark.parametrize('what', ['checkout', 'python'])
def test_ensureRunning_leavesASiblingDownWithNothingToStartItFrom(monkeypatch, _noStart,
                                                                  tmp_path, what):
    """A sibling a test merely brushes against, with no checkout or python
    here, stays down (the call fails as before) and warns once."""
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: False)
    environ = {'PYTEST_SERVICE_ROOT_ACCOUNTING_SERVICE': str(tmp_path / 'none')}
    if what == 'python':
        _checkout(tmp_path / 'none')
        environ['PYTEST_SERVICE_PYTHON_ACCOUNTING_SERVICE'] = str(tmp_path / 'nope.exe')

    with pytest.warns(testServices.TestServiceWarning) as warned:
        testServices.ensureRunning('http://127.0.0.1:5000/', environ)
        testServices.ensureRunning('http://127.0.0.1:5000/', environ)

    assert len(warned) == 1
    expected = 'no checkout at' if what == 'checkout' else 'its python'
    assert str(warned[0].message).startswith('not auto-starting accountingService: ')
    assert expected in str(warned[0].message)
    assert _noStart == []


def test_ensureRunning_aFailedStartFailsAtOnceAfterwards(monkeypatch, _records, tmp_path,
                                                         capsys):
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: False)
    python = _checkout(tmp_path / 'acct')
    environ = {'PYTEST_SERVICE_ROOT_ACCOUNTING_SERVICE': str(tmp_path / 'acct'),
               'PYTEST_SERVICE_PYTHON_ACCOUNTING_SERVICE': str(python)}
    starts = []

    def failingStart(config, environ):
        starts.append(config)
        raise testServices.TestServiceError('accountingService exited with code 1')

    monkeypatch.setattr(testServices, '_start', failingStart)

    for attempt in range(2):
        with pytest.raises(testServices.TestServiceError, match='exited with code 1'):
            testServices.ensureRunning('http://127.0.0.1:5000/', environ)

    assert len(starts) == 1
    assert 'testServices: accountingService exited with code 1' in capsys.readouterr().err


def test_ensureRunning_aServerUpOnceTheLockIsHeldIsReusedNotStarted(monkeypatch, _noStart,
                                                                   tmp_path):
    """Another worker started it while this one waited on the port lock."""
    answers = iter([False, True])
    monkeypatch.setattr(testServices, 'portAnswers', lambda port: next(answers))
    python = _checkout(tmp_path / 'acct')
    environ = {OWNER_ENV: str(os.getpid()),
               'PYTEST_SERVICE_ROOT_ACCOUNTING_SERVICE': str(tmp_path / 'acct'),
               'PYTEST_SERVICE_PYTHON_ACCOUNTING_SERVICE': str(python)}

    testServices.ensureRunning('http://127.0.0.1:5000/', environ)

    assert _noStart == []
    assert [(record['kind'], record['port']) for record in testServices.ownedServers()] == [
        ('reused', 5000)]


def _freePort() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_start_aServerThatExitsFailsWithItsLogAndLeavesNoRecord(_records, tmp_path):
    """The real start path: a suspended Popen put in a job and resumed, its
    record written and removed again, the log tail in the error."""
    root = tmp_path / 'sibling'
    (root / 'server').mkdir(parents=True)
    (root / 'server' / 'app.py').write_text(
        "print('booting the sibling', flush=True)\nraise SystemExit(4)\n", encoding='utf-8')
    config = ServiceConfig('accountingService', _freePort(), root, Path(sys.executable),
                           'testaccountingForEcomScratch')

    with pytest.raises(testServices.TestServiceError) as excinfo:
        testServices._start(config, {OWNER_ENV: str(os.getpid()), 'PATH': os.environ['PATH'],
                                     'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                                     'PYTEST_SKIP_RAM_CHECK': '1'})

    message = str(excinfo.value)
    assert 'exited with code 4' in message
    assert 'booting the sibling' in message
    assert testServices.readRecords() == []


def test_start_refusesADatabaseThatIsNoScratch(tmp_path):
    python = _checkout(tmp_path)
    config = ServiceConfig('accountingService', 5000, tmp_path, python, 'testaccounting')

    with pytest.raises(testServices.TestServiceError, match='not a Scratch one'):
        testServices._start(config, {})


def test_start_refusedWithoutTheRam(monkeypatch, tmp_path):
    import testRamGate
    python = _checkout(tmp_path)
    monkeypatch.setattr(testRamGate, 'availableMemoryGB', lambda: 0.5)
    config = ServiceConfig('accountingService', 5000, tmp_path, python, 'testaScratch')

    with pytest.raises(testServices.TestServiceError) as excinfo:
        testServices._start(config, {'PYTEST_SERVER_GB': '0.3'})

    assert str(excinfo.value) == (
        'Not enough RAM to start accountingService for tests: 0.5 GB available, '
        '0.8 GB needed. Another test run or app is probably using it: wait for it '
        'to finish or free RAM (/free-ram). Override: PYTEST_SKIP_RAM_CHECK=1.')


def test_ramShortfall_needsTheServerAndASmallFloorOnly():
    """Not the gate's reserve: the gate admitted the workers with it."""
    assert testServices.ramShortfall(0.9, 0.3) is None
    assert testServices.ramShortfall(0.7, 0.3) == pytest.approx(0.8)


def test_fileLock_isExclusiveAcrossHandles(_records):
    with testServices.fileLock('x.lock', 5):
        with pytest.raises(TimeoutError):
            with testServices.fileLock('x.lock', 0.3):
                pass
    with testServices.fileLock('x.lock', 1):
        pass


# --- identity, the orphan sweep and the teardown -------------------------------------------


def _startTimes(**times):
    """startTimeOf for PIDs p<N>=<start time>; any other PID is not running."""
    table = {int(name[1:]): value for name, value in times.items()}
    return table.get


def test_isSame_matchesThePidAndItsStartTime():
    startTimeOf = _startTimes(p20=111)

    assert testServices.isSame(20, 111, startTimeOf)
    assert not testServices.isSame(20, 222, startTimeOf)  # recycled PID
    assert not testServices.isSame(21, 111, startTimeOf)  # gone
    assert not testServices.isSame(20, None, startTimeOf)  # never recorded


def test_processStartTime_ofThisProcessIsStable():
    me = testServices.processStartTime(os.getpid())

    assert me is not None
    assert testServices.processStartTime(os.getpid()) == me
    assert testServices.processStartTime(0x7FFFFFF0) is None


def test_orphans_aServerWhoseRunIsGoneIsOrphaned():
    records = [
        {'pid': 20, 'startTime': 1, 'ownerPid': 10, 'ownerStartTime': 1, 'port': 5000},
        {'pid': 21, 'startTime': 1, 'ownerPid': 11, 'ownerStartTime': 1, 'port': 5001},
        {'pid': 22, 'startTime': 1, 'ownerPid': 12, 'ownerStartTime': 1, 'port': 5002},
        {'pid': 23, 'startTime': 1, 'ownerPid': 13, 'ownerStartTime': 1, 'port': 5003},
        {'kind': 'reused', 'pid': 0, 'ownerPid': 11, 'ownerStartTime': 1, 'port': 6100},
        {'kind': 'reused', 'pid': 0, 'ownerPid': 10, 'ownerStartTime': 1, 'port': 7000},
    ]
    # 10 gone: 20 orphaned. 11 alive: 21 and its reuse kept. 22 gone: dropped.
    # 13 is a recycled owner PID (another start time): 23 orphaned.
    startTimeOf = _startTimes(p20=1, p21=1, p23=1, p11=1, p13=2)

    orphaned, kept = orphans(records, startTimeOf)

    assert orphaned == [records[0], records[3]]
    assert kept == [records[1], records[4]]


def test_orphans_aLiveOwnerWithoutAStartTimeIsNeverGuessedGone():
    records = [{'pid': 20, 'startTime': 1, 'ownerPid': 10, 'ownerStartTime': None,
                'port': 5000},
               {'pid': 21, 'startTime': 1, 'ownerPid': 11, 'ownerStartTime': None,
                'port': 5001}]

    orphaned, kept = orphans(records, _startTimes(p20=1, p21=1, p10=7))

    assert orphaned == [records[1]]  # owner 11 is not running at all
    assert kept == [records[0]]


def _record(pid, owner, startTime=1, **fields):
    return {'name': 'accountingService', 'pid': pid, 'startTime': startTime,
            'ownerPid': owner, 'ownerStartTime': 1, 'port': 5000, **fields}


def test_sweep_neverKillsARecycledPid(monkeypatch, _records):
    testServices.writeRecords([
        _record(20, 10),  # owner gone, server still the one recorded: killed
        _record(30, 11, startTime=5),  # owner gone, PID now another process
        _record(40, 12),  # owner alive
    ])
    killed = []
    monkeypatch.setattr(testServices, 'processStartTime',
                        _startTimes(p20=1, p30=6, p40=1, p12=1))
    monkeypatch.setattr(testServices, 'killTree', killed.append)

    lines = testServices.sweepOrphans()

    assert killed == [20]
    assert lines == ['stopped the accountingService server (PID 20) left running by '
                     'test run 10, which is gone']
    assert testServices.readRecords() == [_record(40, 12)]


def test_sweep_killsWithoutHoldingTheRecordLock(monkeypatch, _records):
    """A kill can take seconds; a worker starting a server must not wait on
    the record lock meanwhile."""
    testServices.writeRecords([_record(20, 10)])
    lockFreeDuringKill = []

    def kill(pid):
        try:
            with testServices.fileLock(testServices.RECORD_LOCK, 0):
                lockFreeDuringKill.append(True)
        except TimeoutError:
            lockFreeDuringKill.append(False)

    monkeypatch.setattr(testServices, 'processStartTime', _startTimes(p20=1))
    monkeypatch.setattr(testServices, 'killTree', kill)

    testServices.sweepOrphans()

    assert lockFreeDuringKill == [True]


def test_stopSession_killsOnlyThisRunsServersStillRecorded(monkeypatch, _records):
    me = os.getpid()
    log = _records / 'server.log'
    log.write_text('started', encoding='utf-8')
    testServices.writeRecords([
        _record(20, me, log=str(log)),
        _record(21, me, startTime=5),  # recycled PID: not killed
        _record(22, me + 1),  # another run's
        {'kind': 'reused', 'name': 'salesApp', 'pid': 0, 'ownerPid': me, 'port': 7000},
    ])
    killed = []
    monkeypatch.setattr(testServices, '_ownerJob', None)
    monkeypatch.setattr(testServices, 'processStartTime', _startTimes(p20=1, p21=6, p22=1))
    monkeypatch.setattr(testServices, 'killTree', killed.append)

    stopped = testServices.stopSession()

    assert [record['pid'] for record in stopped] == [20, 21, 0]
    assert killed == [20]
    assert not log.exists()  # a clean stop drops the log
    assert testServices.readRecords() == [_record(22, me + 1)]


def test_records_unreadableFileReadsAsEmpty(_records):
    path = _records / testServices.RECORD_NAME
    assert testServices.readRecords() == []
    path.write_text('not json', encoding='utf-8')
    assert testServices.readRecords() == []
    path.write_text('[{"pid": 1}, 3]', encoding='utf-8')
    assert testServices.readRecords() == []


# --- the hook ------------------------------------------------------------------------


@pytest.fixture
def _hookEnv(monkeypatch):
    from services import __request as serviceRequest
    import requests
    calls = []
    monkeypatch.setattr(serviceRequest, '_session', requests.Session())
    monkeypatch.setattr(testServices, 'ensureRunning', calls.append)
    monkeypatch.setenv('STAR_TEST_SERVICES_AUTOSTART', '1')
    monkeypatch.setenv(OWNER_ENV, '1234')
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    return serviceRequest, calls


@pytest.mark.parametrize('name, value', [
    ('STAR_TEST_SERVICES_AUTOSTART', '0'), ('STAR_TEST_SERVICES_AUTOSTART', None),
    (OWNER_ENV, None), ('ENVIRONMENT', 'clouddev'), ('ENVIRONMENT', 'cloudprod')])
def test_theHookRunsOnlyInALocaldevTestRun(monkeypatch, _hookEnv, name, value):
    serviceRequest, calls = _hookEnv
    serviceRequest._ensureTestService('http://127.0.0.1:5000/')
    assert calls == ['http://127.0.0.1:5000/']
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)

    serviceRequest._ensureTestService('http://127.0.0.1:5000/')

    assert calls == ['http://127.0.0.1:5000/']


def test_theHookSkipsAFakeSession(monkeypatch, _hookEnv):
    """A test that swaps the session for a fake sends nothing: nothing to
    start."""
    serviceRequest, calls = _hookEnv
    monkeypatch.setattr(serviceRequest, '_session', object())

    serviceRequest._ensureTestService('http://127.0.0.1:5000/')

    assert calls == []


class _Response:
    ok = True
    status_code = 200

    def json(self):
        return {}


class _Session:

    def __init__(self):
        self.urls = []

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        return _Response()


def test_requestCallsTheHookWithTheDevUrlNeverProd(monkeypatch):
    from services import __request as serviceRequest
    from services.__abstractService import Path as ServicePath
    hooked, session = [], _Session()
    monkeypatch.setattr(serviceRequest, '_session', session)
    monkeypatch.setattr(serviceRequest, '_ensureTestService', hooked.append)
    path = ServicePath(dev='http://127.0.0.1:5000/', prod='https://acct.example.com/')

    serviceRequest.request(path, 'someRoute', 'POST', {})
    serviceRequest.request(path, 'someRoute', 'POST', {}, useProd=True)

    assert hooked == ['http://127.0.0.1:5000/']
    assert session.urls == ['http://127.0.0.1:5000/someRoute',
                            'https://acct.example.com/someRoute']


def test_theSessionTurnsTheHookOn():
    assert os.environ['STAR_TEST_SERVICES_AUTOSTART'] == (
        '0' if os.environ.get('PYTEST_NO_AUTOSTART_SERVICES') == '1' else '1')
    assert os.environ[OWNER_ENV].isdigit()


def _appSources():
    """Every app .py file but the services/ request layer, the tests and the
    manual tools (tools/devPubSub.py posts to siblings itself, by design)."""
    for path in sorted(SERVER.rglob('*.py')):
        rel = path.relative_to(SERVER).as_posix()
        if rel.startswith(('venv/', 'tools/', 'services/', '.')) or '/venv/' in rel \
                or path.name.startswith('test_') or path.name in ('conftest.py',):
            continue
        if '__pycache__' in rel or 'node_modules' in rel:
            continue
        yield rel, path.read_text(encoding='utf-8')


# What a service's ping() may use of requests: a HEAD probe and the
# exceptions it catches. Anything else there sends.
_PING_REQUESTS = {'head', 'ConnectionError', 'Timeout', 'RequestException'}


def _requestsOutsideAPing(source: str, filename: str) -> list:
    """`<filename>:<line>: requests.<x>` for every use of `requests` in a
    services/ module but a HEAD probe (and its exceptions) inside ping()."""
    tree = ast.parse(source)
    allowed = {id(node) for function in ast.walk(tree)
               if isinstance(function, ast.FunctionDef) and function.name == 'ping'
               for node in ast.walk(function)
               if isinstance(node, ast.Attribute) and node.attr in _PING_REQUESTS}
    found = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
             and node.value.id == 'requests' and id(node) not in allowed]
    return [f'{filename}:{node.lineno}: requests.{node.attr}'
            for node in sorted(found, key=lambda node: node.lineno)]


def test_aPingMayOnlyProbe():
    source = (
        'import requests\n'
        'class S:\n'
        '    def ping(self):\n'
        '        try:\n'
        '            requests.head(self.url, timeout=0.2)\n'
        '            requests.post(self.url)\n'
        '        except (requests.ConnectionError, requests.Timeout):\n'
        '            return False\n'
        '        except requests.RequestException:\n'
        '            return False\n'
        '    def other(self):\n'
        '        return requests.head(self.url)\n')

    assert _requestsOutsideAPing(source, 'services/s.py') == [
        'services/s.py:6: requests.post', 'services/s.py:12: requests.head']


def test_siblingCallsGoOnlyThroughServicesRequest():
    """The lazy start hooks services/__request.py, so nothing else may reach a
    sibling: no app code reads a service's path.dev / path.prod (the only way
    to a sibling's URL) or names a sibling's dev port, and inside services/
    only __request.py sends. A service's ping() may only probe with a HEAD (a
    call it gates, `if service.isOnline`, is skipped while the sibling is
    down, as before: the lazy start never sees it)."""
    from services.AllServices import serviceGetter
    ports = sorted({devPort(service.path.dev) for service in serviceGetter.listOfServices}
                   - {None})
    portPattern = re.compile(r'(127\.0\.0\.1|localhost):(%s)\b' % '|'.join(map(str, ports))) \
        if ports else None
    offenders = []
    for rel, source in _appSources():
        if rel in ('servicePortCheck.py', 'testServices.py'):
            continue  # read the dev URLs to compare or match them, send nothing
        for number, line in enumerate(source.splitlines(), 1):
            if re.search(r'\.path\.(dev|prod)\b', line) or (
                    portPattern is not None and portPattern.search(line)):
                offenders.append(f'{rel}:{number}: {line.strip()}')
    for path in sorted((SERVER / 'services').glob('*.py')):
        if path.name in ('__request.py', '__abstractService.py'):
            continue
        offenders += _requestsOutsideAPing(path.read_text(encoding='utf-8'),
                                           f'services/{path.name}')
    assert offenders == []
