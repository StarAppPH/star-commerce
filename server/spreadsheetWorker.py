"""spreadsheetWorker.py — one spreadsheet conversion in a child process
(hand-written; started by spreadsheetSandbox.convert, never imported by
the app).

Run as `python -I spreadsheetWorker.py`. stdin: one JSON line
{"contentType", "maxChars", "seconds"}, then the file's bytes. stdout: one
JSON object, {"text", "cutBy"}, {"refused": <REASONS key>} or
{"outOfTime": true}. Anything else (a crash, a kill by a limit, garbage) is
the parent's to read as a refusal.

On Linux it first caps itself, after its imports and before it reads a
byte of the file: RLIMIT_AS at its address space then plus
WORKER_MEMORY_HEADROOM_BYTES (so the cap follows the interpreter, never a
guess of it), and RLIMIT_CPU at WORKER_CPU_SECONDS. Past either the kernel
stops it (a MemoryError or SIGXCPU) and the parent refuses the file. On
Windows (localdev) there is no `resource` module: the parent's wall clock
is the only bound.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spreadsheetText  # noqa: E402

# Measured (Windows, private bytes above an idle worker's): at most about
# 38 MiB, the file (10 MB, held once as read) and the text (100,000
# characters, once as JSON) included; the conversion's own peak is at most
# about 30 MiB (spreadsheetText's bounds). 64 MiB leaves about 26 MiB.
WORKER_MEMORY_HEADROOM_BYTES = 64 * 1024 * 1024
WORKER_CPU_SECONDS = int(spreadsheetText.SPREADSHEET_SECONDS_MAX) + 3
# What stdin may carry: the header line and a file of 10 MB.
INPUT_MAX_BYTES = 11 * 1024 * 1024
REASONS = {'unreadable': spreadsheetText.UNREADABLE,
           'notText': spreadsheetText.NOT_TEXT,
           'tooLarge': spreadsheetText.TOO_LARGE}


def _addressSpace() -> int:
    """This process's address space now, in bytes (Linux)."""
    with open('/proc/self/statm') as statm:
        return int(statm.read().split()[0]) * os.sysconf('SC_PAGE_SIZE')


def _limitSelf() -> None:
    try:
        import resource
    except ImportError:
        return  # Windows: the parent's wall clock only.
    memory = _addressSpace() + WORKER_MEMORY_HEADROOM_BYTES
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU,
                       (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS + 1))


def _reason(error: spreadsheetText.Refused) -> str:
    message = str(error)
    for key, text in REASONS.items():
        if message.endswith(text):
            return key
    return 'unreadable'


def _sheet(header: dict) -> str | None:
    """The one sheet whose rows are read, when the header names one."""
    sheet = header.get('sheet')
    return sheet if isinstance(sheet, str) else None


def main() -> None:
    _limitSelf()
    stdin = sys.stdin.buffer
    header = json.loads(stdin.readline(4096))
    data = stdin.read(INPUT_MAX_BYTES)
    try:
        sheet = spreadsheetText.spreadsheetText(
            data, header['contentType'], '', int(header['maxChars']),
            seconds=float(header['seconds']), sheet=_sheet(header))
        answer = {'text': sheet.text, 'cutBy': sheet.cutBy}
    except spreadsheetText.Refused as error:
        answer = {'refused': _reason(error)}
    except spreadsheetText.OutOfTime:
        answer = {'outOfTime': True}
    sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=False)
                            .encode('utf-8', 'surrogatepass'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
