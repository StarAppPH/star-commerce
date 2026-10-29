'use client';
import { useState } from 'react';
import { PackageIcon, ReceiptIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ChatCard, SentNote } from './ChatCard';
import { OrderDelivery } from './OrderDelivery';
import {
  CANCEL_REASON_LABELS, ORDER_STATUS_LABELS, PAYMENT_METHOD_LABELS, formatMoney, formatWhen, itemsText, nonEmpty,
  orderRef,
} from './format';
import type { ComponentProps } from './types';

type BadgeVariant = 'info' | 'success' | 'warning' | 'error' | 'secondary';

const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  pending: 'warning',
  paid: 'info',
  completed: 'success',
  cancelled: 'secondary',
};

function StatusBadge({ status }: { status: string }) {
  return <Badge variant={STATUS_VARIANTS[status] ?? 'secondary'}>{ORDER_STATUS_LABELS[status] ?? status}</Badge>;
}

// The store's orders: tick the paid ones handed over, and the assistant
// completes them.
export function StoreOrdersCard({ component, answered, busy, onAnswer }: ComponentProps<'storeOrders'>) {
  const [ticked, setTicked] = useState<string[]>([]);
  const { formId } = component;
  const canHandOver = formId !== null && !answered;

  const handOver = () => {
    const orderIds = nonEmpty(ticked);
    if (formId !== null && orderIds) onAnswer({ kind: 'handover', formId, orderIds });
  };

  const toggle = (orderId: string, on: boolean) => {
    setTicked((current) => (on ? [...current, orderId] : current.filter((id) => id !== orderId)));
  };

  return (
    <ChatCard
      dataComponent="storeOrdersCard"
      icon={<PackageIcon />}
      title={component.orders.length === 1 ? '1 order' : `${component.orders.length} orders`}
      footer={answered ? <SentNote text="Sent to the assistant" /> : canHandOver ? (
        <Button
          data-component="storeOrdersHandover"
          disabled={ticked.length === 0 || busy}
          onClick={handOver}
        >
          {ticked.length > 0 ? `Mark ${ticked.length} handed over` : 'Tick the orders handed over'}
        </Button>
      ) : undefined}
    >
      <ul className="divide-y">
        {component.orders.map((order) => (
          <li key={order._id} className="flex items-start gap-3 px-4 py-3">
            {canHandOver && order.status === 'paid' && (
              <Checkbox
                data-component="storeOrdersTick"
                aria-label={`Order ${orderRef(order._id)} handed over`}
                className="mt-0.5"
                checked={ticked.includes(order._id)}
                disabled={busy}
                onCheckedChange={(checked) => toggle(order._id, checked === true)}
              />
            )}
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs">{orderRef(order._id)}</span>
                <StatusBadge status={order.status} />
                <span className="ms-auto font-medium text-sm">{formatMoney(order.total, order.currency)}</span>
              </div>
              <span className="text-sm">{itemsText(order.items)}</span>
              <OrderDelivery order={order} currency={order.currency} />
              <span className="text-muted-foreground text-xs">
                {[
                  `Placed ${formatWhen(order.createdAt)}`,
                  order.paidAt && order.paymentMethod
                    ? `paid by ${PAYMENT_METHOD_LABELS[order.paymentMethod] ?? order.paymentMethod} ${formatWhen(order.paidAt)}`
                    : null,
                  order.completedAt ? `handed over ${formatWhen(order.completedAt)}` : null,
                  order.cancelReason ? (CANCEL_REASON_LABELS[order.cancelReason] ?? order.cancelReason).toLowerCase() : null,
                ].filter(Boolean).join(' · ')}
              </span>
              <a className="text-muted-foreground text-xs underline-offset-2 hover:underline" href={`tel:${order.contactNumber}`}>
                {order.contactNumber}
              </a>
            </div>
          </li>
        ))}
      </ul>
    </ChatCard>
  );
}

// The shopper's own orders.
export function MyOrdersCard({ component }: ComponentProps<'myOrders'>) {
  return (
    <ChatCard
      dataComponent="myOrdersCard"
      icon={<ReceiptIcon />}
      title={component.orders.length === 1 ? `Order ${orderRef(component.orders[0]._id)}` : `${component.orders.length} orders`}
    >
      <ul className="divide-y">
        {component.orders.map((order) => (
          <li key={order._id} className="flex flex-col gap-1 px-4 py-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs">{orderRef(order._id)}</span>
              <StatusBadge status={order.status} />
              <span className="ms-auto font-medium text-sm">{formatMoney(order.total, order.currency)}</span>
            </div>
            <span className="text-sm">{itemsText(order.items)}</span>
            <OrderDelivery order={order} currency={order.currency} />
            <span className="text-muted-foreground text-xs">
              {[
                `Placed ${formatWhen(order.createdAt)}`,
                order.status === 'pending' ? `pay by ${formatWhen(order.expiresAt)}` : null,
                order.paidAt && order.paymentMethod
                  ? `paid by ${PAYMENT_METHOD_LABELS[order.paymentMethod] ?? order.paymentMethod}`
                  : null,
                order.completedAt ? `handed over ${formatWhen(order.completedAt)}` : null,
                order.cancelReason ? CANCEL_REASON_LABELS[order.cancelReason] ?? order.cancelReason : null,
                order.refundedAt ? `refunded ${formatWhen(order.refundedAt)}` : null,
              ].filter(Boolean).join(' · ')}
            </span>
          </li>
        ))}
      </ul>
    </ChatCard>
  );
}
