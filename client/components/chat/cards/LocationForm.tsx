'use client';
import { PinCard } from './PinCard';
import type { ComponentProps } from './types';

// Pin the store: move the map until the pin sits on the door.
export function LocationForm({ component, answered, busy, onAnswer }: ComponentProps<'locationForm'>) {
  return (
    <PinCard
      dataComponent="locationForm"
      title="Pin your store"
      note={component.address ? `Near ${component.address}` : 'Move the map until the pin sits on your door.'}
      start={component.location}
      startZoom={component.location ? 17 : 12}
      sendLabel="Save pin"
      sentText="Pin sent"
      answered={answered}
      busy={busy}
      onSend={(point) => onAnswer({ kind: 'location', formId: component.formId, ...point })}
    />
  );
}

// Pin where a delivery goes: the map only. The assistant asks for the
// address and notes in the chat after.
export function DeliveryPinForm({ component, answered, busy, onAnswer }: ComponentProps<'deliveryPinForm'>) {
  return (
    <PinCard
      dataComponent="deliveryPinForm"
      title="Where should we deliver?"
      note={component.deliveryZones
        ? 'Move the map until the pin sits on your door. The store delivers inside the shaded areas.'
        : 'Move the map until the pin sits on your door.'}
      start={component.storeLocation}
      startZoom={component.storeLocation ? 14 : 12}
      sendLabel="Deliver here"
      sentText="Pin sent"
      requireMove
      zones={component.deliveryZones ?? undefined}
      answered={answered}
      busy={busy}
      onSend={(point) => onAnswer({ kind: 'deliveryPin', formId: component.formId, ...point })}
    />
  );
}
