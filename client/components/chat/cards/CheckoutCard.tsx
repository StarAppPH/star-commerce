'use client';
import { useEffect, useState } from 'react';
import {
  BanknoteIcon, BitcoinIcon, CreditCardIcon, LandmarkIcon, ReceiptTextIcon, WalletIcon,
  type LucideIcon,
} from 'lucide-react';
import type { Method, PaymentOption } from '@/app/schemas/PaymentOptionSchema';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { ChatCard, SentNote } from './ChatCard';
import { CopyButton } from './CopyButton';
import { OrderDelivery } from './OrderDelivery';
import {
  PAYMENT_METHOD_LABELS, PAYMENT_PROVIDER_LABELS, formatMoney, itemsText, minutesLeft, orderRef, paymentText,
} from './format';
import type { ComponentProps } from './types';

const METHOD_ICONS: Record<Method, LucideIcon> = {
  cash: BanknoteIcon,
  card: CreditCardIcon,
  eWallet: WalletIcon,
  bankTransfer: LandmarkIcon,
  check: ReceiptTextIcon,
  crypto: BitcoinIcon,
};

// One line of what paying by hand takes: what it is, and the account number
// to pay to (copyable), if any.
interface PayToLine {
  text: string;
  number: string | null;
}

// What paying by hand with `option` takes: the store's accounts, or when.
function payToLines(option: PaymentOption, pickup: boolean): PayToLine[] {
  if (option.eWallets) {
    return option.eWallets.map((account) => ({ text: account.ewalletName, number: account.accountNumber }));
  }
  if (option.banks) {
    return option.banks.map((account) => ({ text: `${account.bankName} · ${account.accountName}`, number: account.accountNumber }));
  }
  if (option.checkPayees) {
    return option.checkPayees.map((payee) => ({ text: `Pay to the order of ${payee}, at the handover`, number: null }));
  }
  return [{ text: pickup ? 'Pay when you pick it up' : 'Pay when it’s delivered', number: null }];
}

function optionLabel(option: PaymentOption): string {
  if (option.provider === null) return paymentText(option.method, null);
  return `${PAYMENT_METHOD_LABELS[option.method] ?? option.method} online`;
}

interface OptionGroupProps {
  title: string;
  note: string;
  entries: { option: PaymentOption; index: number }[];
  picked: number;
  busy: boolean;
  pickup: boolean;
}

// One kind of payment (online now, or by hand to the store): a heading, then
// its options in one list. The picked option paid by hand opens to show the
// store's accounts.
function OptionGroup({ title, note, entries, picked, busy, pickup }: OptionGroupProps) {
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <p className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
        {title} <span className="font-normal normal-case tracking-normal">· {note}</span>
      </p>
      <div className="divide-y overflow-hidden rounded-lg border">
        {entries.map(({ option, index }) => {
          const Icon = METHOD_ICONS[option.method];
          const open = index === picked && option.provider === null;
          return (
            <div key={`${option.method}-${option.provider ?? 'manual'}`} className={index === picked ? 'bg-accent/40' : undefined}>
              <Label
                data-component={`checkoutMethod${option.method[0].toUpperCase()}${option.method.slice(1)}${option.provider === null ? 'Manual' : 'Online'}`}
                className="flex cursor-pointer items-center gap-3 px-3 py-3"
              >
                <RadioGroupItem value={String(index)} disabled={busy} />
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <span className="flex-1">{PAYMENT_METHOD_LABELS[option.method] ?? option.method}</span>
                {option.provider !== null && (
                  <span className="text-muted-foreground text-xs">{PAYMENT_PROVIDER_LABELS[option.provider] ?? option.provider}</span>
                )}
              </Label>
              {open && (
                <ul data-component="checkoutPayTo" className="flex flex-col gap-2 ps-17 pe-3 pb-3 text-sm">
                  {payToLines(option, pickup).map((line) => (
                    <li key={`${line.text}-${line.number ?? ''}`} className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 flex-col">
                        <span className={line.number === null ? undefined : 'text-muted-foreground text-xs'}>{line.text}</span>
                        {line.number !== null && <span className="font-medium tabular-nums">{line.number}</span>}
                      </span>
                      {line.number !== null && <CopyButton dataComponent="checkoutCopyNumber" value={line.number} label={`Copy ${line.number}`} />}
                    </li>
                  ))}
                  {option.method !== 'cash' && (
                    <li className="text-muted-foreground text-xs">Once you’ve paid, send a photo of the receipt from your order.</li>
                  )}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// The order a shopper just placed: how long is left to choose how to pay, and
// the store's payment options. One paid by hand shows the store's accounts.
export function CheckoutCard({ component, answered, busy, onAnswer }: ComponentProps<'checkout'>) {
  const { order } = component;
  const options = component.paymentOptions ?? [];
  const [picked, setPicked] = useState(0);
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
  const entries = options.map((option, index) => ({ option, index }));
  const pickup = order.fulfillment === 'pickup';

  return (
    <ChatCard
      dataComponent="checkoutCard"
      icon={<CreditCardIcon />}
      title={`Order ${orderRef(order._id)}`}
      meta={answered || !chosen ? undefined : (
        <Badge variant={left <= 5 ? 'error' : 'warning'}>{left > 0 ? `${left} min left to choose` : 'Time is up'}</Badge>
      )}
      footer={answered ? <SentNote text="Payment choice sent in the chat" /> : chosen ? (
        <Button
          data-component="checkoutPay"
          className="w-full sm:w-auto"
          disabled={busy || left === 0}
          onClick={() => onAnswer({
            kind: 'payment', formId: component.formId, paymentMethod: chosen.method, paymentProvider: chosen.provider,
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
        {!answered && options.length > 0 && (
          <RadioGroup
            data-component="checkoutMethod"
            aria-label="Payment method"
            value={String(picked)}
            onValueChange={(value) => setPicked(Number(value))}
            className="gap-4"
          >
            <OptionGroup
              title="Pay online now"
              note="confirmed right away"
              entries={entries.filter(({ option }) => option.provider !== null)}
              picked={picked}
              busy={busy}
              pickup={pickup}
            />
            <OptionGroup
              title="Pay the store"
              note="the store confirms it once it arrives"
              entries={entries.filter(({ option }) => option.provider === null)}
              picked={picked}
              busy={busy}
              pickup={pickup}
            />
          </RadioGroup>
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
