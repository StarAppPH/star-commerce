'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { ChatReplyResponse } from '@/app/schemas/ChatReplyResponseSchema';
import type { ConversationTurnsResponse } from '@/app/schemas/ConversationTurnsResponseSchema';

// The most turns one page of history loads (getConversationTurns takes 1-50).
export const TURNS_PAGE = 20;
// The longest message the chat routes take (server CHAT_MESSAGE_MAX_LENGTH).
export const MESSAGE_MAX_LENGTH = 2000;

export type ChatTurnStatus = 'sending' | 'done' | 'failed';

export interface ChatTurnView {
  key: string;
  seq: number | null;
  userText: string;
  reply: string | null;
  createdAt: string | null;
  status: ChatTurnStatus;
  error: string | null;
}

// How a chat reaches the server: the admin's routes or a shopper's.
export interface ChatTransport {
  send: (message: string, conversationId: string | null) => Promise<ApiResponse<ChatReplyResponse>>;
  loadTurns: (conversationId: string, beforeSeq: number | null, limit: number) => Promise<ApiResponse<ConversationTurnsResponse>>;
}

interface UseChatOptions {
  transport: ChatTransport;
  conversationId: string | null;
  // Turns already on screen (a chat that just started on the new-chat
  // screen): shown as they are, without loading the history again.
  initialTurns?: ChatTurnView[];
  // The first reply of a new chat: its id and its turns so far.
  onStarted?: (conversationId: string, turns: ChatTurnView[]) => void;
}

export function useChat({ transport, conversationId, initialTurns, onStarted }: UseChatOptions) {
  const [turns, setTurns] = useState<ChatTurnView[]>(initialTurns ?? []);
  const [loading, setLoading] = useState(conversationId !== null && !initialTurns);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const idRef = useRef<string | null>(conversationId);
  const turnsRef = useRef<ChatTurnView[]>(turns);
  useEffect(() => { turnsRef.current = turns; }, [turns]);

  const loadPage = useCallback(async (beforeSeq: number | null) => {
    const id = idRef.current;
    if (!id) return;
    const res = await transport.loadTurns(id, beforeSeq, TURNS_PAGE);
    if (res.status !== 200) {
      setLoadError(res.message);
      return;
    }
    setLoadError(null);
    const page: ChatTurnView[] = res.data.turns
      .map((row) => ({
        key: `seq-${row.seq}`,
        seq: row.seq,
        userText: row.userText,
        reply: row.replyText,
        createdAt: row.createdAt,
        status: 'done' as const,
        error: null,
      }))
      .reverse();
    setHasEarlier(res.data.turns.length === TURNS_PAGE);
    setTurns((current) => (beforeSeq === null ? page : [...page, ...current]));
  }, [transport]);

  useEffect(() => {
    if (conversationId === null || initialTurns) return;
    let cancelled = false;
    (async () => {
      await loadPage(null);
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
    // A new conversationId mounts a new chat (a key on the page): load once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The first page again, after it failed to load.
  const reload = useCallback(async () => {
    setLoading(true);
    await loadPage(null);
    setLoading(false);
  }, [loadPage]);

  const loadEarlier = useCallback(async () => {
    const oldest = turnsRef.current.find((turn) => turn.seq !== null);
    if (!oldest || oldest.seq === null) return;
    setLoadingEarlier(true);
    await loadPage(oldest.seq);
    setLoadingEarlier(false);
  }, [loadPage]);

  // Sends one message and resolves to the assistant's reply (null when it
  // failed: the turn then shows the error and a way to try again).
  const ask = useCallback(async (text: string): Promise<string | null> => {
    const message = text.trim();
    if (!message) return null;
    const key = `local-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const pending: ChatTurnView = {
      key, seq: null, userText: message, reply: null, createdAt: null, status: 'sending', error: null,
    };
    const withPending = [...turnsRef.current, pending];
    turnsRef.current = withPending;
    setTurns(withPending);

    const isNew = idRef.current === null;
    const res = await transport.send(message, idRef.current);
    if (res.status !== 200) {
      const failed = turnsRef.current.map((turn) => (
        turn.key === key ? { ...turn, status: 'failed' as const, error: res.message } : turn
      ));
      turnsRef.current = failed;
      setTurns(failed);
      return null;
    }
    idRef.current = res.data.conversationId;
    const done = turnsRef.current.map((turn) => (
      turn.key === key
        ? { ...turn, seq: res.data.turnSeq, reply: res.data.reply, status: 'done' as const, createdAt: new Date().toISOString() }
        : turn
    ));
    turnsRef.current = done;
    setTurns(done);
    if (isNew) onStarted?.(res.data.conversationId, done);
    return res.data.reply;
  }, [transport, onStarted]);

  const retry = useCallback(async (key: string) => {
    const failed = turnsRef.current.find((turn) => turn.key === key);
    if (!failed) return;
    const rest = turnsRef.current.filter((turn) => turn.key !== key);
    turnsRef.current = rest;
    setTurns(rest);
    await ask(failed.userText);
  }, [ask]);

  const sending = turns.some((turn) => turn.status === 'sending');

  return { turns, loading, loadError, hasEarlier, loadingEarlier, sending, ask, retry, reload, loadEarlier };
}
