'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ClipboardListIcon, KeyboardIcon, Volume2Icon, VolumeXIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogDescription, DialogHeader, DialogPanel, DialogPopup, DialogTitle } from '@/components/ui/dialog';
import { Kbd } from '@/components/ui/kbd';
import { cn } from '@/lib/utils';
import { AssistantText } from '@/components/chat/AssistantText';
import { answerLabel, type OnThinking, type VoiceAnswer, type VoiceInput } from '@/components/chat/useChat';
import { ChatComponents } from '@/components/chat/cards/ChatComponents';
import type { ChatComponent, ChatFormAnswer } from '@/components/chat/cards/types';
import { streamSpeech, type SpeechStream } from './elevenLabsStream';
import { clipSource, closeVoiceAudio, setAudioSession, unlockVoiceAudio, voiceAudioContext } from './voiceAudio';
import {
  primeSpeaking,
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
  // Sends the spoken words as a chat message, or the answer to a form the
  // reply opened; resolves to the reply. onThinking hears what the assistant
  // is doing while it answers. The reply comes with its speech (ElevenLabs),
  // else the browser speaks it.
  ask: (input: VoiceInput, onThinking?: OnThinking) => Promise<VoiceAnswer | null>;
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
  listening: 'Listening · tap the orb when you’re done',
  thinking: 'Working on it',
  answering: 'Speaking',
  idle: 'Tap the orb to talk',
  unsupported: 'Voice isn’t available here',
};

// Listening runs from one tap on the orb to the next. The browser may end
// recognition on its own (a long pause): it starts again, keeping what was
// said, unless it ended within RESTART_MIN_MS (it would only end again).
// Past MAX_RECORDING_MS what was said is sent, so the microphone is never
// left on.
const MAX_RECORDING_MS = 120000;
const RESTART_MIN_MS = 1000;
// Errors after which the microphone can't be used: listening stops.
const FATAL_ERRORS = new Set(['not-allowed', 'service-not-allowed', 'audio-capture', 'network']);

// What a reply asks the user to fill in or choose: its forms and choosing
// cards (a formId).
function formsOf(components: ChatComponent[]): ChatComponent[] {
  return components.filter((component) => 'formId' in component && component.formId !== null);
}

// The voice modal shows only the latest reply's forms, none answered yet.
const NONE_ANSWERED: ReadonlySet<string> = new Set();

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
  // The latest reply's forms, and whether their modal is open (closing it
  // keeps them, to open again).
  const [forms, setForms] = useState<ChatComponent[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const formOpenRef = useRef(formOpen);
  const recognition = useRef<SpeechRecognitionLike | null>(null);
  // Ends the listening in progress and sends what was said.
  const stopListening = useRef<(() => void) | null>(null);
  const speakRef = useRef(speakReplies);
  const stateRef = useRef<VoiceState>(state);
  // The thinking text playing (ElevenLabs, in the store's voice), and
  // whether one is being fetched: one at a time, never over the reply.
  const thinkingAudio = useRef<AudioBufferSourceNode | null>(null);
  const fetchingThinking = useRef(false);
  // The reply streaming from ElevenLabs, and a count of what was stopped:
  // a reply whose stream ends after a stop is not spoken again.
  const replyStream = useRef<SpeechStream | null>(null);
  const stops = useRef(0);
  // Bumped by closing: an answer or thinking text that arrives after it
  // belongs to a closed voice mode and is never said.
  const session = useRef(0);
  const lang = typeof navigator === 'undefined' ? 'en-US' : navigator.language || 'en-US';

  useEffect(() => { speakRef.current = speakReplies; }, [speakReplies]);
  useEffect(() => { stateRef.current = state; }, [state]);
  useEffect(() => { formOpenRef.current = formOpen; }, [formOpen]);

  const stopThinkingAudio = useCallback(() => {
    thinkingAudio.current?.stop();
    thinkingAudio.current = null;
  }, []);

  // Everything voice mode is saying: the reply (streaming or the
  // browser's) and a thinking text.
  const stopVoice = useCallback(() => {
    stops.current += 1;
    replyStream.current?.stop();
    replyStream.current = null;
    stopSpeaking();
    stopThinkingAudio();
  }, [stopThinkingAudio]);

  // Shows each thinking text and says it, unless one is already playing or
  // being fetched, or the answer came first. It plays on the shared context
  // (not an Audio element, which iOS refuses outside a tap).
  const onThinking = useCallback<OnThinking>(async (text, spoken) => {
    setThinkingText(text);
    if (!speakRef.current || thinkingAudio.current || fetchingThinking.current) return;
    const current = session.current;
    fetchingThinking.current = true;
    const audio = await spoken();
    const source = audio && session.current === current ? await clipSource(audio) : null;
    if (session.current !== current) return;
    fetchingThinking.current = false;
    if (!source || !speakRef.current || stateRef.current !== 'thinking' || thinkingAudio.current) return;
    source.onended = () => {
      if (thinkingAudio.current === source) thinkingAudio.current = null;
    };
    thinkingAudio.current = source;
    source.start();
  }, []);

  // Sends what was said, or a form's answer, then shows and says the reply
  // and opens its forms, if any, once it has been said.
  const finish = useCallback(async (input: VoiceInput) => {
    const spoken = typeof input === 'string' ? input.trim() : input;
    if (!spoken) {
      setState('idle');
      return;
    }
    setQuestion(typeof spoken === 'string' ? spoken : answerLabel(spoken));
    setForms([]);
    setFormOpen(false);
    setReply(null);
    setThinkingText(null);
    setState('thinking');
    stateRef.current = 'thinking';
    // Done listening: play through the silent switch, and resume the
    // context in time for the reply.
    setAudioSession('playback');
    voiceAudioContext();
    const current = session.current;
    // A turn polls its thinking texts until it ends: none reaches a newer
    // session after a quick close and reopen.
    const answer = await ask(spoken, (text, spokenText) => {
      if (session.current === current) onThinking(text, spokenText);
    });
    if (session.current !== current) return;
    stateRef.current = 'answering';
    stopThinkingAudio();
    if (answer === null) {
      setReply('That didn’t go through. Tap the orb to try again.');
      setState('idle');
      return;
    }
    setReply(answer.reply);
    const replyForms = formsOf(answer.components);
    setForms(replyForms);
    setState('answering');
    const stopsBefore = stops.current;
    let spokenAloud = false;
    if (speakRef.current && answer.speech) {
      const stream = streamSpeech(answer.speech);
      replyStream.current = stream;
      spokenAloud = await stream.done;
      if (replyStream.current === stream) replyStream.current = null;
    }
    if (session.current !== current) return;
    // No speech, or the stream failed before a word: the browser's voice.
    if (!spokenAloud && speakRef.current && stops.current === stopsBefore) {
      await speak(speakableText(answer.reply), lang);
    }
    if (session.current !== current) return;
    // A tap on the orb while it spoke started listening: no modal over it.
    if (stateRef.current !== 'answering') return;
    stateRef.current = 'idle';
    setState('idle');
    if (replyForms.length > 0) setFormOpen(true);
  }, [ask, lang, onThinking, stopThinkingAudio]);

  const listen = useCallback(() => {
    const Recognition = speechRecognitionConstructor();
    if (!Recognition) {
      setState('unsupported');
      return;
    }
    stopVoice();
    setAudioSession('auto');
    const current = session.current;
    let finalText = '';
    // The orb was tapped (or the cap reached): send what was said.
    let done = false;
    // The microphone can't be used: nothing is sent.
    let failed = false;
    const stop = () => {
      done = true;
      recognition.current?.stop();
    };
    const cap = window.setTimeout(stop, MAX_RECORDING_MS);
    stopListening.current = stop;

    const start = () => {
      const startedAt = Date.now();
      const instance = new Recognition();
      instance.lang = lang;
      instance.interimResults = true;
      instance.continuous = true;
      instance.onresult = (event) => {
        let live = '';
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const result = event.results[i];
          const transcript = result[0].transcript.trim();
          if (result.isFinal) finalText = finalText ? `${finalText} ${transcript}` : transcript;
          else live += result[0].transcript;
        }
        setHeard(finalText);
        setInterim(live && finalText ? ` ${live.trim()}` : live);
      };
      instance.onerror = (event) => {
        if (FATAL_ERRORS.has(event.error)) {
          failed = true;
          finalText = '';
        }
      };
      instance.onend = () => {
        // Closed (or unmounted): nothing more is heard or sent.
        if (session.current !== current) return;
        if (!done && !failed && Date.now() - startedAt >= RESTART_MIN_MS) {
          try {
            start();
            return;
          } catch {
            // It can't start again: send what was said.
          }
        }
        window.clearTimeout(cap);
        if (stopListening.current === stop) stopListening.current = null;
        recognition.current = null;
        setInterim('');
        finish(finalText);
      };
      recognition.current = instance;
      instance.start();
    };

    setHeard('');
    setInterim('');
    setForms([]);
    setFormOpen(false);
    stateRef.current = 'listening';
    setState('listening');
    try {
      start();
    } catch {
      window.clearTimeout(cap);
      stopListening.current = null;
      recognition.current = null;
      stateRef.current = 'idle';
      setState('idle');
    }
  }, [finish, lang, stopVoice]);

  // A form answered in the modal: the modal closes, and the answer goes as
  // the next turn (its reply said aloud). Submitting is a tap: audio
  // unlocks again.
  const answerForm = useCallback((form: ChatFormAnswer) => {
    unlockVoiceAudio();
    setFormOpen(false);
    finish(form);
  }, [finish]);

  const close = useCallback(() => {
    session.current += 1;
    fetchingThinking.current = false;
    recognition.current?.abort();
    recognition.current = null;
    stopListening.current = null;
    stopVoice();
    closeVoiceAudio();
    setState('idle');
    setHeard('');
    setInterim('');
    setQuestion('');
    setReply(null);
    setForms([]);
    setFormOpen(false);
    onClose();
  }, [onClose, stopVoice]);

  // Opening starts listening once; later changes to the callbacks (a new
  // chat id, say) must not restart it mid-answer.
  const listenRef = useRef(listen);
  const closeRef = useRef(close);
  useEffect(() => {
    listenRef.current = listen;
    closeRef.current = close;
  }, [listen, close]);

  // Unmounting while open says nothing more and gives the audio back.
  useEffect(() => () => {
    session.current += 1;
    recognition.current?.abort();
    stopVoice();
    closeVoiceAudio();
  }, [stopVoice]);

  useEffect(() => {
    if (!open) return;
    const start = window.setTimeout(() => listenRef.current(), 150);
    const onKey = (event: KeyboardEvent) => {
      // Esc with the form open closes only the form.
      if (event.key === 'Escape' && !formOpenRef.current) closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.clearTimeout(start);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Every orb tap unlocks audio again (iOS may have suspended it since);
  // only ChatShell's open tap primes the browser's voice, never a tap that
  // stops the microphone.
  const tapOrb = () => {
    unlockVoiceAudio();
    if (state === 'listening') stopListening.current?.();
    else if (state === 'answering') {
      stopVoice();
      listen();
    } else if (state === 'idle') listen();
  };

  if (!open) return null;

  const orbLabel = state === 'listening' ? 'Done talking' : state === 'thinking' ? 'Working on it' : 'Talk';
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
                {state === 'idle' && forms.length > 0 && !formOpen && (
                  <Button
                    data-component={`${variant}VoiceFormReopen`}
                    variant="outline"
                    className="self-start"
                    onClick={() => setFormOpen(true)}
                  >
                    <ClipboardListIcon />
                    Open form
                  </Button>
                )}
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
            if (speakReplies) stopVoice();
            else {
              // Turning it back on is a tap: unlock audio for the replies.
              unlockVoiceAudio();
              if (state !== 'listening') primeSpeaking();
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

      <Dialog open={formOpen && forms.length > 0} onOpenChange={setFormOpen}>
        <DialogPopup data-component={`${variant}VoiceFormDialog`} className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Fill this in</DialogTitle>
            <DialogDescription>Or close it and just say it.</DialogDescription>
          </DialogHeader>
          <DialogPanel className="flex flex-col gap-4">
            {reply && <AssistantText text={reply} />}
            <ChatComponents
              components={forms}
              place="below"
              answeredFormIds={NONE_ANSWERED}
              busy={state === 'thinking'}
              onAnswer={answerForm}
            />
          </DialogPanel>
        </DialogPopup>
      </Dialog>
    </div>
  );
}
