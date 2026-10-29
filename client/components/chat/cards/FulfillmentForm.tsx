'use client';
import { useState } from 'react';
import { LocateFixedIcon, StoreIcon, TruckIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import { ChatCard, SentNote } from './ChatCard';
import { deliveryPerKmText, formatMoney } from './format';
import { DEFAULT_MAP_START, MapPicker, type MapPoint } from './MapPicker';
import { mapLink } from './OrderDelivery';
import type { AnswerOf, ComponentProps } from './types';
import { useWhereIAm } from './useWhereIAm';

type Fulfillment = AnswerOf<'fulfillment'>['fulfillment'];

// The server's limits (objects/order.py).
const ADDRESS_MAX = 200;
const NOTES_MAX = 300;

function days(count: number): string {
  return count === 1 ? '1 day' : `${count} days`;
}

// Pickup or delivery, and for a delivery where: the address, a pin on the
// map and notes for the rider.
export function FulfillmentForm({ component, answered, busy, onAnswer }: ComponentProps<'fulfillmentForm'>) {
  const options: Fulfillment[] = [...component.options];
  const [choice, setChoice] = useState<Fulfillment | ''>(options.length === 1 ? options[0] : '');
  const [address, setAddress] = useState('');
  const [notes, setNotes] = useState('');
  const start = component.storeLocation ?? DEFAULT_MAP_START;
  const [point, setPoint] = useState<MapPoint>(start);
  const [target, setTarget] = useState<MapPoint | null>(null);
  const { locating, locate } = useWhereIAm((here) => {
    setTarget(here);
    setPoint(here);
  });
  const locked = answered || busy;
  const money = (amount: number) => formatMoney(amount, component.currency);
  const ready = choice === 'pickup' || (choice === 'delivery' && address.trim() !== '');

  const deliveryTerms = [
    `${deliveryPerKmText(component.deliveryFeePerKm, component.currency)} from the store`,
    component.freeDeliveryMinimum !== null ? `free from ${money(component.freeDeliveryMinimum)}` : null,
    component.deliveryAreas ? `to ${component.deliveryAreas}` : null,
  ].filter(Boolean).join(' · ');

  const send = () => {
    if (choice === 'pickup') {
      onAnswer({ kind: 'fulfillment', formId: component.formId, fulfillment: 'pickup', delivery: null });
    } else if (choice === 'delivery' && address.trim() !== '') {
      onAnswer({
        kind: 'fulfillment',
        formId: component.formId,
        fulfillment: 'delivery',
        delivery: {
          address: address.trim(),
          ...point,
          notes: notes.trim() === '' ? null : notes.trim(),
        },
      });
    }
  };

  return (
    <ChatCard
      dataComponent="fulfillmentForm"
      icon={<TruckIcon />}
      title="Pickup or delivery"
      footer={answered ? <SentNote text="Sent" /> : (
        <Button data-component="fulfillmentFormSend" disabled={!ready || busy} onClick={send}>
          {choice === 'delivery' ? 'Deliver here' : choice === 'pickup' ? 'I’ll pick it up' : 'Choose one'}
        </Button>
      )}
    >
      <div className="flex flex-col gap-4 p-4">
        <RadioGroup
          data-component="fulfillmentFormChoice"
          aria-label="Pickup or delivery"
          value={choice}
          onValueChange={(value) => setChoice(value as Fulfillment)}
          className="gap-2"
        >
          {options.includes('pickup') && (
            <Label className="flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 font-normal has-data-checked:border-primary has-data-checked:bg-accent/50">
              <StoreIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="font-medium">Pick it up</span>
                <span className="text-muted-foreground text-xs">
                  {component.storeAddress ?? 'At the store'}
                  {component.storeLocation && (
                    <>
                      {' · '}
                      <a
                        data-component="fulfillmentFormStoreMap"
                        className="underline-offset-2 hover:underline"
                        href={mapLink(component.storeLocation.latitude, component.storeLocation.longitude)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        map
                      </a>
                    </>
                  )}
                </span>
              </span>
              <RadioGroupItem value="pickup" disabled={locked} />
            </Label>
          )}
          {options.includes('delivery') && (
            <Label className="flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 font-normal has-data-checked:border-primary has-data-checked:bg-accent/50">
              <TruckIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="font-medium">Deliver it</span>
                {deliveryTerms && <span className="text-muted-foreground text-xs">{deliveryTerms}</span>}
              </span>
              <RadioGroupItem value="delivery" disabled={locked} />
            </Label>
          )}
        </RadioGroup>
        {component.handoverDays !== null && (
          <p className="text-muted-foreground text-xs">
            {component.handoverDays === 0
              ? 'Ready the day you pay.'
              : `Ready within ${days(component.handoverDays)} of paying.`}
          </p>
        )}
        {choice === 'delivery' && !answered && (
          <div className="flex flex-col gap-4">
            <Field>
              <FieldLabel>Delivery address</FieldLabel>
              <Input
                data-component="fulfillmentFormAddress"
                value={address}
                maxLength={ADDRESS_MAX}
                placeholder="House no., street, barangay, city"
                autoComplete="street-address"
                disabled={locked}
                onChange={(event) => setAddress(event.target.value)}
              />
            </Field>
            <Field>
              <FieldLabel>Pin the spot</FieldLabel>
              <div className="w-full overflow-hidden rounded-lg border">
                <MapPicker
                  dataComponent="fulfillmentFormMap"
                  start={start}
                  startZoom={component.storeLocation ? 14 : 12}
                  target={target}
                  onMove={setPoint}
                  disabled={locked}
                />
              </div>
              <div className="flex w-full items-center gap-2">
                <FieldDescription className="me-auto tabular-nums">
                  {point.latitude.toFixed(5)}, {point.longitude.toFixed(5)}
                </FieldDescription>
                <Button
                  data-component="fulfillmentFormHere"
                  variant="outline"
                  size="sm"
                  loading={locating}
                  disabled={locked}
                  onClick={locate}
                >
                  <LocateFixedIcon />
                  Use where I am
                </Button>
              </div>
            </Field>
            <Field>
              <FieldLabel>Notes for the rider <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
              <Textarea
                data-component="fulfillmentFormNotes"
                value={notes}
                maxLength={NOTES_MAX}
                placeholder="A landmark, a gate code"
                disabled={locked}
                onChange={(event) => setNotes(event.target.value)}
              />
            </Field>
          </div>
        )}
      </div>
    </ChatCard>
  );
}
