"""A chat turn's time and its turnKey: a tool with its own deadline leaves
the turn time to save and to reply (turnDeadline), and one turnKey runs one
turn, whose status a client that lost its reply can ask (chatTurnKeys,
getChatTurnStatus)."""

import datetime
import json
import time
from types import SimpleNamespace

import pytest

import chatModel
import chatThinking
import chatTurnKeys
import photoStorage
import turnDeadline
from actions import ChatActions, PhotoActions
from ApiRequests import ApiRequests
from conftest import assertExactShape, createTestStore, wipeCollections
from mongoDb import db
from objects import ChatTurnStatusResponse, turnKeyId
from services.__request import ServiceUnavailable
from test_chat import (ADMIN, FAR, OTHER_ADMIN, After, calls, clock,
                       frozenNow, model, offline, reply)
from test_photos import BUCKET, FakeBucket, downloadUrl
from test_product_finder import groupPhoto

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'Conversations',
               'ChatTurns', 'ChatUsage', 'ChatThinking', 'ChatTurnKeys',
               'PhotoUsage']
TURN_KEY = 'turn-time-0123456789abcdef'
api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def status(userId: str = ADMIN, turnKey: str = TURN_KEY) -> dict:
    res = api.getChatTurnStatus(userId, turnKey)
    assertExactShape(ChatTurnStatusResponse, res)
    return res


def send(turnKey: str = TURN_KEY, conversationId=None) -> dict:
    return api.sendAdminChatMessage(ADMIN, 'What is my policy?',
                                    conversationId, None, None, turnKey)


def claimDoc(turnKey: str = TURN_KEY) -> dict | None:
    return db.read({'_id': turnKeyId(ADMIN, turnKey)},
                   chatTurnKeys.TURN_KEYS, findOne=True)


# ----------------------------- the turn's time ----------------------------- #


def test_outsideATurn_aToolKeepsItsOwnTime():
    before = time.monotonic()
    assert turnDeadline.toolDeadline(90) == pytest.approx(before + 90,
                                                          abs=1)
    assert turnDeadline.saveDeadline(20) == pytest.approx(before + 20,
                                                          abs=1)


def test_insideATurn_aToolLeavesItsSavesAndTheReplyTheirTime():
    with turnDeadline.runningTool(40):
        now = time.monotonic()
        work = turnDeadline.toolDeadline(90)
        saves = turnDeadline.saveDeadline(60)
        turn = now + 40
        assert work == pytest.approx(
            turn - turnDeadline.TOOL_REPLY_RESERVE_SECONDS
            - turnDeadline.SAVE_MIN_SECONDS, abs=1)
        assert work <= turn - 25 + 0.1
        assert saves <= turn - 15 + 0.1
        assert saves == pytest.approx(
            turn - turnDeadline.TOOL_REPLY_RESERVE_SECONDS, abs=1)
        # Work that ends by its deadline leaves the saves their window.
        assert saves - work >= 10 - 0.1
        assert turnDeadline.SAVE_MIN_SECONDS == 10
        # Its own time, when shorter, is kept.
        assert turnDeadline.toolDeadline(5) == pytest.approx(now + 5, abs=1)
    assert turnDeadline.toolDeadline(90) == pytest.approx(
        time.monotonic() + 90, abs=1)


def test_aPhotoToolOutOfTurnTime_isAToolError_andTheTurnStillReplies(
        clean, model, monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    bucket = FakeBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    createTestStore(ADMIN)
    path = f'users/{ADMIN}/chat/a/group.jpg'
    bucket.objects[path] = {'data': groupPhoto(),
                            'contentType': 'image/jpeg', 'metadata': None}
    # The model asks for the find 70 s into the 90 s turn: the tool's
    # work time is past (under its two reserves left), the model's reply
    # is not.
    fake = model(After(70, calls(('call-1', 'findProductsInPhoto',
                                  json.dumps({'imageUrl':
                                              downloadUrl(path)})))),
                 reply('That took too long, please try again.'))

    res = api.sendAdminChatMessage(ADMIN, 'Add these products')

    assert res['reply'] == 'That took too long, please try again.'
    toolMessage = fake.requests[1]['messages'][-1]
    assert toolMessage['role'] == 'tool'
    assert 'error' in json.loads(toolMessage['content'])
    # The find failed before reading the photo: its quota is given back.
    assert [doc['count'] for doc in db.read(
        {'_id': {'$regex': f'^{PhotoActions.FINDS_KIND}:'}},
        PhotoActions.PHOTO_USAGE)] == [0, 0]


# ----------------------------- one turn per turnKey ----------------------------- #


def test_aSavedTurn_keepsItsTurnKey_andReadsSaved(clean, model):
    createTestStore(ADMIN)
    model(reply('No returns policy yet.'))
    assert status() == {'status': 'unknown', 'conversationId': None}

    res = send()

    [turn] = db.read({'conversationId': res['conversationId']}, 'ChatTurns')
    assert turn['turnKey'] == TURN_KEY
    assert status() == {'status': 'saved',
                        'conversationId': res['conversationId']}
    # Only the caller reads their own.
    assert status(OTHER_ADMIN) == {'status': 'unknown',
                                   'conversationId': None}


def test_theSameTurnKeyAgain_isRefused_withoutTheModel(clean, model):
    createTestStore(ADMIN)
    fake = model(reply('No returns policy yet.'))
    send()

    with pytest.raises(ValueError, match=f'^{chatTurnKeys.TURN_SAVED}$'):
        send()

    assert len(fake.requests) == 1


def test_aRunningTurnKey_isRefused_andReadsRunning(clean, model):
    createTestStore(ADMIN)
    fake = model()
    claimId = chatTurnKeys.claim(ADMIN, TURN_KEY, 110)

    with pytest.raises(ValueError, match=f'^{chatTurnKeys.TURN_RUNNING}$'):
        send()

    assert fake.requests == []
    assert status() == {'status': 'running', 'conversationId': None}
    # The refused send never released the claim it did not hold.
    assert claimDoc()['claimId'] == claimId


def test_aFailedTurn_isReleased_andMayBeSentAgain(clean, model):
    createTestStore(ADMIN)
    model(RuntimeError('the model broke'))
    with pytest.raises(Exception):
        send()
    assert claimDoc() is None
    assert status() == {'status': 'unknown', 'conversationId': None}

    model(reply('No returns policy yet.'))
    res = send()

    assert status() == {'status': 'saved',
                        'conversationId': res['conversationId']}


def test_aBusyRefusal_releasesTheClaim(clean, model, monkeypatch):
    createTestStore(ADMIN)
    model()
    monkeypatch.setattr(ChatActions, '_turnSlots',
                        ChatActions.threading.BoundedSemaphore(1))
    ChatActions._turnSlots.acquire()

    with pytest.raises(ServiceUnavailable):
        send()

    assert claimDoc() is None


def test_aLockRefusal_releasesTheClaim(clean, model):
    createTestStore(ADMIN)
    model(reply('No returns policy yet.'))
    conversationId = send('first-turn-0123456789abcdef')['conversationId']
    db.findOneAndUpdate({'_id': conversationId},
                        {'$set': {'busyUntil': FAR + datetime.timedelta(
                            days=1)}}, 'Conversations')

    with pytest.raises(ValueError, match=f'^{ChatActions.STILL_ANSWERING}$'):
        send(conversationId=conversationId)

    assert claimDoc() is None


def test_aRefusedFormAnswer_releasesTheClaim(clean, model):
    createTestStore(ADMIN)
    model()
    answer = {'kind': 'location', 'formId': 'some-form', 'latitude': 14.55,
              'longitude': 121.02}

    with pytest.raises(ValueError):
        api.sendAdminChatMessage(ADMIN, None, None, answer, None, TURN_KEY)

    assert claimDoc() is None


def test_aFailedRelease_neverHidesTheTurnsOwnError(clean, model,
                                                   monkeypatch, caplog):
    createTestStore(ADMIN)
    model(RuntimeError('the model broke'))

    class BrokenDelete:
        # Only chatTurnKeys' own handle: the shared db stays whole.
        def __getattr__(self, name):
            return getattr(db, name)

        def delete(self, *args, **kwargs):
            raise ConnectionError('the database is down')

    monkeypatch.setattr(chatTurnKeys, 'db', BrokenDelete())

    with pytest.raises(Exception) as error:
        send()

    assert not isinstance(error.value, ConnectionError)
    assert 'chatTurnKeyNotReleased' in [
        r.msg['event'] for r in caplog.records if isinstance(r.msg, dict)]


def test_aTurnRunningPastItsTime_readsFailed_andMayBeSentAgain(clean,
                                                               model):
    createTestStore(ADMIN)
    # A process that stopped mid-turn: its claim's time is over.
    chatTurnKeys.claim(ADMIN, TURN_KEY, -1)
    assert status() == {'status': 'failed', 'conversationId': None}

    model(reply('No returns policy yet.'))
    res = send()

    assert status()['conversationId'] == res['conversationId']


def test_aTakenOverClaim_isNeitherSavedNorReleasedByItsOldTurn(clean):
    stale = chatTurnKeys.claim(ADMIN, TURN_KEY, -1)
    current = chatTurnKeys.claim(ADMIN, TURN_KEY, 110)
    assert stale != current

    with pytest.raises(RuntimeError):
        chatTurnKeys.markSaved(ADMIN, TURN_KEY, stale, 'conversation-1')
    chatTurnKeys.release(ADMIN, TURN_KEY, stale)

    assert claimDoc()['claimId'] == current
    assert status() == {'status': 'running', 'conversationId': None}


def test_aTurnWhoseClaimWasTakenOver_savesNothing(clean, model):
    createTestStore(ADMIN)
    fake = model(reply('No returns policy yet.'))
    taken = {}

    def takeOver(_):
        # The claim runs out mid-turn and a resend takes it over.
        db.findOneAndUpdate({'_id': turnKeyId(ADMIN, TURN_KEY)},
                            {'$set': {'runningUntil': datetime.datetime(
                                2000, 1, 1, tzinfo=datetime.timezone.utc)}},
                            chatTurnKeys.TURN_KEYS)
        taken['claimId'] = chatTurnKeys.claim(ADMIN, TURN_KEY, 110)

    fake.onCall = takeOver

    with pytest.raises(RuntimeError):
        send()

    assert db.read({}, 'ChatTurns') == []
    assert claimDoc()['claimId'] == taken['claimId']
    assert status() == {'status': 'running', 'conversationId': None}


def test_aBadTurnKey_isRefused(clean):
    with pytest.raises(ValueError, match='^turnKey must be'):
        api.getChatTurnStatus(ADMIN, 'short')
    # A trailing newline is no turnKey (fullmatch).
    with pytest.raises(ValueError, match='^turnKey must be'):
        chatThinking.validTurnKey(TURN_KEY + '\n')


# ------------------------ more of the turn's time ------------------------ #

PHOTO_COLLECTIONS = [*COLLECTIONS, 'GeneratedPhotos']


def workReserve() -> float:
    """What a tool's work leaves the turn (its reply and its saves), read
    from turnDeadline so no constant's name is repeated here."""
    with turnDeadline.runningTool(1000):
        return 1000 - (turnDeadline.toolDeadline(10 ** 6) - time.monotonic())


def photoUsageCounts() -> list[int]:
    return [doc['count'] for doc in db.read({}, PhotoActions.PHOTO_USAGE)]


@pytest.fixture
def photoFakes(monkeypatch):
    from test_photos import FakeImages, modelClient
    wipeCollections(PHOTO_COLLECTIONS)
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    bucket, images = FakeBucket(), FakeImages()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.setattr(chatModel, 'client', lambda: modelClient(images))
    yield SimpleNamespace(bucket=bucket, images=images)
    wipeCollections(PHOTO_COLLECTIONS)


def storeWithMug(fakes) -> str:
    from test_photos import JPEG
    createTestStore(ADMIN)
    path = f'users/{ADMIN}/mug.jpg'
    fakes.bucket.objects[path] = {'data': JPEG, 'contentType': 'image/jpeg',
                                  'metadata': None}
    return downloadUrl(path)


def test_findColorsInPhoto_aFocusOutOfTurnTime_isAToolError_andGivesTheQuotaBack(
        clean, model, monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    bucket = FakeBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    createTestStore(ADMIN)
    path = f'users/{ADMIN}/chat/a/logo.jpg'
    bucket.objects[path] = {'data': groupPhoto(), 'contentType': 'image/jpeg',
                            'metadata': None}
    fake = model(After(70, calls(('call-1', 'findColorsInPhoto', json.dumps(
        {'imageUrl': downloadUrl(path), 'focus': 'red mug'})))),
        reply('That took too long, please try again.'))

    res = api.sendAdminChatMessage(ADMIN, 'What colors is the mug?')

    assert res['reply'] == 'That took too long, please try again.'
    toolMessage = fake.requests[1]['messages'][-1]
    assert toolMessage['role'] == 'tool'
    assert 'error' in json.loads(toolMessage['content'])
    assert set(doc['count'] for doc in db.read(
        {'_id': {'$regex': f'^{PhotoActions.COLOR_FINDS_KIND}:'}},
        PhotoActions.PHOTO_USAGE)) <= {0}


def test_generateProductPhotos_withTheTurnsWorkTimeSpent_skipsTheModel_andGivesTheQuotaBack(
        photoFakes):
    from ToolRequests import ToolRequests
    url = storeWithMug(photoFakes)
    left = workReserve() + PhotoActions.MODEL_MIN_SECONDS - 1

    with turnDeadline.runningTool(left):
        with pytest.raises(Exception, match='photo generation'):
            ToolRequests().generateProductPhotos(ADMIN, [url])

    assert photoFakes.images.calls == []
    assert photoUsageCounts() == [0, 0]


def test_generateProductPhotos_inATurn_givesTheModelNoMoreThanTheTurnLeaves(
        photoFakes, monkeypatch):
    from ToolRequests import ToolRequests
    url = storeWithMug(photoFakes)
    workTime = PhotoActions.MODEL_MIN_SECONDS + 30
    real = PhotoActions.imageModel.editImages
    left = []

    def spy(references, prompt, n, deadline, *args, **kwargs):
        left.append(deadline - time.monotonic())
        return real(references, prompt, n, deadline, *args, **kwargs)

    monkeypatch.setattr(PhotoActions.imageModel, 'editImages', spy)

    with turnDeadline.runningTool(workReserve() + workTime):
        res = ToolRequests().generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == 3
    assert left and all(0 < seconds <= workTime for seconds in left)
    # Out of a turn, it keeps its own time (the MCP servers, a direct call).
    left.clear()
    ToolRequests().generateProductPhotos(ADMIN, [url])
    assert left and all(seconds > workTime for seconds in left)


def test_aTurnsTime_neverOutlivesItsTurn_onTheThread(clean, model):
    createTestStore(ADMIN)
    model(calls(('call-1', 'getMyStorePolicy', '{}')),
          reply('No returns policy yet.'))

    send()

    assert turnDeadline.toolDeadline(90) == pytest.approx(
        time.monotonic() + 90, abs=1)
    assert turnDeadline.saveDeadline(20) == pytest.approx(
        time.monotonic() + 20, abs=1)


def raceForClaim(turnKey: str) -> list[tuple[str, str]]:
    import threading
    barrier = threading.Barrier(2)
    outcomes = []

    def race():
        barrier.wait()
        try:
            outcomes.append(('won', chatTurnKeys.claim(ADMIN, turnKey, 110)))
        except ValueError as error:
            outcomes.append(('refused', str(error)))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


def test_twoSendsRacingOneTurnKey_oneWins(clean):
    for attempt in range(20):
        turnKey = f'race-turn-{attempt:02d}-0123456789'

        outcomes = raceForClaim(turnKey)

        assert sorted(kind for kind, _ in outcomes) == ['refused', 'won']
        winner = next(value for kind, value in outcomes if kind == 'won')
        assert [value for kind, value in outcomes
                if kind == 'refused'] == [chatTurnKeys.TURN_RUNNING]
        assert claimDoc(turnKey)['claimId'] == winner


def test_aTurnKeyWhoseClaimRanOut_isTakenOverByOneOfTwoRacers(clean):
    chatTurnKeys.claim(ADMIN, TURN_KEY, -1)

    outcomes = raceForClaim(TURN_KEY)

    assert sorted(kind for kind, _ in outcomes) == ['refused', 'won']
    winner = next(value for kind, value in outcomes if kind == 'won')
    assert claimDoc()['claimId'] == winner


def test_takingOverAClaim_refreshesItsTimes(clean):
    first = chatTurnKeys.claim(ADMIN, TURN_KEY, -1)
    old = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    db.findOneAndUpdate({'_id': turnKeyId(ADMIN, TURN_KEY)},
                        {'$set': {'expireAt': old}}, chatTurnKeys.TURN_KEYS)

    second = chatTurnKeys.claim(ADMIN, TURN_KEY, 110)

    held = claimDoc()
    now = datetime.datetime.now(datetime.timezone.utc)
    assert second != first and held['claimId'] == second
    assert held['status'] == 'running' and held['conversationId'] is None
    for field, seconds in (('runningUntil', 110),
                           ('expireAt', chatTurnKeys.TURN_KEY_SECONDS)):
        stored = held[field]
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=datetime.timezone.utc)
        assert abs((stored - now).total_seconds() - seconds) < 30

