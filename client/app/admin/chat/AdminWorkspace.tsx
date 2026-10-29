'use client';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import type { ChatTransport } from '@/components/chat/useChat';
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

  // The chats need a store: the server refuses an admin who has none yet.
  const hasStore = storeState.status === 'ready' && storeState.store !== null;
  const fetchPage = useCallback((before: string | null) => adminRequest(
    () => server.listMyConversations(userId, null, before, CONVERSATIONS_PAGE),
  ), [adminRequest, server, userId]);
  const list = useConversationList(fetchPage, hasStore);

  const transport = useMemo<ChatTransport>(() => ({
    send: (message, conversationId) => adminRequest(() => server.sendAdminChatMessage(userId, message, conversationId)),
    loadTurns: (conversationId, beforeSeq, limit) => adminRequest(() => server.getConversationTurns(userId, conversationId, beforeSeq, limit)),
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
