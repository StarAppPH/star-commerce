'use client';
import { useState } from 'react';
import { ShoppingBagIcon, ShoppingCartIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChatCard, ProductPhoto, QuantityStepper, SentNote, countsStock, stockMax } from './ChatCard';
import { formatMoney, nonEmpty } from './format';
import { useOptionalShopCart } from '@/lib/ShopCartContext';
import { useOptionalChatShell } from '@/components/chat/ChatShell';
import type { ComponentProps } from './types';

// What the chat is told after an add, so the assistant can offer checkout
// (chatModel's customer prompt answers it).
function addedMessage(lines: { name: string; quantity: number }[]): string {
  return `I added ${lines.map((line) => `${line.quantity} × ${line.name}`).join(', ')} to my cart.`;
}

// The products a shopper asked about: pick quantities and add them to the
// cart (checked out from the cart in the shop's header, or in the chat).
// A card answered before the cart (an order placed from it) says so.
export function ShopProductsCard({ component, answered, busy }: ComponentProps<'shopProducts'>) {
  const shopCart = useOptionalShopCart();
  const shell = useOptionalChatShell();
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const picked = component.products.filter((product) => (quantities[product._id] ?? 0) > 0);
  const total = picked.reduce((sum, product) => sum + product.price * (quantities[product._id] ?? 0), 0);
  const count = picked.reduce((sum, product) => sum + (quantities[product._id] ?? 0), 0);

  const working = busy || (shopCart?.busy ?? false);

  const addToCart = async () => {
    const items = nonEmpty(picked.map((product) => ({ productId: product._id, quantity: quantities[product._id] ?? 0 })));
    if (!items || !shopCart) return;
    if (!(await shopCart.add(items))) return;
    const lines = picked.map((product) => ({ name: product.name, quantity: quantities[product._id] ?? 0 }));
    setQuantities({});
    // The cart is saved either way; when no chat is on screen, or a reply is
    // still on its way, the snackbar alone confirms it.
    shell?.sendText(addedMessage(lines));
  };

  return (
    <ChatCard
      dataComponent="shopProductsCard"
      icon={<ShoppingBagIcon />}
      title={component.products.length === 1 ? '1 product' : `${component.products.length} products`}
      footer={answered ? <SentNote text="Ordered in the chat" /> : shopCart ? (
        <>
          {count > 0 && (
            <span className="me-auto text-muted-foreground text-sm">
              {count} {count === 1 ? 'item' : 'items'} · {formatMoney(total, component.currency)}
            </span>
          )}
          <Button data-component="shopProductsAddToCart" disabled={count === 0 || working} onClick={() => { void addToCart(); }}>
            <ShoppingCartIcon />
            Add {count > 0 ? `${count} ${count === 1 ? 'item' : 'items'} ` : ''}to cart
          </Button>
        </>
      ) : undefined}
    >
      <ul className="divide-y">
        {component.products.map((product) => {
          // A product with no stock count (hasInventory false) is never sold out.
          const soldOut = countsStock(product) && product.inventory === 0;
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
                  ) : countsStock(product) && product.inventory <= 5 ? (
                    <span className="text-muted-foreground text-xs">{product.inventory} left</span>
                  ) : null}
                </span>
              </div>
              {!answered && shopCart && (
                <QuantityStepper
                  dataComponent="shopProductsQuantity"
                  label={product.name}
                  value={quantities[product._id] ?? 0}
                  max={stockMax(product)}
                  disabled={working}
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
