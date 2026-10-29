'use client';
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { onAuthStateChanged, signInAnonymously, type User } from 'firebase/auth';
import { guestAuth } from './guestFirebase';
import { useServer } from './ServerContext';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { StoreResponse } from '@/app/schemas/StoreResponseSchema';
import { guestSlot, saveTokenSlot, withTokenSlot } from './tokenSlots';

// A shopper never signs in: their browser holds an anonymous Firebase account
// (lib/guestFirebase.ts), which startGuestSession turns into a customer of the
// store whose link they opened.

export interface GuestStore {
  id: string;
  name: string;
  slug: string;
  currency: StoreResponse['currency'];
  description: string | null;
  contactNumber: string | null;
  address: string | null;
  openingHours: string | null;
}

export interface GuestSession {
  customerId: string;
  phoneNumber: string | null;
  store: GuestStore;
}

type GuestSessionState =
  | { status: 'loading' }
  | { status: 'ready'; session: GuestSession }
  | { status: 'error'; message: string };

interface GuestSessionContextType {
  state: GuestSessionState;
  // Runs one request as the shopper. A request refused for an expired token
  // (401) starts the session again and is sent once more.
  guestRequest: <T>(send: (session: GuestSession) => Promise<ApiResponse<T>>) => Promise<ApiResponse<T>>;
}

const GuestSessionContext = createContext<GuestSessionContextType | null>(null);

export function useGuestSession(): GuestSessionContextType {
  const context = useContext(GuestSessionContext);
  if (!context) throw new Error('useGuestSession must be used within a GuestSessionProvider');
  return context;
}

// The shopper's session where there is one (a shop page), else null.
export function useOptionalGuestSession(): GuestSessionContextType | null {
  return useContext(GuestSessionContext);
}

function waitForGuestUser(): Promise<User | null> {
  return new Promise((resolve) => {
    const unsubscribe = onAuthStateChanged(guestAuth, (user) => {
      unsubscribe();
      resolve(user);
    });
  });
}

export function GuestSessionProvider({ storeSlug, children }: { storeSlug: string; children: React.ReactNode }) {
  const { server } = useServer();
  const [state, setState] = useState<GuestSessionState>({ status: 'loading' });
  const sessionRef = useRef<GuestSession | null>(null);
  const starting = useRef<Promise<GuestSession | null> | null>(null);
  const slot = guestSlot(storeSlug);

  const start = useCallback((): Promise<GuestSession | null> => {
    if (starting.current) return starting.current;
    const attempt = (async (): Promise<GuestSession | null> => {
      try {
        const user = (await waitForGuestUser()) ?? (await signInAnonymously(guestAuth)).user;
        const res = await server.startGuestSession(await user.getIdToken(), storeSlug);
        if (res.status !== 200) {
          setState({ status: 'error', message: res.message });
          return null;
        }
        saveTokenSlot(slot);
        const store = res.data.store;
        const session: GuestSession = {
          customerId: res.data._id,
          phoneNumber: res.data.phoneNumber,
          store: {
            id: store._id,
            name: store.name ?? 'Store',
            slug: store.slug ?? storeSlug,
            currency: store.currency,
            description: store.description ?? null,
            contactNumber: store.contactNumber ?? null,
            address: store.address ?? null,
            openingHours: store.openingHours,
          },
        };
        sessionRef.current = session;
        setState({ status: 'ready', session });
        return session;
      } catch {
        setState({ status: 'error', message: 'We couldn’t open this store. Check your connection and try again.' });
        return null;
      }
    })();
    starting.current = attempt;
    attempt.finally(() => { starting.current = null; });
    return attempt;
  }, [server, storeSlug, slot]);

  useEffect(() => {
    start();
  }, [start]);

  const guestRequest = useCallback(async <T,>(send: (session: GuestSession) => Promise<ApiResponse<T>>): Promise<ApiResponse<T>> => {
    const session = sessionRef.current ?? (await start());
    if (!session) return { message: 'This store isn’t available right now', status: 0, data: null };
    const res = await withTokenSlot(slot, () => send(session));
    if (res.status !== 401) return res;
    const renewed = await start();
    if (!renewed) return res;
    return withTokenSlot(slot, () => send(renewed));
  }, [start, slot]);

  return (
    <GuestSessionContext.Provider value={{ state, guestRequest }}>
      {children}
    </GuestSessionContext.Provider>
  );
}
