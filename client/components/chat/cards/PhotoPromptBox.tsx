'use client';
import type { PhotoShotRow } from '@/app/schemas/PhotoShotRowSchema';
import { useId, useState } from 'react';
import { RotateCcwIcon, SparklesIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { InputGroup, InputGroupAddon, InputGroupTextarea } from '@/components/ui/input-group';
import { cn } from '@/lib/utils';
import { PHOTOS_PER_CALL, PHOTO_PROMPT_MAX_LENGTH } from '@/lib/generated/serverConstants';

const PHOTO_PROMPT_WARN_AT = 1800;

// What the answer sends: null (the server's default) for a blank or unchanged box.
export function promptAnswer(value: string, defaultValue: string | null): string | null {
  const trimmed = value.trim();
  return trimmed === '' || trimmed === (defaultValue ?? '').trim() ? null : trimmed;
}

interface PhotoPromptBoxProps {
  value: string;
  defaultValue: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  dataComponent?: string;
  // The submit button, when the box has its own.
  id?: string;
  // The fixed shot each photo adds to the prompt, shown read-only (none on older cards).
  shots?: readonly PhotoShotRow[];
  submit?: { label: string; loading?: boolean; onSubmit: () => void };
  // Hide the "Photo prompt" label (still read by screen readers) under a heading that says it.
  hideLabel?: boolean;
}

// The prompt the photos are made from: starts as our default, the owner may
// edit it. One prompt makes PHOTOS_PER_CALL photos.
export function PhotoPromptBox({ value, defaultValue, onChange, disabled = false, dataComponent = 'photoPromptBox', submit, id, shots = [], hideLabel = false }: PhotoPromptBoxProps) {
  const unchanged = value === defaultValue;
  const labelId = useId();
  // The shot whose line shows under the chips (none until one is tapped).
  const [openShot, setOpenShot] = useState<number | null>(null);
  const shown = openShot === null ? null : shots[openShot];
  return (
    <div id={id} data-component={dataComponent} className="flex flex-col gap-3">
      <Field>
        <FieldLabel id={labelId} className={hideLabel ? 'sr-only' : undefined}>Photo prompt</FieldLabel>
        <InputGroup>
          <InputGroupTextarea
            data-component={`${dataComponent}Text`}
            aria-labelledby={labelId}
            value={value}
            maxLength={PHOTO_PROMPT_MAX_LENGTH}
            rows={6}
            className="field-sizing-fixed max-h-56 overflow-y-auto"
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          />
          <InputGroupAddon align="block-end" className="justify-between">
            <Button
              data-component={`${dataComponent}Reset`}
              type="button"
              variant="ghost"
              size="xs"
              disabled={disabled || unchanged}
              onClick={() => onChange(defaultValue)}
            >
              <RotateCcwIcon />
              Reset
            </Button>
            <span data-component={`${dataComponent}Count`} className={cn('text-xs tabular-nums', value.length >= PHOTO_PROMPT_WARN_AT ? 'text-warning-foreground' : 'text-muted-foreground')}>
              {value.length}/{PHOTO_PROMPT_MAX_LENGTH}
            </span>
          </InputGroupAddon>
        </InputGroup>
        <FieldDescription>The base of all {PHOTOS_PER_CALL} photos.{shots.length > 0 && ' Each adds its own shot:'}</FieldDescription>
      </Field>
      {shots.length > 0 && (
        <div data-component={`${dataComponent}Shots`} className="flex flex-col gap-2">
          <div className="flex flex-wrap gap-1.5">
            {shots.map((shot, index) => (
              <Button
                key={shot.label}
                data-component={`${dataComponent}Shot${index + 1}`}
                type="button"
                size="xs"
                variant={openShot === index ? 'secondary' : 'outline'}
                aria-pressed={openShot === index}
                onClick={() => setOpenShot((current) => (current === index ? null : index))}
              >
                <span className="text-muted-foreground tabular-nums">{index + 1}</span>
                {shot.label}
              </Button>
            ))}
          </div>
          {shown && <p className="text-muted-foreground text-sm sm:text-xs">{shown.line}</p>}
        </div>
      )}
      {submit && (
        <Button
          data-component={`${dataComponent}Submit`}
          className="self-end"
          loading={submit.loading}
          disabled={disabled}
          onClick={submit.onSubmit}
        >
          <SparklesIcon />
          {submit.label}
        </Button>
      )}
    </div>
  );
}
