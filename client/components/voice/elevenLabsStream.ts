'use client';
import type { ChatSpeech } from '@/app/schemas/ChatSpeechSchema';

// A reply spoken straight from ElevenLabs: the browser opens their
// text-to-speech WebSocket with the single-use token the chat route gave
// it (our key never leaves the server), sends the reply's text, and plays
// the raw 24 kHz PCM chunks as they arrive, so the voice starts with the
// first chunk however long the reply is.

const STREAM_URL = 'wss://api.elevenlabs.io/v1/text-to-speech';
const OUTPUT_FORMAT = 'pcm_24000';
const SAMPLE_RATE = 24000;

interface StreamMessage {
  audio?: string | null;
  isFinal?: boolean | null;
}

function streamMessage(data: unknown): StreamMessage | null {
  if (typeof data !== 'string') return null;
  try {
    const parsed: unknown = JSON.parse(data);
    return typeof parsed === 'object' && parsed !== null ? (parsed as StreamMessage) : null;
  } catch {
    return null;
  }
}

// 16-bit little-endian PCM, as Web Audio's floats.
function pcmSamples(base64: string): Float32Array {
  const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
  const view = new DataView(bytes.buffer);
  const samples = new Float32Array(Math.floor(bytes.length / 2));
  for (let i = 0; i < samples.length; i += 1) samples[i] = view.getInt16(i * 2, true) / 32768;
  return samples;
}

export interface SpeechStream {
  // Resolves when it has been spoken (or stopped, or failed): true when any
  // of it played, so a caller can fall back to the browser's voice.
  done: Promise<boolean>;
  stop: () => void;
}

export function streamSpeech(speech: ChatSpeech): SpeechStream {
  const context = new AudioContext({ sampleRate: SAMPLE_RATE });
  const params = new URLSearchParams({
    model_id: speech.modelId,
    output_format: OUTPUT_FORMAT,
    single_use_token: speech.token,
  });
  const socket = new WebSocket(`${STREAM_URL}/${encodeURIComponent(speech.voiceId)}/stream-input?${params}`);
  const sources = new Set<AudioBufferSourceNode>();
  let playAt = 0;
  let played = false;
  let streamEnded = false;
  let finished = false;
  let resolveDone: (played: boolean) => void = () => {};
  const done = new Promise<boolean>((resolve) => { resolveDone = resolve; });

  const finish = () => {
    if (finished) return;
    finished = true;
    sources.forEach((source) => source.stop());
    sources.clear();
    if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) socket.close();
    void context.close();
    resolveDone(played);
  };
  const finishWhenPlayed = () => {
    if (streamEnded && sources.size === 0) finish();
  };

  const play = (base64: string) => {
    const samples = pcmSamples(base64);
    if (samples.length === 0) return;
    const buffer = context.createBuffer(1, samples.length, SAMPLE_RATE);
    buffer.getChannelData(0).set(samples);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    playAt = Math.max(playAt, context.currentTime);
    source.start(playAt);
    playAt += buffer.duration;
    played = true;
    sources.add(source);
    source.onended = () => {
      sources.delete(source);
      finishWhenPlayed();
    };
  };

  void context.resume();
  socket.onopen = () => {
    // The protocol: a first message of one space, the text (flush: speak it
    // all now), then an empty text to end the stream.
    socket.send(JSON.stringify({ text: ' ' }));
    socket.send(JSON.stringify({ text: `${speech.text} `, flush: true }));
    socket.send(JSON.stringify({ text: '' }));
  };
  socket.onmessage = (event: MessageEvent) => {
    if (finished) return;
    const message = streamMessage(event.data);
    if (!message) return;
    if (message.audio) play(message.audio);
    if (message.isFinal) {
      streamEnded = true;
      finishWhenPlayed();
    }
  };
  socket.onerror = () => {
    streamEnded = true;
    finishWhenPlayed();
  };
  socket.onclose = () => {
    streamEnded = true;
    finishWhenPlayed();
  };

  return { done, stop: finish };
}
