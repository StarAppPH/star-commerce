"""CustomerActions: the customer side.

A customer never "logs in". Opening a store's link, the client signs in to
Firebase anonymously (no step for the customer) and startGuestSession turns
that verified guest into our JWT. At checkout the client links a phone
number to the same Firebase user (same uid), and verifyCustomerPhone stores
the number Firebase verified as the contact number. The server never takes
a phone number from the client, only from a verified token.

A customer is a Customers document; an admin (a Users document) is none, so
an admin's JWT reaches no customer route and a customer's no admin route.
"""

import datetime
import re

from pymongo.errors import DuplicateKeyError

from AuthHandler import verifyFirebaseIdToken
from actions.StoreActions import SET_UP, STORE_NOT_FOUND
from mongoDb import db
from objects import (FIREBASE_ID_TOKEN_MAX_LENGTH, PHONE_NUMBER_PATTERN,
                     Customer, CustomerSessionResponse, PhoneVerifiedResponse,
                     StoreResponse)
from route_config import route_config

CUSTOMERS = 'Customers'
STORES = 'Stores'
# The Firebase sign-ins a customer may visit a store with: the anonymous
# guest, or a phone sign-in (a returning customer on another device).
GUEST_SIGN_IN_PROVIDERS = ('anonymous', 'phone')


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
                StoreResponse, {'slug': {'$eq': storeSlug, **SET_UP['slug']}},
                STORES)
        if store is None:
            raise ValueError(STORE_NOT_FOUND)
        customer = _customerOf(claims)
        return {
            '_id': customer['_id'],
            'phoneNumber': customer.get('phoneNumber'),
            'store': store,
        }

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Phone number verified',
                  responseModel=PhoneVerifiedResponse)
    def verifyCustomerPhone(self, userId: str, idToken: str) -> dict:
        """Store the number Firebase verified for this customer. The token
        must be this customer's own (the guest the phone was linked to):
        a token of anyone else is refused."""
        customer = requireCustomer(userId)
        claims = _verifiedClaims(idToken)
        if claims['uid'] != userId:
            raise PermissionError('This phone sign-in is for another visitor')
        phoneNumber = _verifiedPhoneNumber(claims)
        if phoneNumber is None:
            raise PermissionError('Verify your phone number to continue')
        db.update({
            '_id': userId,
            '_version': customer['_version']
        }, {'phoneNumber': phoneNumber}, CUSTOMERS)
        return {'phoneNumber': phoneNumber}
