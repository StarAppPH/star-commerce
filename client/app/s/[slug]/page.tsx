'use client';
import { useCallback } from 'react';
import { ChatComposer } from '@/components/chat/ChatComposer';
import { ChatThread } from '@/components/chat/ChatThread';
import { useChatShell } from '@/components/chat/ChatShell';
import { useShellChat } from '@/components/chat/useShellChat';
import { StoreMark } from '@/components/brand/StoreMark';
import { useShopWorkspace } from './ShopWorkspace';
import { SHOP_FOOTNOTE, shopComposerPlaceholder } from './shopChatText';

// Where a shopper usually starts.
const SUGGESTIONS = [
  { key: 'Best', title: 'Best sellers', hint: 'What everyone’s buying', prompt: 'What are your best sellers?' },
  { key: 'Choose', title: 'Help me choose', hint: 'Tell me what you need', prompt: 'Can you help me choose something?' },
  { key: 'Track', title: 'My order', hint: 'Where is it?', prompt: 'Where is my order?' },
  { key: 'Return', title: 'Return an item', hint: 'Changed my mind, or it’s broken', prompt: 'I want to return an item.' },
];

export default function ShopHomePage() {
  const { session, transport } = useShopWorkspace();
  const { store } = session;
  const { openVoice } = useChatShell();
  const threadHref = useCallback((id: string) => `/s/${store.slug}/c/${id}`, [store.slug]);
  const chat = useShellChat({ transport, conversationId: null, threadHref });
  const placeholder = shopComposerPlaceholder(store.name);

  if (chat.turns.length > 0) {
    return (
      <ChatThread
        chat={chat}
        variant="shop"
        dataComponent="shopChat"
        placeholder={placeholder}
        onVoice={openVoice}
        footnote={SHOP_FOOTNOTE}
      />
    );
  }

  const about = [store.description, store.openingHours].filter(Boolean).join(' ');

  return (
    <div data-component="shopHome" className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col justify-center gap-7 overflow-y-auto px-5 pb-6">
        <div className="flex flex-col items-center gap-2.5 text-center">
          <StoreMark name={store.name} size="lg" />
          <h1 className="font-heading text-[28px] leading-tight">{store.name}</h1>
          {about && <p className="max-w-sm text-[15px] text-muted-foreground leading-[22px]">{about}</p>}
          {store.address && <p className="max-w-sm text-muted-foreground text-sm">{store.address}</p>}
        </div>
        <div className="mx-auto flex w-full max-w-md flex-col gap-2.5">
          <span className="text-center text-muted-foreground text-sm">Hi! What can I get you today?</span>
          <div className="grid grid-cols-2 gap-2">
            {SUGGESTIONS.map((item) => (
              <button
                key={item.key}
                type="button"
                data-component={`shopSuggest${item.key}`}
                className="flex min-h-19 flex-col items-start gap-1 rounded-[14px] border bg-card p-3 text-left shadow-xs/5 outline-none transition-colors hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => chat.ask(item.prompt)}
              >
                <span className="font-medium text-[15px]">{item.title}</span>
                <span className="text-muted-foreground text-[13px]">{item.hint}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="mx-auto w-full max-w-3xl px-3 pt-2 pb-[max(env(safe-area-inset-bottom),--spacing(6))]">
        <ChatComposer
          dataComponent="shopHomeComposer"
          variant="shop"
          placeholder={placeholder}
          onSend={chat.sendMessage}
          onUpload={chat.upload}
          onVoice={openVoice}
        />
      </div>
    </div>
  );
}
