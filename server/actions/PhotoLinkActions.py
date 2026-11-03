"""PhotoLinkActions.py — the photos of a spreadsheet import's photo links
(hand-written).

showNewProductsFromSpreadsheet with a photoColumn (chatForms) reads each
row's link here, when the form opens: each distinct link once
(photoLinks.fetchPhoto, which keeps the request to a public web server's
photo), decoded and re-encoded with no metadata, at most MAX_SIDE pixels a
side (photoStorage.storeOriginal), and saved in the admin's own
users/<uid>/imported/ folder (photoStorage.saveImportedPhoto). The form
then shows it on the row, as a photo the admin attached; its submit puts it
on the product as any upload (setUploadedProductPhotos). A link that gives
no photo leaves its row without one, with the reason (REASONS): it never
fails the import.

Limits:
- one import reads its links at a time per process (linkSlot: a second is
  refused, a 503 the import turns into a tool error, before any quota);
- LINK_WORKERS links at once, each holding MAX_BYTES of the process's
  attachment bytes in flight (fewer workers when they are not free, none:
  every link BUSY); one decode at a time per process (DECODES_AT_ONCE);
- each link has photoLinks.LINK_SECONDS at most, which its hops'
  watchdogs hold it to wherever it blocks; the links' work ends by the
  caller's deadline (chatForms: turnDeadline.toolDeadline(LINKS_SECONDS),
  so the chat turn keeps the time for its saves and reply), the saves by
  turnDeadline.saveDeadline(LINKS_SECONDS + LINK_SAVE_SECONDS); a link not
  started by then is NOT_IN_TIME;
- a daily quota per store (STORE_DAILY_LINK_CAP) and for all stores
  (PHOTO_LINK_DAILY_CAP_ALL, 0 turns links off), taken for the distinct
  links that pass photoLinks.checkedLink, all or none: refused, every link
  of the import is refused with its message. It is given back for each
  link whose TCP connection was never made (refused by its address, not
  resolved, not connected, not started in time), also when the reads end
  in an unexpected error; a link whose TCP connection was made keeps it,
  whatever came of it (a failed TLS handshake too: no free probing).

One log line per import (spreadsheetPhotoLinks): counts only, never a
link, host, address or path.
"""

import collections
import contextlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, NamedTuple

import photoLinks
import photoStorage
from actions.callSlots import slot
from actions.PhotoActions import PHOTO_USAGE, _takeStoreQuota
from actions.usageCounters import envCap, giveBackQuotas
from services.__request import ServiceUnavailable
from utils import generateRandomString

# The PhotoUsage counter kind of the links read, the store's and all
# stores'; carried over by a store reset (StoreActions._carryOverUsage).
LINKS_KIND = 'photoLinks'
STORE_USAGE_KINDS = (PHOTO_USAGE, (LINKS_KIND, ))
STORE_DAILY_LINK_CAP = 300
LINK_DAILY_CAP_ALL_VARIABLE = 'PHOTO_LINK_DAILY_CAP_ALL'
DEFAULT_LINK_DAILY_CAP_ALL = 3000
LINK_CAP_REACHED = ("Your store has reached today's limit of photos from "
                    'links: try again tomorrow')
LINK_ALL_CAP_REACHED = 'Photos from links are busy today: try again tomorrow'
LINKS_TURNED_OFF = 'Photos from links are turned off right now'

IMPORTS_AT_ONCE = 1
_linkSlots = threading.BoundedSemaphore(IMPORTS_AT_ONCE)
LINK_WORKERS = 4
DECODES_AT_ONCE = 1
_decodeSlots = threading.BoundedSemaphore(DECODES_AT_ONCE)
MAX_SIDE = photoStorage.MODEL_REFERENCE_MAX_SIDE
LINKS_SECONDS = 40
LINK_SAVE_SECONDS = 10
LINKS_SERVICE = 'photo links'
PHOTO_LABEL = 'The photo'

# Why a link gave no photo, beyond photoLinks' codes.
NOT_IN_TIME = 'notInTime'
BUSY = 'busy'
TOO_MANY_PIXELS = 'tooManyPixels'
NOT_A_REAL_PHOTO = 'notARealPhoto'
NOT_SAVED = 'notSaved'
QUOTA = 'quota'
REASONS = {
    **photoLinks.REASONS,
    NOT_IN_TIME: 'Took too long to load: import these rows again',
    BUSY: ('Photo links are busy right now: import these rows again in a '
           'minute'),
    TOO_MANY_PIXELS: 'The photo is too large: use a smaller one',
    NOT_A_REAL_PHOTO: "Not a real JPG, PNG or WebP photo",
    NOT_SAVED: "The photo couldn't be saved",
}


class LinkedPhotos(NamedTuple):
    """Each row's photo (its Storage path, or None), how many rows have
    one, and each row with a link that gave none: {'row', 'name',
    'reason'}."""
    paths: list[str | None]
    fetched: int
    notFetched: list[dict]


class _Result(NamedTuple):
    path: str | None
    code: str | None
    connected: bool


def busy() -> ServiceUnavailable:
    return ServiceUnavailable(LINKS_SERVICE, 'photos')


@contextlib.contextmanager
def linkSlot() -> Iterator[None]:
    """Hold the process's one import reading links (IMPORTS_AT_ONCE), or
    raise busy() (a 503) at once."""
    with slot(_linkSlots, busy):
        yield


def _takeLinkQuota(storeId: str, count: int) -> list[str]:
    return _takeStoreQuota(
        LINKS_KIND, storeId, STORE_DAILY_LINK_CAP,
        envCap(LINK_DAILY_CAP_ALL_VARIABLE, DEFAULT_LINK_DAILY_CAP_ALL,
               'photoLinkDailyCapAllInvalid'),
        (LINK_CAP_REACHED, LINK_ALL_CAP_REACHED, LINKS_TURNED_OFF),
        amount=count)


def _decoded(data: bytes, contentType: str,
             workBy: float) -> tuple[bytes, str] | str:
    """The photo re-encoded (storeOriginal, at most MAX_SIDE a side), one
    decode at a time per process; else the code of why not."""
    if not _decodeSlots.acquire(timeout=max(0.0, workBy - time.monotonic())):
        return NOT_IN_TIME
    try:
        return photoStorage.storeOriginal(data, contentType, PHOTO_LABEL,
                                          MAX_SIDE)
    except ValueError as error:
        return (TOO_MANY_PIXELS if str(error).endswith((
            photoStorage.MODEL_IMAGE_TOO_LARGE,
            photoStorage.DECODE_TOO_LARGE)) else NOT_A_REAL_PHOTO)
    finally:
        _decodeSlots.release()


def _readLink(userId: str, link: str, workBy: float,
              saveBy: float) -> _Result:
    if time.monotonic() >= workBy:
        return _Result(None, NOT_IN_TIME, False)
    try:
        data, contentType = photoLinks.fetchPhoto(link, workBy)
    except photoLinks.LinkRefused as refused:
        return _Result(None, refused.code, refused.connected)
    decoded = _decoded(data, contentType, workBy)
    del data
    if isinstance(decoded, str):
        return _Result(None, decoded, True)
    try:
        path = photoStorage.saveImportedPhoto(
            userId, generateRandomString(), decoded[0], decoded[1], saveBy)
    except (ServiceUnavailable, ValueError):
        return _Result(None, NOT_SAVED, True)
    return _Result(path, None, True)


def _safelyRead(userId: str, link: str, workBy: float,
                saveBy: float) -> _Result:
    """_readLink, an unexpected failure (logged by class) a row with no
    photo, never a failed import."""
    try:
        return _readLink(userId, link, workBy, saveBy)
    except Exception as error:
        logging.error({'event': 'photoLinkFailed',
                       'errorClass': type(error).__name__})
        return _Result(None, NOT_SAVED, True)


def _read(userId: str, valid: list[str], workers: int, workBy: float,
          saveBy: float, results: dict) -> None:
    """Read each link of `valid` into `results`, `workers` at once; the
    pool is always waited for (no read is left running)."""
    with ThreadPoolExecutor(workers) as pool:
        read = pool.map(lambda link: _safelyRead(userId, link, workBy,
                                                 saveBy), valid)
        results.update(zip(valid, read))


def importLinkedPhotos(userId: str, storeId: str,
                       rows: list[tuple[int, str | None, str | None]],
                       workBy: float, saveBy: float,
                       reserve: Callable[[int], bool],
                       release: Callable[[int], None]) -> LinkedPhotos:
    """The photo of each row (its number as the spreadsheet shows it, its
    link or None, its product's name) of the store `storeId`'s admin
    `userId` (see the module docstring), the links read by `workBy` and
    saved by `saveBy` (time.monotonic()). `reserve` / `release` hold and
    give back the process's attachment bytes in flight (called on this
    thread only). Call it within linkSlot()."""
    started = time.monotonic()
    results: dict[str, _Result] = {}
    valid: list[str] = []
    for _, link, _ in rows:
        if link is None or link in results or link in valid:
            continue
        try:
            photoLinks.checkedLink(link)
            valid.append(link)
        except photoLinks.LinkRefused as refused:
            results[link] = _Result(None, refused.code, False)
    keys, quotaReason, workers, givenBack = [], None, 0, 0
    if valid:
        try:
            keys = _takeLinkQuota(storeId, len(valid))
        except ValueError as error:
            quotaReason = str(error)
    if valid and quotaReason is None:
        workers = min(LINK_WORKERS, len(valid))
        while workers and not reserve(workers * photoLinks.MAX_BYTES):
            workers -= 1
        if not workers:
            results.update({link: _Result(None, BUSY, False)
                            for link in valid})
        else:
            try:
                _read(userId, valid, workers, workBy, saveBy, results)
            except BaseException:
                # Every link but those that connected gets its quota back.
                giveBackQuotas(PHOTO_USAGE, keys, len(valid) - sum(
                    link in results and results[link].connected
                    for link in valid))
                raise
            finally:
                release(workers * photoLinks.MAX_BYTES)
        givenBack = sum(not results[link].connected for link in valid)
        if givenBack:
            giveBackQuotas(PHOTO_USAGE, keys, givenBack)
    elif valid:
        results.update({link: _Result(None, QUOTA, False) for link in valid})
    paths, notFetched = [], []
    for number, link, name in rows:
        result = results.get(link) if link is not None else None
        paths.append(result.path if result is not None else None)
        if result is not None and result.path is None:
            notFetched.append({
                'row': number,
                'name': name,
                'reason': (quotaReason if result.code == QUOTA
                           else REASONS[result.code])})
    # Never a link, a host, an address or a path.
    logging.info({
        'event': 'spreadsheetPhotoLinks',
        'storeId': storeId,
        'rows': sum(link is not None for _, link, _ in rows),
        'links': len(results),
        'fetched': sum(r.path is not None for r in results.values()),
        'refused': dict(collections.Counter(
            r.code for r in results.values() if r.path is None)),
        'quotaTaken': len(valid) if keys else 0,
        'quotaGivenBack': givenBack,
        'workers': workers,
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return LinkedPhotos(paths, sum(path is not None for path in paths),
                        notFetched)
