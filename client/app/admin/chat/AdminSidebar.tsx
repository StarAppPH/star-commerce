'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { ExternalLinkIcon, LogOutIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Button } from '@/components/ui/button';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInput,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  useSidebar,
} from '@/components/ui/sidebar';
import { Logo } from '@/components/brand/Logo';
import { StoreMark } from '@/components/brand/StoreMark';
import { initials } from '@/lib/utils';
import { useAuth } from '@/lib/AuthContext';
import { groupConversationsByDay } from '@/lib/storeTime';
import { matchingConversations } from '@/components/chat/useConversationList';
import { useAdminWorkspace } from './AdminWorkspace';
import { ResetStoreDialog, storeResetAvailable } from './ResetStoreDialog';

export function AdminSidebar() {
  const router = useRouter();
  const pathname = usePathname();
  const { userData, logout } = useAuth();
  const { storeState, conversations, conversationsLoading, hasOlderConversations, loadOlderConversations } = useAdminWorkspace();
  const { isMobile, setOpenMobile } = useSidebar();
  const [search, setSearch] = useState('');
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  // Reset needs a loaded store (a store not set up yet is ready with none).
  const resetShown = Boolean(userData) && storeState.status === 'ready' && storeResetAvailable();

  // Hiding the item closes its dialog, so the dialog cannot reopen by itself.
  const [resetWasShown, setResetWasShown] = useState(resetShown);
  if (resetWasShown !== resetShown) {
    setResetWasShown(resetShown);
    if (!resetShown) setResetOpen(false);
  }

  const store = storeState.status === 'ready' ? storeState.store : null;
  // A store not set up yet has its setup chats.
  const chatsShown = storeState.status === 'ready';
  const storeName = store?.name ?? 'Star Commerce';
  const closeOnMobile = () => { if (isMobile) setOpenMobile(false); };

  const groups = useMemo(
    () => groupConversationsByDay(matchingConversations(conversations, search), (row) => row.lastMessageAt),
    [conversations, search],
  );

  const showOlder = async () => {
    setLoadingOlder(true);
    await loadOlderConversations();
    setLoadingOlder(false);
  };

  const signOut = async () => {
    await logout();
    router.replace('/admin');
  };

  return (
    <Sidebar data-component="adminSidebar">
      <SidebarHeader className="gap-3">
        <div className="flex items-center gap-2.5 px-2 py-1.5">
          {store ? (
            <StoreMark name={storeName} size="sm" />
          ) : (
            <Logo size="sm" />
          )}
          <span className="truncate font-medium">{storeName}</span>
        </div>
        {chatsShown && (
          <Button
            data-component="adminNewChat"
            variant="outline"
            className="w-full justify-start"
            render={<Link href="/admin/chat" onClick={closeOnMobile} />}
          >
            <PlusIcon />
            New chat
          </Button>
        )}
        {chatsShown && (
          <div className="relative">
            <label htmlFor="adminSearchChats" className="sr-only">Search chats</label>
            <SearchIcon aria-hidden="true" className="pointer-events-none absolute start-2.5 top-1/2 z-10 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <SidebarInput
              id="adminSearchChats"
              data-component="adminSearchChats"
              type="search"
              placeholder="Search chats"
              className="[&_input]:ps-8"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        )}
      </SidebarHeader>

      <SidebarContent>
        {conversationsLoading && (
          <SidebarGroup>
            <SidebarMenu>
              {Array.from({ length: 5 }, (_, index) => (
                <SidebarMenuItem key={index}><SidebarMenuSkeleton /></SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        )}
        {!conversationsLoading && chatsShown && conversations.length === 0 && (
          <p className="px-4 py-2 text-muted-foreground text-sm">Your chats show up here.</p>
        )}
        {!conversationsLoading && search && groups.length === 0 && conversations.length > 0 && (
          <p className="px-4 py-2 text-muted-foreground text-sm">No chat matches “{search}”.</p>
        )}
        {groups.map((group) => (
          <SidebarGroup key={group.label} className="py-1">
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarMenu>
              {group.rows.map((row) => {
                const href = `/admin/chat/${row._id}`;
                return (
                  <SidebarMenuItem key={row._id}>
                    <SidebarMenuButton
                      data-component="adminChatLink"
                      isActive={pathname === href}
                      render={<Link href={href} onClick={closeOnMobile} />}
                    >
                      <span>{row.title ?? 'Untitled chat'}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroup>
        ))}
        {hasOlderConversations && !search && (
          <div className="px-2 pb-2">
            <Button data-component="adminOlderChats" variant="ghost" size="sm" className="text-muted-foreground" loading={loadingOlder} onClick={showOlder}>
              Show older
            </Button>
          </div>
        )}
      </SidebarContent>

      <SidebarFooter className="gap-1">
        {store?.slug && (
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                data-component="adminOpenStore"
                render={<a href={`/s/${store.slug}`} target="_blank" rel="noreferrer" />}
              >
                <ExternalLinkIcon />
                <span>Open my store</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        )}
        {resetShown && (
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton data-component="adminResetStore" className="text-destructive" onClick={() => setResetOpen(true)}>
                <Trash2Icon />
                <span>Reset my store</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        )}
        {userData && (
          <div className="flex items-center gap-2.5 border-t px-1 pt-2">
            <Avatar className="size-8">
              <AvatarFallback className="text-xs">{initials(userData.name ?? userData.email)}</AvatarFallback>
            </Avatar>
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="truncate font-medium text-sm">{userData.name ?? 'Store admin'}</span>
              <span className="truncate text-muted-foreground text-xs">{userData.email}</span>
            </div>
            <Button data-component="adminSignOut" variant="ghost" size="icon-sm" aria-label="Sign out" onClick={signOut}>
              <LogOutIcon />
            </Button>
          </div>
        )}
      </SidebarFooter>
      {resetShown && <ResetStoreDialog open={resetOpen} onOpenChange={setResetOpen} />}
    </Sidebar>
  );
}
