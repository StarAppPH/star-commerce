'use client';
import React, { createContext, useCallback, useContext, useMemo } from 'react';
import { useGuestSession, type GuestSession } from '@/lib/GuestSessionContext';
import { useServer } from '@/lib/ServerContext';
import type { ChatTransport } from '@/components/chat/useChat';
import { CONVERSATIONS_PAGE, useConversationList, type ConversationList } from '@/components/chat/useConversationList';

interface ShopWorkspaceValue extends ConversationList {
  session: GuestSession;
  transport: ChatTransport;
}

const ShopWorkspaceContext = createContext<ShopWorkspaceValue | null>(null);

export function useShopWorkspace(): ShopWorkspaceValue {
  const context = useContext(ShopWorkspaceContext);
  if (!context) throw new Error('useShopWorkspace must be used within a ShopWorkspaceProvider');
  return context;
}

// A shopper's chats at the store they opened, once their guest session is up.
export function ShopWorkspaceProvider({ session, children }: { session: GuestSession; children: React.ReactNode }) {
  const { server } = useServer();
  const { guestRequest } = useGuestSession();

  const fetchPage = useCallback((before: string | null) => guestRequest((current) => server.listMyConversations(
    current.customerId, current.store.id, before, CONVERSATIONS_PAGE,
  )), [guestRequest, server]);
  const list = useConversationList(fetchPage, true);

  const transport = useMemo<ChatTransport>(() => ({
    send: (message, conversationId) => guestRequest((current) => server.sendStoreChatMessage(
      current.customerId, current.store.id, message, conversationId,
    )),
    loadTurns: (conversationId, beforeSeq, limit) => guestRequest((current) => server.getConversationTurns(
      current.customerId, conversationId, beforeSeq, limit,
    )),
  }), [guestRequest, server]);

  const value = useMemo<ShopWorkspaceValue>(() => ({ ...list, session, transport }), [list, session, transport]);

  return <ShopWorkspaceContext.Provider value={value}>{children}</ShopWorkspaceContext.Provider>;
}
