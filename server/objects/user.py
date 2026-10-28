"""Users, sign-in and roles.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field, model_validator
from roles import RoleSetter, AllRoles, UserRoles

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)


# ──────────────────────────────────────────────
# Users and sign-in
# ──────────────────────────────────────────────


class User(MongoModel):
    id: str | None = Field(...,
                           alias='_id',
                           serialization_alias='_id',
                           description="Unique identifier for the user")
    name: str | None = Field(..., description="Username of the user")
    email: str = Field(..., description="Email address of the user")
    roles: UserRoles = Field(..., description="Roles assigned to the user")
    userType: str | None = Field(None, description='User type')
    createdAt: datetime.datetime = Field(...,
                                         description="Account creation date")
    needToBeLinked: bool = Field(
        False,
        description=
        "User has been created by pubSub function and needs to be linked by authentication system"
    )
    version: int = Field(1, alias='_version', serialization_alias='_version')

    @model_validator(mode='after')
    def validateRoles(self):
        if self.needToBeLinked == False:
            if self.id is None:
                raise ValueError("User id cannot be None for linked users")
            if self.name is None:
                raise ValueError("User name cannot be empty for linked users")
        return self

    def setRole(self, userType: str) -> dict:
        roleSetter = RoleSetter(userRoles=self.roles)
        if userType == 'superAdmin':
            roleSetter.setSuperAdminRoles()
            self.userType = 'superAdmin'
        else:
            roleSetter.setRole(userType)
            self.userType = userType
        return self

    def setSpecificRole(self, roleId: str, value: bool) -> dict:

        if self.userType == None:
            raise ValueError('User type not set, cannot set role')

        roleSetter = RoleSetter(userRoles=self.roles)
        roleSetter.setSpecificRole(roleId, value)
        return self

    def checkIfAuthorized(self, roleId: str) -> bool | PermissionError:
        try:
            role = self.roles.getRole(roleId)
        except ValueError:
            raise PermissionError(
                AllRoles().getSpecificRole(roleId).unauthorizedMessage)
        if role.value == False:
            raise PermissionError(
                AllRoles().getSpecificRole(roleId).unauthorizedMessage)
        return True

    def isAuthorized(self, roleId: str) -> bool:
        try:
            role = self.roles.getRole(roleId)
        except Exception:
            return AllRoles().getSpecificRole(roleId).value
        return role.value


class UserView(StoredDocumentKeys, User):
    """A Users document as stored."""


class RoleResponse(ResponseModel):
    """A role as a response sends it: roles.Role as it serializes (its
    excluded fields are never sent)."""
    id: str = Field(..., alias='_id', description='Role id')
    value: bool = Field(..., description='Role value')
    description: str = Field(..., description='Role description')


class UserResponse(ResponseModel):
    """A Users document as loginWithGoogle and devLogin send it: the client
    keeps it (client/lib/storedShapes.ts). Read with
    projection_of(UserResponse), or cut down with projectedCopy."""
    id: str | None = Field(...,
                           alias='_id',
                           description='Unique identifier for the user')
    name: str | None = Field(..., description='Username of the user')
    email: str = Field(..., description='Email address of the user')
    roles: list[RoleResponse] = Field(...,
                                      description='Roles assigned to the user')
    userType: str | None = Field(None, description='User type')
    createdAt: datetime.datetime = Field(...,
                                         description='Account creation date')

    @model_validator(mode='after')
    def _user_has_an_id(self):
        # Only the id: a user Pub/Sub created has no name until it signs in.
        if not self.id:
            raise ValueError('User id cannot be empty')
        return self


# A Firebase ID token is a JWT of about a kilobyte; anything far longer is
# refused before it reaches the verifier.
FIREBASE_ID_TOKEN_MAX_LENGTH = 4096


class FirebaseUserObject(AppModel):
    """The sign-in payload loginWithGoogle receives. Anything else the client
    sends is dropped.

    The client sends just `idToken`, and the uid, email and name are then
    taken from the VERIFIED token. The raw identity fields are honoured ONLY
    in localdev without an idToken (signing in with no Firebase project, and
    the tests); trusting them anywhere else would let a client pick whose
    account it signs into."""
    idToken: str | None = Field(
        None,
        max_length=FIREBASE_ID_TOKEN_MAX_LENGTH,
        description='Firebase ID token; the only field read outside '
        'localdev')
    uid: str | None = Field(None,
                            description='Localdev only: Firebase uid, the '
                            'Users _id')
    email: str | None = Field(None, description='Localdev only: email')
    displayName: str | None = Field(None,
                                    description='Localdev only: display name')


class DevUserRow(ResponseModel):
    """One row of devGetUserList: the fields its projection reads
    (projection_of(DevUserRow)). Each field restates User's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Users _id')
    name: str | None = Field(..., description='Username of the user')
    userType: str | None = Field(None, description='User type')


class DevUserListResponse(ResponseModel):
    users: list[DevUserRow] = Field(..., description='Every user')


# ──────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────


class RoleListResponse(ResponseModel):
    roles: list[RoleResponse] = Field(..., description='Every role')
