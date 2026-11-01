'use client';
import { useId, useState } from 'react';
import Image from 'next/image';
import { SparklesIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { PHOTOS_PER_CALL } from '@/lib/generated/serverConstants';
import { ChatCard, SentNote } from './ChatCard';
import { PhotoPromptBox, promptAnswer } from './PhotoPromptBox';
import type { ComponentOf, ComponentProps } from './types';

type OfferRow = ComponentOf<'productPhotoOffer'>['products'][number];

interface OfferRowViewProps {
  row: OfferRow;
  // The row's position on the card, for its data-component names.
  index: number;
  shots: ComponentOf<'productPhotoOffer'>['shots'];
  answered: boolean;
  busy: boolean;
  onMake: (row: OfferRow, prompt: string) => void;
}

function OfferRowView({ row, index, shots, answered, busy, onMake }: OfferRowViewProps) {
  const [open, setOpen] = useState(false);
  const boxId = useId();
  const [prompt, setPrompt] = useState(row.defaultPrompt);
  return (
    <li className="flex flex-col gap-3 p-3">
      <div className="flex items-center gap-3">
        <div className="relative size-12 shrink-0 overflow-hidden rounded-md bg-muted">
          <Image src={row.photoUrl} alt={row.productName} fill sizes="48px" className="object-cover" />
        </div>
        <span className="min-w-0 flex-1 truncate font-medium text-sm">{row.productName}</span>
        {answered ? (
          <Button data-component={`productPhotoOfferRequested${index + 1}`} className="shrink-0" size="sm" variant="outline" disabled>Photos requested</Button>
        ) : (
          <Button data-component={`productPhotoOfferOpen${index + 1}`} className="shrink-0" size="sm" variant="outline" aria-expanded={open} aria-controls={open ? boxId : undefined} disabled={busy} onClick={() => setOpen((current) => !current)}>
            {open ? 'Hide prompt' : 'Make better photos'}
          </Button>
        )}
      </div>
      {open && !answered && (
        <PhotoPromptBox
          id={boxId}
          dataComponent={`productPhotoOfferPrompt${index + 1}`}
          value={prompt}
          defaultValue={row.defaultPrompt}
          disabled={busy}
          onChange={setPrompt}
          shots={shots}
          submit={{ label: `Make ${PHOTOS_PER_CALL} photos`, loading: busy, onSubmit: () => onMake(row, prompt) }}
        />
      )}
    </li>
  );
}

interface ProductPhotoOfferCardProps extends ComponentProps<'productPhotoOffer'> {
  // Each row is answered on its own, by its formId.
  answeredFormIds: ReadonlySet<string>;
}

// After new products: their uploaded photos are already on them; offers
// better photos made from them (optional).
export function ProductPhotoOfferCard({ component, answeredFormIds, busy, onAnswer }: ProductPhotoOfferCardProps) {
  const [dismissed, setDismissed] = useState(false);
  const answeredRows = component.products.filter((row) => answeredFormIds.has(row.formId));
  // "No thanks" hides the rows not answered; the requested ones stay.
  const shown = dismissed ? answeredRows : component.products;
  if (shown.length === 0) return null;
  const allAnswered = answeredRows.length === component.products.length;
  return (
    <ChatCard
      dataComponent="productPhotoOffer"
      icon={<SparklesIcon />}
      title="Better photos (optional)"
      footer={allAnswered || dismissed ? <SentNote text="Photos requested" /> : (
        <Button data-component="productPhotoOfferDismiss" variant="ghost" disabled={busy} onClick={() => setDismissed(true)}>
          No thanks
        </Button>
      )}
    >
      <p className="px-4 pt-3 text-muted-foreground text-sm">Optional: make {PHOTOS_PER_CALL} new photos of each product from its photo. Tap Make better photos to see or change the prompt.</p>
      <ul className="divide-y">
        {shown.map((row) => (
          <OfferRowView
            key={row.formId}
            row={row}
            shots={component.shots}
            index={component.products.indexOf(row)}
            answered={answeredFormIds.has(row.formId)}
            busy={busy}
            onMake={(chosen, prompt) => onAnswer({
              kind: 'makeProductPhotos',
              formId: chosen.formId,
              productId: chosen.productId,
              prompt: promptAnswer(prompt, chosen.defaultPrompt),
            })}
          />
        ))}
      </ul>
    </ChatCard>
  );
}
