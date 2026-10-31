'use client';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { VoiceMode } from '@/components/voice/VoiceMode';
import { unlockVoiceAudio } from '@/components/voice/voiceAudio';
import { primeSpeaking } from '@/components/voice/useSpeech';
import type { ChatComponent } from './cards/types';
import type { ChatTurnView, OnThinking, VoiceAnswer } from './useChat';

type Ask = (text: string, onThinking?: OnThinking) => Promise<VoiceAnswer | null>;

interface VoiceOptions {
  title: string;
  subtitle: string;
  glow: string;
  glowSecond: string;
  variant: 'admin' | 'shop';
}

interface ChatShellValue {
  // A chat started on the new-chat screen hands its turns to the thread page
  // it moves to, so the thread shows them without loading them again.
  stash: (conversationId: string, turns: ChatTurnView[]) => void;
  peek: (conversationId: string) => ChatTurnView[] | undefined;
  forget: (conversationId: string) => void;
  // A turn was saved: the chat list reloads (a new chat moves to the top),
  // and its components may change what the page shows (a store set up).
  turnSaved: (components: ChatComponent[]) => void;
  // Voice mode lives here, above the pages, so a voice chat that starts on
  // the new-chat screen keeps talking after the move to its thread page.
  // The page on screen binds its chat's ask.
  bindAsk: (ask: Ask) => void;
  // Unbinds `ask` only if it is still the one bound: the next page may
  // have bound its own first.
  unbindAsk: (ask: Ask) => void;
  openVoice: () => void;
}

const ChatShellContext = createContext<ChatShellValue | null>(null);

export function useChatShell(): ChatShellValue {
  const context = useContext(ChatShellContext);
  if (!context) throw new Error('useChatShell must be used within a ChatShellProvider');
  return context;
}

interface ChatShellProviderProps {
  voice: VoiceOptions;
  onTurnSaved: (components: ChatComponent[]) => void;
  children: React.ReactNode;
}

export function ChatShellProvider({ voice, onTurnSaved, children }: ChatShellProviderProps) {
  const started = useRef(new Map<string, ChatTurnView[]>());
  const askRef = useRef<Ask | null>(null);
  const savedRef = useRef(onTurnSaved);
  useEffect(() => { savedRef.current = onTurnSaved; }, [onTurnSaved]);
  const [voiceOpen, setVoiceOpen] = useState(false);

  const ask = useCallback<Ask>((text, onThinking) => (askRef.current ? askRef.current(text, onThinking) : Promise.resolve(null)), []);
  const closeVoice = useCallback(() => setVoiceOpen(false), []);

  const value = useMemo<ChatShellValue>(() => ({
    stash: (conversationId, turns) => { started.current.set(conversationId, turns); },
    peek: (conversationId) => started.current.get(conversationId),
    forget: (conversationId) => { started.current.delete(conversationId); },
    turnSaved: (components) => savedRef.current(components),
    bindAsk: (next) => { askRef.current = next; },
    unbindAsk: (bound) => { if (askRef.current === bound) askRef.current = null; },
    // Runs in the voice button's tap: iOS plays audio only once unlocked by one.
    openVoice: () => {
      unlockVoiceAudio();
      primeSpeaking();
      setVoiceOpen(true);
    },
  }), []);

  return (
    <ChatShellContext.Provider value={value}>
      {children}
      <VoiceMode
        open={voiceOpen}
        onClose={closeVoice}
        ask={ask}
        title={voice.title}
        subtitle={voice.subtitle}
        glow={voice.glow}
        glowSecond={voice.glowSecond}
        variant={voice.variant}
      />
    </ChatShellContext.Provider>
  );
}
