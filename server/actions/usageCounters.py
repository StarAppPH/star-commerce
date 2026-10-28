"""usageCounters: daily usage counters (chat messages, product photos).

A counter is one document of its collection, _id
"<kind>:<ownerId>:<YYYY-MM-DD>" (the app day, UTC+8), holding `count`; a
TTL index on expireAt (mongoDb.ensureIndexes) deletes it a day after its
day ends. takeQuota counts against a cap atomically, so racing callers
never pass it together; giveBackQuota takes back what a failed call
counted.
"""

import datetime

from pymongo.errors import DuplicateKeyError

from AppConfig import Timezone
from mongoDb import db
from utils import appDayStart

UTC = datetime.timezone.utc
# A counter that a racing first call of the day inserted between our read
# and write: retried once.
QUOTA_ATTEMPTS = 2


def appDay(now: datetime.datetime) -> datetime.date:
    """The app day (UTC+8) of the instant `now`."""
    return now.astimezone(Timezone().getTimezone()).date()


def usageExpiry(day: datetime.date) -> datetime.datetime:
    """When a counter of `day` is deleted: a day after the day ends."""
    return (appDayStart(day.isoformat()) +
            datetime.timedelta(days=2)).astimezone(UTC)


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
