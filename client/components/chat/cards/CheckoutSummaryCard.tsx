'use client';
import { ExternalLinkIcon, ReceiptTextIcon, StoreIcon, TruckIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ChatCard, SentNote } from './ChatCard';
import { mapLink } from './OrderDelivery';
import { daysText, formatMoney } from './format';
import type { ComponentProps } from './types';

// When the order arrives (a delivery) or is ready (a pickup), counted from payment.
function readyText(days: number | null, delivery: boolean): string | null {
  if (days === null) return null;
  const when = days === 0 ? 'the same day' : `${daysText(days)} after payment`;
  return delivery ? `Arrives ${when}` : `Ready for pickup ${when}`;
}

// What checking out the cart would place, priced, before anything is placed:
// each line, the subtotal, the delivery fee of the zone under the pin (or
// free), the total and where it goes. Place order asks the chat to check out.
export function CheckoutSummaryCard({ component, answered, busy, onAnswer }: ComponentProps<'checkoutSummary'>) {
  const { currency } = component;
  const delivery = component.fulfillment === 'delivery';
  const ready = readyText(component.readyInDays, delivery);

  return (
    <ChatCard
      dataComponent="checkoutSummaryCard"
      icon={<ReceiptTextIcon />}
      title="Your order"
      footer={answered ? <SentNote text="Order confirmed in the chat" /> : (
        <Button
          data-component="checkoutSummaryPlaceOrder"
          className="w-full sm:w-auto"
          disabled={busy}
          onClick={() => onAnswer({ kind: 'placeOrder', formId: component.formId })}
        >
          Place order · {formatMoney(component.total, currency)}
        </Button>
      )}
    >
      <div className="flex flex-col gap-3 px-4 py-4 text-sm">
        <ul data-component="checkoutSummaryItems" className="flex flex-col gap-1.5">
          {component.items.map((item, index) => (
            <li key={`${item.name}-${index}`} className="flex items-baseline justify-between gap-3">
              <span className="min-w-0">
                {item.quantity} × {item.name}
                {item.quantity > 1 && (
                  <span className="text-muted-foreground text-xs"> · {formatMoney(item.price, currency)} each</span>
                )}
              </span>
              <span className="shrink-0 tabular-nums">{formatMoney(item.lineTotal, currency)}</span>
            </li>
          ))}
        </ul>
        <dl className="flex flex-col gap-1 border-t pt-3">
          <div className="flex justify-between gap-3 text-muted-foreground">
            <dt>Subtotal</dt>
            <dd className="tabular-nums">{formatMoney(component.subtotal, currency)}</dd>
          </div>
          {delivery && (
            <div data-component="checkoutSummaryDeliveryFee" className="flex justify-between gap-3 text-muted-foreground">
              <dt>Delivery{component.deliveryZone ? ` (${component.deliveryZone})` : ''}</dt>
              <dd className="tabular-nums">{component.deliveryFee > 0 ? formatMoney(component.deliveryFee, currency) : 'Free'}</dd>
            </div>
          )}
          <div className="flex items-baseline justify-between gap-3 font-medium">
            <dt>Total</dt>
            <dd className="font-heading text-xl tabular-nums">{formatMoney(component.total, currency)}</dd>
          </div>
        </dl>
        <div data-component="checkoutSummaryFulfillment" className="flex flex-col gap-0.5 text-xs">
          {delivery ? (
            <>
              <span className="flex items-start gap-1.5">
                <TruckIcon className="mt-px size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">Delivery to {component.deliveryAddress ?? 'the pinned spot'}</span>
              </span>
              {component.deliveryNotes && <span className="ps-5 text-muted-foreground">“{component.deliveryNotes}”</span>}
              {component.deliveryLatitude !== null && component.deliveryLongitude !== null && (
                <a
                  data-component="checkoutSummaryMap"
                  className="inline-flex items-center gap-1 ps-5 text-muted-foreground underline-offset-2 hover:underline"
                  href={mapLink(component.deliveryLatitude, component.deliveryLongitude)}
                  target="_blank"
                  rel="noreferrer"
                >
                  See the pin on the map
                  <ExternalLinkIcon className="size-3" />
                </a>
              )}
            </>
          ) : (
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <StoreIcon className="size-3.5 shrink-0" />
              Pickup at the store
            </span>
          )}
          {ready && <span className="ps-5 text-muted-foreground">{ready}</span>}
        </div>
      </div>
    </ChatCard>
  );
}
