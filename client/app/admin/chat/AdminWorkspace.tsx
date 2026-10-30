'use client';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { auth, storage } from '@/lib/firebase';
import { useServer } from '@/lib/ServerContext';
import type { ChatTransport } from '@/components/chat/useChat';
import { uploadChatFiles } from '@/components/chat/attachments';
import { CONVERSATIONS_PAGE, useConversationList, type ConversationList } from '@/components/chat/useConversationList';
import type { AdminStoreResponse } from '@/app/schemas/MyStoreResponseSchema';

type StoreState =
  | { status: 'loading' }
  | { status: 'ready'; store: AdminStoreResponse | null }
  | { status: 'error'; message: string };

interface AdminWorkspaceValue extends ConversationList {
  userId: string;
  storeState: StoreState;
  reloadStore: () => Promise<void>;
  transport: ChatTransport;
}

const AdminWorkspaceContext = createContext<AdminWorkspaceValue | null>(null);

export function useAdminWorkspace(): AdminWorkspaceValue {
  const context = useContext(AdminWorkspaceContext);
  if (!context) throw new Error('useAdminWorkspace must be used within an AdminWorkspaceProvider');
  return context;
}

// The signed-in admin's store and chats, for every admin chat page.
export function AdminWorkspaceProvider({ userId, children }: { userId: string; children: React.ReactNode }) {
  const { server } = useServer();
  const { adminRequest } = useAuth();
  const [storeState, setStoreState] = useState<StoreState>({ status: 'loading' });

  const reloadStore = useCallback(async () => {
    const res = await adminRequest(() => server.getUserStore(userId));
    if (res.status === 200) {
      setStoreState({ status: 'ready', store: res.data.store });
    } else {
      setStoreState({ status: 'error', message: res.message });
    }
  }, [adminRequest, server, userId]);

  useEffect(() => {
    (async () => { await reloadStore(); })();
  }, [reloadStore]);

  // The chats load once the store is known: a store not set up yet has its
  // setup chats too.
  const storeKnown = storeState.status === 'ready';
  const fetchPage = useCallback((before: string | null) => adminRequest(
    () => server.listMyConversations(userId, null, before, CONVERSATIONS_PAGE),
  ), [adminRequest, server, userId]);
  const list = useConversationList(fetchPage, storeKnown);

  const transport = useMemo<ChatTransport>(() => ({
    send: (message, conversationId, form, attachments, turnKey, speak) => adminRequest(() => server.sendAdminChatMessage(userId, message, conversationId, form, attachments, turnKey, speak)),
    upload: (files) => {
      const user = auth.currentUser;
      if (!user) return Promise.reject(new Error('Signed out'));
      return uploadChatFiles(storage, user.uid, files);
    },
    loadTurns: (conversationId, beforeSeq, limit) => adminRequest(() => server.getConversationTurns(userId, conversationId, beforeSeq, limit)),
    thinking: (turnKey) => adminRequest(() => server.getChatThinking(userId, turnKey)),
    speakThinking: (turnKey) => adminRequest(() => server.speakChatThinking(userId, turnKey)),
  }), [adminRequest, server, userId]);

  const value = useMemo<AdminWorkspaceValue>(() => ({
    ...list,
    userId,
    storeState,
    reloadStore,
    transport,
  }), [list, userId, storeState, reloadStore, transport]);

  return <AdminWorkspaceContext.Provider value={value}>{children}</AdminWorkspaceContext.Provider>;
}
