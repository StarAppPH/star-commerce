'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { ChatReplyResponse } from '@/app/schemas/ChatReplyResponseSchema';
import type { ChatSpeech } from '@/app/schemas/ChatSpeechSchema';
import type { ChatThinkingResponse } from '@/app/schemas/ChatThinkingResponseSchema';
import type { ChatTurnStatusResponse } from '@/app/schemas/ChatTurnStatusResponseSchema';
import type { SpokenThinkingResponse } from '@/app/schemas/SpokenThinkingResponseSchema';
import type { ConversationTurnsResponse } from '@/app/schemas/ConversationTurnsResponseSchema';
import type { ChatComponent, ChatFormAnswer } from './cards/types';
import type { TurnFile, UploadedFiles } from './attachments';
import { PHOTOS_PER_CALL } from '@/lib/generated/serverConstants';

// The most turns one page of history loads (getConversationTurns takes 1-50).
export const TURNS_PAGE = 20;
// The longest message the chat routes take (server CHAT_MESSAGE_MAX_LENGTH).
export const MESSAGE_MAX_LENGTH = 2000;
// How often a turn being answered asks what the assistant is doing.
const THINKING_POLL_MS = 1000;
// How often a turn whose reply was lost asks if the server is still answering
// it, and when it stops asking (the server treats a turn running past 110 s as
// failed).
const TURN_STATUS_POLL_MS = 2000;
const TURN_STATUS_GIVE_UP_MS = 120000;

export type ChatTurnStatus = 'sending' | 'done' | 'failed';

export interface ChatTurnView {
  key: string;
  seq: number | null;
  // The message, or a form answer's summary (the server's, once saved).
  userText: string;
  // The files sent with it.
  files: TurnFile[];
  reply: string | null;
  components: ChatComponent[];
  // The form this turn answered.
  answeredFormId: string | null;
  createdAt: string | null;
  status: ChatTurnStatus;
  error: string | null;
  // What a failed turn sends again.
  form: ChatFormAnswer | null;
  uploaded: UploadedFiles | null;
  // What the assistant is doing while it answers (the running tool's
  // thinking text); null before its first tool.
  thinkingText: string | null;
  // The key this turn was sent with (a failed one is sent again with it, so
  // the server can tell it is the same turn); null for a loaded turn.
  turnKey: string | null;
}

// The photo choice cards whose photo was put on the product by a later
// turn (the owner said "use the 2nd one" instead of tapping): that turn's
// product card shows one of the card's photos.
function photoChoicesSettled(turns: ChatTurnView[]): string[] {
  const settled: string[] = [];
  // The photos later turns put on each product, by its _id.
  const later = new Map<string, Set<string>>();
  // Oldest first, so from the newest back: a card is settled by what came after it.
  [...turns].reverse().forEach((turn) => {
    if (turn.status === 'failed') return;
    turn.components.forEach((component) => {
      if (component.kind !== 'productPhotoChoice') return;
      const set = later.get(component.productId);
      if (set && component.photos.some((photo) => set.has(photo.url))) settled.push(component.formId);
    });
    turn.components.forEach((component) => {
      if (component.kind !== 'storeProducts') return;
      component.products.forEach((product) => {
        if (!product.photoUrl) return;
        const set = later.get(product._id) ?? new Set<string>();
        set.add(product.photoUrl);
        later.set(product._id, set);
      });
    });
  });
  return settled;
}

// Called with each new thinking text of a turn being answered, and a way to
// fetch that text spoken (base64 MP3, null when it cannot be).
export type OnThinking = (text: string, spoken: () => Promise<string | null>) => void;

// A spoken message's reply, with how to stream it spoken (null: the
// browser's own voice speaks it), and its cards and forms (voice mode opens
// a form in a modal).
export interface VoiceAnswer {
  reply: string;
  speech: ChatSpeech | null;
  components: ChatComponent[];
}

// What voice mode sends: the spoken words, or the answer to a form it
// opened.
export type VoiceInput = string | ChatFormAnswer;

// What a message asked for outside the chat came to: sent (or queued behind
// the turn being answered), or refused because the chat is new and its first
// turn is still being answered (the move to its thread page would lose it).
export type OutsideSendResult = 'sent' | 'busy';

// How a chat reaches the server: the admin's routes or a shopper's.
// `turnKey` names a turn being sent, so thinking and speakThinking can ask
// what it is doing; `speak` asks for the reply's speech (voice mode).
// `upload` puts files in the user's own chat folder, for `attachments`
// (their download URLs); `fileUrl` reads the download URL of one of the
// user's own chat files by its Storage path.
export interface ChatTransport {
  send: (message: string | null, conversationId: string | null, form: ChatFormAnswer | null, attachments: string[] | null, turnKey: string, speak: boolean) => Promise<ApiResponse<ChatReplyResponse>>;
  upload: (files: File[]) => Promise<UploadedFiles>;
  fileUrl: (path: string) => Promise<string>;
  loadTurns: (conversationId: string, beforeSeq: number | null, limit: number) => Promise<ApiResponse<ConversationTurnsResponse>>;
  thinking: (turnKey: string) => Promise<ApiResponse<ChatThinkingResponse>>;
  getTurnStatus: (turnKey: string) => Promise<ApiResponse<ChatTurnStatusResponse>>;
  speakThinking: (turnKey: string) => Promise<ApiResponse<SpokenThinkingResponse>>;
}

// A random key for one turn (the server takes 16 to 64 letters and digits).
function newTurnKey(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

// Asks every THINKING_POLL_MS what the turn `turnKey` is doing, calling
// onText with each new text, until the returned stop is called.
function pollThinking(transport: ChatTransport, turnKey: string, onText: (text: string) => void): () => void {
  let stopped = false;
  let last: string | null = null;
  let timer = 0;
  const poll = async () => {
    const res = await transport.thinking(turnKey);
    if (stopped) return;
    if (res.status === 200 && res.data.thinkingText !== null && res.data.thinkingText !== last) {
      last = res.data.thinkingText;
      onText(last);
    }
    timer = window.setTimeout(poll, THINKING_POLL_MS);
  };
  timer = window.setTimeout(poll, THINKING_POLL_MS);
  return () => {
    stopped = true;
    window.clearTimeout(timer);
  };
}

// Where a turn whose reply never came back ended up.
type TurnOutcome =
  | { kind: 'saved'; conversationId: string }
  // `ended`: the server said it failed, or it was seen running first.
  | { kind: 'failed'; ended: boolean }
  | { kind: 'unreachable'; message: string };

// Asks every TURN_STATUS_POLL_MS where the turn `turnKey` is, while the server
// is still answering it, until it is saved or failed, `alive` turns false or
// TURN_STATUS_GIVE_UP_MS pass.
async function awaitTurnOutcome(transport: ChatTransport, turnKey: string, alive: () => boolean): Promise<TurnOutcome> {
  const deadline = Date.now() + TURN_STATUS_GIVE_UP_MS;
  const giveUp: TurnOutcome = { kind: 'unreachable', message: 'No reply after two minutes. Try again to check for it: your message won’t be sent twice.' };
  let sawRunning = false;
  for (;;) {
    if (!alive()) return { kind: 'unreachable', message: '' };
    const res = await transport.getTurnStatus(turnKey);
    if (!alive()) return { kind: 'unreachable', message: '' };
    if (res.status === 200) {
      const { status, conversationId } = res.data;
      if (status === 'saved') {
        return conversationId ? { kind: 'saved', conversationId } : { kind: 'unreachable', message: 'The reply could not be found. Open the conversation to see it.' };
      }
      if (status !== 'running') return { kind: 'failed', ended: status === 'failed' || sawRunning };
      sawRunning = true;
    } else if (res.status !== 0 || !sawRunning) {
      return { kind: 'unreachable', message: res.message };
    }
    // Once the turn was seen running, a dropped poll (status 0) is transient:
    // keep asking until the deadline.
    if (Date.now() >= deadline) return giveUp;
    await new Promise<void>((resolve) => { window.setTimeout(resolve, TURN_STATUS_POLL_MS); });
  }
}

// What a send came to: the saved reply, or one found saved after its response
// was lost (no speech: voice mode speaks it with the browser's voice).
export type SentReply = Pick<ChatReplyResponse, 'reply' | 'components' | 'speech'>;

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
export function answerLabel(form: ChatFormAnswer): string {
  switch (form.kind) {
    case 'location': return 'Here is the store’s pin.';
    case 'newProducts': return `Add ${form.products.length === 1 ? 'this product' : `these ${form.products.length} products`}.`;
    case 'phoneVerified': return 'I verified my phone number.';
    case 'photoRequest': return `Make ${PHOTOS_PER_CALL} product photos.`;
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
  // False once the chat is gone: waiting for a lost reply stops.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // Loads a page of the conversation; with `adoptKey` (the local turn whose
  // reply was found saved) the page's last turn replaces that turn, the
  // other local turns stay around it and the earlier turns already loaded
  // stay. Resolves to the page, or null when it could not load.
  const loadPage = useCallback(async (beforeSeq: number | null, adoptKey: string | null = null): Promise<ChatTurnView[] | null> => {
    const id = idRef.current;
    if (!id) return null;
    const res = await transport.loadTurns(id, beforeSeq, TURNS_PAGE);
    if (res.status !== 200) {
      setLoadError(res.message);
      return null;
    }
    setLoadError(null);
    const page: ChatTurnView[] = res.data.turns
      .map((row) => ({
        key: `seq-${row.seq}`,
        seq: row.seq,
        userText: row.userText,
        files: (row.attachments ?? []).map((file) => ({ name: file.name, contentType: file.contentType, url: null, path: file.path })),
        reply: row.replyText,
        components: row.components,
        answeredFormId: row.answeredFormId,
        createdAt: row.createdAt,
        status: 'done' as const,
        error: null,
        form: null,
        uploaded: null,
        thinkingText: null,
        turnKey: null,
      }))
      .reverse();
    const current = turnsRef.current;
    let next: ChatTurnView[];
    if (adoptKey !== null) {
      const firstSeq = page[0]?.seq ?? 0;
      const earlier = current.filter((turn) => turn.seq !== null && turn.seq < firstSeq);
      if (earlier.length === 0) setHasEarlier(res.data.turns.length === TURNS_PAGE);
      const at = current.findIndex((turn) => turn.key === adoptKey);
      const before = current.slice(0, at < 0 ? current.length : at).filter((turn) => turn.seq === null);
      const after = at < 0 ? [] : current.slice(at + 1).filter((turn) => turn.seq === null);
      next = [...earlier, ...page.slice(0, -1), ...before, ...page.slice(-1), ...after];
    } else {
      setHasEarlier(res.data.turns.length === TURNS_PAGE);
      next = beforeSeq === null ? page : [...page, ...current];
    }
    turnsRef.current = next;
    setTurns(next);
    return page;
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

  // Sends the turn `key` already on screen, once its turn in the queue has
  // come; see send.
  const sendNow = useCallback(async (
    key: string,
    message: string | null,
    form: ChatFormAnswer | null,
    onThinking: OnThinking | undefined,
    speak: boolean,
    uploaded: UploadedFiles | null,
    turnKey: string,
    resume: boolean,
  ): Promise<SentReply | null> => {
    const isNew = idRef.current === null;
    const stopPolling = pollThinking(transport, turnKey, (text) => {
      replaceTurn(key, (turn) => (turn.status === 'sending' ? { ...turn, thinkingText: text } : turn));
      onThinking?.(text, async () => {
        const spoken = await transport.speakThinking(turnKey);
        return spoken.status === 200 ? spoken.data.audio : null;
      });
    });
    const fail = (error: string) => {
      replaceTurn(key, (turn) => ({ ...turn, status: 'failed', answeredFormId: null, error, thinkingText: null }));
      return null;
    };
    // The reply was lost on the way: it is on the server (saved). Shows it
    // as if it had arrived; null when the conversation could not be loaded.
    const adoptSaved = async (conversationId: string): Promise<SentReply | null> => {
      idRef.current = conversationId;
      const page = await loadPage(null, key);
      const last = page?.[page.length - 1];
      if (!last) return null;
      onSaved?.(last);
      if (isNew) onStarted?.(conversationId, turnsRef.current);
      return { reply: last.reply ?? '', components: last.components, speech: null };
    };
    const savedNotLoaded = 'The reply was saved but could not be loaded. Open the conversation to see it.';
    // A saved turn is shown as if it had arrived: its reply, or the failed
    // turn; null when the outcome is not 'saved'.
    const settle = async (outcome: TurnOutcome): Promise<{ reply: SentReply | null } | null> => (
      outcome.kind === 'saved' ? { reply: (await adoptSaved(outcome.conversationId)) ?? fail(savedNotLoaded) } : null
    );
    let res: ApiResponse<ChatReplyResponse>;
    try {
      // Try again always costs one status round trip first.
      if (resume) {
        const outcome = await awaitTurnOutcome(transport, turnKey, () => aliveRef.current);
        const settled = await settle(outcome);
        if (settled) return settled.reply;
        if (outcome.kind === 'unreachable') return fail(outcome.message || 'Failed to fetch data');
      }
      res = await transport.send(message, idRef.current, form, uploaded?.urls ?? null, turnKey, speak);
      if (res.status !== 200) {
        // Any failed send may have reached the server: ask where the turn is.
        const outcome = await awaitTurnOutcome(transport, turnKey, () => aliveRef.current);
        const settled = await settle(outcome);
        if (settled) return settled.reply;
        if (outcome.kind === 'unreachable' && outcome.message) return fail(outcome.message);
        // The turn ended without a reply (it may have been refused as still
        // being answered): ours to explain. A turn never seen is the send's.
        if (outcome.kind === 'failed' && outcome.ended) return fail('Couldn’t get your reply. Try again.');
      }
    } finally {
      stopPolling();
    }
    if (res.status !== 200) return fail(res.message);
    idRef.current = res.data.conversationId;
    const done = replaceTurn(key, (turn) => ({
      ...turn,
      seq: res.data.turnSeq,
      userText: res.data.userText,
      reply: res.data.reply,
      components: res.data.components,
      status: 'done',
      form: null,
      uploaded: null,
      thinkingText: null,
      createdAt: new Date().toISOString(),
    }));
    const saved = done.find((turn) => turn.key === key);
    if (saved) onSaved?.(saved);
    if (isNew) onStarted?.(res.data.conversationId, done);
    return res.data;
  }, [transport, onStarted, onSaved, replaceTurn, loadPage]);

  // The turn being sent, if any: the next one waits for it, as the server
  // answers one turn of a conversation at a time. Never rejects.
  const queueRef = useRef<Promise<unknown>>(Promise.resolve());

  // Sends one turn (a message with the files `uploaded`, or a form's
  // answer) and resolves to the saved reply (null when it failed: the turn
  // then shows the error and a way to try again). It shows at once, and is
  // sent once every turn sent before it has its answer (a failed one too),
  // so turns go one at a time, in order. While it waits for its own answer,
  // the turn shows what the assistant is doing, also passed to onThinking.
  // `speak` asks for its speech.
  const send = useCallback(async (
    message: string | null,
    form: ChatFormAnswer | null,
    onThinking?: OnThinking,
    speak = false,
    uploaded: UploadedFiles | null = null,
    resumeKey: string | null = null,
  ): Promise<SentReply | null> => {
    const key = `local-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const turnKey = resumeKey ?? newTurnKey();
    const pending: ChatTurnView = {
      key,
      seq: null,
      userText: form ? answerLabel(form) : message ?? '',
      files: uploaded?.files ?? [],
      reply: null,
      components: [],
      answeredFormId: form?.formId ?? null,
      createdAt: null,
      status: 'sending',
      error: null,
      form,
      uploaded,
      thinkingText: null,
      turnKey,
    };
    const withPending = [...turnsRef.current, pending];
    turnsRef.current = withPending;
    setTurns(withPending);

    const run = queueRef.current.then(() => sendNow(key, message, form, onThinking, speak, uploaded, turnKey, resumeKey !== null));
    queueRef.current = run.catch(() => null);
    return run;
  }, [sendNow]);

  const ask = useCallback(async (text: string): Promise<string | null> => {
    const message = text.trim();
    if (!message) return null;
    return (await send(message, null))?.reply ?? null;
  }, [send]);

  // A message from the composer: its text, the files uploaded for it, or
  // both.
  const sendMessage = useCallback(async (text: string, uploaded: UploadedFiles | null): Promise<string | null> => {
    const message = text.trim();
    const files = uploaded && uploaded.urls.length > 0 ? uploaded : null;
    if (!message && !files) return null;
    return (await send(message || null, null, undefined, false, files))?.reply ?? null;
  }, [send]);

  // A spoken message, or a form answered in voice mode: its reply, how to
  // stream it spoken, and its components.
  const talk = useCallback(async (input: VoiceInput, onThinking?: OnThinking): Promise<VoiceAnswer | null> => {
    let saved: SentReply | null;
    if (typeof input === 'string') {
      const message = input.trim();
      if (!message) return null;
      saved = await send(message, null, onThinking, true);
    } else {
      saved = await send(null, input, onThinking, true);
    }
    return saved ? { reply: saved.reply, speech: saved.speech, components: saved.components } : null;
  }, [send]);

  const answer = useCallback(async (form: ChatFormAnswer) => (await send(null, form))?.reply ?? null, [send]);

  // A message asked for outside the chat (the shop's cart, a receipt sent):
  // see OutsideSendResult.
  const sendOutside = useCallback((text: string): OutsideSendResult => {
    if (idRef.current === null && turnsRef.current.some((turn) => turn.status === 'sending')) return 'busy';
    void ask(text);
    return 'sent';
  }, [ask]);

  const retry = useCallback(async (key: string) => {
    const failed = turnsRef.current.find((turn) => turn.key === key);
    if (!failed) return;
    const rest = turnsRef.current.filter((turn) => turn.key !== key);
    turnsRef.current = rest;
    setTurns(rest);
    await send(failed.form ? null : failed.userText || null, failed.form, undefined, false, failed.uploaded, failed.turnKey);
  }, [send]);

  const sending = turns.some((turn) => turn.status === 'sending');
  const answeredFormIds = useMemo(() => {
    const answered = new Set(turns.flatMap((turn) => (turn.answeredFormId && turn.status !== 'failed' ? [turn.answeredFormId] : [])));
    photoChoicesSettled(turns).forEach((formId) => answered.add(formId));
    return answered;
  }, [turns]);

  return { turns, loading, loadError, hasEarlier, loadingEarlier, sending, answeredFormIds, ask, sendMessage, upload: transport.upload, fileUrl: transport.fileUrl, talk, answer, sendOutside, retry, reload, loadEarlier };
}
