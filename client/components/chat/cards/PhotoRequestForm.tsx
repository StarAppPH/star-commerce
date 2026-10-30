'use client';
import { useEffect, useId, useRef, useState } from 'react';
import Image from 'next/image';
import { ImagePlusIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Textarea } from '@/components/ui/textarea';
import { auth } from '@/lib/firebase';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { PHOTO_ACCEPT, photoProblem, uploadChatPhoto } from './photoUpload';
import type { AnswerOf, ComponentProps } from './types';

type ImageUrls = AnswerOf<'photoRequest'>['imageUrls'];

// The uploaded photos' URLs as the answer types them: 1 to PHOTOS_MAX.
function imageUrlsOf(urls: string[]): ImageUrls | null {
  return urls.length >= 1 && urls.length <= PHOTOS_MAX ? (urls as ImageUrls) : null;
}

// The server's limits (objects/photo.py).
const PHOTOS_MAX = 10;
const NOTES_MAX = 300;
// Every photo call makes this many (PHOTOS_PER_CALL).
const PHOTOS_MADE = 3;

interface Picked {
  file: File;
  preview: string;
}

// The owner's plain photos of one item, uploaded to their own folder, then
// turned into product photos by the assistant.
export function PhotoRequestForm({ component, answered, busy, onAnswer }: ComponentProps<'photoRequestForm'>) {
  const { showSnackbar } = useSnackbar();
  const inputId = useId();
  const [picked, setPicked] = useState<Picked[]>([]);
  const [notes, setNotes] = useState('');
  const [uploading, setUploading] = useState(false);
  const locked = answered || busy || uploading;
  const pickedRef = useRef<Picked[]>([]);

  // The previews still shown are let go when the form goes.
  useEffect(() => { pickedRef.current = picked; }, [picked]);
  useEffect(() => () => { pickedRef.current.forEach((photo) => URL.revokeObjectURL(photo.preview)); }, []);

  const remove = (photo: Picked) => {
    URL.revokeObjectURL(photo.preview);
    setPicked((current) => current.filter((other) => other !== photo));
  };

  const add = (files: FileList | null) => {
    if (!files) return;
    const accepted: Picked[] = [];
    for (const file of Array.from(files)) {
      const problem = photoProblem(file);
      if (problem) {
        showSnackbar(problem, 'error');
        continue;
      }
      accepted.push({ file, preview: URL.createObjectURL(file) });
    }
    setPicked((current) => {
      const room = PHOTOS_MAX - current.length;
      accepted.slice(room).forEach((photo) => URL.revokeObjectURL(photo.preview));
      return [...current, ...accepted.slice(0, room)];
    });
  };

  const send = async () => {
    const user = auth.currentUser;
    if (!user) {
      showSnackbar('Sign in again to upload photos.', 'error');
      return;
    }
    setUploading(true);
    try {
      const uploaded = await Promise.all(picked.map(({ file }) => uploadChatPhoto(user, file)));
      const imageUrls = imageUrlsOf(uploaded);
      if (!imageUrls) return;
      onAnswer({
        kind: 'photoRequest',
        formId: component.formId,
        imageUrls,
        notes: notes.trim() === '' ? null : notes.trim(),
      });
    } catch {
      showSnackbar('The photos didn’t upload. Check your connection and try again.', 'error');
    } finally {
      setUploading(false);
    }
  };

  return (
    <ChatCard
      dataComponent="photoRequestForm"
      icon={<ImagePlusIcon />}
      title="Photos of one item"
      footer={answered ? <SentNote text="Photos sent" /> : (
        <Button data-component="photoRequestSend" loading={uploading} disabled={picked.length === 0 || busy} onClick={send}>
          Make {PHOTOS_MADE} photos
        </Button>
      )}
    >
      {!answered && (
        <div className="flex flex-col gap-4 p-4">
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
            {picked.map((photo, index) => (
              <div key={photo.preview} className="relative aspect-square overflow-hidden rounded-lg bg-muted">
                <Image
                  src={photo.preview}
                  alt={`Photo ${index + 1}`}
                  fill
                  sizes="120px"
                  // A preview of a file not uploaded yet (a blob: URL in this
                  // browser): no server can fetch it to optimize.
                  // eslint-disable-next-line no-restricted-syntax
                  unoptimized
                  className="object-cover"
                />
                <Button
                  data-component="photoRequestRemove"
                  variant="secondary"
                  size="icon-xs"
                  aria-label={`Remove photo ${index + 1}`}
                  className="absolute top-1 right-1"
                  disabled={locked}
                  onClick={() => remove(photo)}
                >
                  <XIcon />
                </Button>
              </div>
            ))}
            {picked.length < PHOTOS_MAX && (
              <label
                htmlFor={inputId}
                className="flex aspect-square cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed text-muted-foreground text-xs hover:bg-accent/50 has-disabled:pointer-events-none has-disabled:opacity-64"
              >
                <ImagePlusIcon className="size-5" />
                Add photos
                <input
                  id={inputId}
                  data-component="photoRequestFiles"
                  type="file"
                  accept={PHOTO_ACCEPT}
                  multiple
                  className="sr-only"
                  disabled={locked}
                  onChange={(event) => { add(event.target.files); event.target.value = ''; }}
                />
              </label>
            )}
          </div>
          <p className="text-muted-foreground text-xs">Up to 10 JPG, PNG or WebP photos of the same item, each under 10 MB.</p>
          <Field>
            <FieldLabel>Special request <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
            <Textarea data-component="photoRequestNotes" value={notes} maxLength={NOTES_MAX} placeholder="Show it from above" disabled={locked} onChange={(event) => setNotes(event.target.value)} />
            <FieldDescription>Photo 1 uses your brand colors; photos 2 and 3 also use your store’s photo style.</FieldDescription>
          </Field>
        </div>
      )}
    </ChatCard>
  );
}
