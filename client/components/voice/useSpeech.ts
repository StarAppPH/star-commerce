'use client';

// The browser's own speech recognition (Chrome, Edge, Safari) and speech
// synthesis. TypeScript's DOM types have no SpeechRecognition, so the few
// members voice mode uses are declared here.

interface SpeechAlternativeLike {
  transcript: string;
}

interface SpeechResultLike {
  isFinal: boolean;
  readonly length: number;
  [index: number]: SpeechAlternativeLike;
}

interface SpeechResultListLike {
  readonly length: number;
  [index: number]: SpeechResultLike;
}

export interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: SpeechResultListLike;
}

export interface SpeechRecognitionErrorLike {
  error: string;
}

export interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorLike) => void) | null;
  onend: (() => void) | null;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

export function speechRecognitionConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === 'undefined') return null;
  const speechWindow = window as unknown as {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
}

// Reads a reply aloud; resolves when it has been spoken (or at once where
// the browser cannot speak).
export function speak(text: string, lang: string): Promise<void> {
  return new Promise((resolve) => {
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
      resolve();
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = lang;
    utterance.onend = () => resolve();
    utterance.onerror = () => resolve();
    window.speechSynthesis.speak(utterance);
  });
}

// The browser's voice needs a tap on iOS too: an empty utterance spoken in
// one primes it. Never on a tap that stops a live microphone.
export function primeSpeaking(): void {
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(''));
  }
}

export function stopSpeaking(): void {
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel();
  }
}

// Spoken replies skip the light markdown the chat shows.
export function speakableText(text: string): string {
  return text
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/^\s*\d+[.)]\s+/gm, '');
}
