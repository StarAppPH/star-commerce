"""job_predeploy.py: the Cloud Run job cloudbuild.yaml runs before each
deploy. Any failure must exit non-zero, or the build deploys an image whose
topics or indexes are not in place. The repo's own index hook (buildIndexes,
its per-repo block) is tested in the repo; here it is a fake.

No GCP: the topic lookups are fakes.
"""
import pytest
from google.api_core.exceptions import NotFound, PermissionDenied

import checkPubSubTopics
import job_predeploy
import pubSub
from AppConfig import AppConfig


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(AppConfig, 'getIsCloudEnvironment', lambda self: True)


def _indexSpy(monkeypatch, result: int = 0) -> list:
    calls = []

    def buildIndexes():
        calls.append(True)
        return result

    monkeypatch.setattr(job_predeploy, 'buildIndexes', buildIndexes)
    return calls


def test_aFailedTopicCheckFailsTheJobBeforeAnyIndexBuild(monkeypatch):
    def topicCheckFails():
        raise SystemExit(1)

    monkeypatch.setattr(checkPubSubTopics, 'main', topicCheckFails)
    calls = _indexSpy(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        job_predeploy.main()

    assert exited.value.code == 1
    assert calls == []


@pytest.mark.parametrize('result', [0, 1])
def test_theJobExitsWithTheIndexResult(monkeypatch, capsys, result):
    monkeypatch.setattr(checkPubSubTopics, 'main', lambda: None)
    calls = _indexSpy(monkeypatch, result)

    assert job_predeploy.main() == result
    assert calls == [True]
    out = capsys.readouterr().out
    assert ('Index build passed.' if result == 0 else 'Index build FAILED.') in out


def test_anIndexBuildErrorFailsTheJob(monkeypatch):
    monkeypatch.setattr(checkPubSubTopics, 'main', lambda: None)

    def buildIndexes():
        raise RuntimeError('index boom')

    monkeypatch.setattr(job_predeploy, 'buildIndexes', buildIndexes)

    with pytest.raises(RuntimeError, match='index boom'):
        job_predeploy.main()


def test_aTopicLookupThatCannotBeMadeFailsTheJob(cloud, monkeypatch, capsys):
    """No credentials (or no client) is no proof the topics exist: the check
    fails closed, where it used to warn and let the deploy through."""
    def noClient():
        raise RuntimeError('no default credentials')

    topics = [('anyTopic', True)]
    monkeypatch.setattr(checkPubSubTopics, 'manifestTopics', lambda: topics)
    monkeypatch.setattr(checkPubSubTopics, 'scannedTopics', lambda: topics)
    monkeypatch.setattr(pubSub.pubSub, '_ensureClients', noClient)
    calls = _indexSpy(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        job_predeploy.main()

    assert exited.value.code == 1
    assert 'ERROR: Could not verify PubSub topics: no default credentials' in capsys.readouterr().out
    assert calls == []


def test_theTopicLookupsAreSkippedOutsideTheCloud(monkeypatch):
    monkeypatch.setattr(AppConfig, 'getIsCloudEnvironment', lambda self: False)

    def lookup(topicName):
        raise AssertionError('looked up ' + topicName + ' outside the cloud')

    monkeypatch.setattr(pubSub.pubSub, '_checkIfTopicExists', lookup)

    pubSub.checkPubSubTopics([('anyTopic', True)])


def test_anImageOfAnotherCommitFailsTheJobBeforeAnyCheck(monkeypatch, capsys):
    """cloudbuild.yaml runs the job with EXPECTED_COMMIT_SHA; an overlapping
    build that re-imaged the job in between leaves another commit's image."""
    monkeypatch.setenv('EXPECTED_COMMIT_SHA', 'aaa111')
    monkeypatch.setenv('BUILD_COMMIT_SHA', 'bbb222')

    def topicCheck():
        raise AssertionError('checked the topics of the wrong image')

    monkeypatch.setattr(checkPubSubTopics, 'main', topicCheck)
    calls = _indexSpy(monkeypatch)

    assert job_predeploy.main() == 1
    assert calls == []
    out = capsys.readouterr().out
    assert 'ERROR: the job runs the image of commit bbb222, not aaa111' in out


@pytest.mark.parametrize('expected, built', [('aaa111', 'aaa111'), ('', 'bbb222'), ('', '')])
def test_theSameCommitOrARunByHandGoesOn(monkeypatch, expected, built):
    monkeypatch.setenv('EXPECTED_COMMIT_SHA', expected)
    monkeypatch.setenv('BUILD_COMMIT_SHA', built)
    monkeypatch.setattr(checkPubSubTopics, 'main', lambda: None)
    calls = _indexSpy(monkeypatch)

    assert job_predeploy.main() == 0
    assert calls == [True]


def test_anImageWithNoCommitFailsWhenOneIsExpected(monkeypatch):
    monkeypatch.setenv('EXPECTED_COMMIT_SHA', 'aaa111')
    monkeypatch.delenv('BUILD_COMMIT_SHA', raising=False)
    assert 'commit <none>, not aaa111' in job_predeploy.commitMismatch()


class _FakePublisher:
    """get_topic raises errors[topic name] for the topics it names."""

    def __init__(self, errors: dict):
        self.errors = errors

    def topic_path(self, projectId, topicName):
        return f'projects/{projectId}/topics/{topicName}'

    def get_topic(self, request):
        topicName = request['topic'].rsplit('/', 1)[-1]
        if topicName in self.errors:
            raise self.errors[topicName]


def _withPublisher(monkeypatch, errors: dict) -> None:
    monkeypatch.setattr(pubSub.pubSub, '_publisher', _FakePublisher(errors))
    monkeypatch.setattr(pubSub.pubSub, '_projectId', 'p')


def _topic(fn: str) -> str:
    return f'{fn}_{AppConfig().projectName}'


def test_onlyNotFoundReadsAsAMissingTopic(cloud, monkeypatch):
    _withPublisher(monkeypatch, {_topic('gone'): NotFound('no topic')})
    assert pubSub.pubSub._checkIfTopicExists(_topic('gone')) is False
    assert pubSub.pubSub._checkIfTopicExists(_topic('here')) is True


def test_aLookupThatFailsOtherwiseIsReportedAsUncheckedAndFails(cloud, monkeypatch, capsys):
    _withPublisher(monkeypatch, {_topic('denied'): PermissionDenied('pubsub.topics.get')})

    with pytest.raises(SystemExit) as exited:
        pubSub.checkPubSubTopics([('denied', True), ('here', True)])

    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert 'These PubSub topics could not be checked in GCP' in out
    assert f'could not check topic {_topic("denied")}: PermissionDenied' in out
    assert 'do not exist in GCP' not in out


def test_missingAndUncheckedTopicsAreBothListed(cloud, monkeypatch, capsys):
    _withPublisher(monkeypatch, {_topic('gone'): NotFound('no topic'),
                                 _topic('denied'): PermissionDenied('pubsub.topics.get')})

    with pytest.raises(SystemExit) as exited:
        pubSub.checkPubSubTopics([('gone', True), ('denied', True)])

    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert f'could not check topic {_topic("denied")}' in out
    missingSection = out.split('do not exist in GCP:', 1)[1]
    assert f'  - {_topic("gone")}' in missingSection
    assert _topic('denied') not in missingSection
