'use client';

// The one AudioContext voice mode speaks through. iOS Safari plays Web
// Audio only from a context resumed inside a tap, so unlockVoiceAudio runs
// in the tap that opens voice mode (and in each orb or speaker tap), and the replies
// and thinking texts that arrive seconds later play through that same
// context. It runs at the device's own rate: a 24 kHz context is not
// reliable on iOS, and Web Audio resamples a 24 kHz buffer on a 48 kHz one.

let shared: AudioContext | null = null;

interface AudioSessionLike {
  type: 'auto' | 'playback' | 'transient' | 'transient-solo' | 'ambient' | 'play-and-record';
}

// How long a reply waits for a suspended context to run before the
// browser's voice speaks it instead.
const RUNNING_WAIT_MS = 1500;

export function base64Bytes(base64: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

// iOS 17+ Safari: 'playback' plays through the silent switch; listening
// needs 'auto' back, as 'playback' is not a session that records.
export function setAudioSession(type: 'auto' | 'playback'): void {
  if (typeof navigator === 'undefined') return;
  const session = (navigator as Navigator & { audioSession?: AudioSessionLike }).audioSession;
  if (session) session.type = type;
}

// Call synchronously in a tap handler.
export function unlockVoiceAudio(): void {
  if (typeof window === 'undefined' || !('AudioContext' in window)) return;
  if (!shared || shared.state === 'closed') shared = new AudioContext();
  void shared.resume();
  // The standard iOS unlock: a silent sample started in the tap.
  const source = shared.createBufferSource();
  source.buffer = shared.createBuffer(1, 1, shared.sampleRate);
  source.connect(shared.destination);
  source.start(0);
}

// The unlocked context, resumed again in case iOS suspended or
// interrupted it (the microphone, a call); null when voice mode never had
// a tap.
export function voiceAudioContext(): AudioContext | null {
  if (!shared || shared.state === 'closed') return null;
  if (shared.state !== 'running') void shared.resume();
  return shared;
}

// Resolves true once the context runs (at once when it does), false when
// it has not within RUNNING_WAIT_MS.
export function whenRunning(context: AudioContext): Promise<boolean> {
  if (context.state === 'running') return Promise.resolve(true);
  return new Promise((resolve) => {
    const settle = (running: boolean) => {
      window.clearTimeout(timer);
      context.removeEventListener('statechange', onChange);
      resolve(running);
    };
    const onChange = () => {
      if (context.state === 'running') settle(true);
    };
    const timer = window.setTimeout(() => settle(false), RUNNING_WAIT_MS);
    context.addEventListener('statechange', onChange);
    context.resume().then(onChange, () => {});
  });
}

export function closeVoiceAudio(): void {
  void shared?.close();
  shared = null;
  setAudioSession('auto');
}

// A base64 MP3 (a thinking text), decoded into a source on the shared
// context for the caller to start; null when there is no context or it
// cannot be decoded.
export async function clipSource(base64: string): Promise<AudioBufferSourceNode | null> {
  const context = voiceAudioContext();
  if (!context) return null;
  try {
    const buffer = await context.decodeAudioData(base64Bytes(base64).buffer);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    return source;
  } catch {
    return null;
  }
}
