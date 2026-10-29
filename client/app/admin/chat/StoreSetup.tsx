'use client';
import { useState } from 'react';
import { CheckIcon, LocateFixedIcon, MapPinIcon, StoreIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardFooter, CardHeader, CardPanel, CardTitle } from '@/components/ui/card';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import type { StoreResponse } from '@/app/schemas/StoreResponseSchema';
import { useAdminWorkspace } from './AdminWorkspace';

type Currency = StoreResponse['currency'];

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

// A new admin's first screen, laid out as the assistant's first message: the
// chat needs a store, so the store's details come first. The assistant will
// ask for these itself once the chat can run before a store exists.
export function StoreSetup() {
  const { userData } = useAuth();
  const { userId, reloadStore } = useAdminWorkspace();
  const { server } = useServer();
  const { adminRequest } = useAuth();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [currency, setCurrency] = useState<Currency>('PHP');
  const [contactNumber, setContactNumber] = useState('');
  const [address, setAddress] = useState('');
  const [openingHours, setOpeningHours] = useState('');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<StoreResponse | null>(null);
  const [pinning, setPinning] = useState(false);
  const [finishing, setFinishing] = useState(false);

  const firstName = userData?.name?.trim().split(/\s+/)[0];
  const canCreate = [name, description, contactNumber, address].every((value) => value.trim().length > 0);

  const create = async () => {
    setCreating(true);
    const hours = openingHours.trim();
    const res = await adminRequest(() => server.createUserStore(
      userId, name.trim(), description.trim(), contactNumber.trim(), address.trim(), currency, hours === '' ? null : hours,
    ));
    setCreating(false);
    showApiResponseSnackbar(res);
    if (res.status === 200) setCreated(res.data);
  };

  const finish = async () => {
    setFinishing(true);
    await reloadStore();
    setFinishing(false);
  };

  const pinHere = () => {
    if (!('geolocation' in navigator)) {
      showSnackbar('This browser can’t share its location. You can pin the store later.', 'warning');
      return;
    }
    setPinning(true);
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const res = await adminRequest(() => server.setUserStoreLocation(
          userId, position.coords.latitude, position.coords.longitude,
        ));
        setPinning(false);
        showApiResponseSnackbar(res);
        if (res.status === 200) await finish();
      },
      () => {
        setPinning(false);
        showSnackbar('We couldn’t get your location. Allow location access, or pin the store later.', 'warning');
      },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  };

  return (
    <div data-component="adminSetup" className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-8 sm:px-6">
        <p className="text-[15px] leading-6 sm:text-base">
          Welcome{firstName ? `, ${firstName}` : ''}! Let’s get your store ready. I only need a few details. Your contact
          number and address are shown to everyone who visits your store.
        </p>

        {created ? (
          <>
            <section
              aria-label="Store details, done"
              className="flex items-start gap-3 rounded-2xl border bg-card p-4 shadow-xs/5"
            >
              <span className="inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-success/12 text-success-foreground">
                <CheckIcon className="size-3.5" />
              </span>
              <div className="flex min-w-0 flex-col gap-0.5 text-sm">
                <span className="font-medium">Store created</span>
                <span className="text-muted-foreground">
                  {[created.name, created.currency, created.contactNumber].filter(Boolean).join(' · ')}
                </span>
                <span className="text-muted-foreground">
                  {[created.address, created.openingHours].filter(Boolean).join(' · ')}
                </span>
              </div>
            </section>
            {created.slug && (
              <p className="text-[15px] leading-6 sm:text-base">
                Your store is live at <strong className="font-medium">/s/{created.slug}</strong>. Next, show customers where to
                find you: stand at your store and share your location.
              </p>
            )}
            <Card data-component="setupPin">
              <CardHeader className="p-5">
                <CardTitle className="flex items-center gap-2 font-medium font-sans text-base">
                  <MapPinIcon className="size-4.5" />
                  Pin your store
                </CardTitle>
              </CardHeader>
              <CardPanel className="p-5 text-muted-foreground text-sm">
                Customers see the pin when they ask where you are or choose pickup.
              </CardPanel>
              <CardFooter className="justify-end gap-2 p-5">
                <Button data-component="setupPinSkip" variant="ghost" loading={finishing && !pinning} onClick={finish}>
                  Later
                </Button>
                <Button data-component="setupPinMyLocation" loading={pinning} onClick={pinHere}>
                  <LocateFixedIcon />
                  Use where I am
                </Button>
              </CardFooter>
            </Card>
            <p className="text-[15px] leading-6 sm:text-base">
              After this, I’ll help you add your first products and answer a few policy questions.
            </p>
          </>
        ) : (
          <Card data-component="setupDetails">
            <CardHeader className="p-5">
              <CardTitle className="flex items-center gap-2 font-medium font-sans text-base">
                <StoreIcon className="size-4.5" />
                Your store
              </CardTitle>
            </CardHeader>
            <CardPanel className="p-5">
              <form
                id="setupDetailsForm"
                className="grid gap-4 sm:grid-cols-2"
                onSubmit={(event) => { event.preventDefault(); if (canCreate) create(); }}
              >
                <Field className="sm:col-span-2">
                  <FieldLabel>Store name</FieldLabel>
                  <Input
                    data-component="setupName"
                    value={name}
                    maxLength={NAME_MAX}
                    placeholder="Kalye Coffee Roasters"
                    required
                    onChange={(event) => setName(event.target.value)}
                  />
                </Field>
                <Field className="sm:col-span-2">
                  <FieldLabel>What you sell</FieldLabel>
                  <Textarea
                    data-component="setupDescription"
                    value={description}
                    maxLength={DESCRIPTION_MAX}
                    placeholder="Small-batch coffee beans from Benguet, Sagada and Batangas"
                    required
                    onChange={(event) => setDescription(event.target.value)}
                  />
                </Field>
                <Field>
                  <FieldLabel>Currency</FieldLabel>
                  <Select
                    items={CURRENCIES}
                    value={currency}
                    onValueChange={(value) => { if (value) setCurrency(value); }}
                  >
                    <SelectTrigger data-component="setupCurrency">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectPopup>
                      {CURRENCIES.map((item) => (
                        <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                      ))}
                    </SelectPopup>
                  </Select>
                </Field>
                <Field>
                  <FieldLabel>Contact number</FieldLabel>
                  <Input
                    data-component="setupContactNumber"
                    type="tel"
                    inputMode="tel"
                    autoComplete="tel"
                    value={contactNumber}
                    placeholder="+639171234567"
                    required
                    onChange={(event) => setContactNumber(event.target.value)}
                  />
                  <FieldDescription>With the country code.</FieldDescription>
                </Field>
                <Field className="sm:col-span-2">
                  <FieldLabel>Address</FieldLabel>
                  <Input
                    data-component="setupAddress"
                    autoComplete="street-address"
                    value={address}
                    maxLength={ADDRESS_MAX}
                    placeholder="14 Maginhawa St, Teachers Village, Quezon City"
                    required
                    onChange={(event) => setAddress(event.target.value)}
                  />
                </Field>
                <Field className="sm:col-span-2">
                  <FieldLabel>Opening hours <span className="font-normal text-muted-foreground">(optional)</span></FieldLabel>
                  <Input
                    data-component="setupOpeningHours"
                    value={openingHours}
                    maxLength={OPENING_HOURS_MAX}
                    placeholder="Mon–Sat, 8am–6pm"
                    onChange={(event) => setOpeningHours(event.target.value)}
                  />
                </Field>
              </form>
            </CardPanel>
            <CardFooter className="justify-end p-5">
              <Button
                data-component="setupCreate"
                type="submit"
                form="setupDetailsForm"
                disabled={!canCreate}
                loading={creating}
              >
                Create my store
              </Button>
            </CardFooter>
          </Card>
        )}
      </div>
    </div>
  );
}
