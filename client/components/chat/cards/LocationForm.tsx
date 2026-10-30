'use client';
import { zoneAt } from './deliveryZones';
import { zoneRuleText } from './format';
import type { MapPoint } from './MapPicker';
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

// Pin where a delivery goes: the map only, with the store's zones shaded and
// the zone under the pin named with its fee. The assistant asks for the
// address and notes in the chat after.
export function DeliveryPinForm({ component, answered, busy, onAnswer }: ComponentProps<'deliveryPinForm'>) {
  const zones = component.deliveryZones ?? [];
  const check = (point: MapPoint) => {
    const zone = zoneAt(point, zones);
    if (!zone) return { ok: false, text: 'The store doesn’t deliver here. Move the pin inside a shaded zone, or choose pickup.' };
    return { ok: true, text: component.currency ? `${zone.name}: ${zoneRuleText(zone, component.currency)}` : zone.name };
  };
  return (
    <PinCard
      dataComponent="deliveryPinForm"
      title="Where should we deliver?"
      note="Move the map until the pin sits on your door. The store delivers inside the shaded zones."
      start={component.storeLocation}
      startZoom={component.storeLocation ? 14 : 12}
      sendLabel="Deliver here"
      sentText="Pin sent"
      requireMove
      zones={zones}
      check={check}
      answered={answered}
      busy={busy}
      onSend={(point) => onAnswer({ kind: 'deliveryPin', formId: component.formId, ...point })}
    />
  );
}
