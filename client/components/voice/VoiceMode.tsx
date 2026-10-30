'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyboardIcon, Volume2Icon, VolumeXIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Kbd } from '@/components/ui/kbd';
import { cn } from '@/lib/utils';
import { AssistantText } from '@/components/chat/AssistantText';
import type { OnThinking } from '@/components/chat/useChat';
import {
  speak,
  speakableText,
  speechRecognitionConstructor,
  stopSpeaking,
  type SpeechRecognitionLike,
} from './useSpeech';

type VoiceState = 'listening' | 'thinking' | 'answering' | 'idle' | 'unsupported';

interface VoiceModeProps {
  open: boolean;
  onClose: () => void;
  // Sends the spoken words as a chat message; resolves to the reply.
  // onThinking hears what the assistant is doing while it answers.
  ask: (text: string, onThinking?: OnThinking) => Promise<string | null>;
  title: string;
  subtitle: string;
  // The glow's two colors: the store's tones for shoppers, teal and blue
  // for the admin.
  glow: string;
  glowSecond: string;
  variant: 'admin' | 'shop';
}

function alpha(hex: string, amount: number): string {
  const value = hex.replace('#', '');
  const n = parseInt(value.length === 3 ? value.split('').map((c) => c + c).join('') : value, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${amount})`;
}

const STATUS: Record<VoiceState, string> = {
  listening: 'Listening',
  thinking: 'Working on it',
  answering: 'Speaking',
  idle: 'Tap the orb to talk',
  unsupported: 'Voice isn’t available here',
};

// How long a pause ends what was said, and how long listening waits for a
// first word before giving up.
const END_OF_SPEECH_MS = 1500;
const NO_SPEECH_MS = 8000;

// Full-screen voice mode: a breathing glow around the screen while it
// listens, the words appearing as they are spoken, and the reply read aloud
// and shown in large type.
export function VoiceMode({ open, onClose, ask, title, subtitle, glow, glowSecond, variant }: VoiceModeProps) {
  const [state, setState] = useState<VoiceState>('idle');
  const [heard, setHeard] = useState('');
  const [interim, setInterim] = useState('');
  const [question, setQuestion] = useState('');
  const [reply, setReply] = useState<string | null>(null);
  const [speakReplies, setSpeakReplies] = useState(true);
  const [thinkingText, setThinkingText] = useState<string | null>(null);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const speakRef = useRef(speakReplies);
  const stateRef = useRef<VoiceState>(state);
  // The thinking text playing (ElevenLabs, in the store's voice), and
  // whether one is being fetched: one at a time, never over the reply.
  const thinkingAudio = useRef<HTMLAudioElement | null>(null);
  const fetchingThinking = useRef(false);
  const lang = typeof navigator === 'undefined' ? 'en-US' : navigator.language || 'en-US';

  useEffect(() => { speakRef.current = speakReplies; }, [speakReplies]);
  useEffect(() => { stateRef.current = state; }, [state]);

  const stopThinkingAudio = useCallback(() => {
    thinkingAudio.current?.pause();
    thinkingAudio.current = null;
  }, []);

  // Shows each thinking text and says it, unless one is already playing or
  // being fetched, or the answer came first.
  const onThinking = useCallback<OnThinking>(async (text, spoken) => {
    setThinkingText(text);
    if (!speakRef.current || thinkingAudio.current || fetchingThinking.current) return;
    fetchingThinking.current = true;
    const audio = await spoken();
    fetchingThinking.current = false;
    if (!audio || !speakRef.current || stateRef.current !== 'thinking' || thinkingAudio.current) return;
    const element = new Audio(`data:audio/mpeg;base64,${audio}`);
    const done = () => {
      if (thinkingAudio.current === element) thinkingAudio.current = null;
    };
    element.onended = done;
    thinkingAudio.current = element;
    element.play().catch(done);
  }, []);

  const finish = useCallback(async (text: string) => {
    const spoken = text.trim();
    if (!spoken) {
      setState('idle');
      return;
    }
    setQuestion(spoken);
    setReply(null);
    setThinkingText(null);
    setState('thinking');
    stateRef.current = 'thinking';
    const answer = await ask(spoken, onThinking);
    stateRef.current = 'answering';
    stopThinkingAudio();
    if (answer === null) {
      setReply('That didn’t go through. Tap the orb to try again.');
      setState('idle');
      return;
    }
    setReply(answer);
    setState('answering');
    if (speakRef.current) await speak(speakableText(answer), lang);
    setState((current) => (current === 'answering' ? 'idle' : current));
  }, [ask, lang, onThinking, stopThinkingAudio]);

  const listen = useCallback(() => {
    const Recognition = speechRecognitionConstructor();
    if (!Recognition) {
      setState('unsupported');
      return;
    }
    stopSpeaking();
    let finalText = '';
    const instance = new Recognition();
    instance.lang = lang;
    instance.interimResults = true;
    // Continuous, so a stutter or a breath does not end the turn: it ends
    // after END_OF_SPEECH_MS without new words (or a tap on the orb).
    instance.continuous = true;
    let silenceTimer = window.setTimeout(() => instance.stop(), NO_SPEECH_MS);
    const stopAfter = (ms: number) => {
      window.clearTimeout(silenceTimer);
      silenceTimer = window.setTimeout(() => instance.stop(), ms);
    };
    instance.onresult = (event) => {
      let live = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else live += result[0].transcript;
      }
      setHeard(finalText);
      setInterim(live);
      stopAfter(END_OF_SPEECH_MS);
    };
    instance.onerror = () => {
      finalText = '';
    };
    instance.onend = () => {
      window.clearTimeout(silenceTimer);
      recognition.current = null;
      setInterim('');
      finish(finalText);
    };
    recognition.current = instance;
    setHeard('');
    setInterim('');
    setState('listening');
    instance.start();
  }, [finish, lang]);

  const close = useCallback(() => {
    recognition.current?.abort();
    recognition.current = null;
    stopSpeaking();
    stopThinkingAudio();
    setState('idle');
    setHeard('');
    setInterim('');
    setQuestion('');
    setReply(null);
    onClose();
  }, [onClose, stopThinkingAudio]);

  // Opening starts listening once; later changes to the callbacks (a new
  // chat id, say) must not restart it mid-answer.
  const listenRef = useRef(listen);
  const closeRef = useRef(close);
  useEffect(() => {
    listenRef.current = listen;
    closeRef.current = close;
  }, [listen, close]);

  useEffect(() => {
    if (!open) return;
    const start = window.setTimeout(() => listenRef.current(), 150);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.clearTimeout(start);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const tapOrb = () => {
    if (state === 'listening') recognition.current?.stop();
    else if (state === 'answering') {
      stopSpeaking();
      listen();
    } else if (state === 'idle') listen();
  };

  if (!open) return null;

  const orbLabel = state === 'listening' ? 'Stop listening' : state === 'thinking' ? 'Working on it' : 'Talk';
  const showBars = state === 'listening' || state === 'answering';
  const orbFill = `radial-gradient(circle at 34% 28%, #ffffff 0%, ${alpha(glowSecond, 0.85)} 32%, ${glow} 80%)`;

  return (
    <div
      data-component={`${variant}VoiceMode`}
      role="dialog"
      aria-modal="true"
      aria-label="Voice mode"
      className="fixed inset-0 z-50 flex flex-col bg-background/80 backdrop-blur-md"
    >
      <div
        aria-hidden="true"
        className="voice-glow pointer-events-none absolute inset-0"
        style={{ boxShadow: `inset 0 0 26px 3px ${alpha(glow, 0.5)}, inset 0 0 140px 22px ${alpha(glowSecond, 0.26)}` }}
      />

      <div className="relative flex items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="flex flex-col">
          <span className="font-medium text-sm">{title}</span>
          <span className="text-muted-foreground text-xs">{subtitle}</span>
        </div>
        <Button
          data-component={`${variant}VoiceClose`}
          variant="outline"
          size="icon-lg"
          className="rounded-full"
          onClick={close}
          aria-label="Close voice mode"
        >
          <XIcon />
        </Button>
      </div>

      <div className="relative mx-auto flex w-full max-w-2xl flex-1 flex-col justify-end gap-4 px-5 pb-6" aria-live="polite">
        {state === 'unsupported' ? (
          <p className="m-0 font-heading text-2xl leading-tight">
            This browser can’t listen. Try Chrome, Edge or Safari, or type instead.
          </p>
        ) : (
          <>
            {(state === 'listening' || state === 'thinking') && (
              <p className="m-0 font-heading text-3xl leading-tight text-pretty sm:text-4xl">
                {state === 'thinking' ? question : heard}
                {state === 'listening' && <span className="text-muted-foreground">{interim}</span>}
                {state === 'listening' && !heard && !interim && <span className="text-muted-foreground">Go ahead, I’m listening</span>}
              </p>
            )}
            {(state === 'answering' || state === 'idle') && reply && (
              <div className="flex flex-col gap-3">
                {question && <span className="text-muted-foreground text-sm">“{question}”</span>}
                <div className="font-heading text-2xl leading-snug sm:text-3xl [&_[data-component=assistantText]]:text-[inherit] [&_[data-component=assistantText]]:leading-[inherit]">
                  <AssistantText text={reply} />
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <div className="relative grid grid-cols-[48px_1fr_48px] items-center gap-3 px-5 pb-8 sm:mx-auto sm:w-full sm:max-w-md">
        <Button
          data-component={`${variant}VoiceKeyboard`}
          variant="outline"
          size="icon-xl"
          className="rounded-full"
          onClick={close}
          aria-label="Type instead"
        >
          <KeyboardIcon />
        </Button>

        <div className="flex flex-col items-center gap-2">
          <div className="relative flex size-36 items-center justify-center">
            {state === 'listening' && (
              <>
                <span aria-hidden="true" className="voice-pulse absolute inset-0 rounded-full" style={{ background: alpha(glow, 0.06) }} />
                <span aria-hidden="true" className="voice-pulse absolute inset-3.5 rounded-full [animation-delay:.2s]" style={{ background: alpha(glow, 0.1) }} />
                <span aria-hidden="true" className="voice-pulse absolute inset-6.5 rounded-full [animation-delay:.4s]" style={{ background: alpha(glow, 0.18) }} />
              </>
            )}
            {state === 'thinking' && (
              <span
                aria-hidden="true"
                className="voice-spin absolute inset-5 rounded-full border-2"
                style={{ borderColor: alpha(glow, 0.12), borderTopColor: glow }}
              />
            )}
            {state === 'answering' && (
              <>
                <span aria-hidden="true" className="voice-ripple absolute inset-7 rounded-full border-2" style={{ borderColor: alpha(glow, 0.2) }} />
                <span aria-hidden="true" className="voice-ripple absolute inset-7 rounded-full border-2 [animation-delay:.9s]" style={{ borderColor: alpha(glow, 0.2) }} />
              </>
            )}
            <button
              type="button"
              data-component={`${variant}VoiceOrb`}
              aria-label={orbLabel}
              onClick={tapOrb}
              disabled={state === 'thinking' || state === 'unsupported'}
              className="relative flex size-22 cursor-pointer items-center justify-center gap-1 rounded-full disabled:cursor-default"
              style={{ background: orbFill, boxShadow: `0 10px 28px ${alpha(glow, 0.45)}, inset 0 1px 0 rgba(255,255,255,0.4)` }}
            >
              {showBars && [22, 30, 26, 18].map((height, index) => (
                <span
                  key={index}
                  aria-hidden="true"
                  className="voice-bar w-1 rounded-full bg-white/90"
                  style={{ height, animationDelay: `${index * 0.15}s` }}
                />
              ))}
            </button>
          </div>
          <span
            data-component={`${variant}VoiceStatus`}
            className={cn('text-muted-foreground text-sm', state === 'listening' && 'text-foreground')}
          >
            {state === 'thinking' && thinkingText ? thinkingText : STATUS[state]}
          </span>
        </div>

        <Button
          data-component={`${variant}VoiceSpeaker`}
          variant="outline"
          size="icon-xl"
          className="rounded-full"
          onClick={() => {
            if (speakReplies) {
              stopSpeaking();
              stopThinkingAudio();
            }
            setSpeakReplies((value) => !value);
          }}
          aria-pressed={speakReplies}
          aria-label="Read replies aloud"
        >
          {speakReplies ? <Volume2Icon /> : <VolumeXIcon />}
        </Button>
      </div>
      {variant === 'admin' && (
        <span className="relative hidden pb-4 text-center text-muted-foreground text-xs sm:block">
          <Kbd>Esc</Kbd> to close
        </span>
      )}
    </div>
  );
}
