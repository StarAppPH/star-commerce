'use client';
import { useEffect, useRef } from 'react';
import { RotateCwIcon } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/utils';
import { formatStoreTime } from '@/lib/storeTime';
import { AssistantText } from './AssistantText';
import type { ChatTurnView } from './useChat';

interface ChatMessagesProps {
  turns: ChatTurnView[];
  variant: 'admin' | 'shop';
  hasEarlier: boolean;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
  onRetry: (key: string) => void;
}

// One chat's turns: the user's message on the right, the assistant's reply
// as plain text below it, like a conversation with Claude.
export function ChatMessages({ turns, variant, hasEarlier, loadingEarlier, onLoadEarlier, onRetry }: ChatMessagesProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const lastTurn = turns[turns.length - 1];
  const lastKey = lastTurn ? `${lastTurn.key}:${lastTurn.status}` : '';

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [lastKey]);

  return (
    <div data-component="chatMessages" className="flex flex-col gap-6">
      {hasEarlier && (
        <Button
          data-component="chatLoadEarlier"
          className="self-center"
          variant="ghost"
          size="sm"
          onClick={onLoadEarlier}
          loading={loadingEarlier}
        >
          Show earlier messages
        </Button>
      )}
      {turns.map((turn) => (
        <div key={turn.key} className="flex flex-col gap-4">
          <div
            className={cn(
              'max-w-[85%] self-end whitespace-pre-wrap break-words px-3.5 py-2.5 text-[15px] leading-[22px] sm:text-base',
              variant === 'admin'
                ? 'rounded-2xl bg-muted'
                : 'rounded-[18px] rounded-br-md bg-primary text-primary-foreground',
            )}
            title={turn.createdAt ? formatStoreTime(turn.createdAt) : undefined}
          >
            {turn.userText}
          </div>
          {turn.status === 'sending' && (
            <div className="flex items-center gap-2 text-muted-foreground text-sm" aria-live="polite">
              <Spinner className="size-4" />
              Thinking
            </div>
          )}
          {turn.status === 'done' && turn.reply !== null && <AssistantText text={turn.reply} />}
          {turn.status === 'failed' && (
            <Alert variant="error" className="max-w-xl">
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>{turn.error ?? 'That didn’t go through.'}</span>
                <Button
                  data-component="chatRetry"
                  size="sm"
                  variant="outline"
                  onClick={() => onRetry(turn.key)}
                >
                  <RotateCwIcon />
                  Try again
                </Button>
              </AlertDescription>
            </Alert>
          )}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}
