"""StoreActions: the admin side.

An admin is a Users document (a Google sign-in) and owns at most one store.
Every admin route finds the store from the signed-in admin, never from a
store id the client or the AI sends: that is what keeps one store's data
out of another admin's reach.
"""

import datetime
import re
import secrets
import typing

from pymongo.errors import DuplicateKeyError

from mongoDb import db
from mcpTools import mcp_tool
from objects import (DEFAULT_STORE_CURRENCY, LATITUDE_LIMIT, LONGITUDE_LIMIT,
                     PHONE_NUMBER_PATTERN, STORE_ADDRESS_MAX_LENGTH,
                     STORE_DESCRIPTION_MAX_LENGTH, STORE_NAME_MAX_LENGTH,
                     STORE_OPENING_HOURS_MAX_LENGTH, STORE_SLUG_MAX_LENGTH,
                     MyStoreResponse, Store, StoreCurrency, StoreLocation,
                     StoreResponse, projectedCopy)
from route_config import route_config

STORES = 'Stores'
USERS = 'Users'
# A taken link name gets "-" plus 4 hex characters; a few tries at most.
SLUG_SUFFIX_BYTES = 2
SLUG_SUFFIX_LENGTH = 1 + 2 * SLUG_SUFFIX_BYTES
SLUG_ATTEMPTS = 5
# The link name of a store whose name has no latin letter or digit.
FALLBACK_SLUG = 'store'
ALREADY_HAS_A_STORE = 'You already have a store'
NO_STORE_YET = 'Create your store first'
CONTACT_NUMBER_FORMAT_EXAMPLE = '+639171234567'


def ownerQuery(userId: str) -> dict:
    """The tenant boundary: the query of the one store `userId` owns. Every
    admin lookup goes through it, never through a store id the client or
    the AI sends."""
    return {'ownerUserId': userId}


def _isOwnerCollision(error: DuplicateKeyError) -> bool:
    """Whether the insert broke the unique ownerUserId index (a second
    store of the admin) rather than the slug one. The server names the
    index in keyPattern, else keyValue, else only in its message."""
    details = error.details or {}
    keys = details.get('keyPattern') or details.get('keyValue')
    if keys:
        return 'ownerUserId' in keys
    return 'ownerUserId' in str(error)


def requireAdmin(userId: str) -> None:
    """Refuse a caller who is no admin: a customer's JWT carries a
    Customers _id, which has no Users document."""
    if db.read({'_id': userId}, USERS, projection={'_id': 1},
               findOne=True) is None:
        raise PermissionError('Only a store admin can do this')


def storeSlug(name: str) -> str:
    """The link name a store name gives: lowercase letters and digits
    joined by single hyphens, short enough to take a suffix."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = slug[:STORE_SLUG_MAX_LENGTH - SLUG_SUFFIX_LENGTH].rstrip('-')
    return slug or FALLBACK_SLUG


def _validText(value: str | None, label: str, maxLength: int,
               required: bool = True) -> str | None:
    """`value` stripped, refused (naming `label` and its limit, never the
    value) when it is too long, or empty while `required`. An empty
    optional value becomes None."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text')
    stripped = value.strip()
    if not stripped and not required:
        return None
    if required and not 1 <= len(stripped) <= maxLength:
        raise ValueError(f'{label} is 1 to {maxLength} characters')
    if len(stripped) > maxLength:
        raise ValueError(f'{label} is at most {maxLength} characters')
    return stripped


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


def _validCoordinate(value: float, label: str, limit: int) -> float:
    """A latitude or longitude: a number from -limit to limit (a bool is no
    number). The range check alone also refuses NaN and infinity (every
    comparison with NaN is false), and it never converts the value, so a
    huge JSON integer is refused instead of overflowing a float."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not -limit <= value <= limit):
        raise ValueError(f'{label} is a number from -{limit} to {limit}')
    return float(value)


class StoreActions():

    @mcp_tool('admin')
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store fetched',
                  responseModel=MyStoreResponse)
    def getUserStore(self, userId: str) -> dict:
        """Get the admin's store. Returns the store, or null when the admin
        has not created one yet: then collect the store's details from the
        admin and call createUserStore."""
        requireAdmin(userId)
        return {
            'store':
            db.readResponse(StoreResponse, ownerQuery(userId), STORES)
        }

    @mcp_tool('admin')
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store created',
                  responseModel=StoreResponse)
    def createUserStore(self,
                        userId: str,
                        name: str,
                        description: str,
                        contactNumber: str,
                        address: str,
                        currency: StoreCurrency = DEFAULT_STORE_CURRENCY,
                        openingHours: str | None = None) -> dict:
        """Create the admin's store (an admin has one store). Before calling,
        ask the admin for each detail, one question at a time: the store's
        name, a short description of what it sells, its currency (default
        PHP), its contact number, its address and, if the admin wants, its
        opening hours. Never invent or guess a value: use only what the
        admin said. Give the contact number in international format, e.g.
        +639171234567. Confirm all the details with the admin before
        calling. The store has no map location yet: the admin pins it on
        the map separately afterwards. Before calling, tell the admin that
        the contact number and the address are shown to every customer,
        anonymous visitors included. If this tool refuses a detail, relay
        its message to the admin so they can correct that detail, then
        call again."""
        # The unique index on ownerUserId is what enforces "one store per
        # admin"; the read first only gives the usual case a clear message
        # without an insert.
        requireAdmin(userId)
        name = _validText(name, 'A store name', STORE_NAME_MAX_LENGTH)
        description = _validText(description, 'A description',
                                 STORE_DESCRIPTION_MAX_LENGTH)
        contactNumber = _validContactNumber(contactNumber)
        address = _validText(address, 'An address', STORE_ADDRESS_MAX_LENGTH)
        openingHours = _validText(openingHours, 'The opening hours text',
                                  STORE_OPENING_HOURS_MAX_LENGTH,
                                  required=False)
        # The generated HTTP handler passes a missing body key as None, so
        # the default above applies to direct and MCP calls only.
        if currency is None:
            currency = DEFAULT_STORE_CURRENCY
        if currency not in typing.get_args(StoreCurrency):
            raise ValueError('That currency is not supported')
        if db.read(ownerQuery(userId), STORES, projection={'_id': 1},
                   findOne=True) is not None:
            raise ValueError(ALREADY_HAS_A_STORE)
        baseSlug = storeSlug(name)
        createdAt = datetime.datetime.now(tz=datetime.timezone.utc)
        for attempt in range(SLUG_ATTEMPTS):
            slug = baseSlug if attempt == 0 else (
                f'{baseSlug}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}')
            store = Store(ownerUserId=userId,
                          name=name,
                          slug=slug,
                          currency=currency,
                          description=description,
                          contactNumber=contactNumber,
                          address=address,
                          openingHours=openingHours,
                          location=None,
                          createdAt=createdAt,
                          _version=0)
            try:
                created = db.create(store.model_dump(by_alias=True), STORES)
            except DuplicateKeyError as error:
                if _isOwnerCollision(error):
                    raise ValueError(ALREADY_HAS_A_STORE) from None
                continue  # the link name is taken: try one with a suffix
            return projectedCopy(StoreResponse, created)
        raise ValueError(
            'Could not pick a link name for this store, try another name')

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Store location saved',
                  responseModel=StoreResponse)
    def setUserStoreLocation(self, userId: str, latitude: float,
                             longitude: float) -> dict:
        """Pin the admin's store on the map (the map component sends it)."""
        requireAdmin(userId)
        location = StoreLocation(
            latitude=_validCoordinate(latitude, 'The latitude',
                                      LATITUDE_LIMIT),
            longitude=_validCoordinate(longitude, 'The longitude',
                                       LONGITUDE_LIMIT))
        store = db.read(ownerQuery(userId), STORES,
                        projection={'_id': 1, '_version': 1}, findOne=True)
        if store is None:
            raise ValueError(NO_STORE_YET)
        updated = db.update(
            {'_id': store['_id'], '_version': store['_version']},
            {'location': location.model_dump()}, STORES)
        return projectedCopy(StoreResponse, updated)
