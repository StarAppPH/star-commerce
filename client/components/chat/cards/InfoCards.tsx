'use client';
import Image from 'next/image';
import {
  ClockIcon, ExternalLinkIcon, ImageIcon, MapIcon, MapPinIcon, PackageSearchIcon, PhoneIcon, ScrollTextIcon, StoreIcon,
  TriangleAlertIcon, type LucideIcon,
} from 'lucide-react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChatCard, MoreProducts, PhotoPrompts, ProductPhoto, countsStock, productListTitle } from './ChatCard';
import { CopyButton } from './CopyButton';
import { mapLink } from './OrderDelivery';
import { type PolicyAnswers, type PolicyQuestion, missingDeliveryZones, offersDelivery, policyQuestionApplies, unansweredPolicyQuestions } from '@/lib/policyQuestions';
import type { AuthorizedPaymentMethodRow } from '@/app/schemas/AuthorizedPaymentMethodRowSchema';
import { PAYMENT_METHOD_LABELS, PAYMENT_PROVIDER_LABELS, POLICY_VALUE_LABELS, formatMoney, zoneRuleText } from './format';
import type { ComponentProps } from './types';

// The admin's products a tool listed, added or changed.
export function StoreProductsCard({ component, busy }: ComponentProps<'storeProducts'>) {
  return (
    <ChatCard
      dataComponent="storeProductsCard"
      icon={<PackageSearchIcon />}
      title={productListTitle(component.products.length, component.nextSkip)}
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
                {!countsStock(product) ? 'Unlimited' : product.inventory === 0 ? 'Sold out' : `${product.inventory} in stock`}
                {product.reservedQuantity > 0 && ` · ${product.reservedQuantity} held by unpaid orders`}
                {` · ${product.soldCount} sold`}
              </span>
            </div>
            <span className="text-sm tabular-nums">{formatMoney(product.price, component.currency)}</span>
          </li>
        ))}
      </ul>
      <MoreProducts dataComponent="storeProductsShowMore" nextSkip={component.nextSkip} busy={busy} />
    </ChatCard>
  );
}

type PolicyRowValue = string | number | boolean | string[] | null;

function shown(value: PolicyRowValue, money: (amount: number) => string, isMoney: boolean): string {
  if (value === null) return 'Not answered yet';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return isMoney ? money(value) : String(value);
  if (Array.isArray(value)) return value.map((item) => POLICY_VALUE_LABELS[item] ?? item).join(', ');
  return POLICY_VALUE_LABELS[value] ?? value;
}

// One payment method the store takes, with what paying that way needs.
function paymentMethodText(method: AuthorizedPaymentMethodRow): string {
  const ways: string[] = [];
  if (method.provider !== null) ways.push(`online through ${PAYMENT_PROVIDER_LABELS[method.provider] ?? method.provider}`);
  if (method.eWallets) ways.push(method.eWallets.map((account) => `${account.ewalletName} ${account.accountNumber}`).join(', '));
  if (method.banks) ways.push(method.banks.map((account) => `${account.bankName} ${account.accountNumber} (${account.accountName})`).join(', '));
  if (method.checkPayees) ways.push(`payable to ${method.checkPayees.join(' or ')}`);
  const label = PAYMENT_METHOD_LABELS[method.method] ?? method.method;
  return ways.length > 0 ? `${label} (${ways.join('; ')})` : label;
}

// The policy questions in the order a store's policy page reads. A policy card
// saved before each payment method had its details lists the bare methods.
export function policyRows(
  answers: PolicyAnswers & { acceptedPaymentMethods?: string[] | null },
): { question: PolicyQuestion; label: string; value: PolicyRowValue; money?: boolean }[] {
  const methods = answers.authorizedPaymentMethods;
  return [
    {
      question: 'authorizedPaymentMethods',
      label: 'Payment methods',
      value: methods ? methods.map(paymentMethodText) : answers.acceptedPaymentMethods ?? null,
    },
    { question: 'paidOrderCancellation', label: 'Cancelling a paid order', value: answers.paidOrderCancellation },
    { question: 'paidOrderCancellationHours', label: 'Hours after payment to cancel', value: answers.paidOrderCancellationHours },
    { question: 'deliveryOptions', label: 'Pickup or delivery', value: answers.deliveryOptions },
    { question: 'deliveryMethod', label: 'How we deliver', value: answers.deliveryMethod ?? null },
    { question: 'deliveryAreas', label: 'Delivery areas', value: answers.deliveryAreas },
    { question: 'handoverDays', label: 'Days until ready for pickup', value: answers.handoverDays },
    { question: 'acceptsReturns', label: 'Accepts returns', value: answers.acceptsReturns },
    { question: 'returnWindowDays', label: 'Return window (days)', value: answers.returnWindowDays },
    { question: 'returnConditions', label: 'Return conditions', value: answers.returnConditions },
    { question: 'nonReturnableItems', label: 'Not returnable', value: answers.nonReturnableItems },
    { question: 'refundMethod', label: 'Refund', value: answers.refundMethod },
    { question: 'returnShippingPaidBy', label: 'Return shipping paid by', value: answers.returnShippingPaidBy },
    { question: 'defectReportDays', label: 'Days to report a defect', value: answers.defectReportDays },
    { question: 'additionalTerms', label: 'Other terms', value: answers.additionalTerms },
  ];
}

// The store's policy: every answer, each delivery zone's own policy, and how
// many required questions the store has not answered yet (the server's
// unansweredQuestions; worked out here only on a card saved before it).
export function PolicyCard({ component }: ComponentProps<'policy'>) {
  const rows = policyRows(component).filter((row) => policyQuestionApplies(row.question, component));
  const open = unansweredPolicyQuestions(component);
  const money = (amount: number) => formatMoney(amount, component.currency);
  const zones = offersDelivery(component) ? component.deliveryZones ?? [] : [];
  const answeredRows = rows.filter((row) => row.value !== null);
  return (
    <ChatCard
      dataComponent="policyCard"
      icon={<ScrollTextIcon />}
      title="Store policy"
      meta={open > 0 ? <Badge variant="warning">{open} not answered</Badge> : undefined}
    >
      <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-4 gap-y-2 px-4 py-3 text-sm">
        {answeredRows.map((row) => (
          <div key={row.label} className="contents">
            <dt className="text-muted-foreground">{row.label}</dt>
            <dd>{shown(row.value, money, row.money === true)}</dd>
          </div>
        ))}
        {zones.map((zone) => (
          <div key={zone.name} className="contents">
            <dt className="text-muted-foreground">Delivery to {zone.name}</dt>
            <dd data-component="policyCardDeliveryZone">{zoneRuleText(zone, component.currency)}</dd>
          </div>
        ))}
        {missingDeliveryZones(component) && (
          <div className="contents">
            <dt className="text-muted-foreground">Delivery zones</dt>
            <dd data-component="policyCardNoDeliveryZones">None yet: no deliveries until you draw one</dd>
          </div>
        )}
        {answeredRows.length === 0 && <p className="col-span-2 text-muted-foreground">Nothing answered yet.</p>}
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
      {component.notGenerated && (
        <Alert data-component="photosCardNotGenerated" variant="warning" className="m-3 mb-0 w-auto">
          <TriangleAlertIcon />
          <AlertDescription>{component.notGenerated}</AlertDescription>
        </Alert>
      )}
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
      <PhotoPrompts dataComponent="photosCardPrompt" photos={component.photos} prompt={component.prompt} />
    </ChatCard>
  );
}

// The admin's store.
interface StoreDetailProps {
  icon: LucideIcon;
  label: string;
  value: string | null;
  dataComponent: string;
  href?: string;
}

// One labelled line of the store card: "Not set yet" when the owner hasn't said.
function StoreDetail({ icon: Icon, label, value, dataComponent, href }: StoreDetailProps) {
  return (
    <li data-component={dataComponent} className="flex gap-3">
      <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="flex min-w-0 flex-col">
        <span className="text-muted-foreground text-xs">{label}</span>
        {value === null ? (
          <span className="text-muted-foreground italic">Not set yet</span>
        ) : href ? (
          <a href={href} className="break-words underline-offset-2 hover:underline">{value}</a>
        ) : (
          <span className="break-words">{value}</span>
        )}
      </div>
    </li>
  );
}

export function StoreCard({ component }: ComponentProps<'store'>) {
  const shopPath = component.slug ? `/s/${component.slug}` : null;
  return (
    <ChatCard
      dataComponent="storeCard"
      icon={<StoreIcon />}
      title={component.name ?? 'Your store'}
      subtitle={shopPath ?? undefined}
      meta={<Badge variant="outline">{component.currency}</Badge>}
      footer={shopPath ? (
        <>
          <CopyButton
            dataComponent="storeCardCopyLink"
            variant="outline"
            size="sm"
            value={typeof window === 'undefined' ? shopPath : `${window.location.origin}${shopPath}`}
            label="Copy the shop link"
            text="Copy link"
          />
          <Button data-component="storeCardOpen" size="sm" render={<a href={shopPath} target="_blank" rel="noreferrer" />}>
            <ExternalLinkIcon />
            Open shop
          </Button>
        </>
      ) : undefined}
    >
      <div className="flex flex-col gap-4 px-4 py-4 text-sm">
        {component.description && <p data-component="storeCardDescription">{component.description}</p>}
        <ul className="flex flex-col gap-3">
          <StoreDetail dataComponent="storeCardAddress" icon={MapPinIcon} label="Address" value={component.address} />
          <StoreDetail dataComponent="storeCardHours" icon={ClockIcon} label="Opening hours" value={component.openingHours} />
          <StoreDetail
            dataComponent="storeCardContact"
            icon={PhoneIcon}
            label="Contact number"
            value={component.contactNumber}
            href={component.contactNumber ? `tel:${component.contactNumber.replace(/[^\d+]/g, '')}` : undefined}
          />
          <li data-component="storeCardPin" className="flex gap-3">
            <MapIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="flex min-w-0 flex-col">
              <span className="text-muted-foreground text-xs">Map pin</span>
              {component.location ? (
                <a
                  data-component="storeCardMap"
                  className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                  href={mapLink(component.location.latitude, component.location.longitude)}
                  target="_blank"
                  rel="noreferrer"
                >
                  View on map
                  <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
                </a>
              ) : (
                <span className="text-muted-foreground italic">Not pinned yet</span>
              )}
            </div>
          </li>
        </ul>
      </div>
    </ChatCard>
  );
}
