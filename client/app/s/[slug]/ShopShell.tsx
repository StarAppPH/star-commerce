'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { MenuIcon, PlusIcon, RotateCwIcon, SearchIcon, SquarePenIcon, StoreIcon, XIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Sheet, SheetClose, SheetPopup, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { StoreMark } from '@/components/brand/StoreMark';
import { ChatShellProvider } from '@/components/chat/ChatShell';
import { matchingConversations } from '@/components/chat/useConversationList';
import { useGuestSession } from '@/lib/GuestSessionContext';
import { groupConversationsByDay } from '@/lib/storeTime';
import { cn } from '@/lib/utils';
import { ShopWorkspaceProvider, useShopWorkspace } from './ShopWorkspace';

// A shop's voice glow. The store's own brand colors replace it once the
// customer-facing store carries them.
const SHOP_GLOW = '#EA580C';
const SHOP_GLOW_SECOND = '#F59E0B';

// Every shop page: the shopper's guest session, then the header and chats.
export function ShopShell({ children }: { children: React.ReactNode }) {
  const { state } = useGuestSession();

  if (state.status === 'loading') {
    return (
      <div className="flex min-h-dvh items-center justify-center gap-2 text-muted-foreground text-sm" aria-live="polite">
        <Spinner />
        Opening the store…
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <Empty data-component="shopUnavailable" className="min-h-dvh">
        <EmptyHeader>
          <EmptyMedia variant="icon"><StoreIcon /></EmptyMedia>
          <EmptyTitle>This store isn’t open right now</EmptyTitle>
          <EmptyDescription>{state.message}</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button data-component="shopRetry" variant="outline" onClick={() => window.location.reload()}>
            <RotateCwIcon />
            Try again
          </Button>
        </EmptyContent>
      </Empty>
    );
  }

  return (
    <ShopWorkspaceProvider session={state.session}>
      <ShopFrame>{children}</ShopFrame>
    </ShopWorkspaceProvider>
  );
}

function ShopFrame({ children }: { children: React.ReactNode }) {
  const { session, reloadConversations } = useShopWorkspace();
  const { store } = session;

  return (
    <ChatShellProvider
      voice={{
        title: store.name,
        subtitle: 'Ask me anything about the store',
        glow: SHOP_GLOW,
        glowSecond: SHOP_GLOW_SECOND,
        variant: 'shop',
      }}
      onConversationsChanged={reloadConversations}
    >
      <div data-component="shop" className="flex h-dvh flex-col bg-background">
        <header className="flex h-14 shrink-0 items-center justify-between gap-2 px-2">
          <ShopChats />
          <span className="min-w-0 truncate font-medium text-[15px]">{store.name}</span>
          <Button
            data-component="shopNewChat"
            variant="ghost"
            size="icon-lg"
            aria-label="New chat"
            render={<Link href={`/s/${store.slug}`} />}
          >
            <SquarePenIcon />
          </Button>
        </header>
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      </div>
    </ChatShellProvider>
  );
}

// The shopper's chats at this store, in a drawer.
function ShopChats() {
  const pathname = usePathname();
  const { session, conversations, conversationsLoading, hasOlderConversations, loadOlderConversations } = useShopWorkspace();
  const { store } = session;
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [loadingOlder, setLoadingOlder] = useState(false);

  const groups = useMemo(
    () => groupConversationsByDay(matchingConversations(conversations, search), (row) => row.lastMessageAt),
    [conversations, search],
  );
  const close = () => setOpen(false);

  const showOlder = async () => {
    setLoadingOlder(true);
    await loadOlderConversations();
    setLoadingOlder(false);
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger
        render={<Button data-component="shopMenu" variant="ghost" size="icon-lg" aria-label="My chats" />}
      >
        <MenuIcon />
      </SheetTrigger>
      <SheetPopup side="left" showCloseButton={false} className="max-w-80 gap-3 bg-sidebar p-3" data-component="shopChats">
        <div className="flex items-center gap-2.5 ps-2">
          <StoreMark name={store.name} />
          <SheetTitle className="min-w-0 flex-1 truncate font-medium font-sans text-[15px]">{store.name}</SheetTitle>
          <SheetClose render={<Button variant="ghost" size="icon-lg" aria-label="Close" />}>
            <XIcon />
          </SheetClose>
        </div>
        <Button
          data-component="shopChatsNew"
          variant="outline"
          size="lg"
          className="justify-start"
          render={<Link href={`/s/${store.slug}`} onClick={close} />}
        >
          <PlusIcon />
          New chat
        </Button>
        <div className="relative">
          <label htmlFor="shopChatsSearch" className="sr-only">Search chats</label>
          <SearchIcon aria-hidden="true" className="pointer-events-none absolute start-3 top-1/2 z-10 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            id="shopChatsSearch"
            data-component="shopChatsSearch"
            type="search"
            size="lg"
            placeholder="Search chats"
            className="[&_input]:ps-9"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <nav aria-label="My chats" className="-mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1">
          {conversationsLoading && Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} className="my-1 h-9 rounded-lg" />
          ))}
          {!conversationsLoading && conversations.length === 0 && (
            <p className="px-3 py-2 text-muted-foreground text-sm">Your chats with {store.name} show up here.</p>
          )}
          {!conversationsLoading && search && groups.length === 0 && conversations.length > 0 && (
            <p className="px-3 py-2 text-muted-foreground text-sm">No chat matches “{search}”.</p>
          )}
          {groups.map((group) => (
            <div key={group.label} className="flex flex-col gap-0.5">
              <span className="px-3 pt-3 pb-1 font-medium text-muted-foreground text-xs">{group.label}</span>
              {group.rows.map((row) => {
                const href = `/s/${store.slug}/c/${row._id}`;
                const active = pathname === href;
                return (
                  <Button
                    key={row._id}
                    data-component="shopChatLink"
                    variant="ghost"
                    size="lg"
                    aria-current={active ? 'page' : undefined}
                    className={cn('justify-start font-normal', active && 'bg-accent font-medium')}
                    render={<Link href={href} onClick={close} />}
                  >
                    <span className="truncate">{row.title ?? 'Untitled chat'}</span>
                  </Button>
                );
              })}
            </div>
          ))}
          {hasOlderConversations && !search && (
            <Button data-component="shopOlderChats" variant="ghost" size="sm" className="self-start text-muted-foreground" loading={loadingOlder} onClick={showOlder}>
              Show older
            </Button>
          )}
        </nav>
        <p className="border-t px-3 pt-3 text-muted-foreground text-xs leading-5">
          No account needed. Your chats and orders stay in this browser.
        </p>
      </SheetPopup>
    </Sheet>
  );
}
