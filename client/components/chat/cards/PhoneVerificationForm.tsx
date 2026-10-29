'use client';
import { useEffect, useRef, useState } from 'react';
import { FirebaseError } from 'firebase/app';
import { RecaptchaVerifier, linkWithPhoneNumber, type ConfirmationResult } from 'firebase/auth';
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

// What Firebase's refusals mean to a shopper.
function phoneProblem(error: unknown): string {
  const code = error instanceof FirebaseError ? error.code : '';
  if (code === 'auth/invalid-phone-number') return 'That number doesn’t look right. Use the country code, e.g. +639171234567.';
  if (code === 'auth/invalid-verification-code') return 'That code is not the one we sent. Check it and try again.';
  if (code === 'auth/code-expired') return 'That code expired. Send a new one.';
  if (code === 'auth/too-many-requests') return 'Too many tries. Wait a little, then try again.';
  if (code === 'auth/credential-already-in-use' || code === 'auth/account-exists-with-different-credential') {
    return 'That number is already used by another visitor.';
  }
  return 'We couldn’t verify your number. Try again.';
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
  const captcha = useRef<HTMLDivElement>(null);
  const verifier = useRef<RecaptchaVerifier | null>(null);
  const locked = answered || busy || working;

  useEffect(() => () => { verifier.current?.clear(); verifier.current = null; }, []);

  const sendCode = async () => {
    const user = guestAuth.currentUser;
    if (!user || !captcha.current) return;
    setWorking(true);
    try {
      verifier.current ??= new RecaptchaVerifier(guestAuth, captcha.current, { size: 'invisible' });
      setConfirmation(await linkWithPhoneNumber(user, phone.trim(), verifier.current));
    } catch (error) {
      verifier.current?.clear();
      verifier.current = null;
      showSnackbar(phoneProblem(error), 'error');
    } finally {
      setWorking(false);
    }
  };

  const confirm = async () => {
    if (!confirmation || !guest) return;
    setWorking(true);
    try {
      const credential = await confirmation.confirm(code.trim());
      const idToken = await credential.user.getIdToken(true);
      const res = await guest.guestRequest((session) => server.verifyCustomerPhone(session.customerId, idToken));
      showApiResponseSnackbar(res);
      if (res.status === 200) onAnswer({ kind: 'phoneVerified', formId: component.formId });
    } catch (error) {
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
        <div ref={captcha} />
      </div>
    </ChatCard>
  );
}
