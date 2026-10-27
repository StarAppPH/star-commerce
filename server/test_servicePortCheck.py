"""Unit tests for servicePortCheck.py. No database, no app imports.

The layout under tmp_path mirrors the real one: this repo and every sibling
checkout sit side by side under one parent directory, and each sibling
declares its port in server/AppCreator.py.
"""
from pathlib import Path

import pytest

from servicePortCheck import (
    DRIFT_HEADER,
    assertServiceDevPorts,
    checkServiceDevPorts,
    parseDevPort,
    readDeclaredPort,
)


class StubPath:

    def __init__(self, dev):
        self.dev = dev


class StubService:

    def __init__(self, name, localRepoDirName, dev):
        self.name = name
        self.localRepoDirName = localRepoDirName
        self.path = StubPath(dev)


def makeProjectRoot(tmp_path: Path) -> Path:
    projectRoot = tmp_path / 'thisRepo'
    (projectRoot / 'server').mkdir(parents=True)
    return projectRoot


def makeSibling(tmp_path: Path, dirName: str, port: int | None) -> Path:
    serverDir = tmp_path / dirName / 'server'
    serverDir.mkdir(parents=True)
    portLine = f'PORT: int = {port}\n' if port is not None else ''
    (serverDir / 'AppCreator.py').write_text(
        f"from pathlib import Path\n\n{portLine}PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent\n",
        encoding='utf-8')
    return serverDir / 'AppCreator.py'


def test_parseDevPort():
    assert parseDevPort('http://127.0.0.1:6000/') == 6000
    assert parseDevPort('http://localhost:7000') == 7000
    assert parseDevPort('http://127.0.0.1/') is None
    assert parseDevPort('http://127.0.0.1:notaport/') is None
    assert parseDevPort(None) is None
    assert parseDevPort('') is None


def test_readDeclaredPort(tmp_path):
    assert readDeclaredPort(makeSibling(tmp_path, 'withPort', 6000)) == 6000
    assert readDeclaredPort(makeSibling(tmp_path, 'withoutPort', None)) is None
    # A commented-out old port and an indented class attribute placed BEFORE the
    # real line must not win: the regex is anchored at column 0 and the first
    # match at column 0 is the declaration. CRLF bytes, as a Windows checkout has.
    (tmp_path / 'decoys.py').write_bytes(
        b'# PORT: int = 1234\r\nclass X:\r\n    PORT: int = 1234\r\nPORT: int = 6100  # local dev\r\n')
    assert readDeclaredPort(tmp_path / 'decoys.py') == 6100


def test_matchPasses(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', 7000)
    makeSibling(tmp_path, 'warehouse-management-service', 6000)
    services = [
        StubService('salesApp', 'SalesApp', 'http://127.0.0.1:7000/'),
        StubService('warehouseManagementService', 'warehouse-management-service', 'http://127.0.0.1:6000/'),
    ]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert warnings == []
    assert mismatches == []
    assertServiceDevPorts(services, projectRoot)  # must not raise


def test_mismatchDetected(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', 7000)
    makeSibling(tmp_path, 'warehouse-management-service', 6000)
    services = [
        StubService('salesApp', 'SalesApp', 'http://127.0.0.1:6000/'),
        StubService('warehouseManagementService', 'warehouse-management-service', 'http://127.0.0.1:6000/'),
    ]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert warnings == []
    assert len(mismatches) == 1
    assert mismatches[0].startswith('salesApp')
    assert 'dev=http://127.0.0.1:6000/' in mismatches[0]
    assert 'SalesApp/server/AppCreator.py PORT = 7000' in mismatches[0]


def test_missingSiblingWarnsNotFails(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    services = [StubService('salesApp', 'SalesApp', 'http://127.0.0.1:7000/')]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert mismatches == []
    assert len(warnings) == 1
    assert warnings[0].startswith('salesApp: sibling checkout not found')
    assertServiceDevPorts(services, projectRoot)  # warnings only: must not raise


def test_missingAppCreatorWarnsNotFails(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    (tmp_path / 'SalesApp').mkdir()
    services = [StubService('salesApp', 'SalesApp', 'http://127.0.0.1:7000/')]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert mismatches == []
    assert len(warnings) == 1
    assert 'SalesApp/server/AppCreator.py not found' in warnings[0]


def test_appCreatorWithoutPortLineWarnsNotFails(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', None)
    services = [StubService('salesApp', 'SalesApp', 'http://127.0.0.1:7000/')]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert mismatches == []
    assert len(warnings) == 1
    assert 'has no `PORT: int = ...` line' in warnings[0]


def test_devUrlWithoutPortIsMismatch(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', 7000)
    services = [StubService('salesApp', 'SalesApp', 'http://127.0.0.1/')]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert warnings == []
    assert len(mismatches) == 1
    assert 'has no explicit port' in mismatches[0]
    assert 'PORT = 7000' in mismatches[0]


def test_noDevUrlWarnsNotFails(tmp_path):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', 7000)
    services = [StubService('salesApp', 'SalesApp', None)]
    warnings, mismatches = checkServiceDevPorts(services, projectRoot)
    assert mismatches == []
    assert warnings == ['salesApp: no dev URL configured; nothing to verify']


def test_assertExitsOnMismatchWithDriftTable(tmp_path, capsys):
    projectRoot = makeProjectRoot(tmp_path)
    makeSibling(tmp_path, 'SalesApp', 7000)
    makeSibling(tmp_path, 'ecommerce', 8000)
    services = [
        StubService('salesApp', 'SalesApp', 'http://127.0.0.1:9999/'),
        StubService('ecommerceServiceV2', 'ecommerce', 'http://127.0.0.1:7001/'),
        StubService('ghost', 'missing-repo', 'http://127.0.0.1:1/'),
    ]
    with pytest.raises(SystemExit) as excInfo:
        assertServiceDevPorts(services, projectRoot)
    exitText = str(excInfo.value.code)
    assert excInfo.value.code != 0
    assert exitText.startswith(DRIFT_HEADER)
    # the table lists every mismatch, not just the first one
    assert 'salesApp' in exitText
    assert 'SalesApp/server/AppCreator.py PORT = 7000' in exitText
    assert 'ecommerceServiceV2' in exitText
    assert 'ecommerce/server/AppCreator.py PORT = 8000' in exitText
    # warnings are printed before the exit, they never end up in the drift table
    printed = capsys.readouterr().out
    assert 'WARNING: ghost: sibling checkout not found' in printed
    assert 'ghost' not in exitText
