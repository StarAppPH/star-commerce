'use client';
import { useId, useState } from 'react';
import Image from 'next/image';
import { ImagePlusIcon, PlusIcon, SparklesIcon, XIcon } from 'lucide-react';
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

  const fileInput = (
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
  );
  const limits = `Up to ${PHOTO_REFERENCES_MAX} JPG, PNG or WebP photos, ${PHOTO_UPLOAD_MAX_BYTES / (1024 * 1024)} MB each`;

  return (
    <ChatCard
      dataComponent="photoRequestForm"
      icon={<ImagePlusIcon />}
      title="Photos of one item"
      footer={answered ? <SentNote text="Photos sent" /> : (
        <>
          {picked.length === 0 && <span data-component="photoRequestHint" className="me-auto text-muted-foreground text-xs">Add at least 1 photo</span>}
          <Button data-component="photoRequestSend" loading={uploading} disabled={picked.length === 0 || busy} onClick={send}>
            <SparklesIcon />
            Make {PHOTOS_PER_CALL} photos
          </Button>
        </>
      )}
    >
      {!answered && (
        <div className="flex flex-col gap-5 p-4">
          <section data-component="photoRequestPhotos" className="flex flex-col gap-2">
            <StepHeading step={1} title="Add photos of your item" meta={picked.length > 0 ? `${picked.length}/${PHOTO_REFERENCES_MAX}` : undefined} />
            {picked.length === 0 ? (
              <label
                htmlFor={inputId}
                data-component="photoRequestDropZone"
                className="flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed bg-muted/40 px-4 py-8 text-center hover:bg-accent/50 has-disabled:pointer-events-none has-disabled:opacity-64"
              >
                <span className="flex size-10 items-center justify-center rounded-full bg-background shadow-xs">
                  <ImagePlusIcon className="size-5 text-muted-foreground" />
                </span>
                <span className="font-medium text-sm">Tap to add photos</span>
                <span className="text-muted-foreground text-xs">{limits}. All of the same item.</span>
                {fileInput}
              </label>
            ) : (
              <>
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
                      data-component="photoRequestAddMore"
                      className="flex aspect-square cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed text-muted-foreground text-xs hover:bg-accent/50 has-disabled:pointer-events-none has-disabled:opacity-64"
                    >
                      <PlusIcon className="size-5" />
                      Add more
                      {fileInput}
                    </label>
                  )}
                </div>
                <p className="text-muted-foreground text-xs">{limits}.</p>
              </>
            )}
          </section>
          <section data-component="photoRequestLook" className="flex flex-col gap-2">
            <StepHeading step={2} title="Describe the look" />
            <PhotoPromptBox
              dataComponent="photoRequestPrompt"
              value={prompt}
              defaultValue={component.defaultPrompt ?? ''}
              disabled={locked}
              onChange={setPrompt}
              shots={component.shots}
              hideLabel
            />
          </section>
        </div>
      )}
    </ChatCard>
  );
}

// A numbered step of the form, with an optional count on the right.
function StepHeading({ step, title, meta }: { step: number; title: string; meta?: string }) {
  return (
    <div className="flex items-center gap-2">
      <span aria-hidden="true" className="flex size-5 shrink-0 items-center justify-center rounded-full bg-primary font-medium text-[0.6875rem] text-primary-foreground tabular-nums">
        {step}
      </span>
      <h4 className="flex-1 font-medium text-sm">{title}</h4>
      {meta && <span data-component="photoRequestCount" className="text-muted-foreground text-xs tabular-nums">{meta}</span>}
    </div>
  );
}
