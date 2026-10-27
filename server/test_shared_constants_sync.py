"""Guards the sharedConstants.py -> serverConstants.ts copy.

Nothing in client/package.json runs AppCreator, so unless you add a CI step
pytest is the only place a drift check actually executes.
"""
import ast
import datetime
from pathlib import Path

import pytest

import sharedConstants
from AppCreator import CLIENT_CONSTANTS_TS_PATH, renderSharedConstantsTS

REGEN = 'cd server && python AppCreator.py --constants-only'


def test_generated_client_constants_are_up_to_date():
    # read_text uses universal newlines, so the CRLF that core.autocrlf leaves
    # in a working tree does not fail the comparison.
    committed = CLIENT_CONSTANTS_TS_PATH.read_text(encoding='utf-8')
    assert committed == renderSharedConstantsTS(), (
        f'{CLIENT_CONSTANTS_TS_PATH} is stale. Regenerate: {REGEN}')


def test_underscore_names_are_not_exported(monkeypatch):
    monkeypatch.setattr(sharedConstants, '_SERVER_ONLY', 42, raising=False)
    assert '_SERVER_ONLY' not in renderSharedConstantsTS()


def test_secret_looking_name_fails_generation(monkeypatch):
    # PUBLIC name on purpose — an underscore-prefixed one is skipped by
    # discovery and would make this test pass without touching the guard.
    monkeypatch.setattr(sharedConstants, 'API_TOKEN', 'x', raising=False)
    with pytest.raises(ValueError, match='published to every browser'):
        renderSharedConstantsTS()


def test_nested_secret_key_fails_generation(monkeypatch):
    # The top-level name is innocuous — only the nested key gives it away.
    monkeypatch.setattr(sharedConstants,
                        'CONFIG', {'api_secret': 'abc123'},
                        raising=False)
    with pytest.raises(ValueError, match='published to every browser'):
        renderSharedConstantsTS()


def test_nested_camel_case_secret_key_fails_generation(monkeypatch):
    monkeypatch.setattr(sharedConstants,
                        'MAPS_CONFIG', [{'apiKey': 'abc123'}],
                        raising=False)
    with pytest.raises(ValueError, match='published to every browser'):
        renderSharedConstantsTS()


def test_credential_shaped_value_fails_generation(monkeypatch):
    # A real Google key under a benign name: nothing about CONFIG.browserId
    # reads as a secret, so only the value-shape check catches this.
    monkeypatch.setattr(
        sharedConstants,
        'MAPS_CONFIG', {'browserId': 'AIzaSyB' + 'a' * 32},
        raising=False)
    with pytest.raises(ValueError, match='published to every browser'):
        renderSharedConstantsTS()


def test_account_number_shaped_value_fails_generation(monkeypatch):
    monkeypatch.setattr(sharedConstants,
                        'BANK_LIST', ['BDO 0012 3456 7890'],
                        raising=False)
    with pytest.raises(ValueError, match='published to every browser'):
        renderSharedConstantsTS()


def test_unsafe_integer_fails_generation(monkeypatch):
    # 2**53 + 1 emits 9007199254740993, which JS reads back as ...992.
    monkeypatch.setattr(sharedConstants, 'BIG', 2**53 + 1, raising=False)
    with pytest.raises(ValueError, match='safe integer range'):
        renderSharedConstantsTS()


@pytest.mark.parametrize('name,value', [
    # PUBLIC names on purpose: an underscore-prefixed one is skipped by
    # discovery and would make these pass vacuously.
    ('MAX_TOKENS', 4096),
    ('DEFAULT_SORT_KEY', 'name'),
    # A dropdown of bank *names* is legitimately public — the nested-key guard
    # must not block 'bank', and the digit rule must not fire on an 11-digit
    # PH mobile number.
    ('BANK_OPTIONS', [{'bank': 'BDO Unibank'}]),
    ('STORE_PHONE', '0917 123 4567'),
    ('MAX_SAFE_QUANTITY', 2**53 - 1),
])
def test_false_positives_still_generate(monkeypatch, name, value):
    monkeypatch.setattr(sharedConstants, name, value, raising=False)
    assert f'export const {name} =' in renderSharedConstantsTS()


def test_unserializable_value_fails_generation(monkeypatch):
    monkeypatch.setattr(sharedConstants,
                        'EXPIRY',
                        datetime.date.today(),
                        raising=False)
    with pytest.raises(TypeError, match='EXPIRY'):
        renderSharedConstantsTS()


def test_shared_constants_has_no_imports():
    # The file is pure data and needs none. Zero imports is simpler and
    # stricter than "stdlib only", and it closes the hole where a stray
    # `from math import pi` would silently export `pi`. This is what actually
    # enforces the no-cycle rule the docstring states.
    tree = ast.parse(
        Path(sharedConstants.__file__).read_text(encoding='utf-8'))
    assert not [
        n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
