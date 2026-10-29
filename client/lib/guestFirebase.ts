import { initializeApp, getApps } from 'firebase/app';
import { connectAuthEmulator, getAuth } from 'firebase/auth';
import { auth } from './firebase';

// Shoppers sign in anonymously, in a Firebase app of their own: its auth
// state is kept apart from the admin's Google sign-in, so a store owner who
// opens their own shop in the same browser stays signed in to the admin chat,
// and the shopper's anonymous account (their chats and orders) survives an
// admin sign-out.
const GUEST_APP_NAME = 'guest';

const guestApp = getApps().find((app) => app.name === GUEST_APP_NAME)
  ?? initializeApp(auth.app.options, GUEST_APP_NAME);
const guestAuth = getAuth(guestApp);

const USE_EMULATORS = process.env.NEXT_PUBLIC_ENVIRONMENT === 'localdev';
const AUTH_EMULATOR_URL = 'http://127.0.0.1:9099';

const emulatorState = globalThis as typeof globalThis & { __guestAuthEmulatorConnected?: boolean };
if (USE_EMULATORS && !emulatorState.__guestAuthEmulatorConnected) {
  if (!guestAuth.emulatorConfig) {
    connectAuthEmulator(guestAuth, AUTH_EMULATOR_URL);
  }
  emulatorState.__guestAuthEmulatorConnected = true;
}

export { guestAuth };
