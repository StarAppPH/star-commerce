'use client';
import Image from 'next/image';
import type { ReactNode } from 'react';
import { CheckIcon, ChevronDownIcon, ImageIcon } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from '@/components/ui/collapsible';
import { NumberField, NumberFieldDecrement, NumberFieldGroup, NumberFieldIncrement, NumberFieldInput } from '@/components/ui/number-field';
import { cn } from '@/lib/utils';

interface ChatCardProps {
  dataComponent: string;
  icon: ReactNode;
  title: string;
  // Beside the title: a count, a status.
  meta?: ReactNode;
  children: ReactNode;
  // The card's action, or "Sent" once it was answered.
  footer?: ReactNode;
  className?: string;
}

// The frame of every card and form under an assistant's reply.
export function ChatCard({ dataComponent, icon, title, meta, children, footer, className }: ChatCardProps) {
  return (
    <Card data-component={dataComponent} className={cn('gap-0 overflow-hidden', className)}>
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <span aria-hidden="true" className="text-muted-foreground [&_svg]:size-4">{icon}</span>
        <h3 className="min-w-0 flex-1 truncate font-medium text-sm">{title}</h3>
        {meta}
      </div>
      {children}
      {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t bg-muted/40 px-4 py-3">{footer}</div>}
    </Card>
  );
}

// What an answered card shows in place of its action.
export function SentNote({ text = 'Sent' }: { text?: string }) {
  return (
    <span className="me-auto inline-flex items-center gap-1.5 text-muted-foreground text-sm">
      <CheckIcon className="size-4 text-success-foreground" />
      {text}
    </span>
  );
}

interface QuantityStepperProps {
  value: number;
  max: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  label: string;
  dataComponent: string;
}

// A small 0..max quantity picker.
export function QuantityStepper({ value, max, onChange, disabled, label, dataComponent }: QuantityStepperProps) {
  return (
    <NumberField
      data-component={dataComponent}
      className="w-28 shrink-0"
      size="sm"
      min={0}
      max={max}
      value={value}
      disabled={disabled || max === 0}
      onValueChange={(next) => onChange(next ?? 0)}
    >
      <NumberFieldGroup>
        <NumberFieldDecrement aria-label={`Fewer ${label}`} />
        <NumberFieldInput aria-label={`How many ${label}`} />
        <NumberFieldIncrement aria-label={`More ${label}`} />
      </NumberFieldGroup>
    </NumberField>
  );
}

interface ProductPhotoProps {
  url: string | null | undefined;
  name: string;
  dataComponent: string;
}

// A product's photo on a card row (setProductPhoto), or a blank tile.
export function ProductPhoto({ url, name, dataComponent }: ProductPhotoProps) {
  return (
    <div data-component={dataComponent} className="relative size-12 shrink-0 overflow-hidden rounded-md bg-muted">
      {url ? (
        <Image src={url} alt={name} fill sizes="48px" className="object-cover" />
      ) : (
        <ImageIcon aria-hidden="true" className="absolute inset-0 m-auto size-4 text-muted-foreground/60" />
      )}
    </div>
  );
}

interface PromptUsedProps {
  prompt: string;
  dataComponent: string;
}

// The prompt the image model was given for a card's photos, folded away.
export function PromptUsed({ prompt, dataComponent }: PromptUsedProps) {
  return (
    <Collapsible data-component={dataComponent} className="border-t">
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 px-4 py-2.5 text-muted-foreground text-xs hover:text-foreground">
        <ChevronDownIcon aria-hidden="true" className="size-3.5 transition-transform group-data-panel-open:rotate-180" />
        Prompt used
      </CollapsibleTrigger>
      <CollapsiblePanel>
        <p className="whitespace-pre-wrap px-4 pb-3 text-muted-foreground text-xs">{prompt}</p>
      </CollapsiblePanel>
    </Collapsible>
  );
}
