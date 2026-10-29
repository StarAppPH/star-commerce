'use client';
import { useState } from 'react';
import { ShoppingBagIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChatCard, ProductPhoto, QuantityStepper, SentNote } from './ChatCard';
import { formatMoney, nonEmpty } from './format';
import type { ComponentProps } from './types';

// The products a shopper asked about: pick quantities, then order them
// through the chat (the assistant places the order).
export function ShopProductsCard({ component, answered, busy, onAnswer }: ComponentProps<'shopProducts'>) {
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const picked = component.products.filter((product) => (quantities[product._id] ?? 0) > 0);
  const total = picked.reduce((sum, product) => sum + product.price * (quantities[product._id] ?? 0), 0);
  const count = picked.reduce((sum, product) => sum + (quantities[product._id] ?? 0), 0);

  const order = () => {
    const items = nonEmpty(picked.map((product) => ({ productId: product._id, quantity: quantities[product._id] ?? 0 })));
    if (items) onAnswer({ kind: 'orderItems', formId: component.formId, items });
  };

  return (
    <ChatCard
      dataComponent="shopProductsCard"
      icon={<ShoppingBagIcon />}
      title={component.products.length === 1 ? '1 product' : `${component.products.length} products`}
      footer={answered ? <SentNote text="Ordered in the chat" /> : (
        <>
          {count > 0 && (
            <span className="me-auto text-muted-foreground text-sm">
              {count} {count === 1 ? 'item' : 'items'} · {formatMoney(total, component.currency)}
            </span>
          )}
          <Button data-component="shopProductsOrder" disabled={count === 0 || busy} onClick={order}>
            Order {count > 0 ? `${count} ${count === 1 ? 'item' : 'items'}` : ''}
          </Button>
        </>
      )}
    >
      <ul className="divide-y">
        {component.products.map((product) => {
          const soldOut = product.inventory === 0;
          return (
            <li key={product._id} className="flex items-center gap-3 px-4 py-3">
              <ProductPhoto dataComponent="shopProductsPhoto" url={product.photoUrl} name={product.name} />
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate font-medium text-sm">{product.name}</span>
                {product.description && (
                  <span className="line-clamp-2 text-muted-foreground text-xs">{product.description}</span>
                )}
                <span className="flex items-center gap-2 text-sm">
                  {formatMoney(product.price, component.currency)}
                  {soldOut ? (
                    <Badge variant="error" size="sm">Sold out</Badge>
                  ) : product.inventory <= 5 ? (
                    <span className="text-muted-foreground text-xs">{product.inventory} left</span>
                  ) : null}
                </span>
              </div>
              {!answered && (
                <QuantityStepper
                  dataComponent="shopProductsQuantity"
                  label={product.name}
                  value={quantities[product._id] ?? 0}
                  max={product.inventory}
                  disabled={busy}
                  onChange={(value) => setQuantities((current) => ({ ...current, [product._id]: value }))}
                />
              )}
            </li>
          );
        })}
      </ul>
    </ChatCard>
  );
}
