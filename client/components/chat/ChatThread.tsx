'use client';
import { RotateCwIcon } from 'lucide-react';
import { Alert, AlertAction, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ChatComposer } from './ChatComposer';
import { ChatMessages } from './ChatMessages';
import type { ShellChat } from './useShellChat';

interface ChatThreadProps {
  chat: ShellChat;
  variant: 'admin' | 'shop';
  placeholder: string;
  dataComponent: string;
  onVoice: () => void;
  // A line under the composer.
  footnote?: string;
}

// One open chat: its turns scroll above a composer that stays at the bottom.
export function ChatThread({ chat, variant, placeholder, dataComponent, onVoice, footnote }: ChatThreadProps) {
  return (
    <div data-component={dataComponent} className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
          {chat.loading ? (
            <ThreadSkeleton />
          ) : chat.loadError ? (
            <Alert variant="error">
              <AlertDescription>{chat.loadError}</AlertDescription>
              <AlertAction>
                <Button data-component={`${dataComponent}Reload`} size="xs" variant="outline" onClick={chat.reload}>
                  <RotateCwIcon />
                  Try again
                </Button>
              </AlertAction>
            </Alert>
          ) : (
            <ChatMessages
              turns={chat.turns}
              variant={variant}
              hasEarlier={chat.hasEarlier}
              loadingEarlier={chat.loadingEarlier}
              onLoadEarlier={chat.loadEarlier}
              onRetry={chat.retry}
              answeredFormIds={chat.answeredFormIds}
              busy={chat.sending}
              onAnswer={chat.answer}
            />
          )}
        </div>
      </div>
      <div className="mx-auto w-full max-w-3xl px-3 pt-2 pb-[max(env(safe-area-inset-bottom),--spacing(3))] sm:px-6">
        <ChatComposer
          dataComponent={`${dataComponent}Composer`}
          variant={variant}
          placeholder={placeholder}
          disabled={chat.sending || chat.loading}
          onSend={chat.ask}
          onVoice={onVoice}
          autoFocus
        />
        {footnote && <p className="mt-2 text-center text-muted-foreground text-xs">{footnote}</p>}
      </div>
    </div>
  );
}

function ThreadSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading the chat">
      <Skeleton className="h-10 w-2/5 self-end rounded-2xl" />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-11/12" />
        <Skeleton className="h-4 w-4/5" />
        <Skeleton className="h-4 w-3/5" />
      </div>
      <Skeleton className="h-10 w-1/3 self-end rounded-2xl" />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-10/12" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    </div>
  );
}
