'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { ChatReplyResponse } from '@/app/schemas/ChatReplyResponseSchema';
import type { ConversationTurnsResponse } from '@/app/schemas/ConversationTurnsResponseSchema';
import type { ChatComponent, ChatFormAnswer } from './cards/types';

// The most turns one page of history loads (getConversationTurns takes 1-50).
export const TURNS_PAGE = 20;
// The longest message the chat routes take (server CHAT_MESSAGE_MAX_LENGTH).
export const MESSAGE_MAX_LENGTH = 2000;

export type ChatTurnStatus = 'sending' | 'done' | 'failed';

export interface ChatTurnView {
  key: string;
  seq: number | null;
  // The message, or a form answer's summary (the server's, once saved).
  userText: string;
  reply: string | null;
  components: ChatComponent[];
  // The form this turn answered.
  answeredFormId: string | null;
  createdAt: string | null;
  status: ChatTurnStatus;
  error: string | null;
  // What a failed turn sends again.
  form: ChatFormAnswer | null;
}

// How a chat reaches the server: the admin's routes or a shopper's.
export interface ChatTransport {
  send: (message: string | null, conversationId: string | null, form: ChatFormAnswer | null) => Promise<ApiResponse<ChatReplyResponse>>;
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
  // Every turn the server saved, with its components.
  onSaved?: (turn: ChatTurnView) => void;
}

// What a form answer says until the server's summary arrives.
function answerLabel(form: ChatFormAnswer): string {
  switch (form.kind) {
    case 'location': return 'Here is the store’s pin.';
    case 'newProducts': return `Add ${form.products.length === 1 ? 'this product' : `these ${form.products.length} products`}.`;
    case 'phoneVerified': return 'I verified my phone number.';
    case 'photoRequest': return `Make ${form.count} product ${form.count === 1 ? 'photo' : 'photos'}.`;
    case 'orderItems': return 'I’d like to order these.';
    case 'payment': return 'I’ll pay now.';
    case 'handover': return `Mark ${form.orderIds.length === 1 ? 'this order' : `these ${form.orderIds.length} orders`} handed over.`;
    case 'returnDecision': return form.decision === 'approve' ? 'Approve this return.' : 'Reject this return.';
    case 'deliveryPin': return 'Deliver to my pin.';
    default: return 'Here is my answer.';
  }
}

export function useChat({ transport, conversationId, initialTurns, onStarted, onSaved }: UseChatOptions) {
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
        components: row.components,
        answeredFormId: row.answeredFormId,
        createdAt: row.createdAt,
        status: 'done' as const,
        error: null,
        form: null,
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

  const replaceTurn = useCallback((key: string, change: (turn: ChatTurnView) => ChatTurnView) => {
    const next = turnsRef.current.map((turn) => (turn.key === key ? change(turn) : turn));
    turnsRef.current = next;
    setTurns(next);
    return next;
  }, []);

  // Sends one turn (a message, or a form's answer) and resolves to the
  // assistant's reply (null when it failed: the turn then shows the error
  // and a way to try again).
  const send = useCallback(async (message: string | null, form: ChatFormAnswer | null): Promise<string | null> => {
    const key = `local-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const pending: ChatTurnView = {
      key,
      seq: null,
      userText: form ? answerLabel(form) : message ?? '',
      reply: null,
      components: [],
      answeredFormId: form?.formId ?? null,
      createdAt: null,
      status: 'sending',
      error: null,
      form,
    };
    const withPending = [...turnsRef.current, pending];
    turnsRef.current = withPending;
    setTurns(withPending);

    const isNew = idRef.current === null;
    const res = await transport.send(message, idRef.current, form);
    if (res.status !== 200) {
      replaceTurn(key, (turn) => ({ ...turn, status: 'failed', answeredFormId: null, error: res.message }));
      return null;
    }
    idRef.current = res.data.conversationId;
    const done = replaceTurn(key, (turn) => ({
      ...turn,
      seq: res.data.turnSeq,
      userText: res.data.userText,
      reply: res.data.reply,
      components: res.data.components,
      status: 'done',
      form: null,
      createdAt: new Date().toISOString(),
    }));
    const saved = done.find((turn) => turn.key === key);
    if (saved) onSaved?.(saved);
    if (isNew) onStarted?.(res.data.conversationId, done);
    return res.data.reply;
  }, [transport, onStarted, onSaved, replaceTurn]);

  const ask = useCallback(async (text: string): Promise<string | null> => {
    const message = text.trim();
    if (!message) return null;
    return send(message, null);
  }, [send]);

  const answer = useCallback((form: ChatFormAnswer) => send(null, form), [send]);

  const retry = useCallback(async (key: string) => {
    const failed = turnsRef.current.find((turn) => turn.key === key);
    if (!failed) return;
    const rest = turnsRef.current.filter((turn) => turn.key !== key);
    turnsRef.current = rest;
    setTurns(rest);
    await send(failed.form ? null : failed.userText, failed.form);
  }, [send]);

  const sending = turns.some((turn) => turn.status === 'sending');
  const answeredFormIds = useMemo(
    () => new Set(turns.flatMap((turn) => (turn.answeredFormId && turn.status !== 'failed' ? [turn.answeredFormId] : []))),
    [turns],
  );

  return { turns, loading, loadError, hasEarlier, loadingEarlier, sending, answeredFormIds, ask, answer, retry, reload, loadEarlier };
}
