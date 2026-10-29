import type { Metadata, Viewport } from 'next';
import '@fontsource-variable/inter';
import '@fontsource/cal-sans';
import './globals.css';
import { ServerProvider } from '@/lib/ServerContext';
import { SnackbarProvider } from '@/lib/SnackbarContext';
import { DevClickToComponent } from '@/app/components/DevClickToComponent';

export const metadata: Metadata = {
  title: {
    default: 'Star Commerce: your store, as a chat',
    template: '%s · Star Commerce',
  },
  description:
    'Customers ask, choose and pay in one conversation, with no app and no account. You run the whole store by chatting or talking.',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#ffffff',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ServerProvider>
          <SnackbarProvider>
            {/* Alt+hover inspector: everyone in development, only a superAdmin in production. */}
            <DevClickToComponent />
            {children}
          </SnackbarProvider>
        </ServerProvider>
      </body>
    </html>
  );
}
