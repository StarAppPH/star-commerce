"""Refuse to generate when a services/*.py dev= URL names the wrong local port.

Every services/<name>.py hard-codes a dev= URL for a sibling repo. In a local
run that URL is what every PUBSUB_ENABLED-gated .request() call hits, so a wrong
dev port makes each of those calls fail with a connection error, or reach
whatever unrelated app is bound on that port, and nothing else says so.

AppCreator.py calls assertServiceDevPorts() on the full-generation path, before
any route, schema or client file is rewritten (only serverConstants.ts, which
generateSharedConstants() emits one line earlier, precedes it). Each service
names its sibling checkout folder through `localRepoDirName`; the sibling is
expected next to this repo under the shared parent directory, and its declared
port is read straight from the `PORT: int = ...` line of its
server/AppCreator.py, the single source of truth for the port that repo's
generated app.py binds.

Pure stdlib on purpose: it must import without the app, its env vars or Mongo,
and it is unit-tested against a temp directory in test_servicePortCheck.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

PORT_DECLARATION = re.compile(r'^PORT:\s*int\s*=\s*(\d+)', re.MULTILINE)
APP_CREATOR_RELATIVE_PATH = Path('server') / 'AppCreator.py'
DRIFT_HEADER = "Service dev-port drift (fix services/*.py dev= or the sibling's AppCreator PORT):"


def parseDevPort(devUrl: str | None) -> int | None:
    """Explicit port of a dev URL, or None when there is none (or the URL is unparsable)."""
    if not devUrl:
        return None
    try:
        return urlsplit(devUrl).port
    except ValueError:
        return None


def readDeclaredPort(appCreatorPath: Path) -> int | None:
    """The `PORT: int = <n>` value declared in a sibling's server/AppCreator.py, or None."""
    match = PORT_DECLARATION.search(appCreatorPath.read_text(encoding='utf-8'))
    return int(match.group(1)) if match else None


def checkServiceDevPorts(services, projectRoot: Path) -> tuple[list[str], list[str]]:
    """Compare each service's dev= port with its sibling repo's declared PORT.

    Returns (warnings, mismatches). A warning means the port could not be
    verified (sibling checkout absent, no AppCreator.py, no PORT line, no dev
    URL) and generation may proceed. A mismatch means the dev URL definitely
    does not reach the sibling's local server and generation must stop.
    """
    warnings: list[str] = []
    mismatches: list[str] = []
    for service in services:
        appCreatorDisplay = f'{service.localRepoDirName}/{APP_CREATOR_RELATIVE_PATH.as_posix()}'
        siblingRoot = projectRoot.parent / service.localRepoDirName
        appCreatorPath = siblingRoot / APP_CREATOR_RELATIVE_PATH
        if not siblingRoot.is_dir():
            warnings.append(
                f'{service.name}: sibling checkout not found at {siblingRoot}; dev port not verified')
            continue
        if not appCreatorPath.is_file():
            warnings.append(
                f'{service.name}: {appCreatorDisplay} not found; dev port not verified')
            continue
        declaredPort = readDeclaredPort(appCreatorPath)
        if declaredPort is None:
            warnings.append(
                f'{service.name}: {appCreatorDisplay} has no `PORT: int = ...` line; dev port not verified')
            continue
        devUrl = service.path.dev
        if devUrl is None:
            warnings.append(f'{service.name}: no dev URL configured; nothing to verify')
            continue
        devPort = parseDevPort(devUrl)
        if devPort is None:
            mismatches.append(
                f'{service.name:<28} dev={devUrl}  has no explicit port, '
                f'but {appCreatorDisplay} PORT = {declaredPort}')
        elif devPort != declaredPort:
            mismatches.append(
                f'{service.name:<28} dev={devUrl}  but {appCreatorDisplay} PORT = {declaredPort}')
    return warnings, mismatches


def assertServiceDevPorts(services, projectRoot: Path) -> None:
    """Print warnings; exit non-zero with a drift table when any dev port is wrong."""
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    for line in warnings:
        print(f'WARNING: {line}')
    if mismatches:
        table = '\n'.join(f'  {line}' for line in mismatches)
        sys.exit(f'{DRIFT_HEADER}\n{table}')
