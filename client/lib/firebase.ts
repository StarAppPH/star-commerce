import { initializeApp, getApps, type FirebaseOptions } from "firebase/app";
import { getAuth, connectAuthEmulator, GoogleAuthProvider } from "firebase/auth";
import { getStorage, connectStorageEmulator } from "firebase/storage";
import { FIREBASE_PROJECT_ID } from "@/lib/generated/serverConstants";

// The web config of your Firebase project: `create-star-app cloud-setup`
// fills these lines in (Firebase Console > Project Settings > General > Your
// apps has them too). The project id lives in server/sharedConstants.py
// (FIREBASE_PROJECT_ID): the server only accepts sign-ins from that same
// project.
const firebaseConfig = {
  apiKey: "AIzaSyC-NVxra0LUyXUvTxr4O2uupFAU5Ca4Tsg",
  authDomain: "star-commerce-6797.firebaseapp.com",
  projectId: FIREBASE_PROJECT_ID,
  storageBucket: "star-commerce-6797.firebasestorage.app",
  messagingSenderId: "764152965079",
  appId: "1:764152965079:web:b286cdd3ddb93558ed0d92"
};

// Before cloud-setup the project id is a "demo-" one, which Firebase runs only
// in the local Emulator Suite: there is no real web config yet, and the
// emulators accept any API key.
const DEMO_PROJECT = FIREBASE_PROJECT_ID.startsWith("demo-");
const demoConfig: FirebaseOptions = {
  apiKey: "demo-api-key",
  authDomain: `${FIREBASE_PROJECT_ID}.firebaseapp.com`,
  projectId: FIREBASE_PROJECT_ID,
  storageBucket: `${FIREBASE_PROJECT_ID}.firebasestorage.app`
};

// localdev (client/.env.local) signs in and stores files in the Firebase
// Emulator Suite on this computer (`create-star-app dev` starts it; ports from
// firebase.json), never in the real project.
const USE_EMULATORS = process.env.NEXT_PUBLIC_ENVIRONMENT === "localdev";
const AUTH_EMULATOR_URL = "http://127.0.0.1:9099";
const STORAGE_EMULATOR_HOST = "127.0.0.1";
const STORAGE_EMULATOR_PORT = 9199;

const app = getApps().length === 0
  ? initializeApp(DEMO_PROJECT ? demoConfig : firebaseConfig)
  : getApps()[0];
const auth = getAuth(app);
const googleProvider = new GoogleAuthProvider();
const storage = getStorage(app);

// Connected once per page: a hot reload re-runs this module with the same app,
// and connecting a second time throws.
const emulatorState = globalThis as typeof globalThis & { __firebaseEmulatorsConnected?: boolean };
if (USE_EMULATORS && !emulatorState.__firebaseEmulatorsConnected) {
  if (!auth.emulatorConfig) {
    connectAuthEmulator(auth, AUTH_EMULATOR_URL);
  }
  connectStorageEmulator(storage, STORAGE_EMULATOR_HOST, STORAGE_EMULATOR_PORT);
  emulatorState.__firebaseEmulatorsConnected = true;
}

export { auth, googleProvider, storage };
