'use client';
import { Button } from '@/components/ui/button';
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
  submit?: { label: string; loading?: boolean; onSubmit: () => void };
}

// The prompt the photos are made from: starts as our default, the owner may
// edit it. One prompt makes PHOTOS_PER_CALL photos.
export function PhotoPromptBox({ value, defaultValue, onChange, disabled = false, dataComponent = 'photoPromptBox', submit, id }: PhotoPromptBoxProps) {
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
          <span>One prompt makes {PHOTOS_PER_CALL} photos.</span>
          <span data-component={`${dataComponent}Count`} className={value.length >= PHOTO_PROMPT_WARN_AT ? 'text-warning-foreground' : undefined}>{value.length}/{PHOTO_PROMPT_MAX_LENGTH}</span>
        </FieldDescription>
      </Field>
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
