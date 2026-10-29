'use client';
import { useCallback } from 'react';
import { useParams } from 'next/navigation';
import { ChatThread } from '@/components/chat/ChatThread';
import { useChatShell } from '@/components/chat/ChatShell';
import { useShellChat } from '@/components/chat/useShellChat';
import { useShopWorkspace } from '../../ShopWorkspace';
import { SHOP_FOOTNOTE, shopComposerPlaceholder } from '../../shopChatText';

export default function ShopConversationPage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  // A new id is a new chat: nothing of the last one carries over.
  return <ShopConversation key={conversationId} conversationId={conversationId} />;
}

function ShopConversation({ conversationId }: { conversationId: string }) {
  const { session, transport } = useShopWorkspace();
  const { store } = session;
  const { openVoice } = useChatShell();
  const threadHref = useCallback((id: string) => `/s/${store.slug}/c/${id}`, [store.slug]);
  const chat = useShellChat({ transport, conversationId, threadHref });

  return (
    <ChatThread
      chat={chat}
      variant="shop"
      dataComponent="shopChat"
      placeholder={shopComposerPlaceholder(store.name)}
      onVoice={openVoice}
      footnote={SHOP_FOOTNOTE}
    />
  );
}
