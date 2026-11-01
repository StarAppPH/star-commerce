'use client';
import { useId, useState } from 'react';
import Image from 'next/image';
import { ImagePlusIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { auth } from '@/lib/firebase';
import { PHOTOS_PER_CALL, PHOTO_REFERENCES_MAX, PHOTO_UPLOAD_MAX_BYTES } from '@/lib/generated/serverConstants';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { useObjectUrls } from './useObjectUrls';
import { PhotoPromptBox, promptAnswer } from './PhotoPromptBox';
import { PHOTO_ACCEPT, photoProblem, uploadChatPhoto } from './photoUpload';
import type { AnswerOf, ComponentProps } from './types';

type ImageUrls = AnswerOf<'photoRequest'>['imageUrls'];

// The uploaded photos' URLs as the answer types them: 1 to PHOTO_REFERENCES_MAX.
function imageUrlsOf(urls: string[]): ImageUrls | null {
  return urls.length >= 1 && urls.length <= PHOTO_REFERENCES_MAX ? (urls as ImageUrls) : null;
}


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
  const [prompt, setPrompt] = useState(component.defaultPrompt ?? '');
  const [uploading, setUploading] = useState(false);
  const locked = answered || busy || uploading;
  const previews = useObjectUrls();

  const remove = (photo: Picked) => {
    previews.release(photo.preview);
    setPicked((current) => current.filter((other) => other !== photo));
  };

  const add = (files: FileList | null) => {
    if (!files) return;
    const accepted: Picked[] = [];
    // Previews are made only for the files that fit.
    const room = PHOTO_REFERENCES_MAX - picked.length;
    for (const file of Array.from(files)) {
      const problem = photoProblem(file);
      if (problem) {
        showSnackbar(problem, 'error');
        continue;
      }
      if (accepted.length < room) accepted.push({ file, preview: previews.create(file) });
    }
    setPicked((current) => [...current, ...accepted]);
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
        prompt: promptAnswer(prompt, component.defaultPrompt ?? null),
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
          Make {PHOTOS_PER_CALL} photos
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
            {picked.length < PHOTO_REFERENCES_MAX && (
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
          <p className="text-muted-foreground text-xs">Up to {PHOTO_REFERENCES_MAX} JPG, PNG or WebP photos of the same item, each under {PHOTO_UPLOAD_MAX_BYTES / (1024 * 1024)} MB.</p>
          <PhotoPromptBox
            dataComponent="photoRequestPrompt"
            value={prompt}
            defaultValue={component.defaultPrompt ?? ''}
            disabled={locked}
            onChange={setPrompt}
          />
        </div>
      )}
    </ChatCard>
  );
}
