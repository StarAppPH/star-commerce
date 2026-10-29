import type { Metadata } from 'next';
import { AuthProvider } from '@/lib/AuthContext';

export const metadata: Metadata = {
  title: 'Admin',
  robots: { index: false, follow: false },
};

// Only the admin side signs in: shoppers never load the admin's session.
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}
