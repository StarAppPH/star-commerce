"""Running the client's node tooling from pytest.

The client gates (test_client_typecheck.py, and any repo test that runs a
client script) share this: where the client project is, the node/npx checks
with the "npm install" hint they fail with (never skip), running a command
with a timeout, and capping a long list of findings.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

import AppCreator


def client_project_dir() -> Path:
    """The folder with the client's tsconfig.json, at or above the schemas
    (client/, or client/salesapp/ where the package sits one level down)."""
    clientRoot = AppCreator.CLIENT_DIR.resolve()
    directory = AppCreator.CLIENT_SCHEMAS_DIR.resolve()
    while not (directory / 'tsconfig.json').is_file():
        if directory == clientRoot or directory.parent == directory:
            pytest.fail(f'no tsconfig.json at or above {AppCreator.CLIENT_SCHEMAS_DIR} '
                        f'within {AppCreator.CLIENT_DIR}')
        directory = directory.parent
    return directory


def install_hint(client: Path) -> str:
    rel = client.relative_to(AppCreator.PROJECT_ROOT.resolve()).as_posix()
    return f'run `cd {rel} && npm install`'


def require_npx(client: Path, packages: tuple[str, ...]) -> str:
    """npx's path, once node, npx and each of `packages` in the client's
    node_modules are there; fails with the install hint otherwise."""
    npx = shutil.which('npx')
    if npx is None or shutil.which('node') is None:
        pytest.fail(f'node/npx is not on PATH: install Node.js, then {install_hint(client)}.',
                    pytrace=False)
    for package in packages:
        if not (client / 'node_modules' / package).is_dir():
            pytest.fail(f'{client.name}/node_modules/{package} is missing: {install_hint(client)}.',
                        pytrace=False)
    return npx


def run(command: list[str], cwd: Path, timeout: int, hint: str = '') -> subprocess.CompletedProcess:
    """`command` in `cwd`, output captured as text; a timeout fails the test."""
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f'`{" ".join(command[1:])}` did not finish in {timeout}s'
                    + (f'; {hint}' if hint else '') + '.', pytrace=False)


def capped(lines: list[str], limit: int) -> list[str]:
    """The first `limit` lines, then '... and N more' when some are left out."""
    return lines[:limit] + ([f'... and {len(lines) - limit} more'] if len(lines) > limit else [])
