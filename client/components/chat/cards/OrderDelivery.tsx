'use client';
import { ExternalLinkIcon, StoreIcon, TruckIcon } from 'lucide-react';
import type { OrderDeliveryRow } from '@/app/schemas/OrderDeliveryRowSchema';
import { formatMoney } from './format';

// A pin on OpenStreetMap, the map the forms pin on.
export function mapLink(latitude: number, longitude: number): string {
  return `https://www.openstreetmap.org/?mlat=${latitude}&mlon=${longitude}#map=18/${latitude}/${longitude}`;
}

interface OrderDeliveryProps {
  order: OrderDeliveryRow;
  currency: string;
}

// How an order's customer gets it: picked up, or delivered where they pinned
// (with the fee, which the order's total includes).
export function OrderDelivery({ order, currency }: OrderDeliveryProps) {
  if (order.fulfillment === 'pickup') {
    return (
      <span data-component="orderDeliveryPickup" className="flex items-center gap-1.5 text-muted-foreground text-xs">
        <StoreIcon className="size-3.5 shrink-0" />
        Pickup at the store
      </span>
    );
  }
  return (
    <div data-component="orderDelivery" className="flex flex-col gap-0.5 text-xs">
      <span className="flex items-start gap-1.5">
        <TruckIcon className="mt-px size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0">
          Delivery to {order.deliveryAddress ?? 'the pinned spot'}
          <span className="text-muted-foreground">
            {' · '}
            {order.deliveryFee > 0 ? `${formatMoney(order.deliveryFee, currency)} fee` : 'free delivery'}
          </span>
        </span>
      </span>
      {order.deliveryNotes && <span className="ps-5 text-muted-foreground">“{order.deliveryNotes}”</span>}
      {order.deliveryLatitude !== null && order.deliveryLongitude !== null && (
        <a
          data-component="orderDeliveryMap"
          className="inline-flex items-center gap-1 ps-5 text-muted-foreground underline-offset-2 hover:underline"
          href={mapLink(order.deliveryLatitude, order.deliveryLongitude)}
          target="_blank"
          rel="noreferrer"
        >
          See the pin on the map
          <ExternalLinkIcon className="size-3" />
        </a>
      )}
    </div>
  );
}
