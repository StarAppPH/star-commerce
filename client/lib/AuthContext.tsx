'use client';
import React, { createContext, useCallback, useContext, useRef, useState, useEffect } from 'react';
import { onAuthStateChanged, signInWithPopup, signOut, type User } from 'firebase/auth';
import { auth, googleProvider } from './firebase';
import { useServer } from './ServerContext';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { UserResponse } from '@/app/schemas/UserResponseSchema';
import { storedUser, type StoredUser } from './storedShapes';
import { ADMIN_SLOT, clearTokenSlot, saveTokenSlot, withTokenSlot } from './tokenSlots';

interface AuthContextType {
  user: User | null;
  // The user as it is kept across reloads (storedUser), also right after a
  // sign-in, so a page reads the same fields either way.
  userData: StoredUser | null;
  loading: boolean;
  setUserData: (data: UserResponse) => void;
  signInWithGoogle: () => Promise<ApiResponse<UserResponse>>;
  logout: () => Promise<void>;
  // Runs one admin request with the admin's token. A request the server
  // refuses for an expired token (401) signs in again from the Google
  // account Firebase still holds and is sent once more.
  adminRequest: <T>(send: () => Promise<ApiResponse<T>>) => Promise<ApiResponse<T>>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  userData: null,
  loading: true,
  setUserData: () => {},
  signInWithGoogle: async () => ({ message: 'AuthProvider is missing', status: 0, data: null }),
  logout: async () => {},
  adminRequest: async () => ({ message: 'AuthProvider is missing', status: 0, data: null }),
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
  const refreshing = useRef<Promise<boolean> | null>(null);
  const signingIn = useRef(false);

  const logout = useCallback(async () => {
    await signOut(auth);
    localStorage.removeItem('access_token');
    localStorage.removeItem(USER_DATA_KEY);
    clearTokenSlot(ADMIN_SLOT);
    setUserDataState(null);
  }, []);

  const setUserData = useCallback((data: UserResponse) => {
    // Stored field by field (storedShapes.ts), never the whole response.
    const stored = storedUser(data);
    setUserDataState(stored);
    localStorage.setItem(USER_DATA_KEY, JSON.stringify(stored));
  }, []);

  // The server verifies the Firebase ID token and signs in the account it
  // proves: send the token only, never the uid or email (the server ignores
  // them outside localdev). The generated method stores the access token,
  // which is kept in the admin's token slot.
  const signInWithToken = useCallback(async (idToken: string): Promise<ApiResponse<UserResponse>> => {
    const res = await server.loginWithGoogle({ idToken });
    if (res.status === 200) {
      saveTokenSlot(ADMIN_SLOT);
      setUserData(res.data);
    }
    return res;
  }, [server, setUserData]);

  // A new admin token from the Google account Firebase still holds; one
  // refresh at a time, whoever asks.
  const refreshSession = useCallback((): Promise<boolean> => {
    if (refreshing.current) return refreshing.current;
    const attempt = (async () => {
      const current = auth.currentUser;
      if (!current) return false;
      try {
        const res = await signInWithToken(await current.getIdToken());
        return res.status === 200;
      } catch {
        return false;
      }
    })();
    refreshing.current = attempt;
    attempt.finally(() => { refreshing.current = null; });
    return attempt;
  }, [signInWithToken]);

  useEffect(() => {
    // The stored user is restored when Firebase reports the auth state, in
    // its callback: never synchronously in the effect body. A Google account
    // Firebase still holds with no stored user (the generated client drops
    // user_data on any expired token, a shopper's in the same browser too)
    // signs in again, unless a sign-in is already on its way.
    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      setUser(firebaseUser);
      let stored = readStoredUserData();
      if (firebaseUser && !stored && !signingIn.current && (await refreshSession())) {
        stored = readStoredUserData();
      }
      setUserDataState(stored);
      setLoading(false);
    });

    const onJwtExpired = () => {
      refreshSession().then((ok) => { if (!ok) logout(); });
    };
    window.addEventListener("jwt-expired", onJwtExpired);

    return () => {
      unsubscribe();
      window.removeEventListener("jwt-expired", onJwtExpired);
    };
  }, [logout, refreshSession]);

  const signInWithGoogle = async (): Promise<ApiResponse<UserResponse>> => {
    signingIn.current = true;
    try {
      const credential = await signInWithPopup(auth, googleProvider);
      return await signInWithToken(await credential.user.getIdToken());
    } finally {
      signingIn.current = false;
    }
  };

  const adminRequest = useCallback(async <T,>(send: () => Promise<ApiResponse<T>>): Promise<ApiResponse<T>> => {
    const res = await withTokenSlot(ADMIN_SLOT, send);
    if (res.status !== 401) return res;
    if (!(await refreshSession())) return res;
    return withTokenSlot(ADMIN_SLOT, send);
  }, [refreshSession]);

  return (
    <AuthContext.Provider value={{ user, userData, loading, setUserData, signInWithGoogle, logout, adminRequest }}>
      <div data-component="AuthProvider">{children}</div>
    </AuthContext.Provider>
  );
}
