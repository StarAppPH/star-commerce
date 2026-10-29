'use client';
import { useState } from 'react';
import { ScrollTextIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Field, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import { ChatCard, SentNote } from './ChatCard';
import { POLICY_VALUE_LABELS, nonEmpty } from './format';
import type { AnswerOf, ComponentProps } from './types';

type Form = ComponentProps<'policyForm'>['component'];
type PaymentMethod = NonNullable<Form['acceptedPaymentMethods']>[number];
type DeliveryOption = NonNullable<Form['deliveryOptions']>[number];

const SECTION_TITLES: Record<Form['section'], string> = {
  payment: 'Payment',
  delivery: 'Pickup and delivery',
  returns: 'Returns',
  other: 'Other terms',
};
const PAYMENT_METHODS: PaymentMethod[] = ['gcash', 'maya', 'card', 'grabPay', 'bankTransfer'];
const DELIVERY_OPTIONS: DeliveryOption[] = ['pickup', 'delivery'];
// The server's limits (objects/policy.py).
const TEXT_MAX = 300;
const TERMS_MAX = 1000;
const DAYS_MAX = 365;

const text = (value: string | null) => value ?? '';
const numberText = (value: number | null) => (value === null ? '' : String(value));
const yesNo = (value: boolean | null) => (value === null ? '' : value ? 'yes' : 'no');

function wholeDays(value: string, min: number): number | null {
  const days = Number(value);
  return value.trim() !== '' && Number.isInteger(days) && days >= min && days <= DAYS_MAX ? days : null;
}

function amount(value: string): number | null {
  const parsed = Number(value);
  return value.trim() !== '' && Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function Choices<T extends string>({ name, value, options, onChange, disabled, dataComponent }: {
  name: string; value: string; options: { value: T; label: string }[]; onChange: (value: T) => void; disabled: boolean; dataComponent: string;
}) {
  return (
    <RadioGroup
      data-component={dataComponent}
      aria-label={name}
      value={value}
      onValueChange={(next) => onChange(next as T)}
      className="flex flex-row flex-wrap gap-2"
    >
      {options.map((option) => (
        <Label key={option.value} className="flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 font-normal has-data-checked:border-primary">
          <RadioGroupItem value={option.value} disabled={disabled} />
          {option.label}
        </Label>
      ))}
    </RadioGroup>
  );
}

function Ticks<T extends string>({ values, options, onChange, disabled, dataComponent }: {
  values: T[]; options: T[]; onChange: (values: T[]) => void; disabled: boolean; dataComponent: string;
}) {
  return (
    <div data-component={dataComponent} className="flex flex-wrap gap-2">
      {options.map((option) => (
        <Label key={option} className="flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 font-normal has-data-checked:border-primary">
          <Checkbox
            checked={values.includes(option)}
            disabled={disabled}
            onCheckedChange={(checked) => onChange(checked === true ? [...values, option] : values.filter((value) => value !== option))}
          />
          {POLICY_VALUE_LABELS[option] ?? option}
        </Label>
      ))}
    </div>
  );
}

const YES_NO = [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }] as const;

// One section of the store's policy questions, showing the current answers.
export function PolicyForm({ component, answered, busy, onAnswer }: ComponentProps<'policyForm'>) {
  const [methods, setMethods] = useState<PaymentMethod[]>(component.acceptedPaymentMethods ?? []);
  const [cancellation, setCancellation] = useState(text(component.paidOrderCancellation));
  const [options, setOptions] = useState<DeliveryOption[]>(component.deliveryOptions ?? []);
  const [areas, setAreas] = useState(text(component.deliveryAreas));
  const [fee, setFee] = useState(numberText(component.deliveryFee));
  const [freeFrom, setFreeFrom] = useState(numberText(component.freeDeliveryMinimum));
  const [handoverDays, setHandoverDays] = useState(numberText(component.handoverDays));
  const [acceptsReturns, setAcceptsReturns] = useState(yesNo(component.acceptsReturns));
  const [windowDays, setWindowDays] = useState(numberText(component.returnWindowDays));
  const [conditions, setConditions] = useState(text(component.returnConditions));
  const [notReturnable, setNotReturnable] = useState(text(component.nonReturnableItems));
  const [refund, setRefund] = useState(text(component.refundMethod));
  const [shippingBy, setShippingBy] = useState(text(component.returnShippingPaidBy));
  const [defectDays, setDefectDays] = useState(numberText(component.defectReportDays));
  const [offers, setOffers] = useState(yesNo(component.sendsMarketingMessages));
  const [terms, setTerms] = useState(text(component.additionalTerms));
  const locked = answered || busy;
  const currency = component.currency;

  const answer = (): AnswerOf<'policy'> => {
    const base: AnswerOf<'policy'> = { kind: 'policy', formId: component.formId };
    const trimmed = (value: string) => (value.trim() === '' ? null : value.trim());
    switch (component.section) {
      case 'payment':
        return {
          ...base,
          acceptedPaymentMethods: nonEmpty(methods),
          paidOrderCancellation: cancellation === 'notAllowed' || cancellation === 'beforeHandover' ? cancellation : null,
        };
      case 'delivery': {
        const delivers = options.includes('delivery');
        return {
          ...base,
          deliveryOptions: nonEmpty(options),
          deliveryAreas: delivers ? trimmed(areas) : null,
          deliveryFee: delivers ? amount(fee) : null,
          freeDeliveryMinimum: delivers ? amount(freeFrom) : null,
          handoverDays: wholeDays(handoverDays, 0),
        };
      }
      case 'returns': {
        const accepts = acceptsReturns === '' ? null : acceptsReturns === 'yes';
        return {
          ...base,
          acceptsReturns: accepts,
          returnWindowDays: accepts ? wholeDays(windowDays, 1) : null,
          returnConditions: accepts ? trimmed(conditions) : null,
          refundMethod: accepts && (refund === 'originalPayment' || refund === 'storeCredit' || refund === 'exchange') ? refund : null,
          returnShippingPaidBy: accepts && (shippingBy === 'customer' || shippingBy === 'store') ? shippingBy : null,
          nonReturnableItems: trimmed(notReturnable),
          defectReportDays: wholeDays(defectDays, 1),
        };
      }
      default:
        return {
          ...base,
          sendsMarketingMessages: offers === '' ? null : offers === 'yes',
          additionalTerms: trimmed(terms),
        };
    }
  };

  const draft = answer();
  const answeredSomething = [
    draft.acceptedPaymentMethods, draft.paidOrderCancellation, draft.deliveryOptions, draft.deliveryAreas,
    draft.deliveryFee, draft.freeDeliveryMinimum, draft.handoverDays, draft.acceptsReturns, draft.returnWindowDays,
    draft.returnConditions, draft.nonReturnableItems, draft.refundMethod, draft.returnShippingPaidBy,
    draft.defectReportDays, draft.sendsMarketingMessages, draft.additionalTerms,
  ].some((value) => value !== null && value !== undefined);

  return (
    <ChatCard
      dataComponent="policyForm"
      icon={<ScrollTextIcon />}
      title={`Policy: ${SECTION_TITLES[component.section]}`}
      footer={answered ? <SentNote text="Answers sent" /> : (
        <Button data-component="policyFormSend" disabled={!answeredSomething || busy} onClick={() => onAnswer(draft)}>
          Save answers
        </Button>
      )}
    >
      <div className="flex flex-col gap-4 p-4">
        {component.section === 'payment' && (
          <>
            <Field>
              <FieldLabel>How can customers pay?</FieldLabel>
              <Ticks dataComponent="policyFormMethods" values={methods} options={PAYMENT_METHODS} onChange={setMethods} disabled={locked} />
            </Field>
            <Field>
              <FieldLabel>Can a customer cancel a paid order?</FieldLabel>
              <Choices
                dataComponent="policyFormCancellation"
                name="Cancelling a paid order"
                value={cancellation}
                options={[{ value: 'notAllowed', label: 'No' }, { value: 'beforeHandover', label: 'Yes, before handover' }]}
                onChange={setCancellation}
                disabled={locked}
              />
            </Field>
          </>
        )}
        {component.section === 'delivery' && (
          <>
            <Field>
              <FieldLabel>How do customers get their order?</FieldLabel>
              <Ticks dataComponent="policyFormDeliveryOptions" values={options} options={DELIVERY_OPTIONS} onChange={setOptions} disabled={locked} />
            </Field>
            {options.includes('delivery') && (
              <>
                <Field>
                  <FieldLabel>Where do you deliver?</FieldLabel>
                  <Input data-component="policyFormAreas" value={areas} maxLength={TEXT_MAX} placeholder="Quezon City and Marikina" disabled={locked} onChange={(event) => setAreas(event.target.value)} />
                </Field>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field>
                    <FieldLabel>Delivery fee ({currency})</FieldLabel>
                    <Input data-component="policyFormFee" inputMode="decimal" value={fee} disabled={locked} onChange={(event) => setFee(event.target.value)} />
                  </Field>
                  <Field>
                    <FieldLabel>Free delivery from ({currency})</FieldLabel>
                    <Input data-component="policyFormFreeFrom" inputMode="decimal" value={freeFrom} disabled={locked} onChange={(event) => setFreeFrom(event.target.value)} />
                  </Field>
                </div>
              </>
            )}
            <Field>
              <FieldLabel>Days from payment to pickup or delivery</FieldLabel>
              <Input data-component="policyFormHandoverDays" inputMode="numeric" value={handoverDays} disabled={locked} onChange={(event) => setHandoverDays(event.target.value)} />
            </Field>
          </>
        )}
        {component.section === 'returns' && (
          <>
            <Field>
              <FieldLabel>Do you take back items that aren’t defective?</FieldLabel>
              <Choices dataComponent="policyFormAcceptsReturns" name="Accepts returns" value={acceptsReturns} options={[...YES_NO]} onChange={setAcceptsReturns} disabled={locked} />
            </Field>
            {acceptsReturns === 'yes' && (
              <>
                <Field>
                  <FieldLabel>Days after handover to return an item</FieldLabel>
                  <Input data-component="policyFormWindowDays" inputMode="numeric" value={windowDays} disabled={locked} onChange={(event) => setWindowDays(event.target.value)} />
                </Field>
                <Field>
                  <FieldLabel>What must a returned item be like?</FieldLabel>
                  <Input data-component="policyFormConditions" value={conditions} maxLength={TEXT_MAX} placeholder="Unused, in its packaging" disabled={locked} onChange={(event) => setConditions(event.target.value)} />
                </Field>
                <Field>
                  <FieldLabel>How is it refunded?</FieldLabel>
                  <Choices
                    dataComponent="policyFormRefund"
                    name="Refund method"
                    value={refund}
                    options={[{ value: 'originalPayment', label: 'Original payment' }, { value: 'storeCredit', label: 'Store credit' }, { value: 'exchange', label: 'Exchange' }]}
                    onChange={setRefund}
                    disabled={locked}
                  />
                </Field>
                <Field>
                  <FieldLabel>Who pays return shipping?</FieldLabel>
                  <Choices
                    dataComponent="policyFormShippingBy"
                    name="Return shipping paid by"
                    value={shippingBy}
                    options={[{ value: 'customer', label: 'The customer' }, { value: 'store', label: 'The store' }]}
                    onChange={setShippingBy}
                    disabled={locked}
                  />
                </Field>
              </>
            )}
            <Field>
              <FieldLabel>Items that can never be returned <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
              <Input data-component="policyFormNotReturnable" value={notReturnable} maxLength={TEXT_MAX} placeholder="Opened coffee bags" disabled={locked} onChange={(event) => setNotReturnable(event.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Days to report a defective or wrong item</FieldLabel>
              <Input data-component="policyFormDefectDays" inputMode="numeric" value={defectDays} disabled={locked} onChange={(event) => setDefectDays(event.target.value)} />
            </Field>
          </>
        )}
        {component.section === 'other' && (
          <>
            <Field>
              <FieldLabel>Do you message customers about offers?</FieldLabel>
              <Choices dataComponent="policyFormOffers" name="Sends offers" value={offers} options={[...YES_NO]} onChange={setOffers} disabled={locked} />
            </Field>
            <Field>
              <FieldLabel>Anything else customers should know?</FieldLabel>
              <Textarea data-component="policyFormTerms" value={terms} maxLength={TERMS_MAX} disabled={locked} onChange={(event) => setTerms(event.target.value)} />
            </Field>
          </>
        )}
      </div>
    </ChatCard>
  );
}

