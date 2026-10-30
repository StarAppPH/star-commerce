'use client';
import { useCallback, useEffect } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { SquarePenIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { SidebarInset, SidebarProvider, SidebarTrigger } from '@/components/ui/sidebar';
import { Spinner } from '@/components/ui/spinner';
import { ChatShellProvider } from '@/components/chat/ChatShell';
import { useAuth } from '@/lib/AuthContext';
import { useBrandColor } from '@/lib/brandColors';
import { AdminSidebar } from './AdminSidebar';
import { AdminWorkspaceProvider, useAdminWorkspace } from './AdminWorkspace';

// The admin's voice glow before the store has brand colors: teal into blue.
const ADMIN_GLOW = '#0D9488';
const ADMIN_GLOW_SECOND = '#3B82F6';

export default function AdminChatLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { userData, loading } = useAuth();
  const userId = userData?._id ?? null;

  useEffect(() => {
    if (!loading && !userId) router.replace('/admin');
  }, [loading, userId, router]);

  if (loading || !userId) {
    return (
      <div className="flex min-h-svh items-center justify-center gap-2 text-muted-foreground text-sm" aria-live="polite">
        <Spinner />
        Opening your store…
      </div>
    );
  }

  return (
    <AdminWorkspaceProvider userId={userId}>
      <AdminChatFrame>{children}</AdminChatFrame>
    </AdminWorkspaceProvider>
  );
}

function AdminChatFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { storeState, conversations, reloadConversations, reloadStore } = useAdminWorkspace();
  // A saved turn moves its chat to the top, and the store is read again: a
  // turn may have set it up or changed it, its brand colors included (a
  // theme change shows no card).
  const onTurnSaved = useCallback(() => {
    reloadConversations();
    reloadStore();
  }, [reloadConversations, reloadStore]);
  const store = storeState.status === 'ready' ? storeState.store : null;
  // The admin's screens are drawn in the store's brand colors, like its shop.
  const primaryColor = store?.primaryColor ?? null;
  const secondaryColor = store?.secondaryColor ?? null;
  useBrandColor(primaryColor);
  const openId = pathname.startsWith('/admin/chat/') ? pathname.slice('/admin/chat/'.length) : null;
  const openChat = openId ? conversations.find((row) => row._id === openId) : undefined;
  const title = openId ? (openChat?.title ?? 'Chat') : store ? 'New chat' : 'Set up my store';

  return (
    <ChatShellProvider
      voice={{
        title: store?.name ?? 'Star Commerce',
        subtitle: 'Your store assistant',
        glow: primaryColor ?? ADMIN_GLOW,
        glowSecond: secondaryColor ?? primaryColor ?? ADMIN_GLOW_SECOND,
        variant: 'admin',
      }}
      onTurnSaved={onTurnSaved}
    >
      <SidebarProvider>
        <AdminSidebar />
        <SidebarInset className="h-svh min-w-0 overflow-hidden">
          <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3 md:border-transparent">
            <SidebarTrigger data-component="adminSidebarToggle" />
            <h1 className="min-w-0 flex-1 truncate font-medium text-sm">{title}</h1>
            {storeState.status === 'ready' && (
              <Button
                data-component="adminHeaderNewChat"
                variant="ghost"
                size="icon-sm"
                aria-label="New chat"
                className="md:hidden"
                render={<Link href="/admin/chat" />}
              >
                <SquarePenIcon />
              </Button>
            )}
          </header>
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
        </SidebarInset>
      </SidebarProvider>
    </ChatShellProvider>
  );
}
