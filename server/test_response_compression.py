"""Response compression in the generated Flask app (app.py, flask-compress).

  - the settings AppCreator writes: brotli at quality 4, else gzip at level
    6, for JSON and CSV bodies of 500 bytes or more, run by app.py's own
    _compressResponse hook (COMPRESS_REGISTER is off);
  - a large JSON body comes back br or gzip encoded when the request's
    Accept-Encoding allows it, with Vary: Accept-Encoding, and decodes to
    the same JSON; so does a large CSV body (the catalog feeds);
  - a small body, a request without Accept-Encoding and an error response
    are sent as they are;
  - the routes that issue an access token (createAccessToken) are never
    compressed (BREACH). AppCreator._compression_block says why only those;
  - the kill switch: app.config['COMPRESS_DISABLED'] (app.py sets it from
    the COMPRESS_DISABLED environment variable at start-up) turns
    compression off, without a code deploy.

Each response is built here and run through app.process_response inside a
test_request_context, so the real after_request chain runs (CORS included)
without a route of its own. PROBE_PATH is not a route.

This file is identical in every Star App repo.
"""

import gzip
import json

import brotli
from ApiRequests import ApiRequests
from flask import Response, request
from test_route_responses_http import _flask_app

PROBE_PATH = '/compressionProbe'
# Well over COMPRESS_MIN_SIZE, and repetitive enough to compress.
LARGE_BODY = {
    'status': 200,
    'data': [{'_id': f'probe-{i}', 'name': 'probe item'} for i in range(60)]
}
SMALL_BODY = {'status': 200, 'data': {'_id': 'probe'}}


def _jsonResponse(body, status=200) -> Response:
    return Response(json.dumps(body), status=status,
                    mimetype='application/json')


def _send(flaskApp, response, acceptEncoding=None, rule=None):
    """response, run through the app's after_request hooks for a request to
    PROBE_PATH, or to rule (a url_map Rule) when given."""
    headers = {'Accept-Encoding': acceptEncoding} if acceptEncoding else {}
    path, method = PROBE_PATH, 'GET'
    if rule is not None:
        path = rule.rule
        method = sorted(rule.methods - {'HEAD', 'OPTIONS'})[0]
    with flaskApp.test_request_context(path, method=method, headers=headers):
        assert request.endpoint == (rule.endpoint if rule else None)
        return flaskApp.process_response(response)


def _decoded(response) -> bytes:
    encoding = response.headers.get('Content-Encoding')
    data = response.get_data()
    if encoding == 'br':
        return brotli.decompress(data)
    if encoding == 'gzip':
        return gzip.decompress(data)
    assert encoding is None, encoding
    return data


def _tokenIssuingRoutes() -> set:
    return {
        name
        for cls in ApiRequests.__mro__
        for name, func in vars(cls).items()
        if callable(func) and not name.startswith('_')
        and getattr(func, 'createAccessToken', None) is True
    }


def test_compression_settings(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    config = flaskApp.config

    assert config['COMPRESS_ALGORITHM'] == ['br', 'gzip']
    assert config['COMPRESS_ALGORITHM_STREAMING'] == ['br']
    assert config['COMPRESS_BR_LEVEL'] == 4
    assert config['COMPRESS_LEVEL'] == 6
    assert config['COMPRESS_MIN_SIZE'] == 500
    assert set(config['COMPRESS_MIMETYPES']) == {'application/json', 'text/csv'}
    assert config['COMPRESS_REGISTER'] is False
    assert config['COMPRESS_DISABLED'] is False, (
        'COMPRESS_DISABLED is on (set in the env or server/.env?)')
    hooks = [f.__name__ for f in flaskApp.after_request_funcs[None]]
    assert '_compressResponse' in hooks
    assert 'after_request' not in hooks  # flask-compress's own, unregistered


def test_largeJson_isCompressed_whenAccepted(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    raw = json.dumps(LARGE_BODY).encode()
    assert len(raw) > flaskApp.config['COMPRESS_MIN_SIZE']

    for acceptEncoding, expected in (('gzip, br', 'br'), ('br', 'br'),
                                     ('gzip', 'gzip'),
                                     ('gzip, deflate', 'gzip')):
        response = _send(flaskApp, _jsonResponse(LARGE_BODY),
                         acceptEncoding=acceptEncoding)

        assert response.status_code == 200
        assert response.headers['Content-Encoding'] == expected
        assert 'Accept-Encoding' in response.headers['Vary']
        assert len(response.get_data()) < len(raw)
        assert json.loads(_decoded(response)) == LARGE_BODY
        assert response.headers['Access-Control-Allow-Origin'] == '*'


def test_largeCsv_isCompressed(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    csv = 'id,title\n' + ''.join(f'probe-{i},probe item\n' for i in range(60))

    response = _send(flaskApp,
                     Response(csv, mimetype='text/csv; charset=utf-8'),
                     acceptEncoding='gzip')

    assert response.headers['Content-Encoding'] == 'gzip'
    assert _decoded(response).decode() == csv


def test_uncompressed_whenSmall_notAccepted_orAnError(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    raw = json.dumps(LARGE_BODY).encode()

    cases = (
        (_jsonResponse(SMALL_BODY), 'gzip, br', SMALL_BODY),
        (_jsonResponse(LARGE_BODY), None, LARGE_BODY),
        (_jsonResponse(LARGE_BODY), 'identity', LARGE_BODY),
        (_jsonResponse(LARGE_BODY, status=400), 'gzip, br', LARGE_BODY),
    )
    for built, acceptEncoding, body in cases:
        response = _send(flaskApp, built, acceptEncoding=acceptEncoding)

        assert 'Content-Encoding' not in response.headers, acceptEncoding
        assert json.loads(response.get_data()) == body

    response = _send(flaskApp, Response(raw, mimetype='text/html'),
                     acceptEncoding='gzip, br')
    assert 'Content-Encoding' not in response.headers


def test_tokenIssuingRoutes_areNeverCompressed(monkeypatch):
    """A createAccessToken route answers with a fresh access token next to
    sign-in data the caller sent: never compressed, however large."""
    flaskApp = _flask_app(monkeypatch)
    import app as appModule
    exempt = _tokenIssuingRoutes()

    assert exempt
    assert appModule.NO_COMPRESSION_ENDPOINTS == exempt
    rules = {rule.endpoint: rule for rule in flaskApp.url_map.iter_rules()}
    for name in sorted(exempt):
        response = _send(flaskApp, _jsonResponse(LARGE_BODY),
                         acceptEncoding='gzip, br', rule=rules[name])

        assert 'Content-Encoding' not in response.headers, name
        assert json.loads(response.get_data()) == LARGE_BODY


def test_killSwitch_turnsCompressionOff(monkeypatch):
    flaskApp = _flask_app(monkeypatch)

    monkeypatch.setitem(flaskApp.config, 'COMPRESS_DISABLED', True)
    response = _send(flaskApp, _jsonResponse(LARGE_BODY),
                     acceptEncoding='gzip, br')
    assert 'Content-Encoding' not in response.headers
    assert json.loads(response.get_data()) == LARGE_BODY

    monkeypatch.setitem(flaskApp.config, 'COMPRESS_DISABLED', False)
    response = _send(flaskApp, _jsonResponse(LARGE_BODY),
                     acceptEncoding='gzip, br')
    assert response.headers['Content-Encoding'] == 'br'
