'use client';
import { useEffect, useState } from 'react';
import { ChevronDownIcon, CreditCardIcon } from 'lucide-react';
import type { PaymentOption } from '@/app/schemas/PaymentOptionSchema';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { ChatCard, SentNote } from './ChatCard';
import { PolicyList } from './InfoCards';
import { OrderDelivery } from './OrderDelivery';
import {
  PAYMENT_METHOD_LABELS, PAYMENT_PROVIDER_LABELS, formatMoney, itemsText, minutesLeft, orderRef, paymentText,
} from './format';
import type { ComponentProps } from './types';

// What paying by hand with `option` takes: the store's accounts, or where.
function manualDetails(option: PaymentOption): string[] {
  if (option.eWallets) return option.eWallets.map((account) => `${account.ewalletName}: ${account.accountNumber}`);
  if (option.banks) return option.banks.map((account) => `${account.bankName}: ${account.accountNumber} (${account.accountName})`);
  if (option.checkPayees) return option.checkPayees.map((payee) => `Pay to the order of ${payee}, at the handover`);
  return ['Paid at the handover'];
}

function optionLabel(option: PaymentOption): string {
  if (option.provider === null) return paymentText(option.method, null);
  return `${PAYMENT_METHOD_LABELS[option.method] ?? option.method} online`;
}

// The order a shopper just placed: how long is left to choose how to pay, the
// store's payment options, and its policy, which the shopper agrees to before
// paying. One paid by hand shows the store's accounts.
export function CheckoutCard({ component, answered, busy, onAnswer }: ComponentProps<'checkout'>) {
  const { order } = component;
  const options = component.paymentOptions ?? [];
  const [picked, setPicked] = useState(0);
  const [agreed, setAgreed] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (answered) return;
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, [answered]);

  const left = minutesLeft(order.expiresAt, now);
  const chosen: PaymentOption | undefined = options[picked] ?? options[0];
  // A card saved before payment options can no longer be answered.
  const legacyMethods = component.paymentMethods ?? [];
  // Nor can one saved before the shopper agreed to the policy on it.
  const policy = component.policy ?? null;
  const payable = chosen !== undefined && policy !== null;

  return (
    <ChatCard
      dataComponent="checkoutCard"
      icon={<CreditCardIcon />}
      title={`Order ${orderRef(order._id)}`}
      meta={answered || !payable ? undefined : (
        <Badge variant={left <= 5 ? 'error' : 'warning'}>{left > 0 ? `${left} min left to choose` : 'Time is up'}</Badge>
      )}
      footer={answered ? <SentNote text="Payment choice sent in the chat" /> : payable ? (
        <Button
          data-component="checkoutPay"
          className="w-full sm:w-auto"
          disabled={busy || left === 0 || !agreed}
          onClick={() => onAnswer({
            kind: 'payment',
            formId: component.formId,
            paymentMethod: chosen.method,
            paymentProvider: chosen.provider,
            agreedToPolicy: true,
          })}
        >
          Pay {formatMoney(order.total, order.currency)} by {optionLabel(chosen).toLowerCase()}
        </Button>
      ) : undefined}
    >
      <div className="flex flex-col gap-4 px-4 py-4">
        <div className="flex items-baseline justify-between gap-3">
          <span className="min-w-0 text-muted-foreground text-sm">{itemsText(order.items)}</span>
          <span className="font-heading text-2xl">{formatMoney(order.total, order.currency)}</span>
        </div>
        <OrderDelivery order={order} currency={order.currency} />
        {!answered && payable && options.length > 0 && (
          <RadioGroup
            data-component="checkoutMethod"
            aria-label="Payment method"
            value={String(picked)}
            onValueChange={(value) => setPicked(Number(value))}
            className="gap-2"
          >
            {options.map((option, index) => (
              <Label
                key={`${option.method}-${option.provider ?? 'manual'}`}
                className="flex cursor-pointer items-center justify-between gap-3 rounded-lg border px-3 py-2.5 has-data-checked:border-primary has-data-checked:bg-accent/50"
              >
                <span className="flex flex-col gap-0.5">
                  {optionLabel(option)}
                  <span className="text-muted-foreground text-xs">
                    {option.provider === null
                      ? 'The store confirms it once it arrives'
                      : `Through ${PAYMENT_PROVIDER_LABELS[option.provider] ?? option.provider}`}
                  </span>
                </span>
                <RadioGroupItem value={String(index)} disabled={busy} />
              </Label>
            ))}
          </RadioGroup>
        )}
        {!answered && payable && chosen.provider === null && (
          <ul data-component="checkoutPayTo" className="flex flex-col gap-1 rounded-lg bg-muted px-3 py-2.5 text-sm">
            {manualDetails(chosen).map((line) => <li key={line}>{line}</li>)}
            {chosen.method !== 'cash' && (
              <li className="text-muted-foreground text-xs">Once you’ve paid, send a photo of the receipt from your order.</li>
            )}
          </ul>
        )}
        {!answered && payable && (
          <div data-component="checkoutPolicy" className="overflow-hidden rounded-lg border">
            <Collapsible>
              <CollapsibleTrigger className="group flex w-full items-center gap-1.5 px-3 py-2.5 text-sm hover:bg-accent/50">
                <ChevronDownIcon aria-hidden="true" className="size-4 transition-transform group-data-panel-open:rotate-180" />
                Read the store policy
              </CollapsibleTrigger>
              <CollapsiblePanel className="border-t">
                <PolicyList
                  dataComponent="checkoutPolicyList"
                  policy={policy}
                  currency={order.currency}
                  showMissingZones={false}
                  emptyText="The store hasn’t shared its policy yet."
                />
              </CollapsiblePanel>
            </Collapsible>
            <Label className="flex cursor-pointer items-start gap-2.5 border-t px-3 py-2.5 text-sm">
              <Checkbox
                data-component="checkoutAgreePolicy"
                className="mt-0.5"
                checked={agreed}
                disabled={busy}
                onCheckedChange={(checked) => setAgreed(checked === true)}
              />
              I’ve read and agree to the store’s policy
            </Label>
          </div>
        )}
        {!answered && chosen && policy === null && (
          <p className="text-muted-foreground text-sm">
            This checkout is out of date: ask in the chat to place the order again.
          </p>
        )}
        {!answered && !chosen && (
          <p className="text-muted-foreground text-sm">
            {legacyMethods.length > 0
              ? `This checkout offered ${legacyMethods.map((method) => PAYMENT_METHOD_LABELS[method] ?? method).join(', ')}: ask in the chat to pay it.`
              : 'Ask in the chat to pay this order.'}
          </p>
        )}
      </div>
    </ChatCard>
  );
}
