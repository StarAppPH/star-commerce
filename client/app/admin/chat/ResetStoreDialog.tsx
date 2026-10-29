'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Dialog, DialogDescription, DialogFooter, DialogHeader, DialogPanel, DialogPopup, DialogTitle } from '@/components/ui/dialog';
import { Field, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { STORE_RESET_ENDS_AT } from '@/lib/generated/serverConstants';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { useAuth } from '@/lib/AuthContext';
import { useAdminWorkspace } from './AdminWorkspace';

// Reset is offered until the server's cut-off (Philippine time); the server
// refuses it from then on, so a wrong device clock only shows a button it refuses.
export function storeResetAvailable(): boolean {
  return Date.now() < Date.parse(STORE_RESET_ENDS_AT);
}

// A store not set up yet has no name to type, so it types this word.
const SETUP_CONFIRM_WORD = 'reset';

// The same folding the server applies to the typed text.
const foldName = (text: string): string => text.normalize('NFKC').trim().toLowerCase();

export function ResetStoreDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const { adminRequest } = useAuth();
  const { server } = useServer();
  const { showApiResponseSnackbar } = useSnackbar();
  const { userId, storeState, reloadStore, reloadConversations } = useAdminWorkspace();
  const [typed, setTyped] = useState('');
  const [working, setWorking] = useState(false);

  const store = storeState.status === 'ready' ? storeState.store : null;
  const expected = store?.name ?? SETUP_CONFIRM_WORD;
  const matches = foldName(typed) === foldName(expected);

  const changeOpen = (next: boolean) => {
    if (working) return;
    if (!next) setTyped('');
    onOpenChange(next);
  };

  const resetStore = async () => {
    if (!matches || working) return;
    setWorking(true);
    try {
      const res = await adminRequest(() => server.resetMyStore(userId, typed.trim()));
      showApiResponseSnackbar(res);
      // A failure may still have reset the store, so the screen reloads either way.
      await reloadStore();
      await reloadConversations();
      if (res.status !== 200) return;
      changeOpen(false);
      router.replace('/admin/chat');
    } finally {
      setWorking(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogPopup data-component="adminResetStoreDialog">
        <DialogHeader>
          <DialogTitle>Reset my store</DialogTitle>
          <DialogDescription>
            This deletes the following for good. It can’t be undone.
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex min-h-0 flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            resetStore();
          }}
        >
          <DialogPanel className="flex flex-col gap-4">
            <ul className="list-disc ps-5 text-sm">
              <li>Your store’s details: name, description, contact number, address, opening hours, map pin, brand colors, photo style, voice and currency</li>
              <li>Your products</li>
              <li>Your orders and returns, including your customers’</li>
              <li>Your store policy</li>
              <li>Generated product photos, and photos you uploaded and files you sent in this store’s chats. Files you sent in other stores’ chats stay.</li>
              <li>Every chat with your store, including your customers’</li>
            </ul>
            <p className="text-sm">
              Your shop link stops working, and anyone can then take the store’s name and link.
            </p>
            <p className="text-sm">Your account stays, and your store starts again from setup.</p>
            <Field data-component="adminResetStoreField">
              <FieldLabel>Type “{expected}” to confirm</FieldLabel>
              <Input
                data-component="adminResetStoreConfirm"
                autoComplete="off"
                value={typed}
                disabled={working}
                onChange={(event) => setTyped(event.target.value)}
              />
            </Field>
          </DialogPanel>
          <DialogFooter>
            <Button data-component="adminResetStoreCancel" type="button" variant="outline" disabled={working} onClick={() => changeOpen(false)}>
              Cancel
            </Button>
            <Button
              data-component="adminResetStoreSubmit"
              type="submit"
              variant="destructive"
              loading={working}
              disabled={!matches || working}
            >
              Reset my store
            </Button>
          </DialogFooter>
        </form>
      </DialogPopup>
    </Dialog>
  );
}
