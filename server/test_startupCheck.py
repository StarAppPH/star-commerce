"""The startupCheck route of the generated Flask app (app.py,
AppCreator._startup_check_block): the path of Cloud Run's HTTP startup probe
(cloudbuild.yaml's deploy-service step). It answers 200 {'status': 'ok'} to a
bare GET, with no JWT, no caller header and no body, once app.py has
imported. test_cloudbuild.py pins the probe's arg in cloudbuild.yaml; the
last test here reads the path from that arg, so the probe and the route
cannot drift apart.

This file is identical in every Star App repo.
"""

import re

from test_cloudbuild import _steps, _yaml
from test_route_responses_http import _flask_app


def test_startupCheck_answersABareGet(monkeypatch):
    client = _flask_app(monkeypatch).test_client()

    response = client.get('/startupCheck', headers={'Accept-Encoding': 'gzip, br'})

    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok'}
    assert 'Content-Encoding' not in response.headers


def test_startupCheck_isOnlyAGetRoute(monkeypatch):
    flaskApp = _flask_app(monkeypatch)

    rules = [rule for rule in flaskApp.url_map.iter_rules() if rule.rule == '/startupCheck']

    assert [rule.endpoint for rule in rules] == ['startupCheck']
    assert rules[0].methods - {'HEAD', 'OPTIONS'} == {'GET'}
    assert flaskApp.test_client().post('/startupCheck').status_code == 405


def test_startupCheck_answersTheProbesPath(monkeypatch):
    """The path cloudbuild.yaml's deploy-service step probes is the route."""
    paths = re.findall(r'httpGet\.path=([^,\s]+)', _steps(_yaml())['deploy-service'])

    assert len(paths) == 1, paths
    assert _flask_app(monkeypatch).test_client().get(paths[0]).status_code == 200
