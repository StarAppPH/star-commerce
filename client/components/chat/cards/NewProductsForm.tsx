'use client';
import { useEffect, useId, useRef, useState } from 'react';
import Image from 'next/image';
import { ImagePlusIcon, PackagePlusIcon, PlusIcon, Trash2Icon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { auth } from '@/lib/firebase';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { nonEmpty } from './format';
import { PHOTO_ACCEPT, photoProblem, uploadChatPhoto } from './photoUpload';
import type { ComponentProps } from './types';

// The server's limits (objects/product.py).
const NAME_MAX = 80;
const DESCRIPTION_MAX = 500;
const ROWS_MAX = 50;

interface Photo {
  file: File;
  preview: string;
}

interface Row {
  key: number;
  name: string;
  price: string;
  inventory: string;
  description: string;
  // Its photo, uploaded when the form is sent; the assistant then offers it
  // and styled versions of it to pick from.
  photo: Photo | null;
}

type TextField = 'name' | 'price' | 'inventory' | 'description';

function validRow(row: Row): boolean {
  const price = Number(row.price);
  const inventory = Number(row.inventory);
  return row.name.trim() !== '' && row.price.trim() !== '' && Number.isFinite(price) && price >= 0
    && row.inventory.trim() !== '' && Number.isInteger(inventory) && inventory >= 0;
}

// New products, one row each, prefilled with what the owner told the
// assistant, each with an optional photo.
export function NewProductsForm({ component, answered, busy, onAnswer }: ComponentProps<'newProductsForm'>) {
  const { showSnackbar } = useSnackbar();
  const [uploading, setUploading] = useState(false);
  const [rows, setRows] = useState<Row[]>(() => {
    const drafts = component.products.length > 0 ? component.products : [null];
    return drafts.map((draft, index) => ({
      key: index,
      name: draft?.name ?? '',
      price: draft?.price != null ? String(draft.price) : '',
      inventory: draft?.inventory != null ? String(draft.inventory) : '',
      description: draft?.description ?? '',
      photo: null,
    }));
  });
  const [nextKey, setNextKey] = useState(rows.length);
  const locked = answered || busy || uploading;
  const ready = rows.length > 0 && rows.every(validRow);
  const rowsRef = useRef<Row[]>([]);

  // The previews still shown are let go when the form goes.
  useEffect(() => { rowsRef.current = rows; }, [rows]);
  useEffect(() => () => { rowsRef.current.forEach((row) => { if (row.photo) URL.revokeObjectURL(row.photo.preview); }); }, []);

  const update = (key: number, field: TextField, value: string) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
  };

  const setPhoto = (key: number, file: File | null) => {
    if (file) {
      const problem = photoProblem(file);
      if (problem) {
        showSnackbar(problem, 'error');
        return;
      }
    }
    setRows((current) => current.map((row) => {
      if (row.key !== key) return row;
      if (row.photo) URL.revokeObjectURL(row.photo.preview);
      return { ...row, photo: file ? { file, preview: URL.createObjectURL(file) } : null };
    }));
  };

  const removeRow = (key: number) => {
    setRows((current) => current.filter((row) => {
      if (row.key === key && row.photo) URL.revokeObjectURL(row.photo.preview);
      return row.key !== key;
    }));
  };

  const add = () => {
    setRows((current) => [...current, { key: nextKey, name: '', price: '', inventory: '', description: '', photo: null }]);
    setNextKey((key) => key + 1);
  };

  const send = async () => {
    const user = auth.currentUser;
    if (rows.some((row) => row.photo) && !user) {
      showSnackbar('Sign in again to upload photos.', 'error');
      return;
    }
    setUploading(true);
    try {
      const imageUrls = await Promise.all(rows.map((row) => (row.photo && user ? uploadChatPhoto(user, row.photo.file) : null)));
      const products = nonEmpty(rows.map((row, index) => ({
        name: row.name.trim(),
        price: Number(row.price),
        inventory: Number(row.inventory),
        description: row.description.trim() === '' ? null : row.description.trim(),
        isAvailable: true,
        imageUrl: imageUrls[index],
      })));
      if (products) onAnswer({ kind: 'newProducts', formId: component.formId, products });
    } catch {
      showSnackbar('The photos didn’t upload. Check your connection and try again.', 'error');
    } finally {
      setUploading(false);
    }
  };

  return (
    <ChatCard
      dataComponent="newProductsForm"
      icon={<PackagePlusIcon />}
      title="New products"
      footer={answered ? <SentNote /> : (
        <>
          {rows.length < ROWS_MAX && (
            <Button data-component="newProductsAdd" variant="ghost" className="me-auto" disabled={locked} onClick={add}>
              <PlusIcon />
              Another product
            </Button>
          )}
          <Button data-component="newProductsSend" loading={uploading} disabled={!ready || busy} onClick={send}>
            Add {rows.length === 1 ? 'product' : `${rows.length} products`}
          </Button>
        </>
      )}
    >
      <div className="flex flex-col divide-y">
        {rows.map((row, index) => (
          <fieldset key={row.key} className="grid grid-cols-2 gap-2 px-4 py-3 sm:grid-cols-[auto_minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]">
            <legend className="sr-only">Product {index + 1}</legend>
            <RowPhoto
              photo={row.photo}
              index={index}
              disabled={locked}
              onPick={(file) => setPhoto(row.key, file)}
              onRemove={() => setPhoto(row.key, null)}
            />
            <Input
              data-component="newProductsName"
              aria-label={`Product ${index + 1} name`}
              className="col-span-2 sm:col-span-1"
              placeholder="Name"
              value={row.name}
              maxLength={NAME_MAX}
              disabled={locked}
              onChange={(event) => update(row.key, 'name', event.target.value)}
            />
            <Input
              data-component="newProductsPrice"
              aria-label={`Product ${index + 1} price in ${component.currency}`}
              placeholder={`Price (${component.currency})`}
              inputMode="decimal"
              value={row.price}
              disabled={locked}
              onChange={(event) => update(row.key, 'price', event.target.value)}
            />
            <Input
              data-component="newProductsInventory"
              aria-label={`Product ${index + 1} units in stock`}
              placeholder="In stock"
              inputMode="numeric"
              value={row.inventory}
              disabled={locked}
              onChange={(event) => update(row.key, 'inventory', event.target.value)}
            />
            <Button
              data-component="newProductsRemove"
              variant="ghost"
              size="icon"
              aria-label={`Remove product ${index + 1}`}
              className="hidden sm:inline-flex"
              disabled={locked || rows.length === 1}
              onClick={() => removeRow(row.key)}
            >
              <Trash2Icon />
            </Button>
            <Input
              data-component="newProductsDescription"
              aria-label={`Product ${index + 1} description`}
              className="col-span-2 sm:col-span-3"
              placeholder="Description (optional)"
              value={row.description}
              maxLength={DESCRIPTION_MAX}
              disabled={locked}
              onChange={(event) => update(row.key, 'description', event.target.value)}
            />
          </fieldset>
        ))}
      </div>
    </ChatCard>
  );
}

interface RowPhotoProps {
  photo: Photo | null;
  index: number;
  disabled: boolean;
  onPick: (file: File) => void;
  onRemove: () => void;
}

// A product row's photo: a square beside its fields, "Add photo" until one
// is picked, then its preview.
function RowPhoto({ photo, index, disabled, onPick, onRemove }: RowPhotoProps) {
  const inputId = useId();
  if (photo) {
    return (
      <div data-component="newProductsPhoto" className="relative col-span-2 size-20 overflow-hidden rounded-lg bg-muted sm:col-span-1 sm:row-span-2">
        <Image
          src={photo.preview}
          alt={`Product ${index + 1} photo`}
          fill
          sizes="80px"
          // A preview of a file not uploaded yet (a blob: URL in this
          // browser): no server can fetch it to optimize.
          // eslint-disable-next-line no-restricted-syntax
          unoptimized
          className="object-cover"
        />
        <Button
          data-component="newProductsPhotoRemove"
          variant="secondary"
          size="icon-xs"
          aria-label={`Remove product ${index + 1} photo`}
          className="absolute top-1 right-1"
          disabled={disabled}
          onClick={onRemove}
        >
          <XIcon />
        </Button>
      </div>
    );
  }
  return (
    <label
      htmlFor={inputId}
      data-component="newProductsPhoto"
      className="col-span-2 flex size-20 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border border-dashed text-center text-muted-foreground text-xs hover:bg-accent/50 has-disabled:pointer-events-none has-disabled:opacity-64 sm:col-span-1 sm:row-span-2"
    >
      <ImagePlusIcon className="size-5" />
      Add photo
      <input
        id={inputId}
        data-component="newProductsPhotoFile"
        type="file"
        accept={PHOTO_ACCEPT}
        className="sr-only"
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onPick(file);
          event.target.value = '';
        }}
      />
    </label>
  );
}
