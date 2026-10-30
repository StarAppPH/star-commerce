// The owner's photo uploads: what a chat form takes (JPG, PNG or WebP under 10 MB,
// never HEIC) and where it goes (their own folder, server photoStorage.py).
import { getDownloadURL, ref, uploadBytes } from 'firebase/storage';
import type { User } from 'firebase/auth';
import { storage } from '@/lib/firebase';

// The server's limits (photoStorage.py, storage.rules).
export const PHOTO_BYTES_MAX = 10 * 1024 * 1024;
export const PHOTO_ACCEPT = 'image/jpeg,image/png,image/webp';
const HEIC_TYPES = ['image/heic', 'image/heif'];
const HEIC_EXTENSIONS = ['.heic', '.heif'];
const ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

// Why a photo can't be uploaded, or null when it can.
export function photoProblem(file: File): string | null {
  const name = file.name.toLowerCase();
  if (HEIC_TYPES.includes(file.type) || HEIC_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return `${file.name} is a HEIC photo. Convert it to JPG, PNG or WebP before uploading.`;
  }
  if (!ALLOWED_TYPES.includes(file.type)) return `${file.name} is not a JPG, PNG or WebP photo.`;
  if (file.size >= PHOTO_BYTES_MAX) return `${file.name} is over 10 MB.`;
  return null;
}

// Upload a photo that passed photoProblem to the user's own chat-photos
// folder; its download URL.
export async function uploadChatPhoto(user: User, file: File): Promise<string> {
  const problem = photoProblem(file);
  if (problem) throw new Error(problem);
  const extension = file.type === 'image/png' ? 'png' : file.type === 'image/webp' ? 'webp' : 'jpg';
  const path = `users/${user.uid}/chat-photos/${Date.now()}-${Math.random().toString(36).slice(2)}.${extension}`;
  const done = await uploadBytes(ref(storage, path), file, { contentType: file.type });
  return getDownloadURL(done.ref);
}
