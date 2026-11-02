'use client';
import { ChatComponents } from '@/components/chat/cards/ChatComponents';
import type { ChatComponent } from '@/components/chat/cards/types';

// Sample data for the landing page's screenshots: Kalye Coffee, a made-up
// store. The photos are the mock ones in public/landing/; swap them for real
// generated photos and rerun scripts/landingShots.mjs.
const PRODUCT_PROMPT = 'A professional product photo of the product in the reference image. Keep the product, its label and its colors exactly as they are.\nProduct: Barako Blend 500g\nBrand colors: #6B2E1A (primary), #E8B44A (secondary).';

const SHOTS: { id: string; component: ChatComponent }[] = [
  {
    id: 'shotPhotoChoice',
    component: {
      kind: 'productPhotoChoice',
      formId: 'f1',
      productId: 'p1',
      productName: 'Barako Blend 500g',
      notGenerated: null,
      photos: [
        { _id: 'o', url: '/landing/original.webp', source: 'original', prompt: null },
        { _id: 'a', url: '/landing/catalog.webp', source: 'generated', prompt: `${PRODUCT_PROMPT}\nShot: Catalog` },
        { _id: 'b', url: '/landing/inuse.webp', source: 'generated', prompt: `${PRODUCT_PROMPT}\nShot: In use` },
        { _id: 'c', url: '/landing/hero.webp', source: 'generated', prompt: `${PRODUCT_PROMPT}\nShot: Hero` },
      ],
    },
  },
  {
    id: 'shotPhotoColors',
    component: {
      kind: 'photoColors',
      formId: 'f2',
      focus: 'logo',
      focusFound: true,
      colors: [
        { hex: '#6B2E1A', share: 0.46, neutral: false },
        { hex: '#E8B44A', share: 0.18, neutral: false },
        { hex: '#F6EBD9', share: 0.31, neutral: true },
        { hex: '#3E1A0F', share: 0.05, neutral: false },
      ],
    },
  },
  {
    id: 'shotStoreProducts',
    component: {
      kind: 'storeProducts',
      currency: 'PHP',
      products: [
        { _id: 'p1', name: 'Barako Blend 500g', price: 620, inventory: 24, hasInventory: true, reservedQuantity: 0, isAvailable: true, soldCount: 0, photoUrl: '/landing/cut-pouch.webp' },
        { _id: 'p2', name: 'Drip Bags, 10 pack', price: 380, inventory: 40, hasInventory: true, reservedQuantity: 0, isAvailable: true, soldCount: 0, photoUrl: '/landing/cut-box.webp' },
        { _id: 'p3', name: 'Kalye Enamel Mug', price: 450, inventory: 12, hasInventory: true, reservedQuantity: 0, isAvailable: true, soldCount: 0, photoUrl: '/landing/cut-mug.webp' },
      ],
    },
  },
  {
    id: 'shotCheckoutSummary',
    component: {
      kind: 'checkoutSummary',
      formId: 'f4',
      currency: 'PHP',
      items: [
        { name: 'Barako Blend 500g', price: 620, quantity: 2, lineTotal: 1240 },
        { name: 'Drip Bags, 10 pack', price: 380, quantity: 1, lineTotal: 380 },
      ],
      subtotal: 1620,
      fulfillment: 'delivery',
      deliveryZone: 'Marikina',
      deliveryFee: 0,
      total: 1620,
      readyInDays: 2,
      deliveryAddress: '12 Bayabas St, Concepcion Uno, Marikina',
      deliveryLatitude: 14.6507,
      deliveryLongitude: 121.1029,
      deliveryNotes: 'Green gate beside the sari-sari store',
    },
  },
];

const noop = () => {};

export function LandingShots() {
  return (
    <div data-component="landingShots" className="flex flex-col items-start gap-10 bg-background p-10">
      {SHOTS.map((shot) => (
        <div key={shot.id} id={shot.id} className="w-[560px] bg-background p-4">
          <ChatComponents components={[shot.component]} place={'formId' in shot.component ? 'below' : 'above'} answeredFormIds={new Set()} busy={false} onAnswer={noop} />
        </div>
      ))}
    </div>
  );
}
