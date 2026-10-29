'use client';
import Image from 'next/image';
import { ExternalLinkIcon, ImageIcon, MapPinIcon, PackageSearchIcon, ScrollTextIcon, StoreIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChatCard, ProductPhoto } from './ChatCard';
import { POLICY_VALUE_LABELS, formatMoney } from './format';
import type { ComponentProps } from './types';

// The admin's products a tool listed, added or changed.
export function StoreProductsCard({ component }: ComponentProps<'storeProducts'>) {
  return (
    <ChatCard
      dataComponent="storeProductsCard"
      icon={<PackageSearchIcon />}
      title={component.products.length === 1 ? '1 product' : `${component.products.length} products`}
    >
      <ul className="divide-y">
        {component.products.map((product) => (
          <li key={product._id} className="flex items-center gap-3 px-4 py-2.5">
            <ProductPhoto dataComponent="storeProductsPhoto" url={product.photoUrl} name={product.name} />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex items-center gap-2 font-medium text-sm">
                <span className="truncate">{product.name}</span>
                {!product.isAvailable && <Badge variant="secondary" size="sm">Hidden</Badge>}
              </span>
              <span className="text-muted-foreground text-xs">
                {product.inventory === 0 ? 'Sold out' : `${product.inventory} in stock`}
                {product.reservedQuantity > 0 && ` · ${product.reservedQuantity} held by unpaid orders`}
                {` · ${product.soldCount} sold`}
              </span>
            </div>
            <span className="text-sm tabular-nums">{formatMoney(product.price, component.currency)}</span>
          </li>
        ))}
      </ul>
    </ChatCard>
  );
}

type PolicyAnswers = Omit<ComponentProps<'policy'>['component'], 'kind' | 'currency'>;

function shown(value: string | number | boolean | string[] | null, money: (amount: number) => string, isMoney: boolean): string {
  if (value === null) return 'Not answered yet';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return isMoney ? money(value) : String(value);
  if (Array.isArray(value)) return value.map((item) => POLICY_VALUE_LABELS[item] ?? item).join(', ');
  return POLICY_VALUE_LABELS[value] ?? value;
}

// The policy questions in the order a store's policy page reads.
export function policyRows(answers: PolicyAnswers): { label: string; value: string | number | boolean | string[] | null; money?: boolean }[] {
  return [
    { label: 'Payment methods', value: answers.acceptedPaymentMethods },
    { label: 'Cancelling a paid order', value: answers.paidOrderCancellation },
    { label: 'Pickup or delivery', value: answers.deliveryOptions },
    { label: 'Delivery areas', value: answers.deliveryAreas },
    { label: 'Delivery fee', value: answers.deliveryFee, money: true },
    { label: 'Free delivery from', value: answers.freeDeliveryMinimum, money: true },
    { label: 'Days to hand over', value: answers.handoverDays },
    { label: 'Accepts returns', value: answers.acceptsReturns },
    { label: 'Return window (days)', value: answers.returnWindowDays },
    { label: 'Return conditions', value: answers.returnConditions },
    { label: 'Not returnable', value: answers.nonReturnableItems },
    { label: 'Refund', value: answers.refundMethod },
    { label: 'Return shipping paid by', value: answers.returnShippingPaidBy },
    { label: 'Days to report a defect', value: answers.defectReportDays },
    { label: 'Sends offers', value: answers.sendsMarketingMessages },
    { label: 'Other terms', value: answers.additionalTerms },
  ];
}

// The store's policy: every answer, and what the store has not said yet.
export function PolicyCard({ component }: ComponentProps<'policy'>) {
  const rows = policyRows(component);
  const open = rows.filter((row) => row.value === null).length;
  const money = (amount: number) => formatMoney(amount, component.currency);
  return (
    <ChatCard
      dataComponent="policyCard"
      icon={<ScrollTextIcon />}
      title="Store policy"
      meta={open > 0 ? <Badge variant="warning">{open} not answered</Badge> : undefined}
    >
      <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-4 gap-y-2 px-4 py-3 text-sm">
        {rows.filter((row) => row.value !== null).map((row) => (
          <div key={row.label} className="contents">
            <dt className="text-muted-foreground">{row.label}</dt>
            <dd>{shown(row.value, money, row.money === true)}</dd>
          </div>
        ))}
        {open === rows.length && <p className="col-span-2 text-muted-foreground">Nothing answered yet.</p>}
      </dl>
    </ChatCard>
  );
}

// The product photos a tool made.
export function PhotosCard({ component }: ComponentProps<'photos'>) {
  return (
    <ChatCard
      dataComponent="photosCard"
      icon={<ImageIcon />}
      title={component.photos.length === 1 ? '1 photo' : `${component.photos.length} photos`}
      footer={component.photos.length > 0 ? (
        <span className="me-auto text-muted-foreground text-xs">
          To show one in your shop, say which product it goes on, e.g. “use photo 1 for the mug”.
        </span>
      ) : undefined}
    >
      {component.photos.length === 0 && <p className="px-4 py-3 text-muted-foreground text-sm">No photos were made this time.</p>}
      <div className="grid grid-cols-2 gap-2 p-3 empty:hidden sm:grid-cols-3">
        {component.photos.map((photo, index) => (
          <a
            key={photo._id}
            href={photo.url}
            target="_blank"
            rel="noreferrer"
            className="relative block aspect-square overflow-hidden rounded-lg bg-muted"
            data-component="photosCardPhoto"
          >
            <Image
              src={photo.url}
              alt={`Product photo ${index + 1}`}
              fill
              sizes="(min-width: 640px) 240px, 45vw"
              className="object-cover"
            />
            <span className="absolute top-1.5 left-1.5 rounded-md bg-background/85 px-1.5 py-0.5 font-medium text-xs tabular-nums">
              {index + 1}
            </span>
          </a>
        ))}
      </div>
    </ChatCard>
  );
}

// The admin's store.
export function StoreCard({ component }: ComponentProps<'store'>) {
  const lines = [component.description, component.address, component.openingHours, component.contactNumber].filter(Boolean);
  return (
    <ChatCard
      dataComponent="storeCard"
      icon={<StoreIcon />}
      title={component.name ?? 'Your store'}
      meta={<Badge variant="outline">{component.currency}</Badge>}
      footer={component.slug ? (
        <Button data-component="storeCardOpen" variant="outline" size="sm" render={<a href={`/s/${component.slug}`} target="_blank" rel="noreferrer" />}>
          <ExternalLinkIcon />
          Open /s/{component.slug}
        </Button>
      ) : undefined}
    >
      <div className="flex flex-col gap-1 px-4 py-3 text-sm">
        {lines.map((line) => <span key={line}>{line}</span>)}
        <span className="flex items-center gap-1.5 text-muted-foreground text-xs">
          <MapPinIcon className="size-3.5" />
          {component.location
            ? `Pinned at ${component.location.latitude.toFixed(5)}, ${component.location.longitude.toFixed(5)}`
            : 'Not pinned on the map yet'}
        </span>
      </div>
    </ChatCard>
  );
}
