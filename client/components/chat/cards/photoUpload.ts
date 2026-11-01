// The owner's photo uploads: what a chat form takes (JPG, PNG or WebP under 10 MB,
// never HEIC) and where it goes (their own folder, server photoStorage.py).
import { getDownloadURL, ref, uploadBytes, type FirebaseStorage } from 'firebase/storage';
import type { User } from 'firebase/auth';
import { fileProblem } from '../fileProblem';
import { PHOTO_UPLOAD_MAX_BYTES } from '@/lib/generated/serverConstants';
import { storage } from '@/lib/firebase';

// The server's limits (photoStorage.py, storage.rules).
export const PHOTO_ACCEPT = 'image/jpeg,image/png,image/webp';
const ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

// Why a photo can't be uploaded, or null when it can.
export function photoProblem(file: File): string | null {
  return fileProblem(file, ALLOWED_TYPES, PHOTO_UPLOAD_MAX_BYTES, 'a JPG, PNG or WebP photo');
}

// Upload a photo that passed photoProblem to `folder` of the user `uid`'s own
// folder in `storage`; its download URL.
export async function uploadOwnPhoto(storage: FirebaseStorage, uid: string, folder: string, file: File): Promise<string> {
  const problem = photoProblem(file);
  if (problem) throw new Error(problem);
  const extension = file.type === 'image/png' ? 'png' : file.type === 'image/webp' ? 'webp' : 'jpg';
  const path = `users/${uid}/${folder}/${Date.now()}-${Math.random().toString(36).slice(2)}.${extension}`;
  const done = await uploadBytes(ref(storage, path), file, { contentType: file.type });
  return getDownloadURL(done.ref);
}

// Upload a photo that passed photoProblem to the owner's own chat-photos
// folder; its download URL.
export function uploadChatPhoto(user: User, file: File): Promise<string> {
  return uploadOwnPhoto(storage, user.uid, 'chat-photos', file);
}
