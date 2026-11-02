'use client';
import { useState } from 'react';
import { Switch } from '@/components/ui/switch';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { useAdminWorkspace } from './AdminWorkspace';

// The store's open switch in the sidebar: open, customers can order; closed,
// they can only look around. The server refuses to open a store whose
// required policy questions are not all answered, and its message names them.
export function StoreOpenSwitch({ isOpen: storeIsOpen }: { isOpen: boolean }) {
  const { adminRequest } = useAuth();
  const { server } = useServer();
  const { showApiResponseSnackbar } = useSnackbar();
  const { userId, reloadStore } = useAdminWorkspace();
  const [working, setWorking] = useState(false);
  // What the switch just saved, shown until the store reloads with it.
  const [saved, setSaved] = useState<boolean | null>(null);
  const isOpen = working && saved !== null ? saved : storeIsOpen;

  const change = async (open: boolean) => {
    if (working) return;
    setWorking(true);
    try {
      const res = await adminRequest(() => server.setMyStoreOpen(userId, open));
      showApiResponseSnackbar(res);
      if (res.status === 200) setSaved(res.data.isOpen);
      await reloadStore();
    } finally {
      setSaved(null);
      setWorking(false);
    }
  };

  return (
    <label data-component="adminStoreOpen" className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm">
      <span aria-hidden="true" className={isOpen ? 'size-2 shrink-0 rounded-full bg-success' : 'size-2 shrink-0 rounded-full bg-muted-foreground/50'} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="font-medium">{isOpen ? 'Store open' : 'Store closed'}</span>
        <span className="text-muted-foreground text-xs">{isOpen ? 'Customers can order' : 'Customers can only browse'}</span>
      </span>
      <Switch
        data-component="adminStoreOpenSwitch"
        aria-label={isOpen ? 'Close my store' : 'Open my store'}
        checked={isOpen}
        disabled={working}
        onCheckedChange={(checked) => { void change(checked); }}
      />
    </label>
  );
}
