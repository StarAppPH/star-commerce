"""The client type-checks: `tsc --noEmit` reports no error.

A server change the client still depends on (a removed or renamed response
field, a changed route signature) shows up here as a tsc error once AppCreator
has regenerated the client types (run it after a model change: no test checks
that the committed ones are current). There is no baseline: every error fails
the test, listed as file:line:col, code and message.

A green run does not prove a removed field is safe: a cast into a client
interface, a string column name, JSON stored in the browser and a Python
caller all get past tsc. The unused-response-field gate, where a repo has one,
covers those.

The client project is the folder holding tsconfig.json at or above
AppCreator.CLIENT_SCHEMAS_DIR (clientNode.client_project_dir). On Next.js 15.5
and later, `next typegen` runs first, so route types are current (it writes
only the gitignored next-env.d.ts and .next/types). Both commands run through
`npx --no --`: without the `--`, npx would take tsc's `-p` as its own
--package option and never run tsc.

Needs node and the client's node_modules (typescript, and next when
package.json depends on it); it fails, never skips, without them. Touches no
database.
"""
import json
import re
from pathlib import Path

import pytest

from clientNode import capped, client_project_dir, install_hint, require_npx, run

TYPEGEN_TIMEOUT_SECONDS = 300
TSC_TIMEOUT_SECONDS = 900
MAX_LISTED_ERRORS = 50
TYPEGEN_SINCE = (15, 5)
# `file(line,col): error TSnnnn: message`, and the --pretty form
# `file:line:col - error TSnnnn: message` should a tsconfig turn it on.
TSC_ERROR = re.compile(r'^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<message>.*)')
TSC_PRETTY_ERROR = re.compile(r'^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+) - error (?P<code>TS\d+): (?P<message>.*)')


def depends_on_next(client: Path) -> bool:
    manifest = json.loads((client / 'package.json').read_text(encoding='utf-8'))
    return 'next' in {**manifest.get('dependencies', {}), **manifest.get('devDependencies', {})}


def next_version(client: Path) -> tuple[int, ...]:
    """The installed next's (major, minor)."""
    manifest = client / 'node_modules' / 'next' / 'package.json'
    version = json.loads(manifest.read_text(encoding='utf-8'))['version']
    return tuple(int(part) for part in re.findall(r'\d+', version)[:2])


def tsc_errors(output: str) -> list[str]:
    """`file:line:col CODE message` for every tsc error, plus any error line
    tsc prints without a location (`error TS5083: Cannot read file ...`)."""
    errors = []
    for raw in output.splitlines():
        line = raw.strip()
        match = TSC_ERROR.match(line) or TSC_PRETTY_ERROR.match(line)
        if match:
            errors.append(f"{match['file']}:{match['line']}:{match['col']} {match['code']} {match['message']}")
        elif re.search(r'\berror TS\d+', line):
            errors.append(line)
    return errors


def tsc_verdict(returncode: int, errors: list[str], output: str, client: Path) -> str | None:
    """The failure message for a tsc run, or None when it passed: exit 0 with
    no error line. An error line fails even on exit 0; a non-zero exit fails
    even without one."""
    if returncode == 0 and not errors:
        return None
    if not errors:
        return (f'tsc exited {returncode} without an error line ({install_hint(client)} if a '
                f'module is missing):\n{output[-3000:]}')
    return '\n'.join([f'tsc found {len(errors)} error(s) in {client.name}; fix each one '
                      '(there is no baseline):']
                     + [f'  {e}' for e in capped(errors, MAX_LISTED_ERRORS)])


def test_clientTypeChecks():
    client = client_project_dir()
    usesNext = depends_on_next(client)
    npx = require_npx(client, ('typescript', 'next') if usesNext else ('typescript',))
    if usesNext and next_version(client) >= TYPEGEN_SINCE:
        typegen = run([npx, '--no', '--', 'next', 'typegen'], client, TYPEGEN_TIMEOUT_SECONDS,
                      install_hint(client))
        if typegen.returncode != 0:
            pytest.fail(f'`next typegen` failed in {client.name}:\n'
                        f'{(typegen.stdout + typegen.stderr)[-3000:]}', pytrace=False)

    tsc = run([npx, '--no', '--', 'tsc', '--noEmit', '--pretty', 'false', '--project', 'tsconfig.json'],
              client, TSC_TIMEOUT_SECONDS, install_hint(client))
    output = tsc.stdout + tsc.stderr
    failure = tsc_verdict(tsc.returncode, tsc_errors(output), output, client)
    if failure:
        pytest.fail(failure, pytrace=False)


# ---- Self-test on canned tsc output -------------------------------------------

CANNED = '\n'.join([
    "app/page.tsx(12,7): error TS2339: Property 'x' does not exist on type 'Y'.",
    "error TS5083: Cannot read file 'missing/tsconfig.json'.",
    "lib/util.ts:3:9 - error TS2322: Type 'string' is not assignable to type 'number'.",
    "Found 3 errors in 2 files.",
])


def test_tscErrorsParsesEveryForm():
    assert tsc_errors(CANNED) == [
        "app/page.tsx:12:7 TS2339 Property 'x' does not exist on type 'Y'.",
        "error TS5083: Cannot read file 'missing/tsconfig.json'.",
        "lib/util.ts:3:9 TS2322 Type 'string' is not assignable to type 'number'.",
    ]


def test_tscVerdict():
    client = client_project_dir()
    assert tsc_verdict(0, [], '', client) is None
    withErrors = tsc_verdict(0, ['a.ts:1:1 TS1 x'], '', client)
    assert withErrors is not None and 'tsc found 1 error(s)' in withErrors and 'a.ts:1:1 TS1 x' in withErrors
    noErrorLine = tsc_verdict(2, [], 'something broke', client)
    assert noErrorLine is not None and 'exited 2' in noErrorLine and 'something broke' in noErrorLine
    many = tsc_verdict(1, [f'f.ts:{i}:1 TS1 x' for i in range(MAX_LISTED_ERRORS + 5)], '', client)
    assert many is not None and many.endswith('... and 5 more')
