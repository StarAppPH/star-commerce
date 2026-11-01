'use client';
import type { PhotoShotRow } from '@/app/schemas/PhotoShotRowSchema';
import { ChevronDownIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Textarea } from '@/components/ui/textarea';
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
}

// The prompt the photos are made from: starts as our default, the owner may
// edit it. One prompt makes PHOTOS_PER_CALL photos.
export function PhotoPromptBox({ value, defaultValue, onChange, disabled = false, dataComponent = 'photoPromptBox', submit, id, shots = [] }: PhotoPromptBoxProps) {
  const unchanged = value === defaultValue;
  return (
    <div id={id} data-component={dataComponent} className="flex flex-col gap-3">
      <Field>
        <FieldLabel>Photo prompt</FieldLabel>
        <Textarea
          data-component={`${dataComponent}Text`}
          value={value}
          maxLength={PHOTO_PROMPT_MAX_LENGTH}
          rows={6}
          className="field-sizing-fixed max-h-56 overflow-y-auto"
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <FieldDescription className="flex items-center justify-between gap-2">
          <span>This is the base for all {PHOTOS_PER_CALL} photos. Each photo also adds the shot below.</span>
          <span data-component={`${dataComponent}Count`} className={value.length >= PHOTO_PROMPT_WARN_AT ? 'text-warning-foreground' : undefined}>{value.length}/{PHOTO_PROMPT_MAX_LENGTH}</span>
        </FieldDescription>
      </Field>
      {shots.length > 0 && (
        <Collapsible defaultOpen data-component={`${dataComponent}Shots`}>
          <CollapsibleTrigger className="group flex items-center gap-1.5 text-muted-foreground text-xs hover:text-foreground">
            <ChevronDownIcon aria-hidden="true" className="size-3.5 transition-transform group-data-panel-open:rotate-180" />
            Each photo adds its own shot
          </CollapsibleTrigger>
          <CollapsiblePanel>
            <ul className="mt-2 flex flex-col gap-1.5 text-muted-foreground text-sm sm:text-xs">
              {shots.map((shot, index) => (
                <li key={shot.label}>
                  <span className="font-medium text-foreground">{index + 1} · {shot.label}</span> — {shot.line}
                </li>
              ))}
            </ul>
          </CollapsiblePanel>
        </Collapsible>
      )}
      <div className="flex items-center gap-2">
        <Button
          data-component={`${dataComponent}Reset`}
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled || unchanged}
          onClick={() => onChange(defaultValue)}
        >
          Reset to default
        </Button>
        {submit && (
          <Button
            data-component={`${dataComponent}Submit`}
            className="ms-auto"
            loading={submit.loading}
            disabled={disabled}
            onClick={submit.onSubmit}
          >
            {submit.label}
          </Button>
        )}
      </div>
    </div>
  );
}
