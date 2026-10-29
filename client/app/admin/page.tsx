'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { FirebaseError } from 'firebase/app';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { Logo } from '@/components/brand/Logo';
import { GoogleIcon } from '@/components/brand/GoogleIcon';
import { useAuth } from '@/lib/AuthContext';
import { useSnackbar } from '@/lib/SnackbarContext';

// Closing the Google window is a change of mind, not an error.
const CANCELLED_SIGN_IN = new Set(['auth/popup-closed-by-user', 'auth/cancelled-popup-request', 'auth/user-cancelled']);

export default function AdminSignInPage() {
  const router = useRouter();
  const { userData, loading, signInWithGoogle } = useAuth();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const [signingIn, setSigningIn] = useState(false);
  const signedIn = Boolean(userData?._id);

  useEffect(() => {
    if (!loading && signedIn) router.replace('/admin/chat');
  }, [loading, signedIn, router]);

  const signIn = async () => {
    setSigningIn(true);
    try {
      const res = await signInWithGoogle();
      showApiResponseSnackbar(res);
      if (res.status === 200) router.replace('/admin/chat');
    } catch (error) {
      if (!(error instanceof FirebaseError && CANCELLED_SIGN_IN.has(error.code))) {
        showSnackbar('Google sign-in didn’t finish. Try again.', 'error');
      }
    } finally {
      setSigningIn(false);
    }
  };

  return (
    <div data-component="adminSignIn" className="flex min-h-svh flex-col bg-background lg:flex-row">
      <main className="flex flex-1 flex-col px-6 py-6 sm:px-10">
        <Link href="/" className="flex items-center gap-2.5 self-start font-medium" data-component="adminSignInHome">
          <Logo />
          Star Commerce
        </Link>
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-8 py-12">
          <div className="flex flex-col gap-3">
            <h1 className="font-heading text-4xl leading-tight">Sign in to your store</h1>
            <p className="text-base text-muted-foreground">
              Run your store by chatting with your assistant: orders, products, returns and policies.
            </p>
          </div>
          {loading || signedIn ? (
            <div className="flex h-10 items-center gap-2 text-muted-foreground text-sm" aria-live="polite">
              <Spinner />
              Checking your sign-in…
            </div>
          ) : (
            <Button
              data-component="adminSignInGoogle"
              size="xl"
              variant="outline"
              className="w-full"
              loading={signingIn}
              onClick={signIn}
            >
              <GoogleIcon className="size-5" />
              Continue with Google
            </Button>
          )}
          <div className="flex flex-col gap-3 border-t pt-6 text-sm">
            <p>
              <span className="font-medium">New here?</span>{' '}
              <span className="text-muted-foreground">Your first sign-in creates your store, and the assistant walks you through the rest.</span>
            </p>
            <p className="text-muted-foreground">
              Customers never sign in. They shop from your store link as guests and confirm a phone number at checkout.
            </p>
          </div>
        </div>
        <p className="text-muted-foreground text-xs">© Star Commerce</p>
      </main>
      <aside className="drafting-grid relative hidden flex-1 flex-col justify-end gap-4 overflow-hidden border-l bg-muted/60 p-10 lg:flex">
        <h2 className="font-heading text-3xl">One store. One link.</h2>
        <p className="max-w-md text-muted-foreground">
          Share your store link and customers browse, order and pay without an account. You hand over, and handle returns, from here.
        </p>
        <div className="flex w-fit items-center gap-1 rounded-full border bg-background px-4 py-2 font-mono text-sm shadow-xs/5">
          <span className="text-muted-foreground">[your domain]/s/</span>
          <span>your-store</span>
        </div>
      </aside>
    </div>
  );
}
