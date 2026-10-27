"""Sibling servers for test runs, started the first time a test calls one and
always stopped when the run ends, so a forgotten server never sits on the RAM
and a run that calls no sibling starts none.

Nothing is declared per service: services/__request.py calls
ensureRunning(baseUrl) before every sibling request in a test run (conftest
sets STAR_TEST_SERVICES_AUTOSTART=1 and STAR_TEST_SERVER_OWNER for the
session, and the hook also needs ENVIRONMENT=localdev, a real requests session
and a dev URL). Everything else comes from the services/<name>.py object
whose `path.dev` is that URL:
  - port: path.dev's;
  - checkout: ../<localRepoDirName>, next to this repo's checkout or worktree;
  - python: that checkout's server/venv python;
  - database: test<its projectName>For<this repo's ProjectName>Scratch
    (testaccountingForEcommerceV2Scratch), unless the STAR_TEST_SERVICE_DATABASES
    map (conftest's per-repo TEST_SERVICE_DATABASES) names another for the
    service (ecommerce keeps testaccountingForEcomScratch). It must end with
    'Scratch': the sibling's suite rules apply to its test database too.
It runs `server/app.py` there with ENVIRONMENT=localdev, that databaseName,
the shell's other variables (not this repo's server/.env, which AppConfig
loaded into this process) and STAR_TEST_SERVER_OWNER=<the pytest controller's
PID>, logging to a temp file (deleted when the server stops cleanly, kept
when it fails), and waits (START_TIMEOUT_SECONDS) until the port answers.

- A URL that answers already is used as is and left running: a server started
  by hand or by another agent (it must then run on the right Scratch database;
  the run prints a line naming it), or one another run started (which stops
  it when that run ends, even while this run still uses it). A URL that is no
  service's dev URL is left alone. Each process remembers the URLs that
  answered.
- A sibling without a checkout or a python to start it from is left down:
  the call fails as it did before (ServiceUnavailable), with one warning per
  service. A start that crashes, times out, has no RAM or no Scratch database
  fails the calling test (TestServiceError), and later calls in that process
  fail the same way at once.
- A per-port lock file in the temp dir makes the xdist workers wait for the
  one that starts a server, then use it.
- One record file in the temp dir lists every started (and reused) server:
  PID and creation time, owner PID and creation time, port. A PID is only
  ever killed while its creation time still matches its record, so a
  recycled PID is never touched. The controller (pytest_unconfigure, atexit
  as a backup) kills the tree of every server it owns. At session start it
  kills the recorded servers whose owner is gone (the orphan sweep); an
  unrecorded server is never killed.
- Windows: the controller holds a kill-on-close Job Object named after its
  PID and creation time; the process that starts a server (a worker under
  xdist) creates it suspended, opens that job by name, assigns the server
  (the venv launcher cannot spawn its port-holding child outside it),
  resumes it and closes its own handle. The server then dies with the
  controller however the run ends (a crash, Ctrl+C, a kill). Without the
  controller's job (a process started outside pytest with the variables
  set, or a name that already existed), the starting process holds a job of
  its own instead, and the server dies with that process. Elsewhere the
  server gets its own process group, killed by the controller.
- RAM: a start needs SERVER_GB plus MIN_FREE_GB available (not testRamGate's
  reserve: the gate admitted the workers with it, and they have taken their
  share by the time a test calls a sibling), or the calling test fails with
  "Not enough RAM to start ...". SERVER_GB is accounting's dev server, 0.2 GB
  after serving a full ecommerce run (2026-10-19), rounded up.

Knobs (environment): PYTEST_NO_AUTOSTART_SERVICES=1 starts nothing (start the
servers yourself, as before); PYTEST_SERVICE_ROOT_<NAME> and
PYTEST_SERVICE_PYTHON_<NAME> point at another checkout or interpreter for a
service (NAME = the services/ file name in UPPER_SNAKE: ACCOUNTING_SERVICE,
SALES_APP), e.g. a worktree whose tracked venv is incomplete;
PYTEST_SERVER_GB overrides SERVER_GB; PYTEST_SKIP_RAM_CHECK=1.

Stdlib (and testRamGate) only at import: the same file serves every repo.
"""
import atexit
import contextlib
import functools
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import testRamGate

AUTOSTART_ENV = 'STAR_TEST_SERVICES_AUTOSTART'
OWNER_ENV = 'STAR_TEST_SERVER_OWNER'
DATABASES_ENV = 'STAR_TEST_SERVICE_DATABASES'
SHELL_KEYS_ENV = 'STAR_TEST_SHELL_ENV_KEYS'
START_TIMEOUT_SECONDS = 60
SERVER_GB = 0.3
MIN_FREE_GB = 0.5
SERVER_SCRIPT = 'server/app.py'
SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
TEMP_DIR = Path(tempfile.gettempdir())
RECORD_NAME = 'starTestServers.json'
RECORD_LOCK = 'starTestServers.lock'
# Never handed to a server: pytest's, xdist's, coverage's and this module's
# variables, and this process's venv.
_DROPPED_ENV_PREFIXES = ('PYTEST_', 'PYTHONPATH', 'STAR_TEST_', 'COV_CORE_',
                         'VIRTUAL_ENV')


class ServiceConfig(NamedTuple):
    name: str  # the services/ file name: 'accountingService'
    port: int
    root: Path  # the checkout server/app.py runs from
    python: Path
    databaseName: str


class TestServiceError(RuntimeError):
    """A sibling server a test needs could not be started."""

    __test__ = False


class TestServiceWarning(UserWarning):
    """A sibling a test calls is left down: nothing to start it from."""

    __test__ = False


# --- derived from the service object (pure) --------------------------------------


def envName(serviceName: str) -> str:
    """accountingService -> ACCOUNTING_SERVICE, salesApp -> SALES_APP."""
    return re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', serviceName).upper()


def devPort(devUrl: Optional[str]) -> Optional[int]:
    try:
        return urlsplit(devUrl).port if devUrl else None
    except ValueError:
        return None


def databaseName(service, thisProjectName: str, overrides: dict) -> str:
    if service.name in overrides:
        return overrides[service.name]
    return (f'test{service.getProjectName()}'
            f'For{thisProjectName[:1].upper()}{thisProjectName[1:]}Scratch')


def serviceConfig(service, thisProjectName: str, environ,
                  projectRoot: Path = PROJECT_ROOT) -> ServiceConfig:
    name = envName(service.name)
    rootOverride = (environ.get(f'PYTEST_SERVICE_ROOT_{name}') or '').strip()
    root = Path(rootOverride) if rootOverride else projectRoot.parent / service.localRepoDirName
    pythonOverride = (environ.get(f'PYTEST_SERVICE_PYTHON_{name}') or '').strip()
    if pythonOverride:
        python = Path(pythonOverride)
    elif os.name == 'nt':
        python = root / 'server' / 'venv' / 'Scripts' / 'python.exe'
    else:
        python = root / 'server' / 'venv' / 'bin' / 'python'
    overrides = json.loads(environ.get(DATABASES_ENV) or '{}')
    return ServiceConfig(service.name, devPort(service.path.dev), root, python,
                         databaseName(service, thisProjectName, overrides))


def serverEnv(environ, config: ServiceConfig, ownerPid: int) -> dict:
    """The env a server starts with: the shell's variables (the keys conftest
    recorded before this repo's .env was loaded; all of them outside pytest)
    without the dropped ones, plus the server's own, which no other casing of
    the same name may shadow (Windows reads names case-insensitively)."""
    shellKeys = environ.get(SHELL_KEYS_ENV)
    keys = json.loads(shellKeys) if shellKeys else list(environ)
    own = {'ENVIRONMENT': 'localdev', 'databaseName': config.databaseName,
           OWNER_ENV: str(ownerPid)}
    ownLower = {key.lower() for key in own}
    env = {key: environ[key] for key in keys if key in environ
           and not key.upper().startswith(_DROPPED_ENV_PREFIXES)
           and key.lower() not in ownLower}
    env.update(own)
    return env


def autostart(environ=os.environ) -> bool:
    return (environ.get('PYTEST_NO_AUTOSTART_SERVICES') or '').strip() != '1'


def serverGB(environ=os.environ) -> float:
    return testRamGate.envNumber('PYTEST_SERVER_GB', SERVER_GB, environ)


def ramShortfall(availableGB: float, serverGB: float) -> Optional[float]:
    """The GB a start needs when `availableGB` falls short, else None."""
    neededGB = serverGB + MIN_FREE_GB
    return neededGB if availableGB < neededGB else None


def isSame(pid: int, startTime, startTimeOf) -> bool:
    """Whether `pid` is still the process recorded with `startTime`."""
    return startTime is not None and startTimeOf(pid) == startTime


def _ownerAlive(record: dict, startTimeOf) -> bool:
    """Whether the run that owns `record` still runs. Without a recorded
    start time (one that could not be read) a live owner PID counts as
    alive: never orphan a server on a guess."""
    if record.get('ownerStartTime') is None:
        return startTimeOf(record['ownerPid']) is not None
    return isSame(record['ownerPid'], record['ownerStartTime'], startTimeOf)


def orphans(records: list, startTimeOf) -> tuple:
    """(orphaned, kept) from the records: a started server whose owner run is
    gone is orphaned while it still runs; a record whose server (or, for a
    reused one, whose owner) is gone is dropped; the others are kept."""
    orphaned, kept = [], []
    for record in records:
        ownerAlive = _ownerAlive(record, startTimeOf)
        if record.get('kind') == 'reused':
            if ownerAlive:
                kept.append(record)
            continue
        if not isSame(record['pid'], record.get('startTime'), startTimeOf):
            continue
        (kept if ownerAlive else orphaned).append(record)
    return orphaned, kept


# --- finding the service ------------------------------------------------------------

_servicesByUrl: dict = {}


def _key(url: str) -> str:
    return url.rstrip('/') + '/'


def serviceModules(serverDir: Path = SERVER_DIR) -> list:
    """The services/ modules that may define a service: every file but the
    dunder ones (__request.py, __abstractService.py...)."""
    return sorted(path.stem for path in (serverDir / 'services').glob('*.py')
                  if not path.stem.startswith('__'))


def servicesIn(module) -> list:
    """The service objects a services/ module defines (built, not imported)."""
    return [value for value in vars(module).values()
            if hasattr(value, 'localRepoDirName') and hasattr(value, 'path')
            and not isinstance(value, type)
            and getattr(value, 'name', None) == module.__name__.rsplit('.', 1)[-1]]


def findService(baseUrl: str):
    """The services/ object whose dev URL is `baseUrl`, or None."""
    if not _servicesByUrl:
        for moduleName in serviceModules():
            for service in servicesIn(importlib.import_module(f'services.{moduleName}')):
                if service.path.dev:
                    _servicesByUrl[_key(service.path.dev)] = service
    return _servicesByUrl.get(_key(baseUrl))


# --- the system ------------------------------------------------------------------------


def portAnswers(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


@functools.lru_cache(maxsize=None)
def _kernel32():
    """This module's own kernel32 (never ctypes.windll's shared one), with
    the argtypes it calls."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, argtypes, restype in (
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('GetExitCodeProcess', [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ('GetProcessTimes', [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
             wintypes.BOOL),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
            ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ('OpenJobObjectW', [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            ('SetInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD], wintypes.BOOL),
            ('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)):
        function = getattr(kernel32, name)
        function.argtypes = argtypes
        function.restype = restype
    return kernel32


def processStartTime(pid: int):
    """When `pid` started (an opaque value, stable for the process's life),
    or None when no such process runs. With the PID, it names one process:
    a recycled PID has another."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = _kernel32()
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not (kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    and code.value == STILL_ACTIVE):
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *map(ctypes.byref, times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:  # Linux: field 22 of /proc/<pid>/stat, after the parenthesised name
        stat = Path(f'/proc/{int(pid)}/stat').read_text()
        return int(stat.rsplit(')', 1)[1].split()[19])
    except OSError:
        pass
    except (IndexError, ValueError):
        return None
    try:  # macOS and other POSIX
        out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(int(pid))],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def killTree(pid: int) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=30)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@contextlib.contextmanager
def fileLock(name: str, timeoutSeconds: float):
    """An exclusive lock on TEMP_DIR/<name> across processes (released by the
    OS should the holder die)."""
    handle = open(TEMP_DIR / name, 'a+b')
    deadline = time.monotonic() + timeoutSeconds
    try:
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'waited {timeoutSeconds:g} s for {name}') from None
                time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _recordPath() -> Path:
    return TEMP_DIR / RECORD_NAME


def _readRecordsUnlocked() -> list:
    try:
        records = json.loads(_recordPath().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)
            and {'pid', 'ownerPid', 'port'} <= set(record)]


def _writeRecordsUnlocked(records: list) -> None:
    path = _recordPath()
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(records, indent=1), encoding='utf-8')
    for attempt in range(20):  # a reader without the lock can hold it open
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(temporary, path)


def readRecords() -> list:
    with fileLock(RECORD_LOCK, 30):
        return _readRecordsUnlocked()


def writeRecords(records: list) -> None:
    with fileLock(RECORD_LOCK, 30):
        _writeRecordsUnlocked(records)


def _updateRecords(change) -> list:
    """records = change(records), under the record lock; returns the old."""
    with fileLock(RECORD_LOCK, 30):
        records = _readRecordsUnlocked()
        _writeRecordsUnlocked(change(records))
        return records


# --- the controller ------------------------------------------------------------------

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_ASSIGN_PROCESS = 0x1
_JOB_OBJECT_TERMINATE = 0x8
_ERROR_ALREADY_EXISTS = 183
_CREATE_SUSPENDED = 0x4
_ownerJob: Optional[int] = None


def jobName(ownerPid: int, ownerStartTime) -> str:
    return f'Local\\starTestServers_{ownerPid}_{ownerStartTime}'


def _killOnCloseJob(name: Optional[str]) -> int:
    """A new kill-on-close Job Object; OSError when `name` already exists."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(field, ctypes.c_uint64) for field in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits),
                    ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel32 = _kernel32()
    ctypes.set_last_error(0)  # a stale 183 must not read as "already exists"
    job = kernel32.CreateJobObjectW(None, name)
    error = ctypes.get_last_error()
    if not job:
        raise ctypes.WinError(error)
    if name is not None and error == _ERROR_ALREADY_EXISTS:
        # Someone else's job: its limits are not ours to trust.
        kernel32.CloseHandle(job)
        raise OSError(f'the job object {name} already exists')
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
            ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job


def startSession() -> list:
    """In the controller, at session start: hold the run's Job Object and
    sweep the orphans. Returns the lines to print."""
    global _ownerJob
    lines = []
    if os.name == 'nt' and _ownerJob is None:
        try:
            _ownerJob = _killOnCloseJob(jobName(os.getpid(), processStartTime(os.getpid())))
        except OSError as error:
            lines.append(f'test servers: no job object for this run ({error}); a '
                         'server dies with the process that started it instead')
    atexit.register(stopSession)
    return lines + sweepOrphans()


def sweepOrphans() -> list:
    """Kill the recorded servers whose run is gone; one line for each."""
    lines = []
    # Collected under the lock, killed without it (a taskkill can take
    # seconds, and every starting worker waits on that lock), then dropped.
    orphaned, _ = orphans(readRecords(), processStartTime)
    killed = []
    for record in orphaned:
        killTree(record['pid'])
        _removeLog(record)
        killed.append((record['pid'], record.get('startTime')))
        lines.append(f"stopped the {record.get('name', 'test')} server "
                     f"(PID {record['pid']}) left running by test run "
                     f"{record['ownerPid']}, which is gone")

    def change(records):
        stillOrphaned, kept = orphans(records, processStartTime)
        return kept + [record for record in stillOrphaned
                       if (record['pid'], record.get('startTime')) not in killed]

    _updateRecords(change)
    return lines


def ownedServers(ownerPid: Optional[int] = None) -> list:
    """This run's records: the servers it started and the ones it reused."""
    ownerPid = os.getpid() if ownerPid is None else ownerPid
    return [record for record in readRecords() if record['ownerPid'] == ownerPid]


def summaryLines(records: list) -> list:
    lines = []
    for record in records:
        if record.get('kind') == 'reused':
            lines.append(
                f"using the {record.get('name')} server already on :{record['port']} "
                + (f"(started by test run {record['startedBy']}; it stops when that "
                   'run ends)' if record.get('startedBy') else
                   '(not started by this run; left running)'))
        else:
            lines.append(
                f"stopping the {record.get('name')} server this run started on "
                f":{record['port']} (PID {record['pid']})")
    return lines


def _removeLog(record: dict) -> None:
    if record.get('log'):
        for attempt in range(10):  # the killed server may still hold it
            try:
                Path(record['log']).unlink(missing_ok=True)
                return
            except PermissionError:
                time.sleep(0.1)


def stopSession() -> list:
    """In the controller, at session end: kill every server this run started
    (by any of its workers) that is still the process it recorded, and delete
    its log. Returns this run's records."""
    global _ownerJob
    owner = os.getpid()
    # Each server's identity is read before the job goes: once the job is
    # terminated a PID may be reused, and only a live match is killed.
    mine = [record for record in _updateRecords(lambda records: [
        record for record in records if record['ownerPid'] != owner])
        if record['ownerPid'] == owner]
    started = [record for record in mine if record.get('kind') != 'reused']
    alive = [record for record in started
             if isSame(record['pid'], record.get('startTime'), processStartTime)]
    if _ownerJob is not None:
        kernel32 = _kernel32()
        kernel32.TerminateJobObject(_ownerJob, 1)
        kernel32.CloseHandle(_ownerJob)
        _ownerJob = None
    for record in alive:
        if isSame(record['pid'], record.get('startTime'), processStartTime):
            killTree(record['pid'])
    for record in started:
        _removeLog(record)
    return mine


# --- the lazy start (any test process) ----------------------------------------------------

_answered: set = set()
_failed: dict = {}  # url key -> the message every later call raises at once
_warned: set = set()


def ensureRunning(baseUrl: str, environ=os.environ) -> None:
    """Before a sibling request in a test run: start the service whose dev
    URL is `baseUrl` unless something answers there."""
    key = _key(baseUrl)
    if key in _answered:
        return
    if key in _failed:
        raise TestServiceError(_failed[key])
    port = devPort(baseUrl)
    service = findService(baseUrl) if port is not None else None
    if service is None:
        return  # no sibling's dev URL: nothing to start
    if portAnswers(port):
        _answered.add(key)
        _recordReuse(service.name, port, environ)
        return
    from AppConfig import AppConfig
    config = serviceConfig(service, AppConfig().projectName, environ)
    missing = _missing(config)
    if missing:
        if config.name not in _warned:
            _warned.add(config.name)
            warnings.warn(f'not auto-starting {config.name}: {missing}', TestServiceWarning,
                          stacklevel=2)
        return  # left down: the call fails as it did before
    try:
        with fileLock(f'starTestServer_{port}.lock', START_TIMEOUT_SECONDS + 30):
            if portAnswers(port):
                _recordReuse(service.name, port, environ)
            else:
                _start(config, environ)
    except (TestServiceError, OSError) as error:
        message = str(error) if isinstance(error, TestServiceError) else (
            f'could not start {config.name} for tests: {error}')
        _failed[key] = message
        # A route turns the exception into a 500 without its message: the
        # test's captured stderr keeps it.
        sys.stderr.write(f'testServices: {message}\n')
        raise TestServiceError(message) from None
    _answered.add(key)


def _owner(environ) -> tuple:
    ownerPid = int(environ.get(OWNER_ENV) or os.getpid())
    return ownerPid, processStartTime(ownerPid)


def _recordReuse(name: str, port: int, environ) -> None:
    """Note, once per run, that this run uses a server it did not start."""
    ownerPid, ownerStartTime = _owner(environ)

    def change(records):
        if any(record['port'] == port and record['ownerPid'] == ownerPid
               for record in records):
            return records  # ours, or already noted
        startedBy = next((record['ownerPid'] for record in records
                          if record['port'] == port and record.get('kind') != 'reused'),
                         None)
        return records + [{'kind': 'reused', 'name': name, 'pid': 0, 'port': port,
                           'ownerPid': ownerPid, 'ownerStartTime': ownerStartTime,
                           'startedBy': startedBy}]

    _updateRecords(change)


def _missing(config: ServiceConfig) -> Optional[str]:
    """What a start lacks to begin at all (left down then), or None."""
    hint = (f'set PYTEST_SERVICE_ROOT_{envName(config.name)} / '
            f'PYTEST_SERVICE_PYTHON_{envName(config.name)} to start it')
    if not (config.root / SERVER_SCRIPT).is_file():
        return f'no checkout at {config.root} ({hint})'
    if not config.python.is_file():
        return f'its python {config.python} is missing ({hint})'
    return None


def _checkStart(config: ServiceConfig, environ) -> None:
    """Raise TestServiceError when a start must not happen."""
    if not config.databaseName.endswith('Scratch'):
        raise TestServiceError(
            f'{config.name} would run on database {config.databaseName!r}, not a '
            "Scratch one: fix TEST_SERVICE_DATABASES in conftest's per-repo block")
    if testRamGate.skipRamCheck(environ):
        return
    availableGB = testRamGate.availableMemoryGB()
    neededGB = ramShortfall(availableGB, serverGB(environ))
    if neededGB is not None:
        raise TestServiceError(
            f'Not enough RAM to start {config.name} for tests: '
            f'{availableGB:.1f} GB available, {neededGB:.1f} GB needed. Another '
            'test run or app is probably using it: wait for it to finish or '
            'free RAM (/free-ram). Override: PYTEST_SKIP_RAM_CHECK=1.')


def _logTail(path: Path, lines: int = 20) -> str:
    try:
        return '\n'.join(path.read_text(encoding='utf-8', errors='replace')
                         .splitlines()[-lines:])
    except OSError:
        return '(no log)'


_ownJobs: list = []  # held until this process exits


def _assignToJob(process: subprocess.Popen, ownerPid: int, ownerStartTime) -> None:
    """Put the suspended `process` in the controller's job (or, without one,
    in a new job this process holds), then let it run."""
    import ctypes
    kernel32 = _kernel32()
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    job = kernel32.OpenJobObjectW(_JOB_OBJECT_ASSIGN_PROCESS | _JOB_OBJECT_TERMINATE,
                                  False, jobName(ownerPid, ownerStartTime))
    own = not job
    try:
        if own:
            job = _killOnCloseJob(None)
        handle = int(process._handle)
        if not kernel32.AssignProcessToJobObject(job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if ntdll.NtResumeProcess(handle) != 0:
            raise OSError('could not resume the test server process')
    except OSError:
        process.kill()
        if job:
            kernel32.CloseHandle(job)
        raise
    if own:
        _ownJobs.append(job)
    else:
        kernel32.CloseHandle(job)


def _start(config: ServiceConfig, environ) -> None:
    _checkStart(config, environ)
    ownerPid, ownerStartTime = _owner(environ)
    logPath = TEMP_DIR / f'starTestServer_{config.name}_{os.getpid()}.log'
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = _CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW
    else:
        kwargs['start_new_session'] = True
    with open(logPath, 'w', encoding='utf-8') as log:
        process = subprocess.Popen([str(config.python), SERVER_SCRIPT], cwd=config.root,
                                   env=serverEnv(environ, config, ownerPid),
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, **kwargs)
    if os.name == 'nt':
        _assignToJob(process, ownerPid, ownerStartTime)
    record = {'name': config.name, 'pid': process.pid,
              'startTime': processStartTime(process.pid), 'port': config.port,
              'ownerPid': ownerPid, 'ownerStartTime': ownerStartTime,
              'log': str(logPath), 'startedAt': time.time()}
    _updateRecords(lambda records: records + [record])
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if portAnswers(config.port):
            return
        if process.poll() is not None:
            break
        time.sleep(0.25)
    if process.poll() is None:
        killTree(process.pid)
    _updateRecords(lambda records: [
        other for other in records
        if other['pid'] != process.pid or other.get('kind') == 'reused'])
    state = (f'exited with code {process.returncode}' if process.poll() is not None
             else f'did not answer within {START_TIMEOUT_SECONDS} s')
    raise TestServiceError(
        f'{config.name}, started for the tests, {state} on :{config.port}. '
        f'Its log ({logPath}) ends:\n{_logTail(logPath)}')
