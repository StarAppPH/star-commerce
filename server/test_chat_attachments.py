"""Chat attachments: up to 4 JPEG, PNG, WebP or PDF files a message carries,
read from Storage by path (never by URL), checked, sent to the model as
base64 parts (a WebP as a JPEG or PNG), stored as path/type/name/size,
and re-sent from the last 3 turns.

No real Meta or Storage call: a counting FakeBucket replaces
photoStorage._bucket(), and the FakeModel, clock and frozen day of
test_chat.py play the model. The clock fixture must also stop photoStorage's
`time`, or every Storage read is a 503 (it measures its deadline with it)."""

import base64
import datetime
import json
import logging
import re
import threading
from types import SimpleNamespace
from urllib.parse import quote

import openai
import pytest

import chatModel
import photoStorage
from actions import ChatActions, usageCounters
from ApiRequests import ApiRequests
from conftest import createTestStore
from mongoDb import db
from objects import (CHAT_ATTACHMENT_MAX_BYTES, CHAT_ATTACHMENTS_MAX,
                     CHAT_ATTACHMENTS_MAX_TOTAL_BYTES, ChatAttachment,
                     ChatTurn)
from objects.chatComponents import LocationAnswer
from routeErrors import routeFailure
from services.__request import ServiceUnavailable
# Fixtures (the autouse ones apply here too) and helpers of the chat suite.
from test_chat import (ADMIN, DAY, ERROR_TEXT, FAR, FakeClock, calls,  # noqa
                       clean, clock, conversationDoc, customerCtx,
                       customerKey, freezeNow, frozenNow, model, offline,
                       reply, sdkError, seedConversation, seedTurn, storeKey,
                       turnDocs, usageDoc)
from test_photos import FakeBlob, FakeBucket

api = ApiRequests()
BUCKET = 'ch-att-bucket.firebasestorage.app'
MAX = CHAT_ATTACHMENT_MAX_BYTES
TOTAL = CHAT_ATTACHMENTS_MAX_TOTAL_BYTES
MEGABYTE = 1024 * 1024
JPG = b'\xff\xd8\xff' + b'jpg-bytes'
PNG = b'\x89PNG\r\n\x1a\n' + b'png-bytes'
PDF = b'%PDF-1.4\n' + b'pdf-bytes'
BY_EXTENSION = {'.jpg': JPG, '.jpeg': JPG, '.png': PNG, '.pdf': PDF}
TYPE_OF = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
           '.pdf': 'application/pdf'}
_SAME = object()
CUSTOMER_ATTACHMENTS = 'attachmentsCustomer'
STORE_ATTACHMENTS = 'attachmentsStore'
ALL_ATTACHMENTS = 'attachmentsAll'


# ----------------------------- fakes ----------------------------- #


class CountingBlob(FakeBlob):

    def download_as_bytes(self, end, **kwargs):
        if self.bucket.downloadError is not None:
            raise self.bucket.downloadError
        self.bucket.downloads.append(self.name)
        return super().download_as_bytes(end, **kwargs)


class CountingBucket(FakeBucket):
    """FakeBucket that records each metadata read (`lookups`) and each
    download (`downloads`), and can fail either."""

    def __init__(self):
        super().__init__()
        self.lookups, self.downloads = [], []
        self.lookupError = self.downloadError = None

    def get_blob(self, name, **kwargs):
        self.lookups.append(name)
        if self.lookupError is not None:
            raise self.lookupError
        found = super().get_blob(name, **kwargs)
        if found is None:
            return None
        blob = CountingBlob(self, name)
        blob.content_type, blob.size = found.content_type, found.size
        return blob

    def put(self, path, data=None, contentType=None, size=_SAME):
        extension = photoStorage.fileExtension(path)
        stored = {'data': BY_EXTENSION[extension] if data is None else data,
                  'contentType': contentType or TYPE_OF[extension],
                  'metadata': None}
        if size is not _SAME:
            stored['size'] = size
        self.objects[path] = stored


@pytest.fixture
def storage(clean, monkeypatch, clock):
    """The fake bucket, with the clock stopped for photoStorage too, and the
    process's bytes in flight at 0: it must be 0 again after every test."""
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv(ChatActions.CHAT_ATTACHMENTS_DAILY_CAP_ALL_VARIABLE,
                       raising=False)
    monkeypatch.setattr(photoStorage, 'time', clock)
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)
    bucket = CountingBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    yield bucket
    assert ChatActions._bytesInFlight == 0


# ----------------------------- helpers ----------------------------- #


def mine(uid: str, name: str) -> str:
    return f'users/{uid}/chat/{name}'


def urlOf(path: str, bucket: str = BUCKET,
          host: str = photoStorage.DOWNLOAD_HOST) -> str:
    return (f'https://{host}/v0/b/{bucket}/o/{quote(path, safe="")}'
            '?alt=media&token=t')


def publicUrl(path: str) -> str:
    """The URL an admin's image note names (no token)."""
    return (f'https://firebasestorage.googleapis.com/v0/b/{BUCKET}/o/'
            f'{quote(path, safe="")}')


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def dataUrl(contentType: str, data: bytes) -> str:
    return f'data:{contentType};base64,{b64(data)}'


def sendAdmin(text, urls=None, conversationId=None, form=None) -> dict:
    return api.sendAdminChatMessage(ADMIN, text, conversationId, form, urls)


def sendCustomer(customerId: str, storeId: str, text, urls=None,
                 conversationId=None) -> dict:
    return api.sendStoreChatMessage(customerId, storeId, text,
                                    conversationId, None, urls)


def chatCounts() -> dict:
    """Every ChatUsage counter above 0, by key."""
    return {doc['_id']: doc['count'] for doc in db.read({}, 'ChatUsage')
            if doc['count']}


def attachmentKey(kind: str, owner: str, day=DAY) -> str:
    return ChatActions.usageKey(kind, owner, day)


def keptCounts(customerId: str, storeId: str, files: int) -> dict:
    """What a customer's one turn with `files` files counts when nothing is
    given back."""
    return {customerKey(customerId): 1, storeKey(storeId): 1,
            attachmentKey(CUSTOMER_ATTACHMENTS, customerId): files,
            attachmentKey(STORE_ATTACHMENTS, storeId): files,
            attachmentKey(ALL_ATTACHMENTS,
                          usageCounters.ALL_STORES): files}


def userMessages(request: dict) -> list[dict]:
    return [m for m in request['messages'] if m['role'] == 'user']


def textOf(message: dict) -> str:
    content = message['content']
    return content if isinstance(content, str) else content[0]['text']


def partsOf(message: dict) -> list[dict]:
    content = message['content']
    return [] if isinstance(content, str) else content[1:]


def resentTurns(request: dict) -> set[int]:
    """The turns ('q<n>') of the history whose user message carries a file
    part."""
    return {int(textOf(m)[1]) for m in userMessages(request)[:-1]
            if partsOf(m)}


def logged(caplog, event: str) -> list:
    return [r for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == event]


def seedFileTurn(storage, conversation: dict, seq: int, name: str,
                 storedSize: int | None = None, **stored) -> bytes:
    """A stored turn `seq` that carried the file `name` (users/<uid>/chat/),
    and the file in the fake bucket (`stored`: CountingBucket.put's
    arguments). Returns the bytes."""
    path = mine(conversation['userId'], name)
    extension = photoStorage.fileExtension(path)
    stored.setdefault('data', BY_EXTENSION[extension] + name.encode())
    storage.put(path, **stored)
    data = stored['data']
    turn = ChatTurn(
        conversationId=conversation['_id'],
        userId=conversation['userId'],
        storeId=conversation['storeId'],
        seq=seq,
        userText=f'q{seq}',
        attachments=[ChatAttachment(
            path=path, contentType=TYPE_OF[extension], name=name,
            size=len(data) if storedSize is None else storedSize)],
        steps=[],
        replyText=f'a{seq}',
        toolCallCount=0,
        usage={'promptTokens': 0, 'completionTokens': 0, 'cachedTokens': 0},
        model=chatModel.CHAT_MODEL,
        createdAt=FAR + datetime.timedelta(seconds=seq),
        _version=0)
    db.create(turn.model_dump(by_alias=True), 'ChatTurns')
    return data


def adminWithHistory(storage, files: list):
    """The admin's set-up store and a conversation whose turns 0.. each
    carried one file (a name, or (name, seedFileTurn kwargs))."""
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], 'admin',
                                    turnCount=len(files))
    for seq, entry in enumerate(files):
        name, options = entry if isinstance(entry, tuple) else (entry, {})
        seedFileTurn(storage, conversation, seq, name, **options)
    return store, conversation


def assertNothingStored() -> None:
    assert db.read({}, 'Conversations') == []
    assert db.read({}, 'ChatTurns') == []


# ============================ A. validation ============================ #

OTHER_USER = 'ch-someone-else'
REFUSALS = {
    'fiveFiles': (lambda uid: [urlOf(mine(uid, f'{i}.jpg'))
                               for i in range(5)],
                  ChatActions.ATTACHMENTS_COUNT),
    'notAList': (lambda uid: urlOf(mine(uid, 'a.jpg')),
                 ChatActions.ATTACHMENTS_COUNT),
    'notText': (lambda uid: [5], 'attachments[0] must be text'),
    'sameFileTwice': (lambda uid: [urlOf(mine(uid, 'a.jpg'))] * 2,
                      ChatActions.ATTACHMENTS_TWICE),
    'twoPdfs': (lambda uid: [urlOf(mine(uid, 'a.pdf')),
                             urlOf(mine(uid, 'b.pdf'))],
                ChatActions.ONE_PDF_PER_MESSAGE),
    'anotherBucket': (lambda uid: [urlOf(mine(uid, 'a.jpg'),
                                         bucket='other-bucket')],
                      'attachments[0] is not a file uploaded to this app'),
    'anotherHost': (lambda uid: [urlOf(mine(uid, 'a.jpg'),
                                       host='evil.example')],
                    'attachments[0] is not a file uploaded to this app'),
    'hostileUrl': (lambda uid: ['javascript:alert(1)'],
                   'attachments[0] is not a file uploaded to this app'),
    'anotherUsersFolder': (lambda uid: [urlOf(mine(OTHER_USER, 'a.jpg'))],
                           'attachments[0] must be a file you uploaded'),
    'notUnderUsers': (lambda uid: [urlOf(f'stores/{uid}/a.jpg')],
                      'attachments[0] must be a file you uploaded'),
    'gif': (lambda uid: [urlOf(mine(uid, 'a.gif'))],
            f'attachments[0] {ChatActions.ATTACHMENT_TYPE}'),
    'noExtension': (lambda uid: [urlOf(mine(uid, 'a'))],
                    f'attachments[0] {ChatActions.ATTACHMENT_TYPE}'),
    'heic': (lambda uid: [urlOf(mine(uid, 'a.HEIC'))],
             photoStorage.CONVERT_PHOTO),
    'heif': (lambda uid: [urlOf(mine(uid, 'a.heif'))],
             photoStorage.CONVERT_PHOTO),
    'secondFileBad': (lambda uid: [urlOf(mine(uid, 'a.jpg')),
                                   urlOf(mine(uid, 'b.gif'))],
                      f'attachments[1] {ChatActions.ATTACHMENT_TYPE}'),
}


@pytest.mark.parametrize('case', list(REFUSALS))
def test_aBadAttachmentList_isRefusedBeforeAnySlotLockQuotaOrStorageRead(
        storage, firebase, model, case):
    store, customerId = customerCtx(firebase)
    build, message = REFUSALS[case]
    urls = build(customerId)
    fake = model(reply('never'))

    with pytest.raises(ValueError) as error:
        sendCustomer(customerId, store['_id'], 'hello', urls)

    assert message in str(error.value)
    if isinstance(urls, list):
        for url in urls:
            assert str(url) not in str(error.value)
    assert 'users/' not in str(error.value)
    assert fake.requests == []
    assert storage.lookups == [] and storage.downloads == []
    assert chatCounts() == {}
    assertNothingStored()


def test_fourFilesOfEveryType_areAccepted_andReachTheModelInOrder(
        storage, model):
    store = createTestStore(ADMIN)
    names = ['a.JPG', 'b.png', 'c.jpeg', 'd.pdf']
    for name in names:
        storage.put(mine(ADMIN, name))
    fake = model(reply('seen'))

    res = sendAdmin('look', [urlOf(mine(ADMIN, name)) for name in names])

    assert res['reply'] == 'seen'
    [message] = userMessages(fake.requests[0])
    # An admin's image note names its URL (no token), a PDF's does not.
    assert message['content'] == [
        {'type': 'text', 'text': (
            'look\n\n'
            f'Attachment 1: image a.JPG, url: {publicUrl(mine(ADMIN, "a.JPG"))}\n'
            f'Attachment 2: image b.png, url: {publicUrl(mine(ADMIN, "b.png"))}\n'
            f'Attachment 3: image c.jpeg, url: {publicUrl(mine(ADMIN, "c.jpeg"))}\n'
            'Attachment 4: PDF d.pdf')},
        {'type': 'image_url', 'image_url': {'url': dataUrl('image/jpeg',
                                                           JPG)}},
        {'type': 'image_url', 'image_url': {'url': dataUrl('image/png',
                                                           PNG)}},
        {'type': 'image_url', 'image_url': {'url': dataUrl('image/jpeg',
                                                           JPG)}},
        {'type': 'file', 'file': {'filename': 'd.pdf',
                                  'file_data': dataUrl('application/pdf',
                                                       PDF)}},
    ]
    assert storage.downloads == [mine(ADMIN, name) for name in names]


def test_aWebP_reachesTheModelAsAJpeg_andIsStoredAsTheWebPItIs(
        storage, model):
    from test_photos import webp
    createTestStore(ADMIN)
    photo = webp('RGB')
    storage.put(mine(ADMIN, 'a.webp'), photo, 'image/webp')
    fake = model(reply('seen'))

    res = sendAdmin('look', [urlOf(mine(ADMIN, 'a.webp'))])

    [message] = userMessages(fake.requests[0])
    [_, part] = message['content']
    assert part['type'] == 'image_url'
    assert part['image_url']['url'].startswith('data:image/jpeg;base64,')
    [turn] = turnDocs(res['conversationId'])
    [attachment] = turn['attachments']
    assert attachment['contentType'] == 'image/webp'
    assert attachment['size'] == len(photo)


def test_aFilesOnlyMessage_forACustomer_hasNoUrl_andTitlesTheConversation(
        storage, firebase, model):
    store, customerId = customerCtx(firebase)
    storage.put(mine(customerId, 'a.jpg'))
    fake = model(reply('nice'))

    res = sendCustomer(customerId, store['_id'], None,
                       [urlOf(mine(customerId, 'a.jpg'))])

    assert res['userText'] == ''
    assert userMessages(fake.requests[0])[-1] == {
        'role': 'user',
        'content': [{'type': 'text', 'text': 'Attachment 1: image a.jpg'},
                    {'type': 'image_url',
                     'image_url': {'url': dataUrl('image/jpeg', JPG)}}]}
    conversation = conversationDoc(res['conversationId'])
    assert conversation['title'] == 'a.jpg'
    assert turnDocs(res['conversationId'])[0]['userText'] == ''
    assert chatCounts() == keptCounts(customerId, store['_id'], 1)


def test_theMessageText_isOptionalOnlyWithFiles(storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.jpg'))
    url = urlOf(mine(ADMIN, 'a.jpg'))
    fake = model(reply('1'), reply('2'), reply('3'))

    for message in (None, '', '   '):
        res = sendAdmin(message, [url])
        assert turnDocs(res['conversationId'])[0]['userText'] == ''
    assert len(fake.requests) == 3

    for message in ('', '   \n'):
        with pytest.raises(ValueError, match=re.escape(ChatActions.EMPTY_MESSAGE)):
            sendAdmin(message)
    with pytest.raises(ValueError, match='Your message'):
        sendAdmin('x' * (ChatActions.CHAT_MESSAGE_MAX_LENGTH + 1), [url])
    assert len(fake.requests) == 3


def test_aFormAnswerWithFiles_isRefusedBeforeAnything(storage, firebase,
                                                      model):
    store, customerId = customerCtx(firebase)
    conversation = seedConversation(customerId, store['_id'], turnCount=1)
    seedTurn(conversation, 0)
    answer = LocationAnswer(kind='location', formId='form1', latitude=1.0,
                            longitude=2.0)
    fake = model(reply('never'))

    with pytest.raises(ValueError, match=re.escape(ChatActions.FORM_WITH_ATTACHMENTS)):
        api.sendStoreChatMessage(customerId, store['_id'], None,
                                 conversation['_id'], answer,
                                 [urlOf(mine(customerId, 'a.jpg'))])

    assert fake.requests == [] and storage.lookups == []
    assert chatCounts() == {}
    assert conversationDoc(conversation['_id'])['busyUntil'] is None


@pytest.mark.parametrize('path, expected', [
    ('users/u/chat/a.jpg', 'a.jpg'),
    ('users/u/chat/my file (1)-x_y.png', 'my file (1)-x_y.png'),
    ('users/u/chat/a<b>/?.jpg', '_.jpg'),
    ('users/u/chat/a<b>?.jpg', 'a_b__.jpg'),
    ('users/u/chat/é.pdf', 'é.pdf'),
    ('users/u/chat/', ChatActions.ATTACHMENT_TITLE),
    ('users/u/chat/   ', ChatActions.ATTACHMENT_TITLE),
])
def test_attachmentName_isTheSanitisedLastSegment(path, expected):
    assert ChatActions.attachmentName(path) == expected


def test_attachmentName_aLongNameKeepsItsExtension():
    limit = ChatActions.CHAT_ATTACHMENT_NAME_MAX_LENGTH

    withExtension = ChatActions.attachmentName('users/u/chat/' + 'a' * 300 +
                                               '.jpg')
    without = ChatActions.attachmentName('users/u/chat/' + 'b' * 300)

    assert len(withExtension) == limit and withExtension.endswith('aa.jpg')
    assert without == 'b' * limit


# ======================= B. contents and metadata ======================= #

NOT_REAL = ChatActions.NOT_A_REAL_ATTACHMENT


@pytest.mark.parametrize('name, data', [
    ('a.png', JPG),
    ('a.pdf', PNG),
    ('a.jpg', PDF),
    ('a.jpg', b''),
    ('a.jpg', b'\xff\xd8'),
    ('a.png', PNG[:7]),
    ('a.pdf', b'%PDF'),
])
def test_aFileOfAnotherType_isRefused_inTheChatsWords(storage, model, name,
                                                      data):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, name), data)
    fake = model(reply('never'))

    with pytest.raises(ValueError) as error:
        sendAdmin('hello', [urlOf(mine(ADMIN, name))])

    assert str(error.value) == f'attachments[0] {NOT_REAL}'
    assert 'photo' not in str(error.value)
    assert fake.requests == []
    assertNothingStored()


def test_aHeicHiddenUnderAJpgName_asksToConvert(storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.jpg'), b'\x00\x00\x00\x18ftypheic' + b'0' * 8)
    model(reply('never'))

    with pytest.raises(ValueError) as error:
        sendAdmin('hello', [urlOf(mine(ADMIN, 'a.jpg'))])

    assert photoStorage.CONVERT_PHOTO in str(error.value)
    assert 'users/' not in str(error.value)


def giveBackRows():
    from google.api_core.exceptions import NotFound, ServiceUnavailable as Down
    cap = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX

    def setBusy(bucket, path, monkeypatch):
        bucket.put(path)
        monkeypatch.setattr(ChatActions, '_bytesInFlight', cap - 1)

    def failLookup(bucket, path, monkeypatch):
        bucket.put(path)
        bucket.lookupError = Down('SENTINEL-STORAGE-DETAIL')

    def failDownload(bucket, path, monkeypatch):
        bucket.put(path)
        bucket.downloadError = NotFound('gone')

    return {
        'missing': (ValueError, 'was not found: upload it again',
                    lambda b, p, m: None),
        'storedTypeDisagreesWithTheName': (
            ValueError, ChatActions.ATTACHMENT_TYPE,
            lambda b, p, m: b.put(p, PNG, contentType='image/png')),
        'storedAsHeic': (
            ValueError, photoStorage.CONVERT_PHOTO,
            lambda b, p, m: b.put(p, contentType='image/heic')),
        'sizeUnknown': (ValueError, 'is over 10 MB',
                        lambda b, p, m: b.put(p, size=None)),
        'overTenMb': (ValueError, 'is over 10 MB',
                      lambda b, p, m: b.put(p, size=MAX + 1)),
        'sizeUnderstated': (
            ValueError, 'is over 10 MB',
            lambda b, p, m: b.put(p, JPG + b'0' * MAX, size=10)),
        'badSignature': (ValueError, NOT_REAL,
                         lambda b, p, m: b.put(p, PNG)),
        'goneBeforeTheDownload': (ValueError, 'was not found', failDownload),
        'storage503': (ServiceUnavailable, 'photo storage', failLookup),
        'processBusy': (ServiceUnavailable, 'attachment reading', setBusy),
    }


@pytest.mark.parametrize('case', list(giveBackRows()))
def test_aFileRefusedInStorage_givesBothQuotasBack_andStoresNothing(
        storage, firebase, model, monkeypatch, case):
    expected, message, setUp = giveBackRows()[case]
    store, customerId = customerCtx(firebase)
    path = mine(customerId, 'a.jpg')
    setUp(storage, path, monkeypatch)
    inFlight = ChatActions._bytesInFlight
    fake = model(reply('never'))

    with pytest.raises(expected) as error:
        sendCustomer(customerId, store['_id'], 'hello', [urlOf(path)])

    assert ChatActions._bytesInFlight == inFlight  # nothing held, none freed
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)
    assert message.lower() in str(error.value).lower()
    assert path not in str(error.value)
    assert fake.requests == []
    assert chatCounts() == {}
    assertNothingStored()


def test_filesOverTheSharedBudget_areRefusedByMetadata_beforeAnyDownload(
        storage, firebase, model):
    store, customerId = customerCtx(firebase)
    names = ['a.jpg', 'b.png', 'c.jpg']
    for name in names:
        storage.put(mine(customerId, name), size=8 * MEGABYTE)
    fake = model(reply('never'))

    with pytest.raises(ValueError) as error:
        sendCustomer(customerId, store['_id'], 'hello',
                     [urlOf(mine(customerId, name)) for name in names])

    assert str(error.value) == ChatActions.ATTACHMENTS_TOO_LARGE
    assert '20 MB' in str(error.value)
    assert storage.downloads == [] and fake.requests == []
    assert chatCounts() == {}
    assertNothingStored()


def test_twoFilesOfExactlyTenMb_makeExactlyTheBudget_andAreAccepted(
        storage, model):
    createTestStore(ADMIN)
    big = JPG + b'0' * (MAX - len(JPG))
    storage.put(mine(ADMIN, 'a.jpg'), big)
    storage.put(mine(ADMIN, 'b.jpg'), big)
    fake = model(reply('ok'))
    held = []
    fake.onCall = lambda i: held.append(ChatActions._bytesInFlight)

    res = sendAdmin('two big ones', [urlOf(mine(ADMIN, 'a.jpg')),
                                     urlOf(mine(ADMIN, 'b.jpg'))])

    assert res['reply'] == 'ok'
    assert held == [TOTAL]  # reserved from the sizes, held during the call
    [turn] = turnDocs(res['conversationId'])
    assert [a['size'] for a in turn['attachments']] == [MAX, MAX]


def test_theBudgets_areConsistent():
    inFlight = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX

    assert inFlight >= 3 * TOTAL  # three full turns at once
    assert CHAT_ATTACHMENTS_MAX * MAX > TOTAL  # the shared budget binds
    assert TOTAL >= 2 * MAX
    assert chatModel.IMAGE_WEIGHT == 1
    assert chatModel.PDF_IMAGE_WEIGHT == chatModel.MAX_IMAGES_PER_REQUEST
    assert ChatActions._imageWeight('image/jpeg') == 1
    assert ChatActions._imageWeight('image/png') == 1
    assert ChatActions._imageWeight('application/pdf') == 50
    assert (ChatActions.CUSTOMER_DAILY_ATTACHMENT_CAP
            <= ChatActions.STORE_DAILY_ATTACHMENT_CAP
            <= ChatActions.DEFAULT_CHAT_ATTACHMENTS_DAILY_CAP_ALL)


def test_bytesInFlight_reservesToTheCap_andReleasesWhatItHolds(monkeypatch):
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)
    cap = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX
    first, second = ChatActions._BytesInFlight(), ChatActions._BytesInFlight()

    assert first.reserve(cap - 10)
    assert not second.reserve(11)
    assert second.held == 0 and ChatActions._bytesInFlight == cap - 10
    assert second.reserve(10)
    assert not first.reserve(1) and first.held == cap - 10
    first.release()
    assert first.held == 0 and ChatActions._bytesInFlight == 10
    first.release()  # nothing more to give back
    assert ChatActions._bytesInFlight == 10
    assert first.reserve(cap - 10) and not first.reserve(1)
    first.release()
    second.release()
    assert ChatActions._bytesInFlight == 0
    assert second.reserve(cap)  # exactly the cap fits
    second.release()
    assert ChatActions._bytesInFlight == 0


def test_bytesInFlight_neverCountsMoreThanTheCap_underThreads(monkeypatch):
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)
    cap = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX
    barrier = threading.Barrier(8)
    won = []

    def grab():
        holder = ChatActions._BytesInFlight()
        barrier.wait()
        if holder.reserve(cap // 3 + 1):
            won.append(holder)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(won) == 2 and ChatActions._bytesInFlight <= cap
    for holder in won:
        holder.release()
    assert ChatActions._bytesInFlight == 0


# ===================== C. what the stored turn carries ===================== #


def test_theStoredTurn_hasPathTypeNameAndSizeOnly_neverBytes(storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.png'))
    storage.put(mine(ADMIN, 'b.pdf'))
    model(reply('ok'))

    res = sendAdmin('look', [urlOf(mine(ADMIN, 'a.png')),
                             urlOf(mine(ADMIN, 'b.pdf'))])

    [raw] = db.read({'conversationId': res['conversationId']}, 'ChatTurns')
    assert raw['attachments'] == [
        {'path': mine(ADMIN, 'a.png'), 'contentType': 'image/png',
         'name': 'a.png', 'size': len(PNG)},
        {'path': mine(ADMIN, 'b.pdf'), 'contentType': 'application/pdf',
         'name': 'b.pdf', 'size': len(PDF)},
    ]
    stored = json.dumps(raw, default=str)
    for data in (PNG, PDF):
        assert b64(data) not in stored
    assert 'png-bytes' not in stored and 'pdf-bytes' not in stored
    assert 'data:' not in stored and 'base64' not in stored
    assert raw['userText'] == 'look'


def test_getConversationTurns_sendsNameTypeAndPath_andNoKeyWithoutFiles(
        storage, model):
    createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.png'))
    model(reply('1'), reply('2'))
    first = sendAdmin('with a file', [urlOf(mine(ADMIN, 'a.png'))])
    sendAdmin('plain', conversationId=first['conversationId'])

    res = api.getConversationTurns(ADMIN, first['conversationId'])

    plain, withFile = res['turns']  # newest first
    assert 'attachments' not in plain
    assert withFile['attachments'] == [
        {'name': 'a.png', 'contentType': 'image/png',
         'path': mine(ADMIN, 'a.png')}]
    assert 'size' not in json.dumps(withFile['attachments'])
    with pytest.raises(ValueError):
        api.getConversationTurns('somebody-else', first['conversationId'])


def test_aTurnStoredBeforeAttachments_stillLoads_andIsReplayed(storage,
                                                               model):
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], 'admin',
                                    turnCount=1)
    seedTurn(conversation, 0)
    # Stored before attachments existed: no attachments key at all.
    db.db['ChatTurns'].update_one({'conversationId': conversation['_id']},
                                  {'$unset': {'attachments': ''}})
    fake = model(reply('ok'))

    turns = api.getConversationTurns(ADMIN, conversation['_id'])['turns']
    sendAdmin('next', conversationId=conversation['_id'])

    assert 'attachments' not in turns[0]
    assert [m['content'] for m in fake.requests[0]['messages']
            if m['role'] in ('user', 'assistant')] == ['q0', 'a0', 'next']


# ============================ D. history ============================ #


def test_onlyTheLast3TurnsFiles_areSentAgain_theOthersAreNotes(storage,
                                                               model):
    store, conversation = adminWithHistory(
        storage, [f't{n}.jpg' for n in range(5)])
    fake = model(reply('ok'))

    sendAdmin('hello', conversationId=conversation['_id'])

    request = fake.requests[0]
    assert resentTurns(request) == {2, 3, 4}
    assert storage.downloads == [mine(ADMIN, f't{n}.jpg')
                                 for n in (4, 3, 2)]
    users = userMessages(request)
    for n in (0, 1):
        assert users[n]['content'] == (
            f'q{n}\n\n[Earlier attachment: image t{n}.jpg, url: '
            f'{publicUrl(mine(ADMIN, f"t{n}.jpg"))}]')
    for n in (2, 3, 4):
        data = BY_EXTENSION['.jpg'] + f't{n}.jpg'.encode()
        assert users[n]['content'] == [
            {'type': 'text', 'text': (
                f'q{n}\n\nAttachment 1: image t{n}.jpg, url: '
                f'{publicUrl(mine(ADMIN, f"t{n}.jpg"))}')},
            {'type': 'image_url',
             'image_url': {'url': dataUrl('image/jpeg', data)}}]
    assert users[5]['content'] == 'hello'


def test_aCustomersHistoryNotes_carryNoUrl(storage, firebase, model):
    store, customerId = customerCtx(firebase)
    conversation = seedConversation(customerId, store['_id'], turnCount=5)
    for seq in range(5):
        seedFileTurn(storage, conversation, seq, f't{seq}.jpg')
    fake = model(reply('ok'))

    sendCustomer(customerId, store['_id'], 'hello', None,
                 conversation['_id'])

    users = userMessages(fake.requests[0])
    assert users[0]['content'] == 'q0\n\n[Earlier attachment: image t0.jpg]'
    assert textOf(users[4]) == 'q4\n\nAttachment 1: image t4.jpg'
    assert 'firebasestorage' not in json.dumps(fake.requests[0])


@pytest.mark.parametrize('own, resent', [
    # A PDF takes all 50 images: no history file fits.
    ('own.pdf', set()),
    # An image leaves 49: the newest history file is a PDF (50), and the
    # files after it wait behind it (the first that overflows ends it).
    ('own.jpg', set()),
    # Nothing of its own leaves 50: the PDF fits, the image after it not.
    (None, {2}),
])
def test_historyFiles_fitWhatTheMessageLeavesOfMetasImageLimit(
        storage, model, own, resent):
    store, conversation = adminWithHistory(
        storage, ['t0.jpg', 't1.jpg', 't2.pdf'])
    if own:
        storage.put(mine(ADMIN, own))
    fake = model(reply('ok'))

    sendAdmin('hello', [urlOf(mine(ADMIN, own))] if own else None,
              conversationId=conversation['_id'])

    request = fake.requests[0]
    assert resentTurns(request) == resent
    users = userMessages(request)
    for n in range(3):
        if n not in resent:
            assert '[Earlier attachment: ' in textOf(users[n])
    if resent:
        [part] = partsOf(users[2])
        assert part == {'type': 'file', 'file': {
            'filename': 't2.pdf',
            'file_data': dataUrl('application/pdf',
                                 PDF + b't2.pdf')}}
    # Its own file first, then the history's that fit.
    assert storage.downloads == (
        ([mine(ADMIN, own)] if own else []) +
        ([mine(ADMIN, 't2.pdf')] if resent else []))


@pytest.mark.parametrize('ownFiles, resent', [
    (1, {3, 4}),  # 10 MB of its own leaves 10: two 4 MB files, not a third
    (2, set()),  # 20 MB of its own leaves nothing, never below 0
])
def test_historyGetsWhatTheMessageLeavesOfTheSharedBudget(
        storage, model, caplog, ownFiles, resent):
    store, conversation = adminWithHistory(storage, [
        't0.jpg', 't1.jpg',
        *[(f't{n}.jpg', {'size': 4 * MEGABYTE, 'storedSize': 4 * MEGABYTE})
          for n in (2, 3, 4)]])
    big = JPG + b'0' * (MAX - len(JPG))
    urls = []
    for i in range(ownFiles):
        storage.put(mine(ADMIN, f'own{i}.jpg'), big)
        urls.append(urlOf(mine(ADMIN, f'own{i}.jpg')))
    fake = model(reply('ok'))
    caplog.set_level(logging.INFO)

    sendAdmin('hello', urls, conversationId=conversation['_id'])

    assert resentTurns(fake.requests[0]) == resent
    # Over the budget by the stored size: an Earlier note, no event.
    assert logged(caplog, 'chatHistoryAttachmentsSkipped') == []
    assert logged(caplog, 'chatHistoryAttachmentsGone') == []


def test_aTurnDroppedByTheHistoryLimit_isNeitherReadNorNoted(storage, model):
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], 'admin',
                                    turnCount=4)
    seedFileTurn(storage, conversation, 1, 't1.jpg')
    seedFileTurn(storage, conversation, 2, 't2.jpg')
    seedFileTurn(storage, conversation, 3, 't3.jpg')
    # Turn 2's reply alone fills the history: it and everything older drop.
    db.db['ChatTurns'].update_one(
        {'conversationId': conversation['_id'], 'seq': 2},
        {'$set': {'replyText': 'x' * ChatActions.HISTORY_MAX_CHARS}})
    fake = model(reply('ok'))

    sendAdmin('hello', conversationId=conversation['_id'])

    texts = ' '.join(textOf(m) for m in userMessages(fake.requests[0]))
    assert resentTurns(fake.requests[0]) == {3}
    assert storage.downloads == [mine(ADMIN, 't3.jpg')]
    assert 't2.jpg' not in texts and 't1.jpg' not in texts
    assert 'q2' not in texts and 'q1' not in texts


@pytest.mark.parametrize('how', ['absent', 'goneAtTheDownload'])
def test_aHistoryFileThatIsGone_isANote_andOneCountedEvent(
        storage, model, caplog, how):
    from google.api_core.exceptions import NotFound
    store, conversation = adminWithHistory(storage, ['t0.jpg'])
    if how == 'absent':
        del storage.objects[mine(ADMIN, 't0.jpg')]
    else:
        storage.downloadError = NotFound('gone')
    fake = model(reply('ok'))
    caplog.set_level(logging.INFO)

    res = sendAdmin('hello', conversationId=conversation['_id'])

    assert res['reply'] == 'ok'
    assert textOf(userMessages(fake.requests[0])[0]) == (
        'q0\n\n[Earlier attachment no longer available: t0.jpg]')
    [event] = logged(caplog, 'chatHistoryAttachmentsGone')
    assert event.msg == {'event': 'chatHistoryAttachmentsGone',
                         'conversationId': conversation['_id'], 'count': 1}
    assert logged(caplog, 'chatHistoryAttachmentsSkipped') == []


HISTORY_SKIPS = {
    # Small in the turn, but the object itself is not what the turn says.
    'overTenMb': {'size': MAX + 1},
    'sizeUnknown': {'size': None},
    'anotherStoredType': {'contentType': 'image/png'},
    'badSignature': {'data': PNG},
    'sizeUnderstated': {'data': JPG + b'0' * MAX, 'size': 10},
}


@pytest.mark.parametrize('case', list(HISTORY_SKIPS))
def test_aHistoryFileThatCannotBeSentAgain_isANote_andOneCountedEvent(
        storage, model, caplog, case):
    store, conversation = adminWithHistory(
        storage, [('t0.jpg', HISTORY_SKIPS[case])])
    fake = model(reply('ok'))
    caplog.set_level(logging.INFO)

    res = sendAdmin('hello', conversationId=conversation['_id'])

    assert res['reply'] == 'ok'
    assert textOf(userMessages(fake.requests[0])[0]) == (
        'q0\n\n[Earlier attachment could not be sent again: t0.jpg]')
    [event] = logged(caplog, 'chatHistoryAttachmentsSkipped')
    assert event.msg == {'event': 'chatHistoryAttachmentsSkipped',
                         'conversationId': conversation['_id'], 'count': 1}
    assert logged(caplog, 'chatHistoryAttachmentsGone') == []


def test_historyFiles_pastTheBudgetByTheirRealSize_areSkippedOldestFirst(
        storage, model, caplog):
    store, conversation = adminWithHistory(storage, [
        (f't{n}.jpg', {'size': 9 * MEGABYTE}) for n in range(3)])
    fake = model(reply('ok'))
    caplog.set_level(logging.INFO)

    sendAdmin('hello', conversationId=conversation['_id'])

    assert resentTurns(fake.requests[0]) == {1, 2}
    assert textOf(userMessages(fake.requests[0])[0]) == (
        'q0\n\n[Earlier attachment could not be sent again: t0.jpg]')
    assert storage.downloads == [mine(ADMIN, 't2.jpg'),
                                 mine(ADMIN, 't1.jpg')]
    [event] = logged(caplog, 'chatHistoryAttachmentsSkipped')
    assert event.msg['count'] == 1


def test_aHistoryFileTheProcessCannotHold_isANote_andIsNeverDownloaded(
        storage, model, monkeypatch, caplog):
    store, conversation = adminWithHistory(storage, ['t0.jpg'])
    cap = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX
    monkeypatch.setattr(ChatActions, '_bytesInFlight', cap)
    fake = model(reply('ok'))
    caplog.set_level(logging.INFO)

    res = sendAdmin('hello', conversationId=conversation['_id'])

    assert res['reply'] == 'ok'
    assert textOf(userMessages(fake.requests[0])[0]) == (
        'q0\n\n[Earlier attachment could not be sent again: t0.jpg]')
    assert storage.downloads == []
    assert len(logged(caplog, 'chatHistoryAttachmentsSkipped')) == 1
    assert ChatActions._bytesInFlight == cap
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)


def test_aMessageTheProcessCannotHold_is503_withNothingDownloaded(
        storage, firebase, model, monkeypatch):
    store, customerId = customerCtx(firebase)
    storage.put(mine(customerId, 'a.jpg'))
    cap = ChatActions.CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX
    monkeypatch.setattr(ChatActions, '_bytesInFlight', cap - 5)
    existing = seedConversation(customerId, store['_id'], turnCount=1)
    seedTurn(existing, 0)
    fake = model(reply('never'))

    with pytest.raises(ServiceUnavailable) as error:
        sendCustomer(customerId, store['_id'], 'hello',
                     [urlOf(mine(customerId, 'a.jpg'))], existing['_id'])

    assert ChatActions._bytesInFlight == cap - 5
    monkeypatch.setattr(ChatActions, '_bytesInFlight', 0)
    assert routeFailure(error.value, status=400).message.startswith(
        'Attachment reading is unavailable')
    assert fake.requests == [] and storage.downloads == []
    assert chatCounts() == {}  # both quotas given back
    assert conversationDoc(existing['_id'])['busyUntil'] is None
    assert len(turnDocs(existing['_id'])) == 1


# ===================== E. Meta's 400, the retry, the quotas ===================== #


def customerWithHistoryFile(storage, firebase, turns: int = 1):
    store, customerId = customerCtx(firebase)
    conversation = seedConversation(customerId, store['_id'],
                                    turnCount=turns)
    for seq in range(turns):
        seedFileTurn(storage, conversation, seq, 'a.png')
    return store, customerId, conversation


def errors(caplog) -> list:
    return logged(caplog, 'chatModelError')


def test_aFirst400_onAMessageWithFiles_isUnreadable_andKeepsTheQuotas(
        storage, firebase, model, caplog):
    store, customerId = customerCtx(firebase)
    storage.put(mine(customerId, 'a.jpg'))
    existing = seedConversation(customerId, store['_id'], turnCount=1)
    seedTurn(existing, 0)
    fake = model(sdkError(openai.BadRequestError, 400))
    caplog.set_level(logging.INFO)

    with pytest.raises(ValueError) as error:
        sendCustomer(customerId, store['_id'], 'hello',
                     [urlOf(mine(customerId, 'a.jpg'))], existing['_id'])

    assert str(error.value) == ChatActions.UNREADABLE_ATTACHMENT
    assert len(fake.requests) == 1  # a 400 is never retried
    assert chatCounts() == keptCounts(customerId, store['_id'], 1)
    [record] = errors(caplog)
    assert record.levelno == logging.ERROR
    assert record.msg == {'event': 'chatModelError',
                          'errorClass': 'BadRequestError', 'status': 400,
                          'refused': True}
    assert ERROR_TEXT not in caplog.text
    assert len(turnDocs(existing['_id'])) == 1
    assert conversationDoc(existing['_id'])['busyUntil'] is None
    assert conversationDoc(existing['_id'])['turnCount'] == 1


def test_a400AfterAToolRound_isOurs_a503_andKeepsTheQuotas(
        storage, model, caplog):
    store = createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.jpg'))
    existing = seedConversation(ADMIN, store['_id'], 'admin', turnCount=1)
    seedTurn(existing, 0)
    fake = model(calls(('c1', 'getMyStorePolicy', '{}')),
                 sdkError(openai.BadRequestError, 400))
    caplog.set_level(logging.INFO)

    with pytest.raises(ServiceUnavailable):
        sendAdmin('hello', [urlOf(mine(ADMIN, 'a.jpg'))], existing['_id'])

    assert len(fake.requests) == 2
    # The model answered (a tool ran): the admin's file counters stay.
    assert chatCounts() == {
        attachmentKey(STORE_ATTACHMENTS, store['_id']): 1,
        attachmentKey(ALL_ATTACHMENTS, usageCounters.ALL_STORES): 1}
    [record] = errors(caplog)
    assert record.levelno == logging.ERROR and 'refused' not in record.msg
    assert len(turnDocs(existing['_id'])) == 1
    assert conversationDoc(existing['_id'])['busyUntil'] is None


MODEL_FAILURES = {
    # (script, own files, error, quotas kept)
    'a400WithoutFiles': (
        lambda c: [sdkError(openai.BadRequestError, 400)], False,
        ServiceUnavailable, False),
    'unreachableWithFiles': (
        lambda c: [sdkError(openai.InternalServerError, 500)
                   for _ in range(9)], True, ServiceUnavailable, False),
    'a400WithFiles': (
        lambda c: [sdkError(openai.BadRequestError, 400)], True,
        ValueError, True),
    'unreachableAfterATool': (
        lambda c: [c, *[sdkError(openai.InternalServerError, 500)
                        for _ in range(9)]], True, ServiceUnavailable, True),
    'a400AfterATool': (
        lambda c: [c, sdkError(openai.BadRequestError, 400)], True,
        ServiceUnavailable, True),
}


@pytest.mark.parametrize('case', list(MODEL_FAILURES))
def test_quotasAreGivenBack_onlyWhenTheModelNeverAnswered_andItWasNotTheFiles(
        storage, firebase, model, case):
    script, ownFiles, expected, kept = MODEL_FAILURES[case]
    store, customerId = customerCtx(firebase)
    urls = None
    if ownFiles:
        storage.put(mine(customerId, 'a.jpg'))
        urls = [urlOf(mine(customerId, 'a.jpg'))]
    fake = model(*script(calls(('c1', 'listStoreProducts', '{}'))))

    with pytest.raises(expected):
        sendCustomer(customerId, store['_id'], 'hello', urls)

    assert fake.requests
    assert chatCounts() == (keptCounts(customerId, store['_id'],
                                       1 if ownFiles else 0)
                            if kept else {})
    assertNothingStored()


def test_aRefusedHistory_isRetriedOnceWithItsFilesAsNotes(
        storage, firebase, model, caplog):
    store, customerId, conversation = customerWithHistoryFile(storage,
                                                              firebase)
    fake = model(sdkError(openai.BadRequestError, 400), reply('ok'))
    held = []
    fake.onCall = lambda i: held.append(ChatActions._bytesInFlight)
    caplog.set_level(logging.INFO)

    res = sendCustomer(customerId, store['_id'], 'hello', None,
                       conversation['_id'])

    assert res['reply'] == 'ok'
    first, second = fake.requests
    assert [partsOf(m) != [] for m in userMessages(first)[:-1]] == [True]
    assert [partsOf(m) for m in userMessages(second)] == [[], []]
    assert textOf(userMessages(second)[0]) == (
        'q0\n\n[Earlier attachment: image a.png]')
    assert userMessages(second)[-1] == userMessages(first)[-1]
    # The same reservation: nothing more held on the retry.
    assert held == [len(PNG + b'a.png')] * 2
    [event] = logged(caplog, 'chatHistoryAttachmentsRefused')
    assert event.levelno == logging.INFO
    assert event.msg == {'event': 'chatHistoryAttachmentsRefused',
                         'conversationId': conversation['_id'], 'count': 1}
    [refused] = errors(caplog)
    assert refused.levelno == logging.WARNING
    assert refused.msg == {'event': 'chatModelError',
                           'errorClass': 'BadRequestError', 'status': 400,
                           'refused': True}
    # One turn's quota, taken once.
    assert chatCounts() == {customerKey(customerId): 1,
                            storeKey(store['_id']): 1}
    assert len(turnDocs(conversation['_id'])) == 2
    assert conversationDoc(conversation['_id'])['busyUntil'] is None


def test_aRetryRefusedAgain_withNoFilesOfItsOwn_is503_andGivesTheQuotasBack(
        storage, firebase, model, caplog):
    store, customerId, conversation = customerWithHistoryFile(storage,
                                                              firebase)
    fake = model(sdkError(openai.BadRequestError, 400),
                 sdkError(openai.BadRequestError, 400))
    caplog.set_level(logging.INFO)

    with pytest.raises(ServiceUnavailable):
        sendCustomer(customerId, store['_id'], 'hello', None,
                     conversation['_id'])

    assert len(fake.requests) == 2
    assert [r.levelno for r in errors(caplog)] == [logging.WARNING,
                                                   logging.ERROR]
    assert chatCounts() == {}
    assert len(turnDocs(conversation['_id'])) == 1
    assert conversationDoc(conversation['_id'])['busyUntil'] is None


def test_aRetryRefusedAgain_withFilesOfItsOwn_isUnreadable_andKeepsTheQuotas(
        storage, firebase, model, caplog):
    store, customerId, conversation = customerWithHistoryFile(storage,
                                                              firebase)
    storage.put(mine(customerId, 'b.jpg'))
    fake = model(sdkError(openai.BadRequestError, 400),
                 sdkError(openai.BadRequestError, 400))
    caplog.set_level(logging.INFO)

    with pytest.raises(ValueError) as error:
        sendCustomer(customerId, store['_id'], 'hello',
                     [urlOf(mine(customerId, 'b.jpg'))],
                     conversation['_id'])

    assert str(error.value) == ChatActions.UNREADABLE_ATTACHMENT
    first, second = fake.requests
    assert len(partsOf(userMessages(first)[0])) == 1  # the history file
    assert partsOf(userMessages(second)[0]) == []  # now a note
    assert userMessages(second)[-1] == userMessages(first)[-1]  # its own
    assert len(partsOf(userMessages(second)[-1])) == 1
    assert [r.levelno for r in errors(caplog)] == [logging.WARNING,
                                                   logging.ERROR]
    assert chatCounts() == keptCounts(customerId, store['_id'], 1)
    assert len(turnDocs(conversation['_id'])) == 1


@pytest.mark.parametrize('ownFiles', [False, True])
def test_aRetryThatTheModelCannotReach_is503_andGivesEverythingBack(
        storage, firebase, model, ownFiles):
    store, customerId, conversation = customerWithHistoryFile(storage,
                                                              firebase)
    urls = None
    if ownFiles:
        storage.put(mine(customerId, 'b.jpg'))
        urls = [urlOf(mine(customerId, 'b.jpg'))]
    model(sdkError(openai.BadRequestError, 400),
          *[sdkError(openai.InternalServerError, 500) for _ in range(9)])

    with pytest.raises(ServiceUnavailable):
        sendCustomer(customerId, store['_id'], 'hello', urls,
                     conversation['_id'])

    assert chatCounts() == {}
    assert conversationDoc(conversation['_id'])['busyUntil'] is None
    assert len(turnDocs(conversation['_id'])) == 1


def test_noHistoryFiles_noRetry_evenWhenTheMessageHasFiles(storage, model):
    store, conversation = adminWithHistory(storage, [])
    storage.put(mine(ADMIN, 'a.jpg'))
    fake = model(sdkError(openai.BadRequestError, 400))

    with pytest.raises(ValueError, match=re.escape(ChatActions.UNREADABLE_ATTACHMENT)):
        sendAdmin('hello', [urlOf(mine(ADMIN, 'a.jpg'))])

    assert len(fake.requests) == 1


# ============================ F. daily caps ============================ #


def test_theCustomersFileCap_refusesTheWholeMessage_andCountsNothing(
        storage, firebase, model):
    store, customerId = customerCtx(firebase)
    for name in ('a.jpg', 'b.jpg'):
        storage.put(mine(customerId, name))
    urls = [urlOf(mine(customerId, name)) for name in ('a.jpg', 'b.jpg')]
    key = attachmentKey(CUSTOMER_ATTACHMENTS, customerId)
    usageDoc(key, ChatActions.CUSTOMER_DAILY_ATTACHMENT_CAP - 1)
    fake = model(reply('the 20th'))

    with pytest.raises(ValueError, match=re.escape(ChatActions.ATTACHMENT_CAP_REACHED)):
        sendCustomer(customerId, store['_id'], 'two', urls)

    # The chat quota taken first is given back, no file counter moved.
    assert chatCounts() == {key: 19}
    assert fake.requests == [] and storage.downloads == []
    assertNothingStored()

    sendCustomer(customerId, store['_id'], 'one', urls[:1])
    assert chatCounts()[key] == 20
    with pytest.raises(ValueError, match=re.escape(ChatActions.ATTACHMENT_CAP_REACHED)):
        sendCustomer(customerId, store['_id'], 'one more', urls[1:])
    assert chatCounts()[key] == 20
    assert len(fake.requests) == 1


def test_theStoresFileCap_refusesACustomerAndTheAdmin(storage, firebase,
                                                      model):
    store, customerId = customerCtx(firebase)
    for uid in (customerId, ADMIN):
        for name in ('a.jpg', 'b.jpg'):
            storage.put(mine(uid, name))
    key = attachmentKey(STORE_ATTACHMENTS, store['_id'])
    usageDoc(key, ChatActions.STORE_DAILY_ATTACHMENT_CAP - 1)
    fake = model(reply('never'))

    with pytest.raises(ValueError,
                       match=re.escape(ChatActions.STORE_ATTACHMENT_CAP_REACHED)):
        sendCustomer(customerId, store['_id'], 'two',
                     [urlOf(mine(customerId, n)) for n in ('a.jpg', 'b.jpg')])
    with pytest.raises(ValueError,
                       match=re.escape(ChatActions.STORE_ATTACHMENT_CAP_REACHED)):
        sendAdmin('two', [urlOf(mine(ADMIN, n)) for n in ('a.jpg', 'b.jpg')])

    assert chatCounts() == {key: 299}
    assert fake.requests == []


def test_anAdminsFiles_countAgainstTheStoreAndAllStoresOnly(storage, model):
    store = createTestStore(ADMIN)
    for name in ('a.jpg', 'b.png'):
        storage.put(mine(ADMIN, name))
    model(reply('ok'))

    sendAdmin('two', [urlOf(mine(ADMIN, n)) for n in ('a.jpg', 'b.png')])

    assert chatCounts() == {
        attachmentKey(STORE_ATTACHMENTS, store['_id']): 2,
        attachmentKey(ALL_ATTACHMENTS, usageCounters.ALL_STORES): 2}


def test_theAllStoresCap_isAnEnvironmentVariable_with0MeaningOff(
        storage, firebase, model, monkeypatch, caplog):
    assert ChatActions.DEFAULT_CHAT_ATTACHMENTS_DAILY_CAP_ALL == 3000
    store, customerId = customerCtx(firebase)
    for name in ('a.jpg', 'b.jpg'):
        storage.put(mine(customerId, name))
    urls = [urlOf(mine(customerId, name)) for name in ('a.jpg', 'b.jpg')]
    variable = ChatActions.CHAT_ATTACHMENTS_DAILY_CAP_ALL_VARIABLE
    fake = model(reply('1'), reply('2'), reply('3'), reply('4'))
    caplog.set_level(logging.INFO)

    # A cap of 1 refuses two files at once, and lets one in.
    monkeypatch.setenv(variable, '1')
    with pytest.raises(ValueError,
                       match=re.escape(ChatActions.STORE_ATTACHMENT_CAP_REACHED)):
        sendCustomer(customerId, store['_id'], 'two', urls)
    assert chatCounts() == {}
    sendCustomer(customerId, store['_id'], 'one', urls[:1])
    assert chatCounts()[attachmentKey(ALL_ATTACHMENTS,
                                      usageCounters.ALL_STORES)] == 1

    # 0 is attachments off: nothing counted, and a message without files
    # still goes through, counting no file.
    monkeypatch.setenv(variable, '0')
    before = chatCounts()
    with pytest.raises(ValueError,
                       match=re.escape(ChatActions.ATTACHMENTS_TURNED_OFF)):
        sendCustomer(customerId, store['_id'], 'one', urls[:1])
    assert chatCounts() == before
    sendCustomer(customerId, store['_id'], 'plain')
    after = chatCounts()
    assert {k: v for k, v in after.items() if 'attachments' in k} == {
        k: v for k, v in before.items() if 'attachments' in k}
    assert after[customerKey(customerId)] == before[
        customerKey(customerId)] + 1

    # A bad value falls back to the default and is logged without it.
    monkeypatch.setenv(variable, 'SENTINEL-NOT-A-NUMBER')
    sendCustomer(customerId, store['_id'], 'two again', urls)
    assert len(logged(caplog, 'chatAttachmentsDailyCapAllInvalid')) >= 1
    assert 'SENTINEL-NOT-A-NUMBER' not in caplog.text
    assert len(fake.requests) == 3


def test_theFileCounters_useTheUtcPlus8Day(storage, model, monkeypatch):
    store = createTestStore(ADMIN)
    storage.put(mine(ADMIN, 'a.jpg'))
    # 2099-03-10 17:30 UTC is 2099-03-11 01:30 in UTC+8.
    freezeNow(monkeypatch, datetime.datetime(
        2099, 3, 10, 17, 30, tzinfo=datetime.timezone.utc))
    model(reply('ok'))

    sendAdmin('hello', [urlOf(mine(ADMIN, 'a.jpg'))])

    day = datetime.date(2099, 3, 11)
    assert chatCounts() == {
        attachmentKey(STORE_ATTACHMENTS, store['_id'], day): 1,
        attachmentKey(ALL_ATTACHMENTS, usageCounters.ALL_STORES, day): 1}


# ===================== G. logs ===================== #


def test_logs_neverCarryAPathNameUrlOrContent(storage, firebase, model,
                                              caplog):
    secret = b'SENTINEL-BYTES'
    name = 'SENTINEL-NAME.jpg'
    store, customerId = customerCtx(firebase)
    conversation = seedConversation(customerId, store['_id'], turnCount=2)
    seedFileTurn(storage, conversation, 0, 'SENTINEL-GONE.jpg')
    del storage.objects[mine(customerId, 'SENTINEL-GONE.jpg')]
    seedFileTurn(storage, conversation, 1, 'SENTINEL-SKIPPED.jpg',
                 size=MAX + 1)
    storage.put(mine(customerId, name), JPG + secret)
    storage.put(mine(customerId, 'SENTINEL-BAD.jpg'), PNG)
    caplog.set_level(logging.INFO)
    model(reply('ok'), sdkError(openai.BadRequestError, 400),
          sdkError(openai.BadRequestError, 400))

    sendCustomer(customerId, store['_id'], 'hello',
                 [urlOf(mine(customerId, name))], conversation['_id'])
    with pytest.raises(ValueError):
        sendCustomer(customerId, store['_id'], 'again',
                     [urlOf(mine(customerId, name))], conversation['_id'])
    with pytest.raises(ValueError):
        sendCustomer(customerId, store['_id'], 'bad',
                     [urlOf(mine(customerId, 'SENTINEL-BAD.jpg'))])

    text = caplog.text
    for forbidden in ('SENTINEL', 'users/', 'firebasestorage',
                      b64(JPG + secret), 'data:image'):
        assert forbidden not in text
    [turnLog] = [r for r in logged(caplog, 'chatTurn')
                 if 'attachments' in r.msg]
    assert turnLog.msg['attachments'] == 1
    assert turnLog.msg['attachmentBytes'] == len(JPG + secret)
    assert len(logged(caplog, 'chatHistoryAttachmentsGone')) == 1
    assert len(logged(caplog, 'chatHistoryAttachmentsSkipped')) == 1


def test_aTurnWithoutFiles_logsNoAttachmentFields(storage, model, caplog):
    createTestStore(ADMIN)
    model(reply('ok'))
    caplog.set_level(logging.INFO)

    sendAdmin('hello')

    [record] = logged(caplog, 'chatTurn')
    assert 'attachments' not in record.msg
    assert 'attachmentBytes' not in record.msg
