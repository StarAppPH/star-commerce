// The generated client (app/api/ServerRequests.ts) reads and writes one
// localStorage key, access_token, for every request. The admin and a shopper
// hold different tokens, so each side keeps its own copy in a slot and puts it
// back in access_token before its requests: a store owner can then use the
// admin chat and their own shop in the same browser.
//
// A slot is per browser, not per tab: two tabs of different sides take turns
// through withTokenSlot, which swaps the token in for one request at a time.

const ACCESS_TOKEN_KEY = 'access_token';
const SLOT_PREFIX = 'token_slot:';

export type TokenSlot = string;

export const ADMIN_SLOT: TokenSlot = 'admin';
export const guestSlot = (storeSlug: string): TokenSlot => `guest:${storeSlug}`;

function slotKey(slot: TokenSlot): string {
  return `${SLOT_PREFIX}${slot}`;
}

// Saves the token a sign-in just stored in access_token into the slot.
export function saveTokenSlot(slot: TokenSlot): void {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (token) localStorage.setItem(slotKey(slot), token);
}

export function hasTokenSlot(slot: TokenSlot): boolean {
  return localStorage.getItem(slotKey(slot)) !== null;
}

export function clearTokenSlot(slot: TokenSlot): void {
  localStorage.removeItem(slotKey(slot));
}

// Runs one request with the slot's token in access_token, then keeps the
// refreshed token the server may have sent back (the generated client writes
// it to access_token) in the slot.
export async function withTokenSlot<T>(slot: TokenSlot, request: () => Promise<T>): Promise<T> {
  const token = localStorage.getItem(slotKey(slot));
  if (token) localStorage.setItem(ACCESS_TOKEN_KEY, token);
  const result = await request();
  const refreshed = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (refreshed) {
    localStorage.setItem(slotKey(slot), refreshed);
  } else {
    clearTokenSlot(slot);
  }
  return result;
}
