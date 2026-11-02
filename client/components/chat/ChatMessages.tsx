'use client';
import { useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { FileTextIcon, ImageIcon, RotateCwIcon } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Dialog, DialogHeader, DialogPanel, DialogPopup, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
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
  // The download URL of one of the user's own chat files, by its path.
  fileUrl: (path: string) => Promise<string>;
}

// One chat's turns: the user's message on the right, the assistant's reply
// as plain text below it, like a conversation with Claude, with the cards the
// reply brought above its text and the forms that answer it below.
export function ChatMessages({
  turns, variant, hasEarlier, loadingEarlier, onLoadEarlier, onRetry, answeredFormIds, busy, onAnswer, fileUrl,
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
            {turn.files.length > 0 && <TurnFiles files={turn.files} fileUrl={fileUrl} />}
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

// The files a turn's message carried: a photo as itself, a PDF by name.
function TurnFiles({ files, fileUrl }: { files: TurnFile[]; fileUrl: (path: string) => Promise<string> }) {
  return (
    <div data-component="chatTurnFiles" className="flex max-w-[85%] flex-wrap justify-end gap-2 self-end">
      {files.map((file, index) => (
        file.contentType === 'application/pdf'
          ? <FileChip key={`${file.name}-${index}`} file={file} />
          : <TurnPhoto key={`${file.path ?? file.url ?? file.name}-${index}`} file={file} fileUrl={fileUrl} />
      ))}
    </div>
  );
}

function FileChip({ file }: { file: TurnFile }) {
  const Icon = file.contentType === 'application/pdf' ? FileTextIcon : ImageIcon;
  return (
    <div data-component="chatTurnFileChip" className="flex max-w-56 items-center gap-2 rounded-xl border bg-card px-3 py-2 text-sm">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="truncate">{file.name}</span>
    </div>
  );
}

const THUMBNAIL_CLASS = 'size-40 rounded-xl sm:size-48';

// A photo the message carried: a thumbnail that opens the full photo. Its
// URL is read from Storage when it has none yet; a photo that can't be read
// (refused, deleted, offline) shows as its name.
function TurnPhoto({ file, fileUrl }: { file: TurnFile; fileUrl: (path: string) => Promise<string> }) {
  const [resolved, setResolved] = useState<{ path: string; url: string | null } | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const path = file.url === null ? file.path : null;

  useEffect(() => {
    if (path === null) return;
    let live = true;
    fileUrl(path).then(
      (url) => { if (live) setResolved({ path, url }); },
      () => { if (live) setResolved({ path, url: null }); },
    );
    return () => { live = false; };
  }, [fileUrl, path]);

  const url = file.url ?? (resolved !== null && resolved.path === path ? resolved.url : undefined);
  if (failed || url === null || (url === undefined && path === null)) return <FileChip file={file} />;
  if (url === undefined) {
    return <Skeleton data-component="chatTurnFilePhotoLoading" className={THUMBNAIL_CLASS} aria-label={`Loading ${file.name}`} />;
  }

  return (
    <>
      <button
        type="button"
        data-component="chatTurnFilePhoto"
        className={cn('relative overflow-hidden border bg-muted focus-visible:outline-2 focus-visible:outline-ring', THUMBNAIL_CLASS)}
        onClick={() => setOpen(true)}
        aria-label={`Open ${file.name}`}
        title={file.name}
      >
        <Image
          src={url}
          alt={file.name}
          fill
          sizes="192px"
          className="object-cover"
          onError={() => setFailed(true)}
        />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogPopup data-component="chatTurnFileDialog">
          <DialogHeader>
            <DialogTitle className="truncate">{file.name}</DialogTitle>
          </DialogHeader>
          <DialogPanel>
            <Image
              src={url}
              alt={file.name}
              width={1600}
              height={1600}
              sizes="(max-width: 640px) 100vw, 600px"
              className="h-auto max-h-[70vh] w-full object-contain"
            />
          </DialogPanel>
        </DialogPopup>
      </Dialog>
    </>
  );
}
