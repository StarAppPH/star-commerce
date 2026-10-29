'use client';
import { useState } from 'react';
import { StoreIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { ChatCard, SentNote } from './ChatCard';
import type { ComponentProps } from './types';

type Currency = ComponentProps<'storeDetailsForm'>['component']['currency'];

// The store's currencies (Store.currency), most likely first.
const CURRENCIES: { value: Currency; label: string }[] = [
  { value: 'PHP', label: 'PHP · Philippine peso' },
  { value: 'USD', label: 'USD · US dollar' },
  { value: 'SGD', label: 'SGD · Singapore dollar' },
  { value: 'MYR', label: 'MYR · Malaysian ringgit' },
  { value: 'THB', label: 'THB · Thai baht' },
  { value: 'IDR', label: 'IDR · Indonesian rupiah' },
  { value: 'VND', label: 'VND · Vietnamese dong' },
  { value: 'HKD', label: 'HKD · Hong Kong dollar' },
  { value: 'CNY', label: 'CNY · Chinese yuan' },
  { value: 'JPY', label: 'JPY · Japanese yen' },
  { value: 'KRW', label: 'KRW · South Korean won' },
  { value: 'INR', label: 'INR · Indian rupee' },
  { value: 'AED', label: 'AED · UAE dirham' },
  { value: 'AUD', label: 'AUD · Australian dollar' },
  { value: 'CAD', label: 'CAD · Canadian dollar' },
  { value: 'EUR', label: 'EUR · Euro' },
  { value: 'GBP', label: 'GBP · British pound' },
];

// The server's limits (objects/store.py).
const NAME_MAX = 60;
const DESCRIPTION_MAX = 300;
const ADDRESS_MAX = 200;
const OPENING_HOURS_MAX = 100;
const CONTACT_NUMBER_MAX = 32;

// The store's details, prefilled with what the owner told the assistant.
export function StoreDetailsForm({ component, answered, busy, onAnswer }: ComponentProps<'storeDetailsForm'>) {
  const [name, setName] = useState(component.name ?? '');
  const [description, setDescription] = useState(component.description ?? '');
  const [currency, setCurrency] = useState<Currency>(component.currency);
  const [contactNumber, setContactNumber] = useState(component.contactNumber ?? '');
  const [address, setAddress] = useState(component.address ?? '');
  const [openingHours, setOpeningHours] = useState(component.openingHours ?? '');
  const locked = answered || busy;
  const complete = [name, description, contactNumber, address].every((value) => value.trim() !== '');
  const formId = `storeDetails-${component.formId}`;

  const send = () => {
    const hours = openingHours.trim();
    onAnswer({
      kind: 'storeDetails',
      formId: component.formId,
      name: name.trim(),
      description: description.trim(),
      currency,
      contactNumber: contactNumber.trim(),
      address: address.trim(),
      openingHours: hours === '' ? null : hours,
    });
  };

  return (
    <ChatCard
      dataComponent="storeDetailsForm"
      icon={<StoreIcon />}
      title="Your store"
      footer={answered ? <SentNote /> : (
        <Button data-component="storeDetailsSend" type="submit" form={formId} disabled={!complete || busy}>
          Create my store
        </Button>
      )}
    >
      <form
        id={formId}
        className="grid gap-4 p-4 sm:grid-cols-2"
        onSubmit={(event) => { event.preventDefault(); if (complete && !locked) send(); }}
      >
        <Field className="sm:col-span-2">
          <FieldLabel>Store name</FieldLabel>
          <Input data-component="storeDetailsName" value={name} maxLength={NAME_MAX} placeholder="Kalye Coffee Roasters" disabled={locked} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field className="sm:col-span-2">
          <FieldLabel>What you sell</FieldLabel>
          <Textarea data-component="storeDetailsDescription" value={description} maxLength={DESCRIPTION_MAX} placeholder="Small-batch coffee beans from Benguet and Sagada" disabled={locked} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field>
          <FieldLabel>Currency</FieldLabel>
          <Select items={CURRENCIES} value={currency} disabled={locked} onValueChange={(value) => { if (value) setCurrency(value); }}>
            <SelectTrigger data-component="storeDetailsCurrency">
              <SelectValue />
            </SelectTrigger>
            <SelectPopup>
              {CURRENCIES.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
            </SelectPopup>
          </Select>
        </Field>
        <Field>
          <FieldLabel>Contact number</FieldLabel>
          <Input data-component="storeDetailsContactNumber" type="tel" inputMode="tel" autoComplete="tel" value={contactNumber} maxLength={CONTACT_NUMBER_MAX} placeholder="+639171234567" disabled={locked} onChange={(event) => setContactNumber(event.target.value)} />
          <FieldDescription>With the country code. Every visitor sees it.</FieldDescription>
        </Field>
        <Field className="sm:col-span-2">
          <FieldLabel>Address</FieldLabel>
          <Input data-component="storeDetailsAddress" autoComplete="street-address" value={address} maxLength={ADDRESS_MAX} placeholder="14 Maginhawa St, Quezon City" disabled={locked} onChange={(event) => setAddress(event.target.value)} />
        </Field>
        <Field className="sm:col-span-2">
          <FieldLabel>Opening hours <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
          <Input data-component="storeDetailsOpeningHours" value={openingHours} maxLength={OPENING_HOURS_MAX} placeholder="Mon–Sat, 8am–6pm" disabled={locked} onChange={(event) => setOpeningHours(event.target.value)} />
        </Field>
      </form>
    </ChatCard>
  );
}
