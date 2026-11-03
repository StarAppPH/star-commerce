"""chatTurnKeys.py — one chat turn per turnKey, and where it is.

A phone drops a long request (iOS puts the page in the background), so the
client shows a failure while the server still runs the turn; its "Try
again" would then run the turn twice (in a new conversation when the first
turn's conversationId never came back, so no conversation lock stops it).
The client sends its turnKey (chatThinking.validTurnKey) with each message,
and:

  - claim(): before the turn's slot, the turn claims turnKeyId(userId,
    turnKey) in ChatTurnKeys, atomically (one upsert on the _id: of two
    racing sends one wins), with its own random claimId. A turnKey running
    or saved is refused (TURN_RUNNING, TURN_SAVED: a 400, before any lock,
    quota, model or tool); one still running past runningUntil (its process
    stopped) is taken over, with a new claimId;
  - markSaved(): in the transaction that saves the turn (ChatTurn.turnKey
    is stored too), only while the claim is still this turn's (its
    claimId): a claim taken over raises, so the save is aborted;
  - release(): a turn that ends unsaved deletes its own running claim (its
    claimId), so a refused or failed request leaves no document and the
    message may be sent again; never raising over the turn's own error;
  - status(): getChatTurnStatus reads the caller's own claim, so the client
    can ask, after a lost reply, whether to wait, open the conversation or
    send again (unknown: nothing claimed, released or expired).

A dedicated collection, not ChatThinking: that document is upserted by
every tool (chatThinking.note), so it cannot be the atomic claim, and it is
deleted when the turn ends and within THINKING_SECONDS, long before a phone
that slept asks. A saved claim lives TURN_KEY_SECONDS (TTL index on
expireAt).
"""

import datetime
import logging
import uuid

from pymongo.errors import DuplicateKeyError

from mongoDb import db
from objects import (ChatTurnKey, ChatTurnStatusResponse, projection_of,
                     turnKeyId)

TURN_KEYS = 'ChatTurnKeys'
# How long a claim is kept: a client asks about a lost reply long after.
TURN_KEY_SECONDS = 24 * 60 * 60
TURN_RUNNING = 'This message is still being answered: wait for its reply'
TURN_SAVED = ('This message was already answered: open the conversation to '
              'see the reply')
UTC = datetime.timezone.utc


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def claim(userId: str, turnKey: str | None,
          runningSeconds: float) -> str | None:
    """Claim the turn `turnKey` (checked) of `userId` for `runningSeconds`
    (the conversation lock's: past it a running claim may be taken over),
    returning its claimId; None for no turnKey. Refused with TURN_RUNNING
    or TURN_SAVED when it is running or saved."""
    if turnKey is None:
        return None
    now = _now()
    key = turnKeyId(userId, turnKey)
    claimId = uuid.uuid4().hex
    claimed = ChatTurnKey(
        _id=key,
        userId=userId,
        claimId=claimId,
        status='running',
        conversationId=None,
        runningUntil=now + datetime.timedelta(seconds=runningSeconds),
        expireAt=now + datetime.timedelta(seconds=TURN_KEY_SECONDS),
    ).model_dump(by_alias=True, exclude={'id', 'version'})
    try:
        # Matches only a claim whose turn ran past its time; none matching,
        # the upsert inserts it, and an existing one's _id refuses the
        # insert.
        db.findOneAndUpdate(
            {'_id': key, 'status': 'running', 'runningUntil': {'$lte': now}},
            {'$set': claimed}, TURN_KEYS, upsert=True,
            projection={'_id': 1})
    except DuplicateKeyError:
        held = db.read({'_id': key}, TURN_KEYS, projection={'status': 1},
                       findOne=True)
        raise ValueError(TURN_SAVED if held is not None
                         and held['status'] == 'saved'
                         else TURN_RUNNING) from None
    return claimId


def markSaved(userId: str, turnKey: str | None, claimId: str | None,
              conversationId: str, session=None) -> None:
    """The turn `turnKey` is saved, in `conversationId` (inside the turn's
    save transaction, `session`). Raises when the claim is no longer this
    turn's (`claimId`: taken over, or gone), so the save is aborted."""
    if turnKey is None:
        return
    marked = db.findOneAndUpdate(
        {'_id': turnKeyId(userId, turnKey), 'claimId': claimId,
         'status': 'running'},
        {'$set': {'status': 'saved', 'conversationId': conversationId}},
        TURN_KEYS, upsert=False, session=session, projection={'_id': 1})
    if marked is None:
        raise RuntimeError('The turnKey claim was lost mid-turn')


def release(userId: str, turnKey: str | None, claimId: str | None) -> None:
    """The turn `turnKey` ended: still running (not saved) and still this
    turn's (`claimId`), its claim is deleted. Never raises: a failure is
    logged (its class), so the turn's own error is the one that counts."""
    if turnKey is None:
        return
    try:
        db.delete({'_id': turnKeyId(userId, turnKey), 'claimId': claimId,
                   'status': 'running'}, TURN_KEYS)
    except Exception as error:
        logging.warning({'event': 'chatTurnKeyNotReleased',
                         'errorClass': type(error).__name__})


def status(userId: str, turnKey: str) -> dict:
    """Where the caller's turn `turnKey` (checked) is
    (ChatTurnStatusResponse): unknown when never claimed, released or
    expired; failed when still running past runningUntil."""
    held = db.read({'_id': turnKeyId(userId, turnKey), 'userId': userId},
                   TURN_KEYS,
                   projection={**projection_of(ChatTurnStatusResponse),
                               'runningUntil': 1},
                   findOne=True)
    if held is None:
        return {'status': 'unknown', 'conversationId': None}
    stale = (held['status'] == 'running'
             and held['runningUntil'] <= _now())
    return {'status': 'failed' if stale else held['status'],
            'conversationId': held.get('conversationId')}
