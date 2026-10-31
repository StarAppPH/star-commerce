'use client';
import { useRef, useState } from 'react';
import Image from 'next/image';
import { ImageUpIcon, ReceiptTextIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogHeader, DialogPanel, DialogPopup, DialogTitle } from '@/components/ui/dialog';
import { useAuth } from '@/lib/AuthContext';
import { guestAuth, guestStorage } from '@/lib/guestFirebase';
import { useOptionalGuestSession } from '@/lib/GuestSessionContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { PHOTO_ACCEPT, photoProblem, uploadOwnPhoto } from './photoUpload';

// The shopper sends a photo of the receipt of a payment made by hand: it goes
// to their own folder, and the server keeps a copy only the store reads.
export function SendReceiptButton({ orderId, sent }: { orderId: string; sent: boolean }) {
  const guest = useOptionalGuestSession();
  const { server } = useServer();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const input = useRef<HTMLInputElement>(null);
  const [working, setWorking] = useState(false);

  const send = async (file: File) => {
    const user = guestAuth.currentUser;
    if (!guest || !user) return;
    const problem = photoProblem(file);
    if (problem) {
      showSnackbar(problem, 'error');
      return;
    }
    setWorking(true);
    try {
      const url = await uploadOwnPhoto(guestStorage, user.uid, 'receipts', file);
      const res = await guest.guestRequest((session) => server.addPaymentReceipt(session.customerId, session.store.id, orderId, url));
      showApiResponseSnackbar(res);
    } catch (error) {
      console.error(error);
      showSnackbar('We couldn’t upload the receipt. Try again.', 'error');
    } finally {
      setWorking(false);
    }
  };

  if (!guest) return null;
  return (
    <>
      <input
        ref={input}
        data-component="myOrdersReceiptFile"
        type="file"
        accept={PHOTO_ACCEPT}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) send(file);
        }}
      />
      <Button
        data-component="myOrdersSendReceipt"
        size="sm"
        variant="outline"
        className="self-start"
        loading={working}
        onClick={() => input.current?.click()}
      >
        <ImageUpIcon />
        {sent ? 'Send another receipt' : 'Send receipt photo'}
      </Button>
    </>
  );
}

// The admin opens the photo of the receipt a customer sent; only the server
// can read it, so it comes through getPaymentReceipt.
export function ViewReceiptButton({ orderId }: { orderId: string }) {
  const { userData, adminRequest } = useAuth();
  const userId = userData?._id ?? null;
  const { server } = useServer();
  const { showApiResponseSnackbar } = useSnackbar();
  const [image, setImage] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const open = async () => {
    if (userId === null) return;
    setWorking(true);
    try {
      const res = await adminRequest(() => server.getPaymentReceipt(userId, orderId));
      if (res.status === 200) setImage(res.data.image);
      else showApiResponseSnackbar(res);
    } finally {
      setWorking(false);
    }
  };

  return (
    <>
      <Button
        data-component="storeOrdersViewReceipt"
        size="sm"
        variant="outline"
        className="self-start"
        loading={working}
        onClick={open}
      >
        <ReceiptTextIcon />
        View receipt
      </Button>
      <Dialog open={image !== null} onOpenChange={(next) => { if (!next) setImage(null); }}>
        <DialogPopup data-component="storeOrdersReceiptDialog">
          <DialogHeader>
            <DialogTitle>Payment receipt</DialogTitle>
          </DialogHeader>
          <DialogPanel>
            {image !== null && (
              <Image
                src={image}
                alt="The customer’s payment receipt"
                width={1600}
                height={1600}
                // eslint-disable-next-line no-restricted-syntax -- a data: URL from getPaymentReceipt: the receipt is in no place a server can fetch it from
                unoptimized
                className="h-auto max-h-[70vh] w-full object-contain"
              />
            )}
          </DialogPanel>
        </DialogPopup>
      </Dialog>
    </>
  );
}
