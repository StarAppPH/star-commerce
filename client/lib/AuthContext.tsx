'use client';
import React, { createContext, useCallback, useContext, useState, useEffect } from 'react';
import { onAuthStateChanged, signInWithPopup, signOut, type User } from 'firebase/auth';
import { auth, googleProvider } from './firebase';
import { useServer } from './ServerContext';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { UserResponse } from '@/app/schemas/UserResponseSchema';
import { storedUser, type StoredUser } from './storedShapes';

interface AuthContextType {
  user: User | null;
  // The user as it is kept across reloads (storedUser), also right after a
  // sign-in, so a page reads the same fields either way.
  userData: StoredUser | null;
  loading: boolean;
  setUserData: (data: UserResponse) => void;
  signInWithGoogle: () => Promise<ApiResponse<UserResponse>>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  userData: null,
  loading: true,
  setUserData: () => {},
  signInWithGoogle: async () => ({ message: 'AuthProvider is missing', status: 0, data: null }),
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);

const USER_DATA_KEY = 'user_data';

// localStorage is not a server response: anything (an older app version, a
// browser extension, a hand edit) may be stored there, so a value is used
// only when it has a stored user's shape.
function isStoredUser(value: unknown): value is StoredUser {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate._id === 'string' &&
    typeof candidate.email === 'string' &&
    Array.isArray(candidate.roles)
  );
}

function readStoredUserData(): StoredUser | null {
  const stored = localStorage.getItem(USER_DATA_KEY);
  if (!stored) return null;
  try {
    const parsed: unknown = JSON.parse(stored);
    if (isStoredUser(parsed)) return parsed;
  } catch {
    // not JSON: dropped below
  }
  localStorage.removeItem(USER_DATA_KEY);
  return null;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { server } = useServer();
  const [user, setUser] = useState<User | null>(null);
  const [userData, setUserDataState] = useState<StoredUser | null>(null);
  const [loading, setLoading] = useState(true);

  const logout = useCallback(async () => {
    await signOut(auth);
    localStorage.removeItem('access_token');
    localStorage.removeItem(USER_DATA_KEY);
    setUserDataState(null);
  }, []);

  useEffect(() => {
    // The stored user is restored when Firebase reports the auth state, in
    // its callback: never synchronously in the effect body.
    const unsubscribe = onAuthStateChanged(auth, (firebaseUser) => {
      setUser(firebaseUser);
      setUserDataState(readStoredUserData());
      setLoading(false);
    });

    const onJwtExpired = () => { logout(); };
    window.addEventListener("jwt-expired", onJwtExpired);

    return () => {
      unsubscribe();
      window.removeEventListener("jwt-expired", onJwtExpired);
    };
  }, [logout]);

  const setUserData = (data: UserResponse) => {
    // Stored field by field (storedShapes.ts), never the whole response.
    const stored = storedUser(data);
    setUserDataState(stored);
    localStorage.setItem(USER_DATA_KEY, JSON.stringify(stored));
  };

  // The server verifies the Firebase ID token and signs in the account it
  // proves: send the token only, never the uid or email (the server ignores
  // them outside localdev). The generated method stores the access token.
  const signInWithGoogle = async (): Promise<ApiResponse<UserResponse>> => {
    const credential = await signInWithPopup(auth, googleProvider);
    const idToken = await credential.user.getIdToken();
    const res = await server.loginWithGoogle({ idToken });
    if (res.status === 200) {
      setUserData(res.data);
    }
    return res;
  };

  return (
    <AuthContext.Provider value={{ user, userData, loading, setUserData, signInWithGoogle, logout }}>
      <div data-component="AuthProvider">{children}</div>
    </AuthContext.Provider>
  );
}
