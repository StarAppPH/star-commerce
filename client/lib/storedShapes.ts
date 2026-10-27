import type { UserResponse } from '@/app/schemas/UserResponseSchema';

// Browser storage keeps a copy of a server value across reloads. It is written
// field by field, and only the fields the app reads back, so the
// unused-response-field gate (server/test_unused_response_fields.py) still sees
// which fields the client needs: serializing a whole value would hide that.
// Reading a new field of a stored value? Add it here as well, or it is missing
// after a reload. A restored value is typed Stored<Name>, so tsc flags a read
// of a field that is not stored.

export function storedUser(user: UserResponse) {
  return {
    _id: user._id,
    name: user.name,
    email: user.email,
    userType: user.userType,
    createdAt: user.createdAt,
    roles: user.roles.map((role) => ({ _id: role._id, value: role.value, description: role.description })),
  };
}

export type StoredUser = ReturnType<typeof storedUser>;
