'use client';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { CartScreenResponse } from '@/app/schemas/CartScreenResponseSchema';
import type { NewOrderItem } from '@/app/schemas/NewOrderItemSchema';
import { useGuestSession, type GuestSession } from './GuestSessionContext';
import { useServer } from './ServerContext';
import { useSnackbar } from './SnackbarContext';

// The shopper's cart at the store they opened (server CartActions): read
// once the shop opens and again after every chat turn (the assistant may
// change it), changed from the products card and the cart panel.

interface ShopCartValue {
  cart: CartScreenResponse | null;
  // How many units the cart holds.
  count: number;
  // A change is on its way: nothing more is sent until it answers.
  busy: boolean;
  refresh: () => Promise<void>;
  // Each returns whether the server took it.
  add: (items: [NewOrderItem, ...NewOrderItem[]]) => Promise<boolean>;
  setQuantity: (productId: string, quantity: number) => Promise<boolean>;
  remove: (productId: string) => Promise<boolean>;
  clear: () => Promise<boolean>;
}

const ShopCartContext = createContext<ShopCartValue | null>(null);

export function useShopCart(): ShopCartValue {
  const context = useContext(ShopCartContext);
  if (!context) throw new Error('useShopCart must be used within a ShopCartProvider');
  return context;
}

// The shopper's cart where there is one (a shop page), else null.
export function useOptionalShopCart(): ShopCartValue | null {
  return useContext(ShopCartContext);
}

export function ShopCartProvider({ children }: { children: React.ReactNode }) {
  const { server } = useServer();
  const { guestRequest } = useGuestSession();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const [cart, setCart] = useState<CartScreenResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const res = await guestRequest((session) => server.getMyCart(session.customerId, session.store.id));
    if (res.status === 200) setCart(res.data);
    else showSnackbar(res.message, 'error');
  }, [guestRequest, server, showSnackbar]);

  useEffect(() => {
    (async () => {
      await refresh();
    })();
  }, [refresh]);

  // A change from a button: its answer is the whole cart.
  const change = useCallback(async (send: (session: GuestSession) => Promise<ApiResponse<CartScreenResponse>>) => {
    setBusy(true);
    try {
      const res = await guestRequest(send);
      showApiResponseSnackbar(res);
      if (res.status !== 200) return false;
      setCart(res.data);
      return true;
    } finally {
      setBusy(false);
    }
  }, [guestRequest, showApiResponseSnackbar]);

  const value = useMemo<ShopCartValue>(() => ({
    cart,
    count: cart ? cart.items.reduce((sum, item) => sum + item.quantity, 0) : 0,
    busy,
    refresh,
    add: (items) => change((session) => server.addToCart(session.customerId, session.store.id, items)),
    setQuantity: (productId, quantity) => change((session) => server.setCartItemQuantity(session.customerId, session.store.id, productId, quantity)),
    remove: (productId) => change((session) => server.removeFromCart(session.customerId, session.store.id, productId)),
    clear: () => change((session) => server.clearMyCart(session.customerId, session.store.id)),
  }), [cart, busy, refresh, change, server]);

  return <ShopCartContext.Provider value={value}>{children}</ShopCartContext.Provider>;
}
