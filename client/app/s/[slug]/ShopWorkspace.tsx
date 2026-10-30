'use client';
import React, { createContext, useCallback, useContext, useMemo } from 'react';
import { useGuestSession, type GuestSession } from '@/lib/GuestSessionContext';
import { useServer } from '@/lib/ServerContext';
import { guestAuth, guestStorage } from '@/lib/guestFirebase';
import type { ChatTransport } from '@/components/chat/useChat';
import { uploadChatFiles } from '@/components/chat/attachments';
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
    send: (message, conversationId, form, attachments, turnKey, speak) => guestRequest((current) => server.sendStoreChatMessage(
      current.customerId, current.store.id, message, conversationId, form, attachments, turnKey, speak,
    )),
    // The shopper's anonymous account is their customer id: the server takes
    // files from users/<customerId>/ only.
    upload: (files) => {
      const user = guestAuth.currentUser;
      if (!user) return Promise.reject(new Error('No guest session'));
      return uploadChatFiles(guestStorage, user.uid, files);
    },
    loadTurns: (conversationId, beforeSeq, limit) => guestRequest((current) => server.getConversationTurns(
      current.customerId, conversationId, beforeSeq, limit,
    )),
    thinking: (turnKey) => guestRequest((current) => server.getChatThinking(current.customerId, turnKey)),
    speakThinking: (turnKey) => guestRequest((current) => server.speakChatThinking(current.customerId, turnKey)),
  }), [guestRequest, server]);

  const value = useMemo<ShopWorkspaceValue>(() => ({ ...list, session, transport }), [list, session, transport]);

  return <ShopWorkspaceContext.Provider value={value}>{children}</ShopWorkspaceContext.Provider>;
}
