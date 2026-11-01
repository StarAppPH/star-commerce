'use client';
import { useState } from 'react';
import { ShoppingCartIcon, Trash2Icon, XIcon } from 'lucide-react';
import type { CartScreenItemRow } from '@/app/schemas/CartScreenResponseSchema';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty';
import { Sheet, SheetClose, SheetPopup, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { ProductPhoto, QuantityStepper } from '@/components/chat/cards/ChatCard';
import { formatMoney } from '@/components/chat/cards/format';
import { useChatShell } from '@/components/chat/ChatShell';
import { useShopCart } from '@/lib/ShopCartContext';
import { useSnackbar } from '@/lib/SnackbarContext';

// What the chat is asked when the shopper checks out from the panel.
const CHECKOUT_MESSAGE = 'Check out my cart';

// Why a row cannot be checked out as it is: the store has fewer left.
function stockProblem(item: CartScreenItemRow): string | null {
  if (item.inventory === 0) return 'Sold out';
  if (item.quantity > item.inventory) return `Only ${item.inventory} left`;
  return null;
}

// The shopper's cart, in a drawer opened from the shop's header. Checkout
// hands over to the chat, which asks pickup or delivery and shows the
// checkout card.
export function ShopCart() {
  const { cart, count, busy, setQuantity, remove, clear } = useShopCart();
  const shell = useChatShell();
  const { showSnackbar } = useSnackbar();
  const [open, setOpen] = useState(false);

  const items = cart?.items ?? [];
  const blocked = items.some((item) => stockProblem(item) !== null);

  const checkout = () => {
    if (!shell.sendText(CHECKOUT_MESSAGE)) {
      showSnackbar('Open a chat to check out.', 'error');
      return;
    }
    setOpen(false);
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger
        render={(
          <Button
            data-component="shopCartButton"
            variant="ghost"
            size="icon-lg"
            className="relative"
            aria-label={count > 0 ? `My cart, ${count} ${count === 1 ? 'item' : 'items'}` : 'My cart'}
          />
        )}
      >
        <ShoppingCartIcon />
        {count > 0 && (
          <span
            aria-hidden="true"
            className="absolute -end-0.5 -top-0.5 flex min-w-4.5 items-center justify-center rounded-full bg-primary px-1 font-medium text-[10px] text-primary-foreground leading-4.5"
          >
            {count > 99 ? '99+' : count}
          </span>
        )}
      </SheetTrigger>
      <SheetPopup side="right" showCloseButton={false} className="max-w-96 gap-0 p-0" data-component="shopCart">
        <div className="flex items-center gap-2.5 border-b px-4 py-3">
          <ShoppingCartIcon aria-hidden="true" className="size-4 text-muted-foreground" />
          <SheetTitle className="min-w-0 flex-1 truncate font-medium font-sans text-[15px]">My cart</SheetTitle>
          <SheetClose render={<Button data-component="shopCartClose" variant="ghost" size="icon-lg" aria-label="Close" />}>
            <XIcon />
          </SheetClose>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {!cart && Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="mx-4 my-3 h-12 rounded-lg" />
          ))}
          {cart && items.length === 0 && (
            <Empty data-component="shopCartEmpty" className="py-12">
              <EmptyHeader>
                <EmptyMedia variant="icon"><ShoppingCartIcon /></EmptyMedia>
                <EmptyTitle>Your cart is empty</EmptyTitle>
                <EmptyDescription>Add products from the chat, or ask the assistant to add them for you.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
          {cart && items.length > 0 && (
            <ul className="divide-y" data-component="shopCartItems">
              {items.map((item) => {
                const problem = stockProblem(item);
                return (
                  <li key={item.productId} className="flex items-center gap-3 px-4 py-3" data-component="shopCartItem">
                    <ProductPhoto dataComponent="shopCartPhoto" url={item.photoUrl} name={item.name} />
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <span className="truncate font-medium text-sm">{item.name}</span>
                      <span className="flex flex-wrap items-center gap-2 text-sm">
                        {formatMoney(item.price * item.quantity, cart.currency)}
                        {item.quantity > 1 && (
                          <span className="text-muted-foreground text-xs">{formatMoney(item.price, cart.currency)} each</span>
                        )}
                        {problem && <Badge variant="error" size="sm">{problem}</Badge>}
                      </span>
                      <QuantityStepper
                        dataComponent="shopCartQuantity"
                        label={item.name}
                        value={item.quantity}
                        // A row over the stock left can still come down.
                        max={Math.max(item.inventory, item.quantity)}
                        disabled={busy}
                        onChange={(value) => {
                          if (value !== item.quantity) void setQuantity(item.productId, value);
                        }}
                      />
                    </div>
                    <Button
                      data-component="shopCartRemove"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove ${item.name}`}
                      disabled={busy}
                      onClick={() => { void remove(item.productId); }}
                    >
                      <Trash2Icon />
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {cart && items.length > 0 && (
          <div className="flex flex-col gap-3 border-t bg-muted/40 px-4 py-3" data-component="shopCartFooter">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-muted-foreground text-sm">Subtotal · {count} {count === 1 ? 'item' : 'items'}</span>
              <span className="font-medium" data-component="shopCartSubtotal">{formatMoney(cart.subtotal, cart.currency)}</span>
            </div>
            {blocked && (
              <p className="text-destructive-foreground text-xs">Lower the items marked above to what is left, or remove them, to check out.</p>
            )}
            <p className="text-muted-foreground text-xs">Delivery, if you choose it, is added at checkout.</p>
            <div className="flex items-center gap-2">
              <Button
                data-component="shopCartClear"
                variant="ghost"
                disabled={busy}
                onClick={() => { void clear(); }}
              >
                Empty cart
              </Button>
              <Button
                data-component="shopCartCheckout"
                className="flex-1"
                disabled={busy || blocked}
                onClick={checkout}
              >
                Check out
              </Button>
            </div>
          </div>
        )}
      </SheetPopup>
    </Sheet>
  );
}
