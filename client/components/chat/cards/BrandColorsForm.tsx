'use client';
import { useState } from 'react';
import { HexColorPicker } from 'react-colorful';
import { CheckIcon, PaletteIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { ChatCard, SentNote } from './ChatCard';
import type { ComponentProps } from './types';

type Role = 'primary' | 'secondary';

const ROLES: { role: Role; label: string }[] = [
  { role: 'primary', label: 'Primary' },
  { role: 'secondary', label: 'Secondary' },
];

// Where a slot starts when the store has no color and nothing is suggested.
const FALLBACK: Record<Role, string> = { primary: '#1A73E8', secondary: '#FBBC04' };

const HEX_DIGITS = /^[0-9A-F]{6}$/;

// A #RRGGBB color, uppercase (react-colorful answers in lowercase).
function upper(hex: string): string {
  return hex.toUpperCase();
}

// Black or white, whichever reads better on `hex` (WCAG relative luminance).
function textOn(hex: string): string {
  const channel = (offset: number) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  return luminance > 0.179 ? '#000000' : '#FFFFFF';
}

// The store's two brand colors, picked on a color square with a hue slider
// and a hex field each (react-colorful), with the colors the assistant
// suggests as swatches to tap and a small preview in both colors.
export function BrandColorsForm({ component, answered, busy, onAnswer }: ComponentProps<'brandColorsForm'>) {
  const { suggestions } = component;
  const [colors, setColors] = useState<Record<Role, string>>(() => ({
    primary: upper(component.primaryColor ?? suggestions[0] ?? FALLBACK.primary),
    secondary: upper(component.secondaryColor ?? suggestions[1] ?? FALLBACK.secondary),
  }));
  const [active, setActive] = useState<Role>('primary');
  // What the hex field holds while it is typed in: a color only once it has
  // all 6 digits.
  const [draft, setDraft] = useState<string>(colors.primary.slice(1));
  const same = colors.primary === colors.secondary;
  const locked = answered || busy;

  function setColor(hex: string) {
    const color = upper(hex);
    setColors((current) => ({ ...current, [active]: color }));
    setDraft(color.slice(1));
  }

  function choose(role: Role) {
    setActive(role);
    setDraft(colors[role].slice(1));
  }

  function typeHex(value: string) {
    const digits = value.replace(/[^0-9a-f]/gi, '').toUpperCase().slice(0, 6);
    setDraft(digits);
    if (HEX_DIGITS.test(digits)) setColors((current) => ({ ...current, [active]: `#${digits}` }));
  }

  const preview = (
    <div
      data-component="brandColorsFormPreview"
      aria-label="Preview of your shop in these colors"
      className="overflow-hidden rounded-lg border"
    >
      <div className="px-3 py-2 font-medium text-sm" style={{ backgroundColor: colors.primary, color: textOn(colors.primary) }}>
        Your shop
      </div>
      <div className="flex items-center justify-between gap-2 bg-background px-3 py-2">
        <span className="text-muted-foreground text-xs">Buttons and highlights</span>
        <span
          className="rounded-md px-2.5 py-1 font-medium text-xs"
          style={{ backgroundColor: colors.secondary, color: textOn(colors.secondary) }}
        >
          Add to cart
        </span>
      </div>
    </div>
  );

  if (answered) {
    return (
      <ChatCard dataComponent="brandColorsForm" icon={<PaletteIcon />} title="Brand colors" footer={<SentNote text="Colors picked" />}>
        <div className="p-3">{preview}</div>
      </ChatCard>
    );
  }

  return (
    <ChatCard
      dataComponent="brandColorsForm"
      icon={<PaletteIcon />}
      title="Pick your brand colors"
      footer={(
        <>
          <span
            data-component="brandColorsFormHint"
            className={cn('me-auto text-xs', same ? 'font-medium text-destructive-foreground' : 'text-muted-foreground')}
          >
            {same ? 'Pick two different colors.' : 'Tap a slot, then pick its color.'}
          </span>
          <Button
            data-component="brandColorsFormSend"
            disabled={same || locked}
            onClick={() => onAnswer({ kind: 'brandColorsPicked', formId: component.formId, primaryColor: colors.primary, secondaryColor: colors.secondary })}
          >
            Use these colors
          </Button>
        </>
      )}
    >
      <div className="flex flex-col gap-3 p-3">
        <div role="radiogroup" aria-label="Color to pick" className="flex gap-2">
          {ROLES.map(({ role, label }) => (
            <button
              key={role}
              type="button"
              role="radio"
              aria-checked={active === role}
              data-component={`brandColorsForm${label}Slot`}
              disabled={locked}
              onClick={() => choose(role)}
              className={cn(
                'flex flex-1 items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default',
                active === role && 'border-primary ring-1 ring-primary',
              )}
            >
              <span aria-hidden className="size-6 shrink-0 rounded-md border" style={{ backgroundColor: colors[role] }} />
              <span className="min-w-0">
                <span className="block font-medium">{label}</span>
                <span className="block font-mono text-muted-foreground text-xs">{colors[role]}</span>
              </span>
            </button>
          ))}
        </div>

        <div
          data-component="brandColorsFormPicker"
          className={cn(
            // react-colorful injects its own unlayered CSS, which beats
            // Tailwind's layered utilities: hence the ! (important). The
            // square has round corners and a gap above the hue slider, a
            // round bar with a big thumb, easy to drag with a thumb.
            '[&_.react-colorful]:gap-3!',
            '[&_.react-colorful\\_\\_saturation]:rounded-lg! [&_.react-colorful\\_\\_saturation]:border-b-0!',
            '[&_.react-colorful\\_\\_hue]:h-5! [&_.react-colorful\\_\\_hue]:flex-none! [&_.react-colorful\\_\\_hue]:rounded-full!',
            '[&_.react-colorful\\_\\_pointer]:size-7!',
            locked && 'pointer-events-none opacity-64',
          )}
        >
          <HexColorPicker
            color={colors[active]}
            onChange={setColor}
            aria-label={`${active === 'primary' ? 'Primary' : 'Secondary'} color`}
            // The square fills the card's width.
            style={{ width: '100%', height: 232 }}
          />
        </div>

        <label className="flex items-center gap-2">
          <span className="w-10 shrink-0 text-muted-foreground text-sm">Hex</span>
          <span className="relative flex-1">
            <span aria-hidden className="pointer-events-none absolute inset-y-0 left-3 z-10 flex items-center font-mono text-muted-foreground text-sm">#</span>
            <Input
              data-component="brandColorsFormHex"
              aria-label={`${active === 'primary' ? 'Primary' : 'Secondary'} color hex code`}
              aria-invalid={!HEX_DIGITS.test(draft) || undefined}
              value={draft}
              disabled={locked}
              inputMode="text"
              autoCapitalize="characters"
              autoComplete="off"
              spellCheck={false}
              maxLength={7}
              onChange={(event) => typeHex(event.target.value)}
              onBlur={() => setDraft(colors[active].slice(1))}
              className="font-mono [&_input]:ps-6"
            />
          </span>
        </label>

        {suggestions.length > 0 && (
          <div data-component="brandColorsFormSuggestions" className="flex flex-col gap-1.5">
            <span className="text-muted-foreground text-xs">Suggested</span>
            <ul aria-label="Suggested colors" className="flex flex-wrap gap-2">
              {suggestions.map((hex) => {
                const picked = colors[active] === hex;
                return (
                  <li key={hex}>
                    <button
                      type="button"
                      data-component="brandColorsFormSuggestion"
                      aria-label={`Use ${hex}`}
                      aria-pressed={picked}
                      disabled={locked}
                      onClick={() => setColor(hex)}
                      className={cn(
                        'flex size-9 items-center justify-center rounded-full border outline-none ring-offset-2 ring-offset-background focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default',
                        picked && 'ring-2 ring-primary',
                      )}
                      style={{ backgroundColor: hex, color: textOn(hex) }}
                    >
                      {picked && <CheckIcon className="size-4" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {preview}
      </div>
    </ChatCard>
  );
}
