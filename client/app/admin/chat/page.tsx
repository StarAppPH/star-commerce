'use client';
import { useCallback } from 'react';
import { RotateCwIcon, StarIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Spinner } from '@/components/ui/spinner';
import { ChatComposer } from '@/components/chat/ChatComposer';
import { ChatThread } from '@/components/chat/ChatThread';
import { useChatShell } from '@/components/chat/ChatShell';
import { useShellChat } from '@/components/chat/useShellChat';
import { useAuth } from '@/lib/AuthContext';
import { storeGreeting } from '@/lib/storeTime';
import { useAdminWorkspace } from './AdminWorkspace';
import { useWaitingOnYou, type WaitingItem } from './useWaitingOnYou';
import { ADMIN_COMPOSER_PLACEHOLDER, ADMIN_FOOTNOTE } from './adminChatText';

// What "Set up my store" asks.
const SET_UP_PROMPT = 'I want to set up my store.';
// Prompts for an admin who doesn't know where to start.
const TRY_PROMPTS = [
  { label: 'Add products', prompt: 'I want to add some products to my store.' },
  { label: 'How did this week go?', prompt: 'How did this week go? Sum up my orders for the last 7 days.' },
  { label: 'What’s running low?', prompt: 'Which of my products are running low on stock?' },
  { label: 'Make product photos', prompt: 'Help me make product photos from my own pictures.' },
];

export default function AdminChatHomePage() {
  const { storeState, reloadStore } = useAdminWorkspace();

  if (storeState.status === 'loading') {
    return (
      <div className="flex flex-1 items-center justify-center" aria-label="Loading your store">
        <Spinner />
      </div>
    );
  }
  if (storeState.status === 'error') {
    return (
      <Empty className="flex-1">
        <EmptyHeader>
          <EmptyTitle>We couldn’t load your store</EmptyTitle>
          <EmptyDescription>{storeState.message}</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button data-component="adminStoreReload" variant="outline" onClick={reloadStore}>
            <RotateCwIcon />
            Try again
          </Button>
        </EmptyContent>
      </Empty>
    );
  }
  return <AdminNewChat setup={storeState.store === null} />;
}

// The new-chat screen. Before the store is set up, the assistant only sets
// it up (it opens the store details form): the screen says so.
function AdminNewChat({ setup }: { setup: boolean }) {
  const { userData } = useAuth();
  const { transport, userId } = useAdminWorkspace();
  const { openVoice } = useChatShell();
  const threadHref = useCallback((id: string) => `/admin/chat/${id}`, []);
  const chat = useShellChat({ transport, conversationId: null, threadHref });
  const waiting = useWaitingOnYou(userId, !setup);

  if (chat.turns.length > 0) {
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

  const firstName = userData?.name?.trim().split(/\s+/)[0];

  return (
    <div data-component="adminHome" className="flex flex-1 items-center justify-center overflow-y-auto px-4 py-10 sm:px-6">
      <div className="flex w-full max-w-2xl flex-col gap-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <span aria-hidden="true" className="inline-flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <StarIcon className="size-5" />
          </span>
          <h2 className="font-heading text-3xl leading-tight sm:text-4xl">
            {setup ? 'Welcome' : storeGreeting()}{firstName ? `, ${firstName}` : ''}
          </h2>
          <p className="text-[15px] text-muted-foreground">
            {setup
              ? 'Let’s set up your store. Tell me about it, and I’ll bring up the form to fill in.'
              : 'Run your store by chatting. I’ll bring up whatever you need.'}
          </p>
        </div>

        <ChatComposer
          dataComponent="adminHomeComposer"
          variant="admin"
          hero
          autoFocus
          placeholder={setup ? 'Tell me about your store' : 'Ask about orders, or tell me what to change in your store'}
          onSend={chat.sendMessage}
          onUpload={chat.upload}
          onVoice={openVoice}
        />

        {setup ? (
          <div className="flex justify-center">
            <Button data-component="adminHomeSetUp" className="rounded-full" onClick={() => chat.ask(SET_UP_PROMPT)}>
              Set up my store
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2.5">
            {waiting.length > 0 && (
              <>
                <span className="text-center font-medium text-muted-foreground text-xs">Waiting on you</span>
                <div className="flex flex-wrap justify-center gap-2">
                  {waiting.map((item) => (
                    <WaitingChip key={item.key} item={item} onAsk={chat.ask} />
                  ))}
                </div>
              </>
            )}
            <span className="mt-1.5 text-center font-medium text-muted-foreground text-xs">
              {waiting.length > 0 ? 'Or try' : 'Try'}
            </span>
            <div className="flex flex-wrap justify-center gap-2">
              {TRY_PROMPTS.map((item) => (
                <Button
                  key={item.label}
                  data-component="adminHomeTry"
                  variant="secondary"
                  className="rounded-full"
                  onClick={() => chat.ask(item.prompt)}
                >
                  {item.label}
                </Button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function WaitingChip({ item, onAsk }: { item: WaitingItem; onAsk: (text: string) => void }) {
  return (
    <Button
      data-component={item.dataComponent}
      variant="outline"
      className="rounded-full"
      onClick={() => onAsk(item.prompt)}
    >
      <span
        className={
          item.tone === 'info'
            ? 'inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-md bg-info/8 px-1 font-semibold text-info-foreground text-xs'
            : 'inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-md bg-warning/8 px-1 font-semibold text-warning-foreground text-xs'
        }
      >
        {item.count}
      </span>
      {item.label}
    </Button>
  );
}
