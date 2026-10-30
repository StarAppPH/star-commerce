'use client';
import { useState } from 'react';
import Image from 'next/image';
import { CheckIcon, ImageIcon, TriangleAlertIcon } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ChatCard, PromptUsed, SentNote } from './ChatCard';
import type { ComponentOf, ComponentProps } from './types';

type Choice = ComponentOf<'productPhotoChoice'>['photos'][number];

// How a photo is named on the card: the original, or styled 1 to 3.
function choiceLabels(photos: Choice[]): string[] {
  let styled = 0;
  return photos.map((photo) => {
    if (photo.source === 'original') return 'Original';
    styled += 1;
    return `Styled ${styled}`;
  });
}

// A new product's photo choices (makeProductPhotoChoices): its original and
// the styled photos made from it in the store's look. The owner picks one
// for the shop.
export function ProductPhotoChoiceCard({ component, answered, busy, onAnswer }: ComponentProps<'productPhotoChoice'>) {
  const [picked, setPicked] = useState<string | null>(null);
  const labels = choiceLabels(component.photos);
  const locked = answered || busy;

  return (
    <ChatCard
      dataComponent="productPhotoChoice"
      icon={<ImageIcon />}
      title={`Photo for ${component.productName}`}
      footer={answered ? <SentNote text="Photo picked" /> : (
        <>
          <span className="me-auto text-muted-foreground text-xs">Pick the photo your shop shows.</span>
          <Button
            data-component="productPhotoChoiceSend"
            disabled={picked === null || busy}
            onClick={() => { if (picked) onAnswer({ kind: 'productPhoto', formId: component.formId, productId: component.productId, photoId: picked }); }}
          >
            Use this photo
          </Button>
        </>
      )}
    >
      {component.notGenerated && (
        <Alert data-component="productPhotoChoiceNotGenerated" variant="warning" className="m-3 mb-0 w-auto">
          <TriangleAlertIcon />
          <AlertDescription>{component.notGenerated}</AlertDescription>
        </Alert>
      )}
      <div role="radiogroup" aria-label={`Photos for ${component.productName}`} className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4">
        {component.photos.map((photo, index) => {
          const selected = picked === photo._id;
          return (
            <button
              key={photo._id}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={labels[index]}
              data-component="productPhotoChoiceOption"
              disabled={locked}
              onClick={() => setPicked(photo._id)}
              className={cn(
                'relative block aspect-square overflow-hidden rounded-lg bg-muted outline-none ring-offset-2 ring-offset-background focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default',
                selected && 'ring-2 ring-primary',
              )}
            >
              <Image src={photo.url} alt={`${component.productName}, ${labels[index].toLowerCase()}`} fill sizes="(min-width: 640px) 160px, 45vw" className="object-cover" />
              <span className="absolute top-1.5 left-1.5 rounded-md bg-background/85 px-1.5 py-0.5 font-medium text-xs">
                {labels[index]}
              </span>
              {selected && (
                <span className="absolute top-1.5 right-1.5 flex size-5 items-center justify-center rounded-full bg-primary text-primary-foreground">
                  <CheckIcon className="size-3.5" />
                </span>
              )}
            </button>
          );
        })}
      </div>
      {component.prompt && <PromptUsed dataComponent="productPhotoChoicePrompt" prompt={component.prompt} />}
    </ChatCard>
  );
}
