'use client';
import { useEffect, useId, useRef, useState } from 'react';
import Image from 'next/image';
import {
  ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, FileSpreadsheetIcon, ImageIcon, ImagePlusIcon, PackagePlusIcon, PlusIcon,
  Trash2Icon, TriangleAlertIcon, XIcon,
} from 'lucide-react';
import { useObjectUrls } from './useObjectUrls';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Input } from '@/components/ui/input';
import { auth, storage } from '@/lib/firebase';
import { ownFileUrl } from '../attachments';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { nonEmpty } from './format';
import { PHOTO_ACCEPT, photoProblem, uploadChatPhoto } from './photoUpload';
import type { ComponentProps } from './types';
import type { SpreadsheetImport } from '@/app/schemas/SpreadsheetImportSchema';
import {
  PRODUCT_BATCH_MAX, PRODUCT_DESCRIPTION_MAX_LENGTH, PRODUCT_IMPORT_ROWS_MAX, PRODUCT_NAME_MAX_LENGTH,
} from '@/lib/generated/serverConstants';

// The rows shown at once; the others are a page away, and all are sent.
const PAGE_SIZE = 25;
// What a row's stock starts at when the assistant did not prefill one.
const DEFAULT_INVENTORY = '1';

// A photo the owner picks here, uploaded when the form is sent, or one they
// attached in the chat that the assistant put on the row (its Storage path,
// shown and sent by its download URL).
type Photo =
  | { kind: 'file'; file: File; preview: string }
  | { kind: 'attached'; path: string; url: string | null };

interface Row {
  key: number;
  name: string;
  price: string;
  inventory: string;
  // Unticked: no stock count, customers order unlimited units.
  hasInventory: boolean;
  description: string;
  // Its photo; the assistant then offers it and styled versions of it to
  // pick from.
  photo: Photo | null;
}

type TextField = 'name' | 'price' | 'inventory' | 'description';

// The row's first field to fix, or null when it can be sent.
function invalidField(row: Row): 'name' | 'price' | 'inventory' | null {
  const price = Number(row.price);
  const inventory = Number(row.inventory);
  if (row.name.trim() === '') return 'name';
  if (row.price.trim() === '' || !Number.isFinite(price) || price < 0) return 'price';
  if (row.hasInventory && (row.inventory.trim() === '' || !Number.isInteger(inventory) || inventory < 0)) return 'inventory';
  return null;
}

function validRow(row: Row): boolean {
  return invalidField(row) === null;
}

// The aria-label each field of a row starts with, by its position.
const FIELD_LABELS = { name: 'name', price: 'price in', inventory: 'units in stock' } as const;

// New products, one row each, prefilled with what the owner told or sent the
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
      inventory: draft?.inventory != null ? String(draft.inventory) : DEFAULT_INVENTORY,
      hasInventory: draft?.hasInventory ?? true,
      description: draft?.description ?? '',
      photo: component.rowPhotos?.[index] ? { kind: 'attached', path: component.rowPhotos[index], url: null } : null,
    }));
  });
  const [nextKey, setNextKey] = useState(rows.length);
  const [page, setPage] = useState(0);
  const imported = component.spreadsheetImport ?? null;
  // A plain form takes a batch; one from a spreadsheet more.
  const rowsMax = imported ? PRODUCT_IMPORT_ROWS_MAX : PRODUCT_BATCH_MAX;
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  // A removed row may empty the last page: show the one before it.
  const currentPage = Math.min(page, pageCount - 1);
  const firstShown = currentPage * PAGE_SIZE;
  const shown = rows.slice(firstShown, firstShown + PAGE_SIZE);
  // The first row to fix, when it is on another page.
  const firstInvalid = rows.findIndex((row) => !validRow(row));
  const invalidElsewhere = firstInvalid >= 0 && Math.floor(firstInvalid / PAGE_SIZE) !== currentPage;
  const locked = answered || busy || uploading;
  const ready = rows.length > 0 && rows.every(validRow)
    && rows.every((row) => row.photo?.kind !== 'attached' || row.photo.url !== null);
  const previews = useObjectUrls();
  const rowsRef = useRef<Row[]>([]);
  // The rows shown, focused and scrolled to on a page change (from the
  // pager's handlers, so nothing moves on mount).
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => { rowsRef.current = rows; }, [rows]);

  // Each attached photo's download URL; a row whose file is gone (or not
  // the owner's) shows "Add photo" instead.
  useEffect(() => {
    let cancelled = false;
    rowsRef.current.forEach((row) => {
      const photo = row.photo;
      if (photo?.kind !== 'attached' || photo.url !== null) return;
      ownFileUrl(storage, photo.path)
        .then((url) => ({ kind: 'attached' as const, path: photo.path, url }))
        .catch(() => null)
        .then((resolved) => {
          if (cancelled) return;
          setRows((current) => current.map((r) => (r.key === row.key && r.photo === photo ? { ...r, photo: resolved } : r)));
        });
    });
    return () => { cancelled = true; };
  }, []);

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
    const preview = file ? previews.create(file) : null;
    setRows((current) => current.map((row) => {
      if (row.key !== key) return row;
      if (row.photo?.kind === 'file') previews.release(row.photo.preview);
      return { ...row, photo: file && preview ? { kind: 'file', file, preview } : null };
    }));
  };

  const setHasInventory = (key: number, hasInventory: boolean) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, hasInventory } : row)));
  };

  const removeRow = (key: number) => {
    setRows((current) => current.filter((row) => {
      if (row.key === key && row.photo?.kind === 'file') previews.release(row.photo.preview);
      return row.key !== key;
    }));
  };

  const add = () => {
    setRows((current) => [...current, { key: nextKey, name: '', price: '', inventory: DEFAULT_INVENTORY, hasInventory: true, description: '', photo: null }]);
    setNextKey((key) => key + 1);
    // The new row is the last: show its page.
    setPage(Math.floor(rows.length / PAGE_SIZE));
  };

  // Shows `target`, then moves focus to the rows (Previous/Next disable
  // themselves at the ends) and brings the page's top into view.
  const goToPage = (target: number, focus?: () => HTMLElement | null) => {
    setPage(target);
    requestAnimationFrame(() => {
      const list = listRef.current;
      if (!list) return;
      list.scrollIntoView({ block: 'start' });
      (focus?.() ?? list).focus({ preventScroll: true });
    });
  };

  // Shows the first row to fix and focuses its first field to fix.
  const goToInvalid = () => {
    const row = rows[firstInvalid];
    const field = row ? invalidField(row) : null;
    goToPage(Math.floor(firstInvalid / PAGE_SIZE), () => (field === null ? null : listRef.current?.querySelector<HTMLElement>(
      `[aria-label^="Product ${firstInvalid + 1} ${FIELD_LABELS[field]}"]`,
    ) ?? null));
  };

  const send = async () => {
    const user = auth.currentUser;
    if (rows.some((row) => row.photo?.kind === 'file') && !user) {
      showSnackbar('Sign in again to upload photos.', 'error');
      return;
    }
    setUploading(true);
    try {
      const imageUrls = await Promise.all(rows.map((row) => {
        if (row.photo?.kind === 'attached') return row.photo.url;
        return row.photo && user ? uploadChatPhoto(user, row.photo.file) : null;
      }));
      const products = nonEmpty(rows.map((row, index) => ({
        name: row.name.trim(),
        price: Number(row.price),
        // No stock count: the server stores 0 and never lessens it.
        inventory: row.hasInventory ? Number(row.inventory) : null,
        hasInventory: row.hasInventory,
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
          {rows.length < rowsMax && (
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
      {imported && <ImportNote imported={imported} formRows={component.products.length} />}
      <div
        ref={listRef}
        tabIndex={-1}
        aria-label={`Products ${firstShown + 1}–${firstShown + shown.length}`}
        className="flex scroll-mt-4 flex-col divide-y outline-none"
      >
        {shown.map((row, shownIndex) => {
          const index = firstShown + shownIndex;
          return (
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
                maxLength={PRODUCT_NAME_MAX_LENGTH}
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
              {row.hasInventory ? (
                <Input
                  data-component="newProductsInventory"
                  aria-label={`Product ${index + 1} units in stock`}
                  placeholder="In stock"
                  inputMode="numeric"
                  value={row.inventory}
                  disabled={locked}
                  onChange={(event) => update(row.key, 'inventory', event.target.value)}
                />
              ) : (
                <span data-component="newProductsUnlimited" className="flex items-center px-3 text-muted-foreground text-sm">
                  Unlimited
                </span>
              )}
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
                maxLength={PRODUCT_DESCRIPTION_MAX_LENGTH}
                disabled={locked}
                onChange={(event) => update(row.key, 'description', event.target.value)}
              />
              <label className="col-span-2 flex items-center gap-2 text-sm sm:col-span-3 sm:col-start-2">
                <Checkbox
                  data-component="newProductsHasInventory"
                  checked={row.hasInventory}
                  disabled={locked}
                  onCheckedChange={(checked) => setHasInventory(row.key, checked === true)}
                />
                Track stock (orders lessen it; untick for unlimited)
              </label>
              <Button
                data-component="newProductsRemoveMobile"
                variant="ghost"
                size="sm"
                className="col-span-2 justify-self-end sm:hidden"
                aria-label={`Remove product ${index + 1}`}
                disabled={locked || rows.length === 1}
                onClick={() => removeRow(row.key)}
              >
                <Trash2Icon />
                Remove
              </Button>
            </fieldset>
          );
        })}
      </div>
      {pageCount > 1 && (
        <div data-component="newProductsFormPages" className="flex flex-col gap-2 border-t px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <Button
              data-component="newProductsFormPagePrevious"
              variant="outline"
              size="sm"
              disabled={currentPage === 0}
              onClick={() => goToPage(currentPage - 1)}
            >
              <ChevronLeftIcon />
              Previous
            </Button>
            <span className="text-muted-foreground text-sm" aria-live="polite">
              Products {firstShown + 1}–{firstShown + shown.length} of {rows.length}
            </span>
            <Button
              data-component="newProductsFormPageNext"
              variant="outline"
              size="sm"
              disabled={currentPage >= pageCount - 1}
              onClick={() => goToPage(currentPage + 1)}
            >
              Next
              <ChevronRightIcon />
            </Button>
          </div>
          {!answered && invalidElsewhere && (
            <Button
              data-component="newProductsFormFixRow"
              variant="link"
              size="sm"
              className="self-center"
              onClick={goToInvalid}
            >
              Product {firstInvalid + 1} needs a name, a price or its stock
            </Button>
          )}
        </div>
      )}
    </ChatCard>
  );
}

// Where a spreadsheet form's rows came from: the file and sheet, which rows
// (the sheet's own row numbers, header and blank rows counted, as in Excel),
// the ones left out and why, and what is left to import. `formRows` is how
// many products the form opened with; totalRows counts product rows. With a
// photo column, one line: how many rows got their photo from their link, and
// (opened) why the others did not.
function ImportNote({ imported, formRows }: { imported: SpreadsheetImport; formRows: number }) {
  const skipped = imported.skippedRows;
  const photoLinks = imported.photoLinks ?? null;
  return (
    <div data-component="newProductsFormImport" className="flex flex-col gap-2 border-b px-4 py-3 text-sm">
      <div className="flex items-start gap-2">
        <FileSpreadsheetIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium">
            {imported.fileName}
            {imported.sheet !== null && <span className="font-normal text-muted-foreground"> · {imported.sheet}</span>}
          </span>
          <span className="text-muted-foreground">
            {`Rows ${imported.firstRow}–${imported.lastRow} of the sheet `}
            {`(${formRows} on the form, ${imported.totalRows} product rows in all)`}
          </span>
          {imported.nextFirstRow !== null && (
            <span className="text-muted-foreground">Rows {imported.nextFirstRow}+ can be imported next</span>
          )}
        </div>
      </div>
      {imported.notFullyRead && (
        <Alert variant="warning">
          <TriangleAlertIcon />
          <AlertDescription>The file was too large to read fully, so some of its rows aren’t here.</AlertDescription>
        </Alert>
      )}
      {skipped.length > 0 && (
        <RowReasons
          dataComponent="newProductsFormImportSkipped"
          toggleDataComponent="newProductsFormImportSkippedToggle"
          label={skipped.length === 1 ? '1 row skipped' : `${skipped.length} rows skipped`}
          rows={skipped}
        />
      )}
      {photoLinks !== null && (
        <div data-component="newProductsFormImportPhotoLinks">
          {photoLinks.notFetched.length > 0 ? (
            <RowReasons
              dataComponent="newProductsFormImportPhotoLinksNotFetched"
              toggleDataComponent="newProductsFormImportPhotoLinksToggle"
              label={`${photosAdded(photoLinks.fetched)} · ${photoLinks.notFetched.length} couldn’t be used`}
              rows={photoLinks.notFetched}
            />
          ) : (
            <span className="flex min-h-6 items-center gap-1.5 text-muted-foreground text-xs">
              <ImageIcon aria-hidden="true" className="size-3.5" />
              {photoLinks.fetched === 0 ? 'No photo links in the photo column' : photosAdded(photoLinks.fetched)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// How many rows got their photo from their link, as the import note says it.
function photosAdded(fetched: number): string {
  if (fetched === 0) return 'No photos from links';
  return fetched === 1 ? '1 photo added from a link' : `${fetched} photos added from links`;
}

interface RowReason {
  row: number;
  name: string | null;
  reason: string;
}

interface RowReasonsProps {
  dataComponent: string;
  toggleDataComponent: string;
  label: string;
  rows: RowReason[];
}

// A folded list of spreadsheet rows, each with its product's name and why:
// the rows an import skipped, the photo links that gave no photo.
function RowReasons({ dataComponent, toggleDataComponent, label, rows }: RowReasonsProps) {
  return (
    <Collapsible data-component={dataComponent}>
      <CollapsibleTrigger
        data-component={toggleDataComponent}
        className="group -my-1 flex min-h-6 items-center gap-1.5 text-muted-foreground text-xs hover:text-foreground"
      >
        <ChevronDownIcon aria-hidden="true" className="size-3.5 transition-transform group-data-panel-open:rotate-180" />
        {label}
      </CollapsibleTrigger>
      <CollapsiblePanel>
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto pt-2 text-xs">
          {rows.map((one) => (
            <li key={one.row} className="flex gap-2">
              <span className="shrink-0 text-muted-foreground tabular-nums">Row {one.row}</span>
              <span className="min-w-0 break-words">
                {one.name !== null && <span className="font-medium">{one.name}: </span>}
                {one.reason}
              </span>
            </li>
          ))}
        </ul>
      </CollapsiblePanel>
    </Collapsible>
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
    const src = photo.kind === 'file' ? photo.preview : photo.url;
    return (
      <div data-component="newProductsPhoto" className="relative col-span-2 size-20 overflow-hidden rounded-lg bg-muted sm:col-span-1 sm:row-span-2">
        {src && (
          <Image
            src={src}
            alt={`Product ${index + 1} photo`}
            fill
            sizes="80px"
            // A preview of a file not uploaded yet (a blob: URL in this
            // browser) no server can fetch to optimize; an attached photo is
            // the owner's, never cached by the optimizer.
            // eslint-disable-next-line no-restricted-syntax
            unoptimized
            className="object-cover"
          />
        )}
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
