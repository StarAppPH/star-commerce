from pydantic import BaseModel, Field, RootModel
from typing import List, Optional

# ----------------------------- Role & User Type Config ----------------------------- #
# Define your roles here. Each role needs an _id, description, and unauthorizedMessage.
# Add new roles to this list and to USER_TYPES_ROLES below.

ALL_ROLES = [
    {"_id": "canUpdateUserRole", "value": False, "description": "can update user roles", "unauthorizedMessage": "You are not authorized to update user roles"},
]

# Define which roles each user type has access to.
# Add new user types here (e.g., 'admin', 'manager', 'viewer').

USER_TYPES_ROLES = {
    'unauthorized': {
        'canUpdateUserRole': False,
    },
}

# ----------------------------- Role Classes ----------------------------- #


class Role(BaseModel):
    id: str = Field(..., description='Role id', alias='_id')
    value: bool = Field(..., description='Role value')
    description: str = Field(..., description='Role description')
    collectionsTransacted: Optional[List[str]] = Field(
        default=[],
        description='List of collections transacted by role',
        exclude=True)
    unauthorizedMessage: Optional[str] = Field(
        default='',
        description=
        'Message to show when user is unauthorized to perform this role action',
        exclude=True)

    def switchRole(self) -> None:
        self.value = not self.value

    def setTrue(self) -> None:
        self.value = True

    def setFalse(self) -> None:
        self.value = False


class AllRoles(BaseModel):
    roles: List[Role] = Field(default_factory=lambda: [Role(**r) for r in ALL_ROLES])

    def getAllRoles(self) -> List[Role]:
        return self.roles

    def getAllRoleIds(self) -> List[str]:
        return [role.id for role in self.roles]

    def getSpecificRole(self, roleId: str) -> Role:
        for role in self.roles:
            if role.id == roleId:
                return role
        raise ValueError(f'Role {roleId} not found')

    def getRoleCount(self) -> int:
        return len(self.roles)


class UserRoles(RootModel[List[Role]]):

    def getRoles(self) -> List[Role]:
        return self.root

    def getRole(self, roleId: str) -> Role:
        for role in self.root:
            if role.id == roleId:
                return role
        raise ValueError(f'Role {roleId} not found')


class RoleTracker(BaseModel):
    ''' This class is used to trach the roles that were set by the role setter and validate if all roles were used'''
    listOfRolesTransacted: List[Role] = Field(default_factory=list,
                                              description='List of roles')

    def addRole(self, role: Role) -> None:
        if role in self.listOfRolesTransacted:
            raise ValueError(f'{role.id} has already been transacted')
        else:
            self.listOfRolesTransacted.append(role)

    def validateIfAllRolesWereTransacted(self) -> bool:
        transacted_ids = {x.id
                          for x in self.listOfRolesTransacted
                          }  # Use a set to avoid duplicates
        allRoles = AllRoles()
        required_ids = set(allRoles.getAllRoleIds())

        for x in required_ids:
            if x not in transacted_ids:
                raise ValueError(f'Role {x} was not transacted')

        return True


class UserRoleTypes():

    def __init__(self):
        self.userTypesRoles = USER_TYPES_ROLES

    def getConfig(self, userType: str) -> dict:
        if userType not in self.userTypesRoles:
            raise ValueError(f'User type {userType} not found')
        return self.userTypesRoles[userType]

    def getUserTypes(self) -> list[str]:
        userTypes = []
        for key, value in self.userTypesRoles.items():
            userTypes.append(key)

        return userTypes


class RoleSetter(BaseModel):
    userRoles: UserRoles = Field(..., description='User roles')

    def __setRole(self, role: Role, value: bool, roleTracker: RoleTracker) -> Role:
        roleTracker.addRole(role)  # add role to tracker
        if value == True:
            role.setTrue()
        else:
            role.setFalse()
        return role

    def setSuperAdminRoles(self) -> None:
        """ Set all roles to true """
        for role in self.userRoles.root:
            role.setTrue()

    def setRole(self, userType: str) -> None:
        """ Set a specific role to true or false """

        if userType not in UserRoleTypes().getUserTypes():
            raise ValueError(f'User type {userType} not found')

        roleTracker = RoleTracker()
        for key, value in UserRoleTypes().getConfig(userType).items():
            foundRole = False
            for role in self.userRoles.root:
                if role.id == key:
                    self.__setRole(role, value, roleTracker)
                    foundRole = True
                    break

            if not foundRole:
                role = AllRoles().getSpecificRole(key)
                role = self.__setRole(role, False, roleTracker)
                self.userRoles.root.append(role)

        roleTracker.validateIfAllRolesWereTransacted()

    def setSpecificRole(self, roleId: str, value: bool) -> List[Role]:
        roleTracker = RoleTracker()
        foundRole = False
        for role in self.userRoles.root:
            if role.id == roleId:
                self.__setRole(role, value, roleTracker)
                foundRole = True
                break

        if not foundRole:
            role = AllRoles().getSpecificRole(roleId)
            self.__setRole(role, value, roleTracker)
            self.userRoles.root.append(role)

        return self.userRoles.root


if __name__ == "__main__":
    allRoles = AllRoles().getAllRoles()
    userRoles = UserRoles(allRoles)
    superAdmin = RoleSetter(userRoles=userRoles).setSuperAdminRoles()
    print(allRoles)
    print(userRoles.model_dump())
    print(userRoles.model_dump())
