'use client';
import { useState } from 'react';
import { ArrowUpIcon, MicIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { InputGroup, InputGroupAddon, InputGroupTextarea } from '@/components/ui/input-group';
import { cn } from '@/lib/utils';
import { MESSAGE_MAX_LENGTH } from './useChat';

interface ChatComposerProps {
  onSend: (text: string) => void;
  onVoice?: () => void;
  disabled?: boolean;
  placeholder: string;
  variant: 'admin' | 'shop';
  autoFocus?: boolean;
  // The big box on a new chat's screen.
  hero?: boolean;
  dataComponent: string;
  className?: string;
}

// The message box. Enter sends; Shift+Enter starts a new line (admin). The
// mic opens voice mode.
export function ChatComposer({
  onSend, onVoice, disabled = false, placeholder, variant, autoFocus = false, hero = false, dataComponent, className,
}: ChatComposerProps) {
  const [text, setText] = useState('');
  const canSend = text.trim().length > 0 && !disabled;

  const send = () => {
    if (!canSend) return;
    onSend(text.trim());
    setText('');
  };

  if (variant === 'shop') {
    return (
      <form
        data-component={dataComponent}
        className={cn('flex items-center gap-2', className)}
        onSubmit={(event) => { event.preventDefault(); send(); }}
      >
        <label htmlFor={`${dataComponent}Input`} className="sr-only">Message</label>
        <Input
          id={`${dataComponent}Input`}
          data-component={`${dataComponent}Input`}
          className="h-12 flex-1 rounded-full px-1.5 shadow-sm/5 [&_input]:h-11.5 [&_input]:text-base"
          placeholder={placeholder}
          value={text}
          maxLength={MESSAGE_MAX_LENGTH}
          autoFocus={autoFocus}
          autoComplete="off"
          onChange={(event) => setText(event.target.value)}
        />
        {canSend || !onVoice ? (
          <Button
            data-component={`${dataComponent}Send`}
            type="submit"
            size="icon-xl"
            className="rounded-full"
            disabled={!canSend}
            aria-label="Send"
          >
            <ArrowUpIcon />
          </Button>
        ) : (
          <Button
            data-component={`${dataComponent}Voice`}
            type="button"
            size="icon-xl"
            className="rounded-full"
            onClick={onVoice}
            aria-label="Talk instead"
          >
            <MicIcon />
          </Button>
        )}
      </form>
    );
  }

  return (
    <form
      data-component={dataComponent}
      className={className}
      onSubmit={(event) => { event.preventDefault(); send(); }}
    >
      <InputGroup className="rounded-2xl bg-background shadow-sm/5 has-[textarea:focus-visible]:ring-[3px]">
        <label htmlFor={`${dataComponent}Input`} className="sr-only">Message</label>
        <InputGroupTextarea
          id={`${dataComponent}Input`}
          data-component={`${dataComponent}Input`}
          className={cn('text-base', hero ? '**:[textarea]:min-h-24' : '**:[textarea]:min-h-12')}
          placeholder={placeholder}
          value={text}
          maxLength={MESSAGE_MAX_LENGTH}
          autoFocus={autoFocus}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              send();
            }
          }}
        />
        <InputGroupAddon align="block-end" className="justify-end gap-1.5">
          {onVoice && (
            <Button
              data-component={`${dataComponent}Voice`}
              type="button"
              variant="outline"
              size="icon-sm"
              onClick={onVoice}
              aria-label="Talk instead"
            >
              <MicIcon />
            </Button>
          )}
          <Button
            data-component={`${dataComponent}Send`}
            type="submit"
            size="icon-sm"
            disabled={!canSend}
            aria-label="Send"
          >
            <ArrowUpIcon />
          </Button>
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
