'use client';
import { useState } from 'react';
import { PackagePlusIcon, PlusIcon, Trash2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ChatCard, SentNote } from './ChatCard';
import { nonEmpty } from './format';
import type { ComponentProps } from './types';

// The server's limits (objects/product.py).
const NAME_MAX = 80;
const DESCRIPTION_MAX = 500;
const ROWS_MAX = 50;

interface Row {
  key: number;
  name: string;
  price: string;
  inventory: string;
  description: string;
}

function validRow(row: Row): boolean {
  const price = Number(row.price);
  const inventory = Number(row.inventory);
  return row.name.trim() !== '' && row.price.trim() !== '' && Number.isFinite(price) && price >= 0
    && row.inventory.trim() !== '' && Number.isInteger(inventory) && inventory >= 0;
}

// New products, one row each, prefilled with what the owner told the
// assistant.
export function NewProductsForm({ component, answered, busy, onAnswer }: ComponentProps<'newProductsForm'>) {
  const [rows, setRows] = useState<Row[]>(() => {
    const drafts = component.products.length > 0 ? component.products : [null];
    return drafts.map((draft, index) => ({
      key: index,
      name: draft?.name ?? '',
      price: draft?.price != null ? String(draft.price) : '',
      inventory: draft?.inventory != null ? String(draft.inventory) : '',
      description: draft?.description ?? '',
    }));
  });
  const [nextKey, setNextKey] = useState(rows.length);
  const locked = answered || busy;
  const ready = rows.length > 0 && rows.every(validRow);

  const update = (key: number, field: keyof Omit<Row, 'key'>, value: string) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
  };

  const add = () => {
    setRows((current) => [...current, { key: nextKey, name: '', price: '', inventory: '', description: '' }]);
    setNextKey((key) => key + 1);
  };

  const send = () => {
    const products = nonEmpty(rows.map((row) => ({
      name: row.name.trim(),
      price: Number(row.price),
      inventory: Number(row.inventory),
      description: row.description.trim() === '' ? null : row.description.trim(),
      isAvailable: true,
    })));
    if (products) onAnswer({ kind: 'newProducts', formId: component.formId, products });
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
          <Button data-component="newProductsSend" disabled={!ready || busy} onClick={send}>
            Add {rows.length === 1 ? 'product' : `${rows.length} products`}
          </Button>
        </>
      )}
    >
      <div className="flex flex-col divide-y">
        {rows.map((row, index) => (
          <fieldset key={row.key} className="grid grid-cols-2 gap-2 px-4 py-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]">
            <legend className="sr-only">Product {index + 1}</legend>
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
              onClick={() => setRows((current) => current.filter((other) => other.key !== row.key))}
            >
              <Trash2Icon />
            </Button>
            <Input
              data-component="newProductsDescription"
              aria-label={`Product ${index + 1} description`}
              className="col-span-2 sm:col-span-4"
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
