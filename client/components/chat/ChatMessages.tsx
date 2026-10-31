'use client';
import { useEffect, useRef } from 'react';
import { FileTextIcon, ImageIcon, RotateCwIcon } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/utils';
import { formatStoreTime } from '@/lib/storeTime';
import { AssistantText } from './AssistantText';
import { ChatComponents } from './cards/ChatComponents';
import type { ChatFormAnswer } from './cards/types';
import type { TurnFile } from './attachments';
import type { ChatTurnView } from './useChat';

interface ChatMessagesProps {
  turns: ChatTurnView[];
  variant: 'admin' | 'shop';
  hasEarlier: boolean;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
  onRetry: (key: string) => void;
  // The forms a turn of this chat answered.
  answeredFormIds: ReadonlySet<string>;
  // A turn is on its way: no form or card sends another.
  busy: boolean;
  onAnswer: (answer: ChatFormAnswer) => void;
}

// One chat's turns: the user's message on the right, the assistant's reply
// as plain text below it, like a conversation with Claude, with the cards the
// reply brought above its text and the forms that answer it below.
export function ChatMessages({
  turns, variant, hasEarlier, loadingEarlier, onLoadEarlier, onRetry, answeredFormIds, busy, onAnswer,
}: ChatMessagesProps) {
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
          <div className="flex flex-col gap-2">
            {turn.files.length > 0 && <TurnFiles files={turn.files} />}
            {turn.userText !== '' && (
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
            )}
          </div>
          {turn.status === 'sending' && (
            <div data-component="chatThinking" className="flex items-center gap-2 text-muted-foreground text-sm" aria-live="polite">
              <Spinner className="size-4" />
              {turn.thinkingText ?? 'Thinking'}
            </div>
          )}
          {turn.status === 'done' && (
            <ChatComponents
              components={turn.components}
              place="above"
              answeredFormIds={answeredFormIds}
              busy={busy}
              onAnswer={onAnswer}
            />
          )}
          {turn.status === 'done' && turn.reply !== null && <AssistantText text={turn.reply} />}
          {turn.status === 'done' && (
            <ChatComponents
              components={turn.components}
              place="below"
              answeredFormIds={answeredFormIds}
              busy={busy}
              onAnswer={onAnswer}
            />
          )}
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

// The files a turn's message carried, by name.
function TurnFiles({ files }: { files: TurnFile[] }) {
  return (
    <div data-component="chatTurnFiles" className="flex max-w-[85%] flex-wrap justify-end gap-2 self-end">
      {files.map((file, index) => {
        const Icon = file.contentType === 'application/pdf' ? FileTextIcon : ImageIcon;
        return (
          <div key={`${file.name}-${index}`} className="flex max-w-56 items-center gap-2 rounded-xl border bg-card px-3 py-2 text-sm">
            <Icon className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{file.name}</span>
          </div>
        );
      })}
    </div>
  );
}
