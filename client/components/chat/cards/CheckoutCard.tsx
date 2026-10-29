'use client';
import { useEffect, useState } from 'react';
import { CreditCardIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { ChatCard, SentNote } from './ChatCard';
import { OrderDelivery } from './OrderDelivery';
import { PAYMENT_METHOD_LABELS, formatMoney, itemsText, minutesLeft, orderRef } from './format';
import type { ComponentProps } from './types';

// The order a shopper just placed: how long is left to pay, and how to pay.
export function CheckoutCard({ component, answered, busy, onAnswer }: ComponentProps<'checkout'>) {
  const { order } = component;
  const [method, setMethod] = useState<string>(component.paymentMethods[0]);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (answered) return;
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, [answered]);

  const left = minutesLeft(order.expiresAt, now);
  const chosen = component.paymentMethods.find((value) => value === method) ?? component.paymentMethods[0];

  return (
    <ChatCard
      dataComponent="checkoutCard"
      icon={<CreditCardIcon />}
      title={`Order ${orderRef(order._id)}`}
      meta={answered ? undefined : (
        <Badge variant={left <= 5 ? 'error' : 'warning'}>{left > 0 ? `${left} min left to pay` : 'Time is up'}</Badge>
      )}
      footer={answered ? <SentNote text="Payment sent in the chat" /> : (
        <Button
          data-component="checkoutPay"
          className="w-full sm:w-auto"
          disabled={busy || left === 0}
          onClick={() => onAnswer({ kind: 'payment', formId: component.formId, paymentMethod: chosen })}
        >
          Pay {formatMoney(order.total, order.currency)} with {PAYMENT_METHOD_LABELS[chosen] ?? chosen}
        </Button>
      )}
    >
      <div className="flex flex-col gap-4 px-4 py-4">
        <div className="flex items-baseline justify-between gap-3">
          <span className="min-w-0 text-muted-foreground text-sm">{itemsText(order.items)}</span>
          <span className="font-heading text-2xl">{formatMoney(order.total, order.currency)}</span>
        </div>
        <OrderDelivery order={order} currency={order.currency} />
        {!answered && (
          <RadioGroup
            data-component="checkoutMethod"
            aria-label="Payment method"
            value={chosen}
            onValueChange={(value) => setMethod(String(value))}
            className="gap-2"
          >
            {component.paymentMethods.map((value) => (
              <Label
                key={value}
                className="flex cursor-pointer items-center justify-between gap-3 rounded-lg border px-3 py-2.5 has-data-checked:border-primary has-data-checked:bg-accent/50"
              >
                {PAYMENT_METHOD_LABELS[value] ?? value}
                <RadioGroupItem value={value} disabled={busy} />
              </Label>
            ))}
          </RadioGroup>
        )}
      </div>
    </ChatCard>
  );
}
