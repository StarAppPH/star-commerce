import { getDownloadURL, ref, uploadBytes, type FirebaseStorage } from 'firebase/storage';
import {
  CHAT_ATTACHMENT_EXTENSIONS, CHAT_ATTACHMENT_NAME_MAX_LENGTH, CHAT_ATTACHMENTS_MAX, CSV_CONTENT_TYPE, PHOTO_UPLOAD_MAX_BYTES,
  XLSX_CONTENT_TYPE,
} from '@/lib/generated/serverConstants';
import { fileProblem } from './fileProblem';
import { PHOTO_ACCEPT } from './cards/photoUpload';
import type { ChatAttachmentRow } from '@/app/schemas/ChatAttachmentRowSchema';

// The files a chat message may carry, as the server checks them
// (objects/chat.py CHAT_ATTACHMENT_*, storage.rules).
export const ATTACHMENTS_MAX = CHAT_ATTACHMENTS_MAX;
// The longest file name the server keeps.
const NAME_MAX_LENGTH = CHAT_ATTACHMENT_NAME_MAX_LENGTH;
const PDF_TYPE = CHAT_ATTACHMENT_EXTENSIONS['.pdf'];
const EXTENSIONS: [string, string][] = Object.entries(CHAT_ATTACHMENT_EXTENSIONS);
const ALLOWED_TYPES = [...new Set(EXTENSIONS.map(([, type]) => type))];
// The extension a type is stored under: its first in the server's map
// (.jpg for image/jpeg).
const EXTENSION_OF = new Map([...EXTENSIONS].reverse().map(([extension, type]) => [type, extension]));
// What the paperclip's file picker offers: the photos, and every other type
// by its extension and its type.
export const ATTACHMENT_ACCEPT = [
  PHOTO_ACCEPT,
  ...EXTENSIONS.filter(([, type]) => !isPhotoType(type)).map(([extension, type]) => `${extension},${type}`),
].join(',');
// A spreadsheet's type comes from its extension: browsers report it
// unreliably (Windows Chrome/Edge often call a .csv application/vnd.ms-excel,
// some report nothing). These are the types a browser may report for one;
// it is uploaded as its canonical type, which the server and storage.rules
// check against the extension.
const SPREADSHEETS: { extension: string; type: string; reported: string[] }[] = [
  {
    extension: '.csv',
    type: CSV_CONTENT_TYPE,
    reported: [
      CSV_CONTENT_TYPE, '', 'application/vnd.ms-excel', 'application/csv', 'text/x-csv', 'text/comma-separated-values', 'text/plain',
    ],
  },
  { extension: '.xlsx', type: XLSX_CONTENT_TYPE, reported: [XLSX_CONTENT_TYPE, '', 'application/octet-stream'] },
];

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
  // A blob: URL of a photo's preview (null for a PDF or a spreadsheet).
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

// The type `file` is uploaded as: a spreadsheet's from its extension, any
// other file's as the browser reports it.
function canonicalType(file: File): string {
  const name = file.name.toLowerCase();
  const spreadsheet = SPREADSHEETS.find((one) => name.endsWith(one.extension));
  if (spreadsheet) return spreadsheet.reported.includes(file.type) ? spreadsheet.type : file.type;
  return file.type;
}

// Why `file` can't be attached, or null when it can.
export function attachmentProblem(file: File): string | null {
  if (file.name.toLowerCase().endsWith('.xls')) {
    return `${file.name} is an old Excel file (.xls): save it as .xlsx or .csv, then attach it again.`;
  }
  return fileProblem(
    file, ALLOWED_TYPES, PHOTO_UPLOAD_MAX_BYTES, 'a JPG, PNG, WebP, PDF, CSV or Excel (.xlsx) file', canonicalType(file),
  );
}

export function isPdf(file: File): boolean {
  return isPdfType(canonicalType(file));
}

export function isPdfType(type: string): boolean {
  return type === PDF_TYPE;
}

// Whether `file` is shown as a photo (a preview); a PDF or a spreadsheet is
// shown by name.
export function isPhoto(file: File): boolean {
  return isPhotoType(canonicalType(file));
}

export function isPhotoType(type: string): boolean {
  return type.startsWith('image/');
}

export function isSpreadsheetType(type: string): boolean {
  return SPREADSHEETS.some((one) => one.type === type);
}

// The name the file is stored under, which the server keeps as its name:
// letters, digits, spaces and ._-() only, its extension matching its type
// (a camera photo may be named image.jpeg or have none).
function storedName(file: File, type: string): string {
  const extension = EXTENSION_OF.get(type) ?? '.jpg';
  const base = file.name.normalize('NFC').replace(/\.[^.]*$/, '').replace(/[^\p{L}\p{N} ._()-]/gu, '_').trim();
  return `${(base || 'file').slice(0, NAME_MAX_LENGTH - extension.length)}${extension}`;
}

// Uploads `files` to the user's own chat folder (users/<uid>/chat/, the
// only folder that takes a PDF or a spreadsheet), each in a folder of its
// own so its name stays as it is.
export async function uploadChatFiles(storage: FirebaseStorage, uid: string, files: File[]): Promise<UploadedFiles> {
  const uploaded = await Promise.all(files.map(async (file) => {
    const folder = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const type = canonicalType(file);
    const contentType: AttachmentType = isAttachmentType(type) ? type : 'image/jpeg';
    const name = storedName(file, contentType);
    const done = await uploadBytes(ref(storage, `users/${uid}/chat/${folder}/${name}`), file, { contentType });
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
