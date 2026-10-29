'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useChatShell } from './ChatShell';
import { useChat, type ChatTransport, type ChatTurnView } from './useChat';

interface UseShellChatOptions {
  transport: ChatTransport;
  // null: the new-chat screen.
  conversationId: string | null;
  // Where a chat lives once it has an id.
  threadHref: (conversationId: string) => string;
}

// useChat inside a ChatShell: a new chat moves to its thread page after its
// first reply, voice mode talks to the chat on screen, and every saved turn
// is reported to the shell (the chat list reloads).
export function useShellChat({ transport, conversationId, threadHref }: UseShellChatOptions) {
  const shell = useChatShell();
  const router = useRouter();
  const [initialTurns] = useState(() => (conversationId ? shell.peek(conversationId) : undefined));
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    if (conversationId) shell.forget(conversationId);
    return () => { mounted.current = false; };
  }, [conversationId, shell]);

  const onStarted = useCallback((id: string, turns: ChatTurnView[]) => {
    // Someone who opened another chat while waiting stays where they went.
    if (!mounted.current) return;
    shell.stash(id, turns);
    router.replace(threadHref(id));
  }, [shell, router, threadHref]);

  const onSaved = useCallback((turn: ChatTurnView) => shell.turnSaved(turn.components), [shell]);
  const chat = useChat({ transport, conversationId, initialTurns, onStarted, onSaved });
  const { ask } = chat;

  useEffect(() => {
    shell.bindAsk(ask);
    return () => shell.unbindAsk(ask);
  }, [ask, shell]);

  return chat;
}

export type ShellChat = ReturnType<typeof useShellChat>;
