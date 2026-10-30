"""StoreActions: the admin side.

An admin is a Users document (a Google sign-in) and owns at most one store.
Every admin route finds the store from the signed-in admin, never from a
store id the client or the AI sends: that is what keeps one store's data
out of another admin's reach.

Store reset (resetMyStore, until STORE_RESET_ENDS_AT): the admin's store and
everything in it are deleted and a new empty store takes its place (a new
_id), with the admin's sign-in kept.
"""

import datetime
import logging
import re
import secrets
import time
import typing
import unicodedata

from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

import geocoding
import photoStorage
import voiceModel
from mongoDb import db
from mcpTools import mcp_tool
from objects import (DEFAULT_STORE_CURRENCY, DELIVERY_ZONES_MAX,
                     HEX_COLOR_EXAMPLE, LATITUDE_LIMIT, LONGITUDE_LIMIT,
                     PHONE_NUMBER_PATTERN,
                     PHOTO_STYLE_MAX_LENGTH,
                     STORE_ADDRESS_MAX_LENGTH, STORE_DESCRIPTION_MAX_LENGTH,
                     STORE_NAME_MAX_LENGTH, STORE_OPENING_HOURS_MAX_LENGTH,
                     STORE_SLUG_MAX_LENGTH, STORE_OWNER_ONLY_FIELDS,
                     STORE_THEME_FIELDS, STORE_VOICE_FIELD,
                     AddressSuggestionResponse, AdminStoreResponse,
                     DeliveryZonesResponse, MyStoreResponse,
                     NewDeliveryZone, Policy, Store,
                     StoreCurrency, StoreLocation, StoreResponse,
                     StoreThemeResponse, deliveryZoneProblem, hexColor,
                     projectedCopy,
                     projection_of)
from route_config import route_config
from actions.usageCounters import appDay, copyDailyQuota, usageKey
from sharedConstants import STORE_RESET_ENDS_AT
from utils import parseIsoInstant

STORES = 'Stores'
USERS = 'Users'
POLICIES = 'Policies'
# A store is set up once createUserStore gave it its details, the slug
# among them: until then (the empty store sign-up creates) every reader
# treats it as no store yet.
SET_UP = {'slug': {'$type': 'string'}}
# A taken link name gets "-" plus 4 hex characters; a few tries at most.
SLUG_SUFFIX_BYTES = 2
SLUG_SUFFIX_LENGTH = 1 + 2 * SLUG_SUFFIX_BYTES
SLUG_ATTEMPTS = 5
# The link name of a store whose name has no latin letter or digit.
FALLBACK_SLUG = 'store'
ALREADY_HAS_A_STORE = 'You already have a store'
NO_STORE_YET = 'Create your store first'
# A store id or link name no set-up store has: what a customer gets.
STORE_NOT_FOUND = 'Store not found'
CONTACT_NUMBER_FORMAT_EXAMPLE = '+639171234567'
# The control characters a text field may hold: line breaks and tabs.
ALLOWED_CONTROL_CHARACTERS = frozenset('\n\r\t')
PHOTO_STYLE_LABEL = 'The photo style'
NO_THEME_SENT = 'Send a photo style, a brand color or a voice to change'
VOICE_NOT_AVAILABLE = "That voice isn't available: pick one from listVoices"
STORE_CHANGED = 'Your store just changed, try again'
NO_DETAILS_SENT = 'Send a store detail to change'
NO_PIN_YET = 'Pin the store on the map first'
DELIVERY_ZONES_NOT_A_LIST = 'zones is a list of areas, each with its points'
TOO_MANY_DELIVERY_ZONES = f'Draw at most {DELIVERY_ZONES_MAX} delivery areas'
# What createUserStore writes: the details the admin gives, and the slug
# that sets the store up.
STORE_DETAILS = ('name', 'slug', 'currency', 'description', 'contactNumber',
                 'address', 'openingHours', 'primaryColor', 'secondaryColor')
# Store reset works until this instant, in every environment.
STORE_RESET_ENDS = parseIsoInstant(STORE_RESET_ENDS_AT, 'STORE_RESET_ENDS_AT')
STORE_RESET_OVER = 'Store reset is no longer available'
NO_STORE_TO_RESET = 'You have no store to reset'
TYPE_NAME_TO_CONFIRM = "Type your store's name to confirm"
# What confirms the reset of a store not set up yet (it has no name).
RESET_WORD = 'reset'
# The conversations whose turns one deleteMany deletes.
RESET_TURNS_BATCH = 500
# The time Storage has to delete the old store's and the admin's files.
RESET_STORAGE_SECONDS = 60


def _now() -> datetime.datetime:
    """Now, timezone aware, in UTC: the clock of the reset's sunset."""
    return datetime.datetime.now(datetime.timezone.utc)


def ownerQuery(userId: str) -> dict:
    """The tenant boundary: the query of the one store `userId` owns. Every
    admin lookup goes through it, never through a store id the client or
    the AI sends."""
    return {'ownerUserId': userId}


def setUpOwnerQuery(userId: str) -> dict:
    """ownerQuery, matching the store only once it is set up (SET_UP)."""
    return {**ownerQuery(userId), **SET_UP}


def ownerStore(userId: str, projection: dict) -> dict:
    """The store `userId` owns (read through setUpOwnerQuery, with
    `projection`). Refuses an admin with no store yet, or one whose store
    is not set up yet."""
    store = db.read(setUpOwnerQuery(userId), STORES, projection=projection,
                    findOne=True)
    if store is None:
        raise ValueError(NO_STORE_YET)
    return store


def setUpStore(storeId: str, projection: dict) -> dict:
    """The store `storeId` (with `projection`), as a customer reaches it:
    refused (STORE_NOT_FOUND) unless it exists and is set up (SET_UP)."""
    store = db.read({'_id': storeId, **SET_UP}, STORES,
                    projection=projection, findOne=True)
    if store is None:
        raise ValueError(STORE_NOT_FOUND)
    return store


def _isOwnerCollision(error: DuplicateKeyError) -> bool:
    """Whether the write broke the unique ownerUserId index (a second
    store of the admin) rather than the slug one. The server names the
    index in keyPattern, else keyValue, else only in its message."""
    details = error.details or {}
    keys = details.get('keyPattern') or details.get('keyValue')
    if keys:
        return 'ownerUserId' in keys
    return 'ownerUserId' in str(error)


def emptyPolicy(storeId: str, createdAt: datetime.datetime) -> dict:
    """The Policies document of the store `storeId` before the admin gave
    any answer: every answer None (not said yet), unchanged since
    `createdAt`, the store's creation. The one source of "no policy yet",
    stored (ensurePolicy, sign-up) or sent for a store without one."""
    return Policy(storeId=storeId,
                  createdAt=createdAt,
                  updatedAt=createdAt,
                  _version=0).model_dump(by_alias=True)


def createEmptyStore(ownerUserId: str, createdAt: datetime.datetime,
                     session) -> dict:
    """Create, in `session`, the store a new admin `ownerUserId` gets: empty
    (not set up: createUserStore fills it in), with a new _id, and its
    policy, every answer unset (emptyPolicy), both created at `createdAt`.
    Sign-up and resetMyStore both create it here. Returns the stored
    store."""
    store = Store(ownerUserId=ownerUserId,
                  currency=DEFAULT_STORE_CURRENCY,
                  location=None,
                  createdAt=createdAt,
                  _version=0).model_dump(by_alias=True)
    created = db.create(store, STORES, session=session)
    db.create(emptyPolicy(created['_id'], createdAt), POLICIES,
              session=session)
    return created


def ensurePolicy(storeId: str, createdAt: datetime.datetime) -> None:
    """Give the store `storeId` (created at `createdAt`) its Policies
    document unless it has one. Sign-up creates it, but not for an admin
    from before that, nor after a sign-up that stopped between its writes
    on a database without transactions. An existing policy is never
    changed."""
    policy = emptyPolicy(storeId, createdAt)
    del policy['storeId']  # the filter's equality sets it on insert
    try:
        db.findOneAndUpdate({'storeId': storeId}, {'$setOnInsert': policy},
                            POLICIES,
                            upsert=True,
                            projection={'_id': 1})
    except DuplicateKeyError:
        pass  # a racing call inserted it


def requireAdmin(userId: str) -> None:
    """Refuse a caller who is no admin: a customer's JWT carries a
    Customers _id, which has no Users document."""
    if db.read({'_id': userId}, USERS, projection={'_id': 1},
               findOne=True) is None:
        raise PermissionError('Only a store admin can do this')


def isSetUp(store: dict) -> bool:
    """Whether `store` (read with its slug) is set up (SET_UP)."""
    return isinstance(store.get('slug'), str)


def requireAnyOwnerStore(userId: str, projection: dict) -> dict:
    """The store the admin `userId` owns, set up or not (the empty store
    sign-up creates is one): refuses a caller who is no admin
    (requireAdmin), then an admin with no store at all (NO_STORE_YET)."""
    requireAdmin(userId)
    store = db.read(ownerQuery(userId), STORES, projection=projection,
                    findOne=True)
    if store is None:
        raise ValueError(NO_STORE_YET)
    return store


def requireOwnerStore(userId: str, projection: dict) -> dict:
    """The store the admin `userId` owns (ownerStore, with `projection`):
    refuses a caller who is no admin first (requireAdmin), then an admin
    with no store yet."""
    requireAdmin(userId)
    return ownerStore(userId, projection)


def myStoreResponse(userId: str) -> dict:
    """What getUserStore sends the admin `userId`: their store once set up,
    else null."""
    store = db.readResponse(AdminStoreResponse, setUpOwnerQuery(userId),
                            STORES)
    if store is not None:
        # A theme field or the voice is sent only once set
        # (AdminStoreResponse).
        store = {key: value for key, value in store.items()
                 if not (key in STORE_OWNER_ONLY_FIELDS and value is None)}
    return {'store': store}


def _confirmsReset(confirmName, store: dict) -> bool:
    """Whether `confirmName` is the store's name (RESET_WORD for a store
    not set up yet), whatever its case and surrounding spaces."""
    expected = store.get('name') if isSetUp(store) else RESET_WORD
    if not isinstance(confirmName, str) or not isinstance(expected, str):
        return False

    def folded(text: str) -> str:
        return unicodedata.normalize('NFKC', text).strip().casefold()

    return folded(confirmName) == folded(expected)


def _deletedCount(result: int | str) -> int:
    """db.delete's answer as a count (it says 'No document to delete' for
    none)."""
    return result if isinstance(result, int) else 0


def _carryOverUsage(oldStoreId: str, newStoreId: str, day: datetime.date,
                    session) -> None:
    """Copy the old store's daily counters of `day` (chat, attachments,
    photos, voice: each module's STORE_USAGE_KINDS) to the new store, in
    `session`, so a reset never gives a day's quota back."""
    # Imported here: each of these modules imports this one.
    from actions import ChatActions, PhotoActions, VoiceActions
    for collection, kinds in (ChatActions.STORE_USAGE_KINDS,
                              PhotoActions.STORE_USAGE_KINDS,
                              VoiceActions.STORE_USAGE_KINDS):
        for kind in kinds:
            copyDailyQuota(collection, usageKey(kind, oldStoreId, day),
                           usageKey(kind, newStoreId, day), session)


def _deleteStoreData(storeId: str, chatPaths: list[str]) -> dict:
    """Delete every document of the store `storeId`: its products, orders
    (and their locks), returns, policy, generated photos and conversations,
    the turns of those conversations with them (by conversationId, in
    batches). The attachment paths those turns carried are added to
    `chatPaths` as each batch is read, so a failure part way still leaves
    them for _deleteResetFiles. Returns the count deleted per collection.
    Never the usage counters, Users, Customers, Bootstrap or
    ExchangeRates."""
    # Imported here: each of these modules imports this one.
    from actions.ChatActions import CHAT_TURNS, CONVERSATIONS
    from actions.OrderActions import ORDER_LOCKS, ORDERS
    from actions.PhotoActions import GENERATED_PHOTOS
    from actions.ProductActions import PRODUCTS
    from actions.ReturnActions import RETURNS
    deleted = {CHAT_TURNS: 0, CONVERSATIONS: 0}
    conversationIds = [
        conversation['_id'] for conversation in db.read(
            {'storeId': storeId}, CONVERSATIONS, projection={'_id': 1})]
    for start in range(0, len(conversationIds), RESET_TURNS_BATCH):
        batch = conversationIds[start:start + RESET_TURNS_BATCH]
        for turn in db.read({'conversationId': {'$in': batch},
                             'attachments.0': {'$exists': True}},
                            CHAT_TURNS, projection={'attachments.path': 1}):
            chatPaths.extend(attachment['path']
                             for attachment in turn['attachments']
                             if isinstance(attachment.get('path'), str))
        deleted[CHAT_TURNS] += _deletedCount(
            db.deleteMany({'conversationId': {'$in': batch}}, CHAT_TURNS))
        deleted[CONVERSATIONS] += _deletedCount(
            db.deleteMany({'_id': {'$in': batch}}, CONVERSATIONS))
    # OrderLocks has no storeId index: an accepted collection scan, once
    # per reset (the route ends at STORE_RESET_ENDS_AT).
    for collection in (PRODUCTS, ORDERS, ORDER_LOCKS, RETURNS, POLICIES,
                       GENERATED_PHOTOS):
        deleted[collection] = _deletedCount(
            db.deleteMany({'storeId': storeId}, collection))
    return deleted


def _deleteResetFiles(storeId: str, userId: str,
                      chatPaths: list[str]) -> tuple[int, int]:
    """Delete from Storage, within RESET_STORAGE_SECONDS, the old store
    `storeId`'s files, the admin `userId`'s own uploads but their chat/
    subfolder (their chats at other stores refer to it), and of that
    subfolder the files of `chatPaths` (the deleted store's turns' own).
    A customer's files are never among them. A step that fails is logged
    (by kind and error class only) and skipped: this never raises. Returns
    the files deleted and the steps that failed."""
    deadline = time.monotonic() + RESET_STORAGE_SECONDS
    files = failed = 0
    steps = (
        ('store', lambda: photoStorage.deletePrefix(
            photoStorage.storeFolder(storeId), deadline)),
        ('user', lambda: photoStorage.deletePrefix(
            photoStorage.userFolder(userId), deadline)),
        ('chat', lambda: photoStorage.deleteOwnChatFiles(
            userId, chatPaths, deadline)),
    )
    for kind, step in steps:
        try:
            files += step()
        except Exception as error:
            failed += 1
            logging.warning({'event': 'storeResetStorageFailed',
                             'folder': kind,
                             'errorClass': type(error).__name__})
    return files, failed


def storeSlug(name: str) -> str:
    """The link name a store name gives: lowercase letters and digits
    joined by single hyphens, short enough to take a suffix."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = slug[:STORE_SLUG_MAX_LENGTH - SLUG_SUFFIX_LENGTH].rstrip('-')
    return slug or FALLBACK_SLUG


def _validText(value: str | None, label: str, maxLength: int,
               required: bool = True) -> str | None:
    """`value` stripped, refused (naming `label` and its limit, never the
    value) when it is too long, empty while `required`, or holds a control
    character other than a line break or a tab. An empty optional value
    becomes None."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text')
    if any(unicodedata.category(c) == 'Cc'
           and c not in ALLOWED_CONTROL_CHARACTERS for c in value):
        raise ValueError(f'{label} cannot contain control characters')
    stripped = value.strip()
    if not stripped and not required:
        return None
    if required and not 1 <= len(stripped) <= maxLength:
        raise ValueError(f'{label} is 1 to {maxLength} characters')
    if len(stripped) > maxLength:
        raise ValueError(f'{label} is at most {maxLength} characters')
    return stripped


def _validVoiceId(voiceId) -> str | None:
    """The store voice to save: "" (or blank) is None, the default voice;
    anything else must be a voice listVoices offers (voiceModel.voiceExists,
    one ElevenLabs lookup), else VOICE_NOT_AVAILABLE, never quoting it."""
    if isinstance(voiceId, str):
        voiceId = voiceId.strip()
        if voiceId == '':
            return None
        if voiceModel.voiceExists(voiceId):
            return voiceId
    raise ValueError(VOICE_NOT_AVAILABLE)


def _validContactNumber(contactNumber: str) -> str:
    """The contact number stripped, refused unless it is in international
    (E.164) format."""
    if not isinstance(contactNumber, str):
        raise ValueError('The contact number must be text')
    stripped = contactNumber.strip()
    if re.fullmatch(PHONE_NUMBER_PATTERN, stripped) is None:
        raise ValueError(
            'The contact number must be in international format: a +, the '
            'country code and the number, with no spaces, e.g. '
            f'{CONTACT_NUMBER_FORMAT_EXAMPLE}')
    return stripped


def _requiredColor(value: str | None, field: str) -> str:
    """A brand color a store must have: hexColor's, refused when missing
    or empty."""
    color = hexColor(value, field)
    if color is None:
        raise ValueError(f'{field} is required: a hex color like '
                         f'{HEX_COLOR_EXAMPLE}')
    return color


def _validCoordinate(value: float, label: str, limit: int) -> float:
    """A latitude or longitude: a number from -limit to limit (a bool is no
    number). The range check alone also refuses NaN and infinity (every
    comparison with NaN is false), and it never converts the value, so a
    huge JSON integer is refused instead of overflowing a float."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not -limit <= value <= limit):
        raise ValueError(f'{label} is a number from -{limit} to {limit}')
    return float(value)


def _plain(value):
    """A list param's item as a dict: a model (the MCP tool's) dumped, a
    JSON object (over HTTP) as is, anything else None."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value if isinstance(value, dict) else None


def _validDeliveryZone(zone, position: int) -> dict:
    """One delivery area of setMyDeliveryZones as stored, refused with
    its position (1 based) and never its values."""
    zone = _plain(zone)
    points = zone.get('points') if zone is not None else None
    if not isinstance(points, list):
        raise ValueError(f'Area {position}: points is a list of corners')
    corners = []
    for point in points:
        point = _plain(point) or {}
        corners.append((
            _validCoordinate(point.get('latitude'),
                             f'Area {position}: a latitude', LATITUDE_LIMIT),
            _validCoordinate(point.get('longitude'),
                             f'Area {position}: a longitude',
                             LONGITUDE_LIMIT)))
    problem = deliveryZoneProblem(corners)
    if problem is not None:
        raise ValueError(f'Area {position}: {problem}')
    return {'points': [{'latitude': latitude, 'longitude': longitude}
                       for latitude, longitude in corners]}


def _validDeliveryZones(zones) -> list[dict]:
    """setMyDeliveryZones' areas as stored: up to DELIVERY_ZONES_MAX
    closed shapes (deliveryZoneProblem)."""
    if not isinstance(zones, list):
        raise ValueError(DELIVERY_ZONES_NOT_A_LIST)
    if len(zones) > DELIVERY_ZONES_MAX:
        raise ValueError(TOO_MANY_DELIVERY_ZONES)
    return [_validDeliveryZone(zone, position)
            for position, zone in enumerate(zones, start=1)]


class StoreActions():

    @mcp_tool('admin',
              thinkingText=['Reading your business',
                            'Accessing your business',
                            'Getting your business'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store fetched',
                  responseModel=MyStoreResponse)
    def getUserStore(self, userId: str) -> dict:
        """Get the admin's store. Returns the store, or null when the admin
        has not created one yet: then collect the store's details from the
        admin and call createUserStore."""
        requireAdmin(userId)
        return myStoreResponse(userId)

    @mcp_tool('admin',
              thinkingText=["Saving your store's look",
                            'Updating your theme',
                            'Styling your store'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store theme saved',
                  responseModel=StoreThemeResponse)
    def updateMyStoreTheme(self,
                           userId: str,
                           photoStyle: str | None = None,
                           primaryColor: str | None = None,
                           secondaryColor: str | None = None,
                           voiceId: str | None = None) -> dict:
        """Set the look of the store's product photos (generateProductPhotos
        needs a photo style first). Ask the owner, in their own words, for
        the photo style: the background, lighting and mood they want (up
        to 300 characters, e.g. "white marble counter, soft morning light,
        calm and airy"), and for the store's two brand colors as hex codes
        like #1A73E8 (primaryColor and secondaryColor; the shop and the
        admin's screens are drawn in them, and the photos use them only as
        accents in the background and props). voiceId is the voice
        the store's assistant speaks its replies with: only a voiceId
        listVoices returned, for the voice the owner chose ("" goes back to
        the default voice; getUserStore shows the voice saved). Never invent a style or a voice: use only
        what the owner said. If the owner asks you to pick the colors,
        suggest two that suit the store. Confirm every value with the owner
        before calling. Send only the fields the owner wants changed: a field left
        out keeps its value, and an empty one ("") clears it. If this tool
        refuses, relay its message to the owner and ask again."""
        requireAdmin(userId)
        # The generated HTTP handler passes a missing body key as None, so
        # None is "unchanged" and "" is how a field is cleared.
        changes = {}
        if photoStyle is not None:
            changes['photoStyle'] = _validText(photoStyle, PHOTO_STYLE_LABEL,
                                               PHOTO_STYLE_MAX_LENGTH,
                                               required=False)
        for field, value in (('primaryColor', primaryColor),
                             ('secondaryColor', secondaryColor)):
            if value is not None:
                changes[field] = hexColor(value, field)
        if voiceId is not None:
            # ElevenLabs is asked first: the store's _version is read
            # after it, so the lookup never widens the write's race.
            changes[STORE_VOICE_FIELD] = _validVoiceId(voiceId)
        if not changes:
            raise ValueError(NO_THEME_SENT)
        store = ownerStore(userId, {'_id': 1, '_version': 1})
        updated = db.findOneAndUpdate(
            {'_id': store['_id'], '_version': store['_version']},
            {'$set': changes, '$inc': {'_version': 1}},
            STORES,
            upsert=False,
            projection=projection_of(StoreThemeResponse))
        if updated is None:
            raise ValueError(STORE_CHANGED)
        # A store from before the theme has none of its fields stored. The
        # voice is not sent back (getUserStore sends it).
        return {field: updated.get(field) for field in STORE_THEME_FIELDS}

    @mcp_tool('admin',
              thinkingText=['Creating your store',
                            'Setting up your business',
                            'Opening your store'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store created',
                  responseModel=StoreResponse)
    def createUserStore(self,
                        userId: str,
                        name: str,
                        description: str,
                        contactNumber: str,
                        primaryColor: str,
                        secondaryColor: str,
                        currency: StoreCurrency = DEFAULT_STORE_CURRENCY,
                        openingHours: str | None = None,
                        address: str | None = None) -> dict:
        """Create the admin's store (an admin has one store). Before calling,
        ask the admin for each detail, one question at a time: the store's
        name, a short description of what it sells, its currency (default
        PHP), its contact number, its two brand colors as hex codes like
        #1A73E8 (primaryColor and secondaryColor: the shop and the admin's
        screens are drawn in them) and, if the admin wants, its opening
        hours. Do not ask for the address: the admin pins the store on the
        map right after it is created, and the address is suggested from
        the pin (pass address only when the admin already said it). Never
        invent or guess a value: use only what the admin said, except the
        colors: if the admin asks you to pick them, suggest two that suit
        the store and use them once the admin agrees. Give the contact
        number in international format, e.g. +639171234567. Confirm all the
        details with the admin before calling. Before calling, tell the
        admin that the contact number and the address are shown to every
        customer, anonymous visitors included. If this tool refuses a
        detail, relay its message to the admin so they can correct that
        detail, then call again."""
        # This fills in the empty store sign-up created, or creates the
        # store of an admin who has none (one from before that). The unique
        # index on ownerUserId is what enforces "one store per admin" for a
        # create, the not-set-up guard of the fill for a fill: of two racing
        # calls only one wins. The read first gives the usual case a clear
        # message without a write.
        requireAdmin(userId)
        name = _validText(name, 'A store name', STORE_NAME_MAX_LENGTH)
        description = _validText(description, 'A description',
                                 STORE_DESCRIPTION_MAX_LENGTH)
        contactNumber = _validContactNumber(contactNumber)
        # Left out, the store has no address yet: the chat suggests one
        # from the map pin after the store is created. One that is sent
        # must be a real address.
        if address is not None:
            address = _validText(address, 'An address',
                                 STORE_ADDRESS_MAX_LENGTH)
        primaryColor = _requiredColor(primaryColor, 'primaryColor')
        secondaryColor = _requiredColor(secondaryColor, 'secondaryColor')
        openingHours = _validText(openingHours, 'The opening hours text',
                                  STORE_OPENING_HOURS_MAX_LENGTH,
                                  required=False)
        # The generated HTTP handler passes a missing body key as None, so
        # the default above applies to direct and MCP calls only.
        if currency is None:
            currency = DEFAULT_STORE_CURRENCY
        if currency not in typing.get_args(StoreCurrency):
            raise ValueError('That currency is not supported')
        existing = db.read(ownerQuery(userId), STORES,
                           projection={'_id': 1, 'slug': 1}, findOne=True)
        if existing is not None and isinstance(existing.get('slug'), str):
            raise ValueError(ALREADY_HAS_A_STORE)
        baseSlug = storeSlug(name)
        createdAt = datetime.datetime.now(tz=datetime.timezone.utc)
        for attempt in range(SLUG_ATTEMPTS):
            slug = baseSlug if attempt == 0 else (
                f'{baseSlug}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}')
            # Built whole, so the details are checked as a store's; a fill
            # writes only the details over the empty store.
            store = Store(ownerUserId=userId,
                          name=name,
                          slug=slug,
                          currency=currency,
                          description=description,
                          contactNumber=contactNumber,
                          address=address,
                          openingHours=openingHours,
                          primaryColor=primaryColor,
                          secondaryColor=secondaryColor,
                          location=None,
                          createdAt=createdAt,
                          _version=0).model_dump(by_alias=True)
            try:
                if existing is None:
                    saved = db.create(store, STORES)
                else:
                    saved = db.findOneAndUpdate(
                        {'_id': existing['_id'],
                         'slug': {'$not': SET_UP['slug']}},
                        {'$set': {key: store[key] for key in STORE_DETAILS},
                         '$inc': {'_version': 1}},
                        STORES,
                        upsert=False)
            except DuplicateKeyError as error:
                if _isOwnerCollision(error):
                    raise ValueError(ALREADY_HAS_A_STORE) from None
                continue  # the link name is taken: try one with a suffix
            if saved is None:
                # A racing call set the store up first.
                raise ValueError(ALREADY_HAS_A_STORE)
            ensurePolicy(saved['_id'], saved['createdAt'])
            return projectedCopy(StoreResponse, saved)
        raise ValueError(
            'Could not pick a link name for this store, try another name')

    @mcp_tool('admin',
              thinkingText=['Saving your store details',
                            'Updating your business',
                            'Writing down your details'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store details saved',
                  responseModel=StoreResponse)
    def updateMyStoreDetails(self,
                             userId: str,
                             name: str | None = None,
                             description: str | None = None,
                             contactNumber: str | None = None,
                             address: str | None = None,
                             openingHours: str | None = None) -> dict:
        """Change the details of the admin's store: its name, description,
        contact number (international format, e.g. +639171234567), address
        or opening hours. The store's link and currency never change. Send
        only the fields the owner wants changed: a field left out keeps its
        value, and an empty openingHours ("") clears the hours. Never invent
        or guess a value: use only what the owner said (or an address
        suggestStoreAddress gave and the owner accepted), and confirm the
        change with the owner before calling. If this tool refuses a detail,
        relay its message to the owner and ask again."""
        requireAdmin(userId)
        # The generated HTTP handler passes a missing body key as None, so
        # None is "unchanged".
        changes = {}
        if name is not None:
            changes['name'] = _validText(name, 'A store name',
                                         STORE_NAME_MAX_LENGTH)
        if description is not None:
            changes['description'] = _validText(
                description, 'A description', STORE_DESCRIPTION_MAX_LENGTH)
        if contactNumber is not None:
            changes['contactNumber'] = _validContactNumber(contactNumber)
        if address is not None:
            changes['address'] = _validText(address, 'An address',
                                            STORE_ADDRESS_MAX_LENGTH)
        if openingHours is not None:
            changes['openingHours'] = _validText(
                openingHours, 'The opening hours text',
                STORE_OPENING_HOURS_MAX_LENGTH, required=False)
        if not changes:
            raise ValueError(NO_DETAILS_SENT)
        store = ownerStore(userId, {'_id': 1, '_version': 1})
        updated = db.findOneAndUpdate(
            {'_id': store['_id'], '_version': store['_version']},
            {'$set': changes, '$inc': {'_version': 1}},
            STORES,
            upsert=False)
        if updated is None:
            raise ValueError(STORE_CHANGED)
        return projectedCopy(StoreResponse, updated)

    @mcp_tool('admin',
              thinkingText=['Finding your address',
                            'Looking up the address',
                            'Reading the map pin'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Address suggested',
                  responseModel=AddressSuggestionResponse)
    def suggestStoreAddress(self, userId: str) -> dict:
        """The street address Google Maps gives for the store's map pin,
        and the store's address now. Call it right after
        setUserStoreLocation saved a pin. When suggestedAddress is not null
        and differs from address, ask the owner whether to use it as the
        store's address, and save it with updateMyStoreDetails only once
        they say yes; never change the address on your own. When it is
        null, say nothing about it, unless the store has no address yet
        (address is null): then ask the owner to type it."""
        requireAdmin(userId)
        store = ownerStore(userId, {'address': 1, 'location': 1})
        location = store.get('location')
        if not isinstance(location, dict):
            raise ValueError(NO_PIN_YET)
        return {'suggestedAddress': geocoding.reverseGeocode(
                    location['latitude'], location['longitude']),
                'address': store.get('address')}

    @mcp_tool('admin',
              thinkingText=['Saving your store pin',
                            'Pinning your store',
                            'Marking your location'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store location saved',
                  responseModel=StoreResponse)
    def setUserStoreLocation(self, userId: str, latitude: float,
                             longitude: float) -> dict:
        """Pin the admin's store on the map, or move its pin: the latitude
        and longitude of the pin the owner sent from showStoreLocationForm,
        never coordinates you guessed or the owner typed. A store that
        delivers prices each delivery from this pin. Then call
        suggestStoreAddress."""
        requireAdmin(userId)
        location = StoreLocation(
            latitude=_validCoordinate(latitude, 'The latitude',
                                      LATITUDE_LIMIT),
            longitude=_validCoordinate(longitude, 'The longitude',
                                       LONGITUDE_LIMIT))
        store = ownerStore(userId, {'_id': 1, '_version': 1})
        updated = db.update(
            {'_id': store['_id'], '_version': store['_version']},
            {'location': location.model_dump()}, STORES)
        return projectedCopy(StoreResponse, updated)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Delivery areas saved',
                  responseModel=DeliveryZonesResponse)
    def setMyDeliveryZones(self, userId: str,
                           zones: list[NewDeliveryZone]) -> dict:
        """Save where the admin's store delivers: the areas drawn on the
        delivery areas map (showDeliveryZonesForm), each a closed shape of
        3 to 100 corners whose sides never cross, at most 10. No area: it
        delivers anywhere. A customer can book a delivery only to a pin
        inside one. No chat or MCP tool: the map sends it."""
        requireAdmin(userId)
        deliveryZones = _validDeliveryZones(zones)
        store = ownerStore(userId, {'_id': 1, '_version': 1})
        updated = db.update(
            {'_id': store['_id'], '_version': store['_version']},
            {'deliveryZones': deliveryZones}, STORES)
        return {'deliveryZones': updated['deliveryZones']}

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store reset',
                  responseModel=MyStoreResponse)
    def resetMyStore(self, userId: str, confirmName: str) -> dict:
        """Delete the admin's store and everything in it (products, orders
        and returns, customers' included, the policy, photos and chats),
        keeping the admin's sign-in: a new empty store takes its place, so
        the admin is back at store setup. `confirmName` is the store's name
        (for a store not set up yet, "reset"). Refused from
        STORE_RESET_ENDS_AT on. No chat or MCP tool."""
        # The sunset first: past it nothing is read or written.
        if _now() >= STORE_RESET_ENDS:
            raise PermissionError(STORE_RESET_OVER)
        requireAdmin(userId)
        old = db.read(ownerQuery(userId), STORES,
                      projection={'_id': 1, '_version': 1, 'name': 1,
                                  'slug': 1},
                      findOne=True)
        if old is None:
            raise ValueError(NO_STORE_TO_RESET)
        if not _confirmsReset(confirmName, old):
            raise ValueError(TYPE_NAME_TO_CONFIRM)
        createdAt = _now()

        # The new store has a new _id: every reader finds the store by its
        # owner or by a set-up id, so once this commits the old id reaches
        # nothing, and a write racing the reset lands only on it.
        def swap(session):
            if _deletedCount(db.delete(
                    {'_id': old['_id'], '_version': old['_version']},
                    STORES, session=session)) != 1:
                raise ValueError(STORE_CHANGED)
            new = createEmptyStore(userId, createdAt, session)
            _carryOverUsage(old['_id'], new['_id'], appDay(createdAt),
                            session)
            return new

        new = db.createTransaction(swap)
        chatPaths = []
        try:
            deleted = _deleteStoreData(old['_id'], chatPaths)
        except Exception as error:
            # The swap is done: what is left of the old id is unreachable,
            # and this id is what a cleanup deletes. The files go anyway.
            _, storageFailures = _deleteResetFiles(old['_id'], userId,
                                                   chatPaths)
            logging.error({'event': 'storeResetIncomplete',
                           'oldStoreId': old['_id'],
                           'errorClass': type(error).__name__,
                           'storageFailures': storageFailures})
            raise
        files, storageFailures = _deleteResetFiles(old['_id'], userId,
                                                   chatPaths)
        logging.info({'event': 'storeReset',
                      'oldStoreId': old['_id'],
                      'newStoreId': new['_id'],
                      'deleted': deleted,
                      'files': files,
                      'storageFailures': storageFailures})
        return myStoreResponse(userId)
