'use client';
import { useState } from 'react';
import { Undo2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldLabel } from '@/components/ui/field';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import { ChatCard, QuantityStepper, SentNote } from './ChatCard';
import { formatMoney, nonEmpty, orderRef } from './format';
import type { AnswerOf, ComponentProps } from './types';

type Reason = AnswerOf<'return'>['reason'];

const REASONS: { value: Reason; label: string }[] = [
  { value: 'defective', label: 'It’s defective' },
  { value: 'wrongItem', label: 'Wrong item' },
  { value: 'changedMind', label: 'I changed my mind' },
];
// The server's limit on a return note (RETURN_NOTE_MAX_LENGTH).
const NOTE_MAX = 300;

// Which items of a completed order go back, and why.
export function ReturnForm({ component, answered, busy, onAnswer }: ComponentProps<'returnForm'>) {
  const [quantities, setQuantities] = useState<Record<number, number>>({});
  const [reason, setReason] = useState<Reason | ''>('');
  const [note, setNote] = useState('');
  const locked = answered || busy;
  const picked = component.items.filter((item) => (quantities[item.item] ?? 0) > 0);
  const refund = picked.reduce((sum, item) => sum + item.price * (quantities[item.item] ?? 0), 0);
  const ready = picked.length > 0 && reason !== '';

  const send = () => {
    const items = nonEmpty(picked.map((item) => ({ item: item.item, quantity: quantities[item.item] ?? 0 })));
    if (reason === '' || !items) return;
    onAnswer({
      kind: 'return',
      formId: component.formId,
      items,
      reason,
      note: note.trim() === '' ? null : note.trim(),
    });
  };

  return (
    <ChatCard
      dataComponent="returnForm"
      icon={<Undo2Icon />}
      title={`Return from order ${orderRef(component.orderId)}`}
      footer={answered ? <SentNote text="Return request sent" /> : (
        <>
          {picked.length > 0 && (
            <span className="me-auto text-muted-foreground text-sm">Refund {formatMoney(refund, component.currency)}</span>
          )}
          <Button data-component="returnFormSend" disabled={!ready || busy} onClick={send}>Request the return</Button>
        </>
      )}
    >
      <ul className="divide-y border-b">
        {component.items.map((item) => (
          <li key={item.item} className="flex items-center gap-3 px-4 py-3">
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="truncate font-medium text-sm">{item.name}</span>
              <span className="text-muted-foreground text-xs">
                {item.quantity} ordered · {formatMoney(item.price, component.currency)} each
              </span>
            </div>
            {!answered && (
              <QuantityStepper
                dataComponent="returnFormQuantity"
                label={item.name}
                value={quantities[item.item] ?? 0}
                max={item.quantity}
                disabled={busy}
                onChange={(value) => setQuantities((current) => ({ ...current, [item.item]: value }))}
              />
            )}
          </li>
        ))}
      </ul>
      {!answered && (
        <div className="flex flex-col gap-4 p-4">
          <Field>
            <FieldLabel>Why is it going back?</FieldLabel>
            <RadioGroup
              data-component="returnFormReason"
              aria-label="Reason"
              value={reason}
              onValueChange={(value) => setReason(value as Reason)}
              className="flex flex-row flex-wrap gap-2"
            >
              {REASONS.map((option) => (
                <Label key={option.value} className="flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 font-normal has-data-checked:border-primary">
                  <RadioGroupItem value={option.value} disabled={locked} />
                  {option.label}
                </Label>
              ))}
            </RadioGroup>
          </Field>
          <Field>
            <FieldLabel>A note for the store <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
            <Textarea data-component="returnFormNote" value={note} maxLength={NOTE_MAX} disabled={locked} onChange={(event) => setNote(event.target.value)} />
          </Field>
        </div>
      )}
    </ChatCard>
  );
}
