'use client';
import { useCallback } from 'react';
import { useParams } from 'next/navigation';
import { ChatThread } from '@/components/chat/ChatThread';
import { useChatShell } from '@/components/chat/ChatShell';
import { useShellChat } from '@/components/chat/useShellChat';
import { useAdminWorkspace } from '../AdminWorkspace';
import { ADMIN_COMPOSER_PLACEHOLDER, ADMIN_FOOTNOTE } from '../adminChatText';

export default function AdminConversationPage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  // A new id is a new chat: nothing of the last one carries over.
  return <AdminConversation key={conversationId} conversationId={conversationId} />;
}

function AdminConversation({ conversationId }: { conversationId: string }) {
  const { transport } = useAdminWorkspace();
  const { openVoice } = useChatShell();
  const threadHref = useCallback((id: string) => `/admin/chat/${id}`, []);
  const chat = useShellChat({ transport, conversationId, threadHref });

  return (
    <ChatThread
      chat={chat}
      variant="admin"
      dataComponent="adminChat"
      placeholder={ADMIN_COMPOSER_PLACEHOLDER}
      onVoice={openVoice}
      footnote={ADMIN_FOOTNOTE}
    />
  );
}
