"""usageCounters: daily usage counters (chat messages, product photos,
voice seconds and characters).

A counter is one document of its collection, _id
"<kind>:<ownerId>:<YYYY-MM-DD>" (the app day, UTC+8), holding `count`; a
TTL index on expireAt (mongoDb.ensureIndexes) deletes it a day after its
day ends. takeQuota counts against a cap atomically, so racing callers
never pass it together; giveBackQuota takes back what a failed call
counted. takeQuotas and giveBackQuotas do the same for several counters
at once (a call counted per user and per store, say); takeDailyQuotas
adds the all-stores counter, its cap an environment variable.
"""

import datetime
import logging
import os

from pymongo.errors import DuplicateKeyError

from AppConfig import Timezone
from mongoDb import db
from utils import appDayStart

UTC = datetime.timezone.utc
# A counter that a racing first call of the day inserted between our read
# and write: retried once.
QUOTA_ATTEMPTS = 2
# The owner of the all-stores counters (a store _id is a random token).
ALL_STORES = 'all'


def appDay(now: datetime.datetime) -> datetime.date:
    """The app day (UTC+8) of the instant `now`."""
    return now.astimezone(Timezone().getTimezone()).date()


def usageExpiry(day: datetime.date) -> datetime.datetime:
    """When a counter of `day` is deleted: a day after the day ends."""
    return (appDayStart(day.isoformat()) +
            datetime.timedelta(days=2)).astimezone(UTC)


def envCap(variable: str, default: int, invalidEvent: str) -> int:
    """The daily cap the environment variable `variable` sets, read at each
    call: a whole number, 0 or more. Unset, it is `default`; anything else
    is refused (a warning, `invalidEvent`, never quoting the value) and
    `default` applies."""
    raw = (os.getenv(variable) or '').strip()
    if not raw:
        return default
    try:
        cap = int(raw)
    except ValueError:
        cap = -1
    if cap < 0:
        logging.warning({'event': invalidEvent, 'default': default})
        return default
    return cap


def usageKey(kind: str, ownerId: str, day: datetime.date) -> str:
    """The counter _id of `kind` (customer, store, photos) `ownerId` on
    `day`."""
    return f'{kind}:{ownerId}:{day.isoformat()}'


def takeQuota(collection: str, key: str, cap: int,
              expireAt: datetime.datetime, amount: int = 1) -> bool:
    """Count `amount` on the counter `key` of `collection`, unless that
    would take it past `cap`: atomic, so racing calls never pass the cap
    together. False when it would (nothing is counted then)."""
    if amount > cap:
        return False
    for _ in range(QUOTA_ATTEMPTS):
        try:
            db.findOneAndUpdate({'_id': key, 'count': {'$lt': cap - amount + 1}},
                                {'$inc': {'count': amount},
                                 '$setOnInsert': {'expireAt': expireAt,
                                                  '_version': 0}},
                                collection,
                                upsert=True,
                                projection={'_id': 1})
            return True
        except DuplicateKeyError:
            # The counter is too full (the upsert's insert collided), or a
            # racing first call inserted it: read which.
            counter = db.read({'_id': key}, collection,
                              projection={'count': 1}, findOne=True)
            if counter is None or counter['count'] + amount > cap:
                return False
    return False


def giveBackQuota(collection: str, key: str, amount: int = 1) -> None:
    """Take `amount` back off the counter `key` of `collection`, never
    below 0 (a counter holding less is left alone)."""
    if amount <= 0:
        return
    db.findOneAndUpdate({'_id': key, 'count': {'$gte': amount}},
                        {'$inc': {'count': -amount}},
                        collection,
                        upsert=False,
                        projection={'_id': 1})


def giveBackQuotas(collection: str, keys, amount: int = 1) -> None:
    """giveBackQuota on each counter of `keys`. A failure is logged (its
    class only) and swallowed, so the error that caused the refund is the
    one the caller sees, and the other counters are still given back."""
    for key in keys:
        try:
            giveBackQuota(collection, key, amount)
        except Exception as error:
            logging.warning({'event': 'quotaRefundFailed',
                             'collection': collection,
                             'errorClass': type(error).__name__})


def takeQuotas(collection: str, counters: list[tuple[str, int, str]],
               expireAt: datetime.datetime, amount: int = 1) -> list[str]:
    """Count `amount` on each counter (its key, cap and refusal message), in
    order. When one is refused, the ones already taken are given back and
    its message is raised (a ValueError); when a take fails (a database
    error), they are given back and the error re-raised. Returns the
    keys."""
    taken = []
    try:
        for key, cap, message in counters:
            if not takeQuota(collection, key, cap, expireAt, amount):
                raise ValueError(message)
            taken.append(key)
    except BaseException:
        giveBackQuotas(collection, taken, amount)
        raise
    return taken


def takeDailyQuotas(collection: str, counters: list[tuple[str, str, int, str]],
                    allStores: tuple[str, str], capAll: int, turnedOff: str,
                    day: datetime.date, amount: int = 1) -> list[str]:
    """Count `amount` on `day` against each counter of `counters` (its
    kind, owner id, cap and refusal) and then all stores' (`allStores`: its
    kind and refusal; its cap `capAll`, the feature's environment variable
    read with envCap). An all-stores cap of 0 is the feature turned off
    (`turnedOff`, a ValueError, nothing counted). Refused when any would
    pass, and then counted against none (takeQuotas). Returns the keys."""
    if capAll == 0:
        raise ValueError(turnedOff)
    allKind, allRefusal = allStores
    return takeQuotas(collection, [
        *[(usageKey(kind, ownerId, day), cap, refusal)
          for kind, ownerId, cap, refusal in counters],
        (usageKey(allKind, ALL_STORES, day), capAll, allRefusal),
    ], usageExpiry(day), amount=amount)
