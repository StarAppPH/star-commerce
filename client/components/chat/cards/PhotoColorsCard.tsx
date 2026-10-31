'use client';
import { useState } from 'react';
import { CheckIcon, PaletteIcon, TriangleAlertIcon } from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ChatCard, SentNote } from './ChatCard';
import type { ComponentProps } from './types';

type Role = 'primary' | 'secondary';

const ROLES: { role: Role; label: string }[] = [
  { role: 'primary', label: 'Primary' },
  { role: 'secondary', label: 'Secondary' },
];

// The colors findColorsInPhoto read from a photo the owner attached, as
// swatches with their hex codes. The owner taps a slot, then a swatch, to
// pick the store's primary and secondary colors.
export function PhotoColorsCard({ component, answered, busy, onAnswer }: ComponentProps<'photoColors'>) {
  const [picked, setPicked] = useState<Record<Role, string | null>>({ primary: null, secondary: null });
  const [active, setActive] = useState<Role>('primary');
  const locked = answered || busy;
  const title = component.focus && component.focusFound ? `Colors in the ${component.focus}` : 'Colors in your photo';

  function pick(hex: string) {
    const other: Role = active === 'primary' ? 'secondary' : 'primary';
    setPicked((current) => ({ ...current, [active]: hex, [other]: current[other] === hex ? null : current[other] }));
    if (active === 'primary' && picked.secondary === null) setActive('secondary');
  }

  return (
    <ChatCard
      dataComponent="photoColors"
      icon={<PaletteIcon />}
      title={title}
      footer={answered ? <SentNote text="Colors picked" /> : (
        <>
          <span className="me-auto text-muted-foreground text-xs">Tap a slot, then a color.</span>
          <Button
            data-component="photoColorsSend"
            disabled={(picked.primary === null && picked.secondary === null) || busy}
            onClick={() => onAnswer({ kind: 'brandColors', formId: component.formId, primaryColor: picked.primary, secondaryColor: picked.secondary })}
          >
            Use these colors
          </Button>
        </>
      )}
    >
      {component.focusFound === false && (
        <Alert data-component="photoColorsFocusNotFound" variant="warning" className="m-3 mb-0 w-auto">
          <TriangleAlertIcon />
          <AlertDescription>{`Couldn't find the ${component.focus} in the photo, so these are the whole photo's colors.`}</AlertDescription>
        </Alert>
      )}
      {!answered && (
        <div role="radiogroup" aria-label="Color to pick" className="flex gap-2 px-3 pt-3">
          {ROLES.map(({ role, label }) => (
            <button
              key={role}
              type="button"
              role="radio"
              aria-checked={active === role}
              data-component={`photoColors${label}Slot`}
              disabled={locked}
              onClick={() => setActive(role)}
              className={cn(
                'flex flex-1 items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default',
                active === role && 'border-primary ring-1 ring-primary',
              )}
            >
              <span
                aria-hidden
                className={cn('size-5 shrink-0 rounded-md border', picked[role] === null && 'border-dashed bg-muted')}
                style={picked[role] ? { backgroundColor: picked[role] } : undefined}
              />
              <span className="min-w-0">
                <span className="block font-medium">{label}</span>
                <span className="block font-mono text-muted-foreground text-xs">{picked[role] ?? 'Not picked'}</span>
              </span>
            </button>
          ))}
        </div>
      )}
      <ul aria-label="Colors" className="grid grid-cols-3 gap-2 p-3 sm:grid-cols-6">
        {component.colors.map((color) => {
          const role = ROLES.find(({ role: r }) => picked[r] === color.hex);
          return (
            <li key={color.hex}>
              <button
                type="button"
                data-component="photoColorsSwatch"
                aria-label={`${color.hex}, ${Math.round(color.share * 100)}% of the photo${role ? `, ${role.label.toLowerCase()}` : ''}`}
                disabled={locked}
                onClick={() => pick(color.hex)}
                className="flex w-full flex-col gap-1 rounded-lg text-left outline-none ring-offset-2 ring-offset-background focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default"
              >
                <span
                  aria-hidden
                  className={cn('relative block aspect-square w-full rounded-lg border', role && 'ring-2 ring-primary')}
                  style={{ backgroundColor: color.hex }}
                >
                  {role && (
                    <span className="absolute top-1.5 right-1.5 flex items-center gap-1 rounded-md bg-background/85 px-1.5 py-0.5 text-xs">
                      <CheckIcon className="size-3" />
                      {role.label}
                    </span>
                  )}
                </span>
                <span className="font-mono text-xs">{color.hex}</span>
                <span className="text-muted-foreground text-xs">
                  {`${Math.round(color.share * 100)}%${color.neutral ? ' · neutral' : ''}`}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </ChatCard>
  );
}
