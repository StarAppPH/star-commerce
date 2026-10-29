import type { Metadata } from 'next';
import { GuestSessionProvider } from '@/lib/GuestSessionContext';
import { ShopShell } from './ShopShell';

export const metadata: Metadata = {
  title: 'Shop',
};

// A store's shop: shoppers never sign in. Each browser gets a guest session
// at the store whose link it opened.
export default async function ShopLayout({ children, params }: { children: React.ReactNode; params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return (
    <GuestSessionProvider storeSlug={slug}>
      <ShopShell>{children}</ShopShell>
    </GuestSessionProvider>
  );
}
