import type { Metadata } from 'next';
import { AuthProvider } from '@/lib/AuthContext';
import { ServerProvider } from '@/lib/ServerContext';
import { DevClickToComponent } from '@/app/components/DevClickToComponent';

export const metadata: Metadata = {
  title: 'starCommerce',
  description: 'starCommerce',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ServerProvider>
          <AuthProvider>
            {/* Alt+hover inspector: everyone in development, only a superAdmin in production. */}
            <DevClickToComponent />
            {children}
          </AuthProvider>
        </ServerProvider>
      </body>
    </html>
  );
}
