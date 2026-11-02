import { getDownloadURL, ref, uploadBytes, type FirebaseStorage } from 'firebase/storage';
import { PHOTO_UPLOAD_MAX_BYTES } from '@/lib/generated/serverConstants';
import { fileProblem } from './fileProblem';
import type { ChatAttachmentRow } from '@/app/schemas/ChatAttachmentRowSchema';

// The files a chat message may carry, as the server checks them
// (objects/chat.py CHAT_ATTACHMENT_*, storage.rules).
export const ATTACHMENTS_MAX = 4;
const PDF_TYPE = 'application/pdf';
const ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/webp', PDF_TYPE];
// The longest file name the server keeps (CHAT_ATTACHMENT_NAME_MAX_LENGTH).
const NAME_MAX_LENGTH = 120;

export type AttachmentType = ChatAttachmentRow['contentType'];

// One file a turn carries, as the chat shows it.
export interface TurnFile {
  name: string;
  contentType: AttachmentType;
  // Where a photo is shown from: its download URL when this page uploaded
  // it, else its Storage path (users/<uid>/chat/...), read when shown.
  url: string | null;
  path: string | null;
}

// A file picked in the composer, not sent yet.
export interface PickedFile {
  file: File;
  // A blob: URL of a photo's preview (null for a PDF).
  preview: string | null;
}

// Files uploaded for one message: their download URLs (what the chat routes
// take) and what the chat shows of them.
export interface UploadedFiles {
  urls: string[];
  files: TurnFile[];
}

export function isAttachmentType(type: string): type is AttachmentType {
  return ALLOWED_TYPES.includes(type);
}

// Why `file` can't be attached, or null when it can.
export function attachmentProblem(file: File): string | null {
  return fileProblem(file, ALLOWED_TYPES, PHOTO_UPLOAD_MAX_BYTES, 'a JPG, PNG, WebP or PDF file');
}

export function isPdf(file: File): boolean {
  return file.type === PDF_TYPE;
}

// The name the file is stored under, which the server keeps as its name:
// letters, digits, spaces and ._-() only, its extension matching its type
// (a camera photo may be named image.jpeg or have none).
function storedName(file: File): string {
  const extensions: Record<string, string> = { [PDF_TYPE]: '.pdf', 'image/png': '.png', 'image/webp': '.webp' };
  const extension = extensions[file.type] ?? '.jpg';
  const base = file.name.normalize('NFC').replace(/\.[^.]*$/, '').replace(/[^\p{L}\p{N} ._()-]/gu, '_').trim();
  return `${(base || 'file').slice(0, NAME_MAX_LENGTH - extension.length)}${extension}`;
}

// Uploads `files` to the user's own chat folder (users/<uid>/chat/, the
// only folder that takes a PDF), each in a folder of its own so its name
// stays as it is.
export async function uploadChatFiles(storage: FirebaseStorage, uid: string, files: File[]): Promise<UploadedFiles> {
  const uploaded = await Promise.all(files.map(async (file) => {
    const folder = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const name = storedName(file);
    const done = await uploadBytes(ref(storage, `users/${uid}/chat/${folder}/${name}`), file, { contentType: file.type });
    const contentType: AttachmentType = isAttachmentType(file.type) ? file.type : 'image/jpeg';
    const url = await getDownloadURL(done.ref);
    return { url, file: { name, contentType, url, path: done.ref.fullPath } };
  }));
  return { urls: uploaded.map((one) => one.url), files: uploaded.map((one) => one.file) };
}

// The download URLs already asked for, by storage and path: re-renders and
// pages of history ask Storage once per file. A failed read is forgotten, so
// the chat opened again tries it again.
const fileUrls = new Map<string, Promise<string>>();

// The download URL of the user's own file at `path`, read with the storage
// the user signed in to (the rules let only the owner read it).
export function ownFileUrl(storage: FirebaseStorage, path: string): Promise<string> {
  const key = `${storage.app.name}:${path}`;
  const known = fileUrls.get(key);
  if (known) return known;
  const asked = getDownloadURL(ref(storage, path));
  fileUrls.set(key, asked);
  asked.catch(() => { fileUrls.delete(key); });
  return asked;
}
