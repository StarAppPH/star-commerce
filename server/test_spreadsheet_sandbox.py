"""spreadsheetSandbox.py: each conversion in a child process
(spreadsheetWorker.py), its outcomes mapped to spreadsheetText's: the text,
a refusal in its words, TOO_LARGE for a child that crashed, was killed,
wrote too much or wrote garbage, OutOfTime (the child killed) past the
caller's time.

Pure: no database, no Storage. The misbehaving children are small scripts
written here, run in the worker's place."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

import spreadsheetSandbox
import spreadsheetText
from spreadsheetText import (CSV_CONTENT_TYPE, NOT_TEXT, TOO_LARGE,
                             UNREADABLE, XLSX_CONTENT_TYPE)
from test_spreadsheet_text import (deepNestingXlsx, inline, number,
                                   sheetXlsx)

LABEL = 'attachments[1]'
SECONDS = 30


def convert(data: bytes, contentType: str, maxChars: int = 100_000,
            seconds: float = SECONDS):
    return spreadsheetSandbox.convert(data, contentType, LABEL, maxChars,
                                      seconds)


@pytest.fixture
def children(monkeypatch):
    """Every child started, to check it ended."""
    started = []
    real = subprocess.Popen

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)
    monkeypatch.setattr(spreadsheetSandbox.subprocess, 'Popen', Recorded)
    yield started
    for child in started:
        assert child.poll() is not None, 'a child still runs'


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Puts a script in the worker's place: worker('<python source>')."""
    def use(source: str) -> None:
        path = tmp_path / 'fakeWorker.py'
        path.write_text(textwrap.dedent(source), encoding='utf-8')
        monkeypatch.setattr(spreadsheetSandbox, 'WORKER', str(path))
    return use


@pytest.mark.parametrize('data, contentType', [
    (b'name,price\r\nMug,"2,50"\r\n', CSV_CONTENT_TYPE),
    (sheetXlsx({'S': '<row r="1">' + inline('A1', 'café \U0001F600') +
                number('B1', 2.5) + '</row>'}), XLSX_CONTENT_TYPE),
    (b'a,b\n' * 50_000, CSV_CONTENT_TYPE),
], ids=['csv', 'xlsx', 'cut'])
def test_aFile_readsInTheChild_asInTheAppsProcess(data, contentType,
                                                  children):
    inProcess = spreadsheetText.spreadsheetText(data, contentType, LABEL,
                                                100_000)

    assert convert(data, contentType) == inProcess
    assert len(children) == 1
    assert children[0].args[1:] == ['-I', spreadsheetSandbox.WORKER]


@pytest.mark.parametrize('data, contentType, words', [
    (b'', CSV_CONTENT_TYPE, NOT_TEXT),
    (b'PK\x03\x04 broken', XLSX_CONTENT_TYPE, UNREADABLE),
    (b'%PDF-', 'application/pdf', UNREADABLE),
], ids=['notText', 'unreadable', 'otherType'])
def test_aRefusal_inTheChild_isTheSameRefusal_namingTheLabel(
        data, contentType, words, children):
    with pytest.raises(spreadsheetText.Refused) as error:
        convert(data, contentType)

    assert str(error.value) == f'{LABEL} {words}'


def test_theChild_getsNoSecretFromTheEnvironment(worker, monkeypatch):
    monkeypatch.setenv('MONGO_URI', 'mongodb://secret')
    worker('''
        import json, os, sys
        sys.stdin.buffer.read()
        print(json.dumps({'text': ','.join(sorted(os.environ)),
                          'cutBy': None}))
    ''')

    names = convert(b'a\n', CSV_CONTENT_TYPE).text.split(',')

    assert 'MONGO_URI' not in names
    assert set(names) <= set(spreadsheetSandbox._ENVIRONMENT)


@pytest.mark.parametrize('source', [
    # Killed by a limit, crashed, out of memory: a non-zero exit.
    'import os, sys; sys.stdin.buffer.read(); os._exit(137)',
    'raise MemoryError()',
    # Nothing at all.
    'import sys; sys.stdin.buffer.read()',
], ids=['killed', 'memoryError', 'silent'])
def test_aChildThatEndsWithoutAnswering_isTooLarge_loggedAsExited(
        source, worker, children, caplog):
    """A broken sandbox (every child failing to run the worker) shows as
    this event, apart from a file the worker could not answer for."""
    worker(source)

    with pytest.raises(spreadsheetText.Refused) as error:
        convert(b'a,b\n', CSV_CONTENT_TYPE)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'
    [record] = [r for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('event', '').startswith('spreadsheetWorker')]
    assert record.msg['event'] == 'spreadsheetWorkerExited'
    assert set(record.msg) == {'event', 'returncode'}


@pytest.mark.parametrize('source', [
    # Garbage, or an answer that is not one.
    'import sys; sys.stdin.buffer.read(); print("not json")',
    'import sys; sys.stdin.buffer.read(); print("[1, 2]")',
    'import sys; sys.stdin.buffer.read(); print(\'{"text": 3, "cutBy": null}\')',
    'import sys; sys.stdin.buffer.read(); print(\'{"text": "", "cutBy": "x"}\')',
    'import sys; sys.stdin.buffer.read(); print(\'{"refused": "words"}\')',
], ids=['notJson', 'notAnObject', 'textNotStr', 'unknownCut',
        'unknownReason'])
def test_aChildThatFails_isTooLarge_withItsExitCodeLoggedOnly(
        source, worker, children, caplog):
    worker(source)

    with pytest.raises(spreadsheetText.Refused) as error:
        convert(b'a,b\n', CSV_CONTENT_TYPE)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'
    [record] = [r for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('event') == 'spreadsheetWorkerFailed']
    assert set(record.msg) == {'event', 'returncode'}


def test_aChildPastTheCallersTime_isKilled_andIsOutOfTime(worker, children):
    worker('import sys, time; sys.stdin.buffer.read(); time.sleep(60)')
    started = time.monotonic()

    with pytest.raises(spreadsheetText.OutOfTime):
        convert(b'a,b\n', CSV_CONTENT_TYPE, seconds=1)

    assert time.monotonic() - started < 10
    assert children[0].returncode is not None


def test_aChildThatReadsNothing_andHangs_isStillKilled(worker, children):
    """The file is written from a thread: a child that never reads it
    cannot block the parent."""
    worker('import time; time.sleep(60)')

    with pytest.raises(spreadsheetText.OutOfTime):
        convert(b'x' * (8 * 1024 * 1024), CSV_CONTENT_TYPE, seconds=1)


def test_theChildsOwnOutOfTime_isOutOfTime(worker, children):
    worker('import sys; sys.stdin.buffer.read(); '
           'print(\'{"outOfTime": true}\')')

    with pytest.raises(spreadsheetText.OutOfTime):
        convert(b'a,b\n', CSV_CONTENT_TYPE)


def test_aChildThatCannotStart_isOutOfTime_withItsErrnoLoggedOnly(
        monkeypatch, caplog):
    def cannotStart(*args, **kwargs):
        raise OSError(12, 'Cannot allocate memory', '/secret/path')
    monkeypatch.setattr(spreadsheetSandbox.subprocess, 'Popen', cannotStart)

    with pytest.raises(spreadsheetText.OutOfTime):
        convert(b'a,b\n', CSV_CONTENT_TYPE)

    [record] = [r for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('event') == 'spreadsheetWorkerFailed']
    assert record.msg == {'event': 'spreadsheetWorkerFailed', 'errno': 12}


def test_noTimeLeft_startsNoChild(children):
    with pytest.raises(spreadsheetText.OutOfTime):
        convert(b'a,b\n', CSV_CONTENT_TYPE, seconds=0)

    assert children == []


def test_anAnswerPastItsByteLimit_isCut_andTheChildKilled(worker, children,
                                                          caplog):
    limit = spreadsheetSandbox.outputMaxBytes(10)
    worker(f'''
        import sys, time
        sys.stdin.buffer.read()
        sys.stdout.buffer.write(b'x' * {limit + 1}); sys.stdout.flush()
        time.sleep(60)
    ''')
    started = time.monotonic()

    with pytest.raises(spreadsheetText.Refused) as error:
        convert(b'a,b\n', CSV_CONTENT_TYPE, maxChars=10)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'
    assert time.monotonic() - started < 10


def test_theWorker_answersEveryKindOfText_withinItsByteLimit():
    """Control characters are the worst case of JSON (6 bytes each)."""
    text = '\x01' * 1000
    data = (text + '\n').encode()

    sheet = convert(data, CSV_CONTENT_TYPE, maxChars=2000)

    assert sheet.text == text + '\n'


def test_theWorker_isAScriptTheImageShips():
    """server/ is copied whole (Dockerfile); .dockerignore keeps out only
    local files."""
    here = os.path.dirname(os.path.abspath(__file__))
    assert spreadsheetSandbox.WORKER == os.path.join(here,
                                                     'spreadsheetWorker.py')
    with open(os.path.join(here, '.dockerignore'), encoding='utf-8') as f:
        ignored = [line.strip() for line in f
                   if line.strip() and not line.startswith('#')]
    assert not any(pattern.startswith('spreadsheet') or pattern == '*.py'
                   for pattern in ignored)


@pytest.mark.skipif(sys.platform == 'win32',
                    reason='RLIMIT_AS is Linux only (no resource module)')
def test_onLinux_aChildPastItsMemory_isKilled_andTheFileTooLarge(
        worker, children):
    """The worker's own limit, past which the kernel refuses its memory."""
    server = os.path.dirname(os.path.abspath(__file__))
    worker(f'''
        import sys
        sys.path.insert(0, {server!r})
        import spreadsheetWorker
        spreadsheetWorker._limitSelf()
        sys.stdin.buffer.read()
        hog = bytearray(spreadsheetWorker.WORKER_MEMORY_HEADROOM_BYTES * 2)
        print('{{"text": "", "cutBy": null}}')
    ''')

    with pytest.raises(spreadsheetText.Refused) as error:
        convert(b'a,b\n', CSV_CONTENT_TYPE)

    assert str(error.value) == f'{LABEL} {TOO_LARGE}'


def test_aDeepFile_inTheChild_isCut_asInTheAppsProcess(children):
    data = deepNestingXlsx()

    assert convert(data, XLSX_CONTENT_TYPE).cutBy == 'cells'
