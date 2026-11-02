'use client';
import { useEffect, useRef, useState } from 'react';
import { FirebaseError } from 'firebase/app';
import {
  PhoneAuthProvider, RecaptchaVerifier, linkWithPhoneNumber, signInWithCredential,
  type ConfirmationResult, type User,
} from 'firebase/auth';
import { SmartphoneIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { guestAuth } from '@/lib/guestFirebase';
import { useOptionalGuestSession } from '@/lib/GuestSessionContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import type { ComponentProps } from './types';

const PHONE_PATTERN = /^\+[1-9][0-9]{6,14}$/;

// The error's code (or text) for the console: never the phone number.
function errorCode(error: unknown): string {
  if (error instanceof FirebaseError) return error.code;
  return error instanceof Error ? error.message : String(error);
}

// What Firebase's refusals mean to a shopper.
function phoneProblem(error: unknown): string {
  const code = error instanceof FirebaseError ? error.code : '';
  if (code === 'auth/invalid-phone-number') return 'That number doesn’t look right. Use the country code, e.g. +639171234567.';
  if (code === 'auth/invalid-app-credential' || code === 'auth/captcha-check-failed') {
    return 'We couldn’t check that you’re not a robot. Reload the page and try again.';
  }
  if (code === 'auth/operation-not-allowed') return 'Phone verification isn’t available right now. Try again later.';
  if (code === 'auth/invalid-verification-code') return 'That code is not the one we sent. Check it and try again.';
  if (code === 'auth/code-expired') return 'That code expired. Send a new one.';
  if (code === 'auth/too-many-requests') return 'Too many tries. Wait a little, then try again.';
  if (code === 'auth/account-exists-with-different-credential') return 'That number is already used by another visitor.';
  return 'We couldn’t verify your number. Try again.';
}

interface Captcha {
  verifier: RecaptchaVerifier;
  element: HTMLDivElement;
}

// reCAPTCHA renders once per element, and an invisible verifier's clear()
// leaves its widget in the element: each verifier gets an element of its
// own, removed with it, so a retry renders into a clean one.
function dropCaptcha(captcha: { current: Captcha | null }) {
  captcha.current?.verifier.clear();
  captcha.current?.element.remove();
  captcha.current = null;
}

// The shopper verifies their phone number with a code Firebase texts them;
// the store then knows whom to reach about the order.
export function PhoneVerificationForm({ component, answered, busy, onAnswer }: ComponentProps<'phoneVerificationForm'>) {
  const guest = useOptionalGuestSession();
  const { server } = useServer();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const [phone, setPhone] = useState('+63');
  const [code, setCode] = useState('');
  const [confirmation, setConfirmation] = useState<ConfirmationResult | null>(null);
  const [working, setWorking] = useState(false);
  const captchaHost = useRef<HTMLDivElement>(null);
  const captcha = useRef<Captcha | null>(null);
  const locked = answered || busy || working;

  useEffect(() => () => dropCaptcha(captcha), []);

  const sendCode = async () => {
    const user = guestAuth.currentUser;
    if (!user || !captchaHost.current) return;
    setWorking(true);
    try {
      if (!captcha.current) {
        const element = document.createElement('div');
        captchaHost.current.appendChild(element);
        captcha.current = { verifier: new RecaptchaVerifier(guestAuth, element, { size: 'invisible' }), element };
      }
      setConfirmation(await linkWithPhoneNumber(user, phone.trim(), captcha.current.verifier));
    } catch (error) {
      console.error('Phone verification failed to send a code:', errorCode(error));
      dropCaptcha(captcha);
      showSnackbar(phoneProblem(error), 'error');
    } finally {
      setWorking(false);
    }
  };

  // A number already linked to another account is a returning customer's
  // (their earlier guest, on another device or before the site data was
  // cleared): sign in as that account with the same code instead.
  // verifyCustomerPhone then gives it this guest's chats here, and its cart
  // when it was used last; `guestIdToken` proves we were this guest.
  const signInAsReturningCustomer = async (error: unknown, newGuest: User): Promise<{ user: User; guestIdToken: string } | null> => {
    if (!guest || !(error instanceof FirebaseError) || error.code !== 'auth/credential-already-in-use') throw error;
    const phoneCredential = PhoneAuthProvider.credentialFromError(error);
    if (!phoneCredential) throw error;
    // Read before the switch, while this guest is still signed in.
    const guestIdToken = await newGuest.getIdToken();
    const { user } = await signInWithCredential(guestAuth, phoneCredential);
    if (!(await guest.restart())) {
      showSnackbar('We couldn’t open your account. Reload the page and try again.', 'error');
      return null;
    }
    return { user, guestIdToken };
  };

  const confirm = async () => {
    const newGuest = guestAuth.currentUser;
    if (!confirmation || !guest || !newGuest) return;
    setWorking(true);
    try {
      let user: User;
      let guestIdToken: string | null = null;
      try {
        user = (await confirmation.confirm(code.trim())).user;
      } catch (error) {
        const returning = await signInAsReturningCustomer(error, newGuest);
        if (!returning) return;
        ({ user, guestIdToken } = returning);
      }
      const idToken = await user.getIdToken(true);
      const res = await guest.guestRequest((session) => server.verifyCustomerPhone(
        session.customerId, idToken, guestIdToken ? session.store.id : null, guestIdToken,
      ));
      showApiResponseSnackbar(res);
      if (res.status === 200) onAnswer({ kind: 'phoneVerified', formId: component.formId });
    } catch (error) {
      console.error('Phone verification failed to confirm the code:', errorCode(error));
      showSnackbar(phoneProblem(error), 'error');
    } finally {
      setWorking(false);
    }
  };

  return (
    <ChatCard
      dataComponent="phoneVerificationForm"
      icon={<SmartphoneIcon />}
      title="Verify your phone number"
      footer={answered ? <SentNote text="Phone number verified" /> : confirmation ? (
        <>
          <Button data-component="phoneVerificationBack" variant="ghost" className="me-auto" disabled={locked} onClick={() => { setConfirmation(null); setCode(''); }}>
            Change number
          </Button>
          <Button data-component="phoneVerificationConfirm" loading={working} disabled={code.trim().length < 6 || busy || !guest} onClick={confirm}>
            Verify
          </Button>
        </>
      ) : (
        <Button data-component="phoneVerificationSend" loading={working} disabled={!PHONE_PATTERN.test(phone.trim()) || busy} onClick={sendCode}>
          Text me a code
        </Button>
      )}
    >
      <div className="flex flex-col gap-4 p-4">
        {!answered && (confirmation ? (
          <Field>
            <FieldLabel>The 6-digit code we texted to {phone.trim()}</FieldLabel>
            <Input
              data-component="phoneVerificationCode"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              disabled={locked}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
            />
          </Field>
        ) : (
          <Field>
            <FieldLabel>Your mobile number</FieldLabel>
            <Input
              data-component="phoneVerificationPhone"
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              value={phone}
              disabled={locked}
              onChange={(event) => setPhone(event.target.value)}
            />
            <FieldDescription>The store uses it only about your orders.</FieldDescription>
          </Field>
        ))}
        <div ref={captchaHost} />
      </div>
    </ChatCard>
  );
}
