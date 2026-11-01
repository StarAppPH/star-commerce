"""adminQueries: what the admin's filtered lists (listMyStoreProducts,
getStoreOrders, getStoreReturns) and storeReport share.

Each of them reads only the admin's own store (its action's permission
check), and turns each param into one fixed $match clause the server
writes: a param value is never an operator or a field name. These are the
checks of those values, and the time budget of every read they make.
"""

import datetime
from contextlib import contextmanager
from typing import Generator

from pymongo.errors import ExecutionTimeout

from sharedConstants import REPORT_RANGE_DAYS_MAX
from utils import appDayBounds

# The server's time budget of one admin list or report read (maxTimeMS):
# every sort is index-backed and every range bounded, so only a store far
# bigger than the caps allow reaches it.
ADMIN_QUERY_MAX_TIME_MS = 5000
TOO_BROAD = ('That reads too much at once: narrow the dates or the filters, '
             'then ask again')
START_AFTER_END = 'startDate is after endDate'
RANGE_TOO_LONG = (f'A date range is at most {REPORT_RANGE_DAYS_MAX} '
                  'days: narrow it')


@contextmanager
def queryBudget() -> Generator[None, None, None]:
    """Run the reads of an admin list or report: a read past its maxTimeMS
    (ADMIN_QUERY_MAX_TIME_MS) becomes a ValueError the AI relays
    (TOO_BROAD), never a 500."""
    try:
        yield
    except ExecutionTimeout:
        raise ValueError(TOO_BROAD) from None


def validFlag(value, name: str) -> bool | None:
    """A true/false filter: None is no filter; anything but a bool is
    refused, never quoted."""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f'{name} is true or false')
    return value


def validChoice(value, choices: tuple[str, ...], name: str) -> str | None:
    """One of `choices`: None or "" is none chosen; anything else is
    refused with the choices, never quoted. A plain str param, not a
    Literal, for the reason OrderActions._statusMatch gives."""
    if value is None or value == '':
        return None
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f'{name} is one of {", ".join(choices)}')
    return value


def boundedDays(startDate: str,
                endDate: str) -> tuple[datetime.datetime, datetime.datetime]:
    """appDayBounds of two ISO dates (whole days in the app timezone),
    refused when the start is after the end, or when the range, both days
    included, is longer than REPORT_RANGE_DAYS_MAX days."""
    start, end = appDayBounds(startDate, endDate)
    if start > end:
        raise ValueError(START_AFTER_END)
    if (end.date() - start.date()).days + 1 > REPORT_RANGE_DAYS_MAX:
        raise ValueError(RANGE_TOO_LONG)
    return start, end


def dayRange(createdAtMatch: dict,
             message: str) -> tuple[datetime.datetime, datetime.datetime]:
    """The first and the last instant of a closed range of whole app days,
    from a createdAt filter (OrderActions._createdAtMatch: a period, or both
    dates through boundedDays, which refused a bad one). Anything open (one
    end, or none) is refused with `message`."""
    bounds = createdAtMatch.get('createdAt', {})
    if '$gte' not in bounds or '$lte' not in bounds:
        raise ValueError(message)
    return bounds['$gte'], bounds['$lte']


def listSort(sortBy, stages: dict) -> dict:
    """The $sort of `sortBy` among `stages` (the first one when None or
    "", as the generated handler passes a missing key)."""
    sortBy = validChoice(sortBy, tuple(stages), 'sortBy')
    return stages[sortBy or next(iter(stages))]


def inMatch(field: str, value, matches: dict, name: str) -> dict:
    """The clause of a choice filter (validChoice) whose stored values are
    `matches[value]`: {} when none is chosen."""
    value = validChoice(value, tuple(matches), name)
    return {} if value is None else {field: {'$in': matches[value]}}
