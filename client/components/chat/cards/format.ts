// How the cards say money, orders and the server's codes.
import { DELIVERY_USD_PER_KM } from '@/lib/generated/serverConstants';
import { formatShortDate, formatStoreTime, parseInstant } from '@/lib/storeTime';

const moneyFormats = new Map<string, Intl.NumberFormat>();

export function formatMoney(amount: number, currency: string): string {
  let format = moneyFormats.get(currency);
  if (!format) {
    format = new Intl.NumberFormat('en-PH', { style: 'currency', currency, maximumFractionDigits: 2 });
    moneyFormats.set(currency, format);
  }
  return format.format(amount);
}

// What a km of delivery costs: in the store's currency at today's rate, else
// (no rate right now) the app's rate in US dollars.
export function deliveryPerKmText(perKm: number | null, currency: string): string {
  return perKm === null ? `${formatMoney(DELIVERY_USD_PER_KM, 'USD')} per km` : `${formatMoney(perKm, currency)} per km`;
}

// The short reference an order is shown by (server chatForms.orderRef).
export function orderRef(orderId: string): string {
  return `#${orderId.slice(-6).toUpperCase()}`;
}

export function formatWhen(value: string): string {
  return `${formatShortDate(value)}, ${formatStoreTime(value)}`;
}

// A store time of day as the server keeps it (HH:MM, 24-hour), e.g. "4:30 PM".
export function formatTimeOfDay(value: string): string {
  const [hours, minutes] = value.split(':').map(Number);
  return `${hours % 12 || 12}:${String(minutes).padStart(2, '0')} ${hours < 12 ? 'AM' : 'PM'}`;
}

export function minutesLeft(until: string, now: number): number {
  const end = parseInstant(until);
  return end ? Math.max(0, Math.ceil((end.getTime() - now) / 60000)) : 0;
}

export const PAYMENT_METHOD_LABELS: Record<string, string> = {
  card: 'Card',
  gcash: 'GCash',
  maya: 'Maya',
  grabPay: 'GrabPay',
  bankTransfer: 'Bank transfer',
};

export const ORDER_STATUS_LABELS: Record<string, string> = {
  pending: 'Awaiting payment',
  paid: 'Paid',
  completed: 'Handed over',
  cancelled: 'Cancelled',
};

export const RETURN_STATUS_LABELS: Record<string, string> = {
  requested: 'Requested',
  approved: 'Approved',
  rejected: 'Rejected',
  received: 'Received',
  cancelled: 'Cancelled',
};

export const RETURN_REASON_LABELS: Record<string, string> = {
  changedMind: 'Changed their mind',
  defective: 'Defective',
  wrongItem: 'Wrong item',
};

export const CANCEL_REASON_LABELS: Record<string, string> = {
  expired: 'Not paid in time',
  byStore: 'Cancelled by the store',
  byCustomer: 'Cancelled by you',
};

export const POLICY_VALUE_LABELS: Record<string, string> = {
  pickup: 'Pickup',
  delivery: 'Delivery',
  originalPayment: 'Original payment',
  storeCredit: 'Store credit',
  exchange: 'Exchange',
  customer: 'The customer',
  store: 'The store',
  notAllowed: 'Not allowed',
  beforeHandover: 'Before handover',
  withinHours: 'Within some hours of payment, before handover',
  ...PAYMENT_METHOD_LABELS,
};

export function itemsText(items: { name: string; quantity: number }[]): string {
  return items.map((item) => `${item.quantity} × ${item.name}`).join(', ');
}

// A non-empty list as the server's types want it, or null for none.
export function nonEmpty<T>(values: T[]): [T, ...T[]] | null {
  return values.length > 0 ? (values as [T, ...T[]]) : null;
}
