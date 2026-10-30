import { initializeApp, getApps } from 'firebase/app';
import { connectAuthEmulator, getAuth } from 'firebase/auth';
import { connectStorageEmulator, getStorage } from 'firebase/storage';
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
// The shopper's uploads (chat attachments), signed in as their anonymous
// account: the admin's `storage` would upload as the admin.
const guestStorage = getStorage(guestApp);

const USE_EMULATORS = process.env.NEXT_PUBLIC_ENVIRONMENT === 'localdev';
const AUTH_EMULATOR_URL = 'http://127.0.0.1:9099';
const STORAGE_EMULATOR_HOST = '127.0.0.1';
const STORAGE_EMULATOR_PORT = 9199;

const emulatorState = globalThis as typeof globalThis & { __guestAuthEmulatorConnected?: boolean };
if (USE_EMULATORS && !emulatorState.__guestAuthEmulatorConnected) {
  if (!guestAuth.emulatorConfig) {
    connectAuthEmulator(guestAuth, AUTH_EMULATOR_URL);
  }
  connectStorageEmulator(guestStorage, STORAGE_EMULATOR_HOST, STORAGE_EMULATOR_PORT);
  emulatorState.__guestAuthEmulatorConnected = true;
}

export { guestAuth, guestStorage };
