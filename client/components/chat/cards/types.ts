import type { ChatReplyResponse } from '@/app/schemas/ChatReplyResponseSchema';
import type ServerRequests from '@/app/api/ServerRequests';

// A card or form under an assistant's reply (server objects/chatComponents.py).
export type ChatComponent = ChatReplyResponse['components'][number];
export type ChatComponentKind = ChatComponent['kind'];
export type ComponentOf<K extends ChatComponentKind> = Extract<ChatComponent, { kind: K }>;

// What answers a form or a card: the chat routes' `form`.
export type ChatFormAnswer = NonNullable<Parameters<ServerRequests['sendAdminChatMessage']>[3]>;
export type AnswerOf<K extends ChatFormAnswer['kind']> = Extract<ChatFormAnswer, { kind: K }>;

// What every component gets from the chat it is in.
export interface ComponentProps<K extends ChatComponentKind> {
  component: ComponentOf<K>;
  // The form was answered (by a later turn of this chat).
  answered: boolean;
  // An answer is on its way, this one's or another's: nothing more is sent.
  busy: boolean;
  onAnswer: (answer: ChatFormAnswer) => void;
}
