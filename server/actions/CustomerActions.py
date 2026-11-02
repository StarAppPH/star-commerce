"""CustomerActions: the customer side.

A customer never "logs in". Opening a store's link, the client signs in to
Firebase anonymously (no step for the customer) and startGuestSession turns
that verified guest into our JWT. At checkout the client links a phone
number to the same Firebase user (same uid), and verifyCustomerPhone stores
the number Firebase verified as the contact number. The server never takes
a phone number from the client, only from a verified token.

A returning customer on a new guest (another device, cleared site data)
cannot link their number to it: Firebase holds it for their first guest.
The client then signs in with the number instead, and verifyCustomerPhone
(with the new guest's token) gives that customer the new guest's chats at the store and its cart, when
the guest's cart was used last (carts are never merged).

A customer is a Customers document; an admin (a Users document) is none, so
an admin's JWT reaches no customer route and a customer's no admin route.
"""

import datetime
import re

from pymongo.errors import DuplicateKeyError

from AuthHandler import verifyFirebaseIdToken
from actions.StoreActions import SET_UP, STORE_NOT_FOUND
from mongoDb import OutdatedDataError, db
from objects import (FIREBASE_ID_TOKEN_MAX_LENGTH, PHONE_NUMBER_PATTERN,
                     Cart, Customer, CustomerSessionResponse,
                     NoContentResponse, ShopStoreResponse)
from route_config import route_config

CUSTOMERS = 'Customers'
STORES = 'Stores'
# The Firebase sign-ins a customer may visit a store with: the anonymous
# guest, or a phone sign-in (a returning customer on another device).
GUEST_SIGN_IN_PROVIDERS = ('anonymous', 'phone')
# The store's brand colors, which the shop is drawn in (ShopStoreResponse).
SHOP_THEME_FIELDS = ('primaryColor', 'secondaryColor')
NOT_A_GUEST_VISIT = 'That sign-in is not a guest visit'
GUEST_VISIT_NEEDS_BOTH = 'Send both storeId and guestIdToken, or neither'
STORE_ID_NOT_TEXT = 'storeId must be text'
GUEST_CART_TAKE_OVER_ATTEMPTS = 3


def _verifiedClaims(idToken: str) -> dict:
    """The claims of a Firebase ID token that verifies; the message never
    quotes the token."""
    if (not isinstance(idToken, str) or not idToken
            or len(idToken) > FIREBASE_ID_TOKEN_MAX_LENGTH):
        raise ValueError('Invalid sign-in request')
    return verifyFirebaseIdToken(idToken)


def _signInProvider(claims: dict) -> str | None:
    return (claims.get('firebase') or {}).get('sign_in_provider')


def _verifiedPhoneNumber(claims: dict) -> str | None:
    """The phone number Firebase verified for this user, if it has one.
    Firebase sets phone_number only for a user with a linked phone."""
    phoneNumber = claims.get('phone_number')
    if isinstance(phoneNumber, str) and re.fullmatch(PHONE_NUMBER_PATTERN,
                                                     phoneNumber):
        return phoneNumber
    return None


def requireCustomer(userId: str) -> dict:
    """The caller's Customers document; refuses anyone else (an admin)."""
    customer = db.read({'_id': userId}, CUSTOMERS, findOne=True)
    if customer is None:
        raise PermissionError('Only a customer can do this')
    return customer


def _customerOf(claims: dict) -> dict:
    """The Customers document of the token's uid, created on its first
    visit. A phone sign-in's first visit keeps its verified number."""
    customerId = claims['uid']
    stored = db.read({'_id': customerId}, CUSTOMERS, findOne=True)
    if stored is not None:
        return stored
    customer = Customer(_id=customerId,
                        phoneNumber=_verifiedPhoneNumber(claims),
                        createdAt=datetime.datetime.now(
                            tz=datetime.timezone.utc),
                        _version=0)
    try:
        return db.create(customer.model_dump(by_alias=True), CUSTOMERS)
    except DuplicateKeyError:
        # Two first visits raced (two tabs): the other one created it.
        return db.read({'_id': customerId}, CUSTOMERS, findOne=True)


def _guestOfVisit(userId: str, storeId: str | None,
                  guestIdToken: str | None) -> str | None:
    """The uid of the guest whose visit to the store the customer takes
    over (None when no guest was sent): its token must verify as an
    anonymous sign-in other than the customer."""
    if storeId is None and guestIdToken is None:
        return None
    if storeId is None or guestIdToken is None:
        raise ValueError(GUEST_VISIT_NEEDS_BOTH)
    if not isinstance(storeId, str):
        raise ValueError(STORE_ID_NOT_TEXT)
    claims = _verifiedClaims(guestIdToken)
    if _signInProvider(claims) != 'anonymous' or claims['uid'] == userId:
        raise PermissionError(NOT_A_GUEST_VISIT)
    return claims['uid']


def _takeOverGuestCart(userId: str, guestId: str, storeId: str) -> None:
    """The guest's cart at the store replaces the customer's own when it
    was used last (its updatedAt is later), or when the customer has none;
    carts are never merged. The guest's cart is then deleted either way."""
    from actions.CartActions import CART_BUSY, CARTS, cartId
    guestCart = db.read({'_id': cartId(guestId, storeId)}, CARTS,
                        findOne=True)
    if guestCart is None:
        return
    ownId = cartId(userId, storeId)
    for attempt in range(GUEST_CART_TAKE_OVER_ATTEMPTS):
        own = db.read({'_id': ownId}, CARTS, findOne=True)
        try:
            if own is None:
                taken = Cart(_id=ownId, customerId=userId, storeId=storeId,
                             items=guestCart.get('items') or [],
                             updatedAt=guestCart['updatedAt'], _version=0)
                db.create(taken.model_dump(by_alias=True), CARTS)
            elif guestCart['updatedAt'] > own['updatedAt']:
                db.update({'_id': ownId, '_version': own['_version']},
                          {'items': guestCart.get('items') or [],
                           'updatedAt': guestCart['updatedAt']}, CARTS)
            break
        except (OutdatedDataError, DuplicateKeyError):
            if attempt == GUEST_CART_TAKE_OVER_ATTEMPTS - 1:
                raise ValueError(CART_BUSY) from None
    db.delete({'_id': guestCart['_id']}, CARTS)


def _takeOverGuestConversations(userId: str, guestId: str,
                                storeId: str) -> None:
    """The guest's conversations at the store, and their turns, become the
    customer's (a conversation and its turns are reached by their userId).
    The turns move first, so a failure halfway leaves the conversation
    with the guest and a retry finishes it."""
    from actions.ChatActions import CHAT_TURNS, CONVERSATIONS
    conversationIds = [conversation['_id'] for conversation in db.read(
        {'userId': guestId, 'storeId': storeId}, CONVERSATIONS,
        projection={'_id': 1})]
    if not conversationIds:
        return
    turnIds = [turn['_id'] for turn in db.read(
        {'conversationId': {'$in': conversationIds}, 'userId': guestId},
        CHAT_TURNS, projection={'_id': 1})]
    for collection, ids in ((CHAT_TURNS, turnIds),
                            (CONVERSATIONS, conversationIds)):
        db.bulkUpdate([{'filter': {'_id': _id, 'userId': guestId},
                        'update': {'$set': {'userId': userId},
                                   '$inc': {'_version': 1}}}
                       for _id in ids], collection)


class CustomerActions():

    @route_config(httpMethod='POST',
                  jwtRequired=False,
                  createAccessToken=True,
                  successMessage='Welcome',
                  responseModel=CustomerSessionResponse)
    def startGuestSession(self, idToken: str, storeSlug: str) -> dict:
        claims = _verifiedClaims(idToken)
        if _signInProvider(claims) not in GUEST_SIGN_IN_PROVIDERS:
            raise PermissionError('Open the store as a guest to shop')
        # Only a set-up store has a slug, a string (StoreActions.SET_UP):
        # a null storeSlug would match every store not set up yet. The
        # query restates SET_UP so the partial slug index serves it.
        store = None
        if isinstance(storeSlug, str):
            store = db.readResponse(
                ShopStoreResponse,
                {'slug': {'$eq': storeSlug, **SET_UP['slug']}}, STORES)
        if store is None:
            raise ValueError(STORE_NOT_FOUND)
        # A brand color is sent only once set (ShopStoreResponse).
        store = {key: value for key, value in store.items()
                 if not (key in SHOP_THEME_FIELDS and value is None)}
        customer = _customerOf(claims)
        return {
            '_id': customer['_id'],
            'phoneNumber': customer.get('phoneNumber'),
            'store': store,
        }

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Phone number verified',
                  responseModel=NoContentResponse)
    def verifyCustomerPhone(self, userId: str, idToken: str,
                            storeId: str | None = None,
                            guestIdToken: str | None = None) -> dict:
        """Store the number Firebase verified for this customer. The token
        must be this customer's own (the guest the phone was linked to, or
        the account a returning customer signed in to with it): a token of
        anyone else is refused.

        A returning customer who opened the store as a new guest (another
        device, cleared site data) sends that guest's own Firebase ID token
        (`guestIdToken`, an anonymous sign-in, proving they were it) and the
        store: the guest's chats at the store, and its cart when it was used
        last, become this customer's."""
        customer = requireCustomer(userId)
        claims = _verifiedClaims(idToken)
        if claims['uid'] != userId:
            raise PermissionError('This phone sign-in is for another visitor')
        phoneNumber = _verifiedPhoneNumber(claims)
        if phoneNumber is None:
            raise PermissionError('Verify your phone number to continue')
        guestId = _guestOfVisit(userId, storeId or None, guestIdToken or None)
        db.update({
            '_id': userId,
            '_version': customer['_version']
        }, {'phoneNumber': phoneNumber}, CUSTOMERS)
        if guestId is not None:
            _takeOverGuestCart(userId, guestId, storeId)
            _takeOverGuestConversations(userId, guestId, storeId)
        return {}
