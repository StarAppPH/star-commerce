"""conftest's pytest-xdist support and the RAM gate: every worker runs
against its own scratch database, `-n auto` sizes the worker count by the
memory available, and a run whose workers do not fit is refused."""
import os
import re
import subprocess
import sys

import pytest

import testRamGate
from testRamGate import Decision, decide, gate, otherTestProcesses, refusal

_KNOBS = {
    'PYTEST_WORKER_GB': '1',
    'PYTEST_RAM_RESERVE_GB': '2.5',
    'PYTEST_MAX_WORKERS': '4',
}
_SERVER = os.path.dirname(os.path.abspath(__file__))
# This run's scratch database without a worker id (testecommerceScratch) and
# its name before 'Scratch' (testecommerce): the same file serves every repo.
_SCRATCH = re.sub(r'_gw\d+Scratch$', 'Scratch', os.environ['databaseName'])
_STEM = _SCRATCH[:-len('Scratch')]


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    """1 GB a worker, 2.5 GB kept free, at most 4 workers, no waiting: the
    tests do not depend on the measured defaults."""
    for name, value in _KNOBS.items():
        monkeypatch.setenv(name, value)
    for name in ('PYTEST_WAIT_FOR_RAM', 'PYTEST_SKIP_RAM_CHECK'):
        monkeypatch.delenv(name, raising=False)


def _run(args: list, **env) -> subprocess.CompletedProcess:
    """`python <args>` with `env` over this process's environment, as a
    controller (not an xdist worker) unless `env` names a worker, that
    starts no test server."""
    childEnv = dict(os.environ, PYTEST_NO_AUTOSTART_SERVICES='1')
    childEnv.pop('PYTEST_XDIST_WORKER', None)
    return subprocess.run([sys.executable, *args],
                          cwd=_SERVER,
                          env=dict(childEnv, **env),
                          capture_output=True,
                          text=True,
                          timeout=120)


def _databaseNameUnder(**env) -> subprocess.CompletedProcess:
    """Import conftest in a fresh interpreter with `env` and print the
    database the mongoDb singleton opened."""
    return _run(['-c', 'import conftest, mongoDb; print(mongoDb.db.databaseName)'],
                **env)


# --- one database per worker -------------------------------------------------


def test_aWorkerGetsItsOwnScratchDatabase():
    result = _databaseNameUnder(PYTEST_XDIST_WORKER='gw3',
                                databaseName=_SCRATCH)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == f'{_STEM}_gw3Scratch'


def test_theWorkerIdGoesInOnce():
    """A subprocess a worker starts inherits the renamed database."""
    result = _databaseNameUnder(PYTEST_XDIST_WORKER='gw3',
                                databaseName=f'{_STEM}_gw3Scratch')

    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == f'{_STEM}_gw3Scratch'


def test_aWorkerStillRefusesANonScratchDatabase():
    result = _databaseNameUnder(PYTEST_XDIST_WORKER='gw0',
                                databaseName=_STEM)

    assert result.returncode != 0
    assert 'scratch database' in result.stderr
    assert 'MongoClient' not in result.stderr


# --- the decision ------------------------------------------------------------


def test_decide_serialRunsWhenOneWorkerFits():
    assert decide(3.6, 0, 1.0, 2.5) == Decision(True, 1, 3.5)


def test_decide_serialRefusedWhenOneWorkerDoesNotFit():
    assert decide(3.4, 0, 1.0, 2.5) == Decision(False, 1, 3.5)


def test_decide_aGivenCountRunsAsGivenOrNotAtAll():
    assert decide(5.0, 2, 1.0, 2.5) == Decision(True, 2, 4.5)
    assert decide(5.0, 3, 1.0, 2.5) == Decision(False, 3, 5.5)


def test_decide_autoTakesAsManyAsFit():
    # (5.0 - 2.5) // 1.0 = 2 fit
    assert decide(5.0, None, 1.0, 2.5, maxWorkers=4, cpuCount=22) == Decision(True, 2, 4.5)


def test_decide_autoCappedByMaxWorkersAndCpus():
    assert decide(64.0, None, 1.0, 2.5, maxWorkers=4, cpuCount=22).workers == 4
    assert decide(64.0, None, 1.0, 2.5, maxWorkers=4, cpuCount=3).workers == 2
    assert decide(64.0, None, 1.0, 2.5, maxWorkers=4, cpuCount=1) == Decision(True, 1, 3.5)


def test_decide_autoRefusedOnlyWhenNotEvenOneFits():
    assert decide(3.0, None, 1.0, 2.5, maxWorkers=4, cpuCount=22) == Decision(False, 1, 3.5)


# --- the knobs ---------------------------------------------------------------


def test_settings_readTheKnobs(monkeypatch):
    monkeypatch.setenv('PYTEST_WORKER_GB', '0.5')
    monkeypatch.setenv('PYTEST_RAM_RESERVE_GB', '1')
    monkeypatch.setenv('PYTEST_MAX_WORKERS', '8')

    assert testRamGate.settings() == (0.5, 1.0, 8)


def test_settings_aRepoDefaultGivesWayToTheKnob(monkeypatch):
    monkeypatch.delenv('PYTEST_WORKER_GB')
    assert testRamGate.settings(1.25).perWorkerGB == 1.25
    monkeypatch.setenv('PYTEST_WORKER_GB', '0.5')
    assert testRamGate.settings(1.25).perWorkerGB == 0.5


def test_settings_defaultsWhenUnsetOrBlank(monkeypatch):
    monkeypatch.delenv('PYTEST_WORKER_GB')
    monkeypatch.setenv('PYTEST_RAM_RESERVE_GB', ' ')
    monkeypatch.delenv('PYTEST_MAX_WORKERS')

    assert testRamGate.settings() == (testRamGate.PER_WORKER_GB,
                                      testRamGate.RESERVE_GB,
                                      testRamGate.MAX_WORKERS)


@pytest.mark.parametrize('value', ['0', '-1', 'lots'])
def test_settings_refuseABadPerWorkerSize(monkeypatch, value):
    monkeypatch.setenv('PYTEST_WORKER_GB', value)

    with pytest.raises(ValueError, match='PYTEST_WORKER_GB'):
        testRamGate.settings()


def test_gate_refusesAtOnceByDefault():
    sleeps = []

    decision, availableGB, _ = gate(0, 22, sleep=sleeps.append, available=lambda: 3.0)

    assert decision == Decision(False, 1, 3.5)
    assert availableGB == 3.0
    assert sleeps == []


def test_gate_waitsForTheMemory(monkeypatch):
    monkeypatch.setenv('PYTEST_WAIT_FOR_RAM', '60')
    readings, sleeps = iter([3.0, 3.0, 4.0]), []

    decision, availableGB, _ = gate(0, 22, sleep=sleeps.append,
                                    available=lambda: next(readings))

    assert decision.run
    assert availableGB == 4.0
    assert sleeps == [15, 15]


def test_gate_givesUpAfterTheWait(monkeypatch):
    monkeypatch.setenv('PYTEST_WAIT_FOR_RAM', '30')
    sleeps = []

    decision, _, _ = gate(0, 22, sleep=sleeps.append, available=lambda: 3.0)

    assert not decision.run
    assert sleeps == [15, 15]


def test_gate_skipRamCheckRunsAnyway(monkeypatch):
    monkeypatch.setenv('PYTEST_SKIP_RAM_CHECK', '1')

    decision, _, _ = gate(2, 22, available=lambda: 1.0)

    assert decision == Decision(True, 2, 4.5)


def test_availableMemoryIsReadable():
    assert testRamGate.availableMemoryGB() > 0


# --- the refusal -------------------------------------------------------------


def test_refusal_namesTheNumbersAndTheOverride():
    message = refusal(3.04, Decision(False, 2, 4.5))

    assert message == (
        'Not enough RAM to run tests: 3.0 GB available, 4.5 GB needed '
        '(2 workers). Another test run or app is probably using it: wait for '
        'it to finish or free RAM (/free-ram). Override: PYTEST_SKIP_RAM_CHECK=1.')


def test_refusal_listsTheOtherTestProcesses():
    message = refusal(3.0, Decision(False, 1, 3.5),
                      [(41, 'python -m pytest -n 2'), (42, 'python pubSubMockDataGenerator.py')])

    assert '(1 worker).' in message
    assert message.endswith('\nOther test processes running:'
                            '\n  PID 41: python -m pytest -n 2'
                            '\n  PID 42: python pubSubMockDataGenerator.py')


def test_otherTestProcesses_leavesOutThisRunAndWorkers(monkeypatch):
    monkeypatch.setattr(os, 'getpid', lambda: 11)
    table = [
        (10, 1, 'venv\\Scripts\\python.exe -m pytest -q'),  # our launcher
        (11, 10, 'python.exe -m pytest -q'),  # us
        (20, 1, 'venv\\Scripts\\python.exe -m pytest -n 2'),  # another run
        (21, 20, 'python.exe -m pytest -n 2'),  # its launcher's child
        (22, 21, 'python.exe -c "import sys;exec(eval(sys.stdin.readline()))" pytest'),
        (30, 1, 'python   pubSubMockDataGenerator.py'),
        (40, 1, 'python app.py'),
    ]

    assert otherTestProcesses(table) == [
        (20, 'venv\\Scripts\\python.exe -m pytest -n 2'),
        (30, 'python pubSubMockDataGenerator.py'),
    ]


# --- the gate in a real run ---------------------------------------------------


def _pytest(tmp_path, *args, **env) -> subprocess.CompletedProcess:
    """A nested pytest run whose temp files (test-server records and locks)
    stay in tmp_path."""
    return _run(['-m', 'pytest', '-p', 'no:cacheprovider', '-q', *args],
                TMP=str(tmp_path), TEMP=str(tmp_path), **env)


def test_aRunIsRefusedBeforeAnyTestWhenTheRamIsShort(tmp_path):
    result = _pytest(tmp_path, 'test_xdist_workers.py::test_availableMemoryIsReadable',
                     PYTEST_RAM_RESERVE_GB='100000')

    assert result.returncode == testRamGate.REFUSED_EXIT_CODE == 75
    assert 'Not enough RAM to run tests' in result.stdout + result.stderr
    assert 'passed' not in result.stdout


def test_aRunInsideAWorkerIsNotGated(tmp_path):
    result = _pytest(tmp_path, 'test_xdist_workers.py::test_availableMemoryIsReadable',
                     PYTEST_RAM_RESERVE_GB='100000', PYTEST_XDIST_WORKER='gw0',
                     databaseName=_SCRATCH)

    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


@pytest.mark.parametrize('option', ['--collect-only', '--markers', '--fixtures'])
def test_anInformationRunIsNotGated(tmp_path, option):
    result = _pytest(tmp_path, option, 'test_xdist_workers.py',
                     PYTEST_RAM_RESERVE_GB='100000')

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Not enough RAM' not in result.stdout + result.stderr
