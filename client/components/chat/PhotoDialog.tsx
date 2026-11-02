'use client';
import Image from 'next/image';
import { Dialog, DialogHeader, DialogPanel, DialogPopup, DialogTitle } from '@/components/ui/dialog';

interface PhotoDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  // null: nothing to show yet (the dialog stays empty).
  src: string | null;
  alt: string;
  dataComponent: string;
}

// One private photo shown full size: a photo the user attached in chat, or
// a receipt the server sent as a data: URL.
export function PhotoDialog({ open, onOpenChange, title, src, alt, dataComponent }: PhotoDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPopup data-component={dataComponent}>
        <DialogHeader>
          <DialogTitle className="truncate">{title}</DialogTitle>
        </DialogHeader>
        <DialogPanel>
          {src !== null && (
            <Image
              src={src}
              alt={alt}
              width={1600}
              height={1600}
              // A private photo is never sent through the optimizer: its
              // tokened URL would land in Vercel's logs and a cached copy would
              // outlive its deletion (resetMyStore); a data: URL no server can fetch.
              // eslint-disable-next-line no-restricted-syntax
              unoptimized
              className="h-auto max-h-[70vh] w-full object-contain"
            />
          )}
        </DialogPanel>
      </DialogPopup>
    </Dialog>
  );
}
