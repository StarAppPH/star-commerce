"""How many test workers fit in the memory available right now, and whether a
test run starts at all.

conftest.py asks this module once, in the pytest controller process (never
in an xdist worker), before any test runs:

- `pytest -n auto` gets as many workers as fit:
      max(1, min(maxWorkers, cpus - 1, (available - reserve) // perWorker))
  or fewer when not all of them fit. Each worker imports the whole app, and
  the local mongod grows with every worker database, so too many workers
  push a 16 GB laptop into swap.
- Every run (serial, `-n <N>`, `-n auto`) is refused when its workers do not
  fit: perWorker * workers + reserve above the available memory, the sign
  that another test run (an agent's, often) or app holds the RAM. The
  refusal names the other pytest and pubSubMockDataGenerator processes.

Available = what the OS can hand out without swapping (Task Manager's
Available: free plus standby). PER_WORKER_GB is measured on the ecommerce
suite (2026-10-19, 4 workers): the available memory fell by 3.0 GB at the
peak, the 4 workers (about 0.4 GB each) plus the 1.5 GB node process
test_unused_response_fields.py starts in one of them; mongod grew by under
0.05 GB (its cache is shared by every worker database). A repo whose
workers take more passes its own measured figure (conftest's per-repo
TEST_WORKER_GB) as the default.

Knobs (environment): PYTEST_WORKER_GB, PYTEST_RAM_RESERVE_GB,
PYTEST_MAX_WORKERS override the three numbers; PYTEST_WAIT_FOR_RAM=<seconds>
waits that long (checking every 15 s) for the memory before refusing (0, the
default, refuses at once); PYTEST_SKIP_RAM_CHECK=1 skips the refusal (`-n
auto` still sizes by memory).

A refused run exits with code 75 (EX_TEMPFAIL: try again later), clear of
pytest's own 0-5.

Pure stdlib, no project imports: the same file serves every repo.
testServices.py reuses envNumber, skipRamCheck and availableMemoryGB.
"""
import functools
import os
import subprocess
import time
from typing import NamedTuple, Optional

PER_WORKER_GB = 0.75
RESERVE_GB = 2.5
MAX_WORKERS = 4
WAIT_POLL_SECONDS = 15
REFUSED_EXIT_CODE = 75


class Settings(NamedTuple):
    perWorkerGB: float
    reserveGB: float
    maxWorkers: int


class Decision(NamedTuple):
    run: bool
    workers: int
    neededGB: float


def envNumber(name: str, default: float, environ=os.environ) -> float:
    """The number in environment variable `name`, `default` when unset or
    blank; ValueError naming it when it is no number."""
    value = (environ.get(name) or '').strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f'{name} must be a number, not {value!r}') from None


def skipRamCheck(environ=os.environ) -> bool:
    return (environ.get('PYTEST_SKIP_RAM_CHECK') or '').strip() == '1'


def settings(defaultPerWorkerGB: float = PER_WORKER_GB) -> Settings:
    perWorkerGB = envNumber('PYTEST_WORKER_GB', defaultPerWorkerGB)
    if perWorkerGB <= 0:
        raise ValueError('PYTEST_WORKER_GB must be above 0')
    return Settings(perWorkerGB,
                    envNumber('PYTEST_RAM_RESERVE_GB', RESERVE_GB),
                    max(1, int(envNumber('PYTEST_MAX_WORKERS', MAX_WORKERS))))


def neededGB(workers: int, perWorkerGB: float, reserveGB: float) -> float:
    return workers * perWorkerGB + reserveGB


def decide(availableGB: float, workers: Optional[int], perWorkerGB: float,
           reserveGB: float, maxWorkers: int = MAX_WORKERS,
           cpuCount: int = 2) -> Decision:
    """Whether a run with `workers` workers (0 = serial, one process; None =
    `-n auto`) starts, and with how many. `-n auto` takes as many as fit,
    within maxWorkers and cpuCount - 1, and is refused only when not even
    one fits; a given count runs as given or not at all."""
    if workers is None:
        byMemory = int((availableGB - reserveGB) // perWorkerGB)
        workers = min(maxWorkers, cpuCount - 1, byMemory)
    workers = max(1, workers)  # a serial run is one process
    needed = neededGB(workers, perWorkerGB, reserveGB)
    return Decision(availableGB >= needed, workers, needed)


@functools.lru_cache(maxsize=None)
def _memoryStatusEx():
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD),
                    ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_uint64),
                    ('ullAvailPhys', ctypes.c_uint64),
                    ('ullTotalPageFile', ctypes.c_uint64),
                    ('ullAvailPageFile', ctypes.c_uint64),
                    ('ullTotalVirtual', ctypes.c_uint64),
                    ('ullAvailVirtual', ctypes.c_uint64),
                    ('ullAvailExtendedVirtual', ctypes.c_uint64)]

    return MemoryStatusEx


@functools.lru_cache(maxsize=None)
def _kernel32():
    """This module's own kernel32 (never ctypes.windll's shared one, whose
    argtypes other code may set), with the argtypes it calls."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_memoryStatusEx())]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    return kernel32


def availableMemoryGB() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 2**30
    except ImportError:
        pass
    if os.name == 'nt':
        import ctypes
        kernel32 = _kernel32()
        status = _memoryStatusEx()()
        status.dwLength = ctypes.sizeof(status)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.ullAvailPhys / 2**30
    with open('/proc/meminfo', encoding='utf-8') as meminfo:
        for line in meminfo:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 2**20
    raise RuntimeError('cannot read the available memory')


def _processTable() -> list:
    """[(pid, parentPid, commandLine)] of every process, or [] when it cannot
    be listed."""
    try:
        if os.name == 'nt':
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 'Get-CimInstance Win32_Process | ForEach-Object { '
                 '"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)" }'],
                capture_output=True, text=True, timeout=30).stdout
        else:
            out = subprocess.run(['ps', '-eo', 'pid=,ppid=,args='],
                                 capture_output=True, text=True,
                                 timeout=30).stdout
            out = '\n'.join('\t'.join(line.split(None, 2))
                            for line in out.splitlines())
    except (OSError, subprocess.SubprocessError):
        return []
    table = []
    for line in out.splitlines():
        parts = line.strip().split('\t', 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table.append((int(parts[0]), int(parts[1]), parts[2]))
    return table


def otherTestProcesses(table: Optional[list] = None) -> list:
    """[(pid, short command line)] of the pytest and pubSubMockDataGenerator
    processes running besides this run (its own process, the ones it was
    started by and the ones it started are left out). A process tree reads
    as one entry: its root."""
    table = _processTable() if table is None else table
    parents = {pid: parent for pid, parent, _ in table}
    ours = set()
    pid = os.getpid()
    while pid in parents and pid not in ours:  # this process and its starters
        ours.add(pid)
        pid = parents[pid]
    ours.add(os.getpid())
    matching = {
        pid: commandLine for pid, _, commandLine in table
        if 'pytest' in commandLine or 'pubSubMockDataGenerator' in commandLine
    }

    def descendsFrom(pid, roots) -> bool:
        seen = set()
        while pid in parents and pid not in seen:
            seen.add(pid)
            pid = parents[pid]
            if pid in roots:
                return True
        return False

    others = []
    for pid, commandLine in sorted(matching.items()):
        if pid in ours or descendsFrom(pid, ours):
            continue
        if descendsFrom(pid, matching):  # a worker or a launcher's child
            continue
        short = ' '.join(commandLine.split())
        others.append((pid, short if len(short) <= 120 else short[:117] + '...'))
    return others


def refusal(availableGB: float, decision: Decision,
            others: Optional[list] = None) -> str:
    workers = decision.workers
    message = (
        f'Not enough RAM to run tests: {availableGB:.1f} GB available, '
        f'{decision.neededGB:.1f} GB needed ({workers} '
        f'worker{"" if workers == 1 else "s"}). Another test run or app is '
        'probably using it: wait for it to finish or free RAM (/free-ram). '
        'Override: PYTEST_SKIP_RAM_CHECK=1.')
    if others:
        message += '\nOther test processes running:' + ''.join(
            f'\n  PID {pid}: {commandLine}' for pid, commandLine in others)
    return message


def gate(workers: Optional[int], cpuCount: int,
         defaultPerWorkerGB: float = PER_WORKER_GB, sleep=time.sleep,
         available=availableMemoryGB) -> tuple:
    """(decision, availableGB, settings) for a run of `workers` workers (0 =
    serial, None = `-n auto`), waiting up to PYTEST_WAIT_FOR_RAM seconds for
    the memory. decision.run is False when the run must be refused;
    PYTEST_SKIP_RAM_CHECK=1 makes it True."""
    config = settings(defaultPerWorkerGB)
    waitSeconds = envNumber('PYTEST_WAIT_FOR_RAM', 0)
    skip = skipRamCheck()
    polls = int(waitSeconds // WAIT_POLL_SECONDS)
    for poll in range(polls + 1):
        if poll:
            sleep(WAIT_POLL_SECONDS)
        availableGB = available()
        decision = decide(availableGB, workers, *config, cpuCount=cpuCount)
        if decision.run or skip:
            break
    return decision._replace(run=decision.run or skip), availableGB, config
