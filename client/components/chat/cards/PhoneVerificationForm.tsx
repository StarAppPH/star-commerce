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
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from '@/components/ui/input-group';
import { OTPField, OTPFieldInput } from '@/components/ui/otp-field';
import { guestAuth } from '@/lib/guestFirebase';
import { useOptionalGuestSession } from '@/lib/GuestSessionContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import type { ComponentProps } from './types';

const PHONE_PATTERN = /^\+[1-9][0-9]{6,14}$/;
const CODE_LENGTH = 6;
const RESEND_AFTER_SECONDS = 30;
const DEFAULT_COUNTRY_CODE = '+63';

// The number as Firebase wants it (E.164). The box holds the local number
// after the +63 prefix ("917 123 4567" or "0917..."), or a whole number
// with another country's code when it starts with "+".
function e164(typed: string): string {
  const trimmed = typed.trim();
  if (trimmed.startsWith('+')) return `+${trimmed.replace(/\D/g, '')}`;
  return `${DEFAULT_COUNTRY_CODE}${trimmed.replace(/\D/g, '').replace(/^0+/, '')}`;
}

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
  const [typed, setTyped] = useState('');
  const [code, setCode] = useState('');
  const [resendIn, setResendIn] = useState(0);
  const [confirmation, setConfirmation] = useState<ConfirmationResult | null>(null);
  const [working, setWorking] = useState(false);
  const captchaHost = useRef<HTMLDivElement>(null);
  const captcha = useRef<Captcha | null>(null);
  const locked = answered || busy || working;
  const phone = e164(typed);
  const international = typed.trim().startsWith('+');

  useEffect(() => () => dropCaptcha(captcha), []);

  // Counts down to when another code may be sent.
  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = window.setTimeout(() => setResendIn((left) => left - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [resendIn]);

  const sendCode = async () => {
    const user = guestAuth.currentUser;
    if (!user || !captchaHost.current) return;
    setWorking(true);
    // A resend renders a fresh reCAPTCHA: the last one's token is spent.
    if (confirmation) dropCaptcha(captcha);
    try {
      if (!captcha.current) {
        const element = document.createElement('div');
        captchaHost.current.appendChild(element);
        captcha.current = { verifier: new RecaptchaVerifier(guestAuth, element, { size: 'invisible' }), element };
      }
      setConfirmation(await linkWithPhoneNumber(user, phone, captcha.current.verifier));
      setCode('');
      setResendIn(RESEND_AFTER_SECONDS);
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

  const confirm = async (entered: string) => {
    const newGuest = guestAuth.currentUser;
    if (!confirmation || !guest || !newGuest || working || entered.length < CODE_LENGTH) return;
    setWorking(true);
    try {
      let user: User;
      let guestIdToken: string | null = null;
      try {
        user = (await confirmation.confirm(entered)).user;
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
      setCode('');
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
          <Button data-component="phoneVerificationConfirm" loading={working} disabled={code.length < CODE_LENGTH || busy || !guest} onClick={() => confirm(code)}>
            Verify
          </Button>
        </>
      ) : (
        <Button data-component="phoneVerificationSend" loading={working} disabled={!PHONE_PATTERN.test(phone) || busy} onClick={sendCode}>
          Text me a code
        </Button>
      )}
    >
      <div className="flex flex-col gap-4 p-4">
        {!answered && (confirmation ? (
          <Field>
            <FieldLabel>Enter the {CODE_LENGTH}-digit code</FieldLabel>
            <FieldDescription>We texted it to {phone}.</FieldDescription>
            <OTPField
              data-component="phoneVerificationCode"
              length={CODE_LENGTH}
              size="lg"
              value={code}
              disabled={locked}
              onValueChange={setCode}
              onValueComplete={confirm}
              className="my-1"
            >
              {Array.from({ length: CODE_LENGTH }, (_, index) => (
                <OTPFieldInput key={index} aria-label={`Digit ${index + 1}`} />
              ))}
            </OTPField>
            <Button
              data-component="phoneVerificationResend"
              variant="link"
              size="sm"
              className="h-auto self-start px-0"
              disabled={locked || resendIn > 0}
              onClick={sendCode}
            >
              {resendIn > 0 ? `Resend code in ${resendIn}s` : 'Resend code'}
            </Button>
          </Field>
        ) : (
          <Field>
            <FieldLabel>Your mobile number</FieldLabel>
            <FieldDescription>We’ll text you a {CODE_LENGTH}-digit code. The store uses your number only about your orders.</FieldDescription>
            <InputGroup>
              {!international && (
                <InputGroupAddon>
                  <InputGroupText data-component="phoneVerificationCountry">🇵🇭 {DEFAULT_COUNTRY_CODE}</InputGroupText>
                </InputGroupAddon>
              )}
              <InputGroupInput
                data-component="phoneVerificationPhone"
                type="tel"
                inputMode="tel"
                autoComplete="tel-national"
                placeholder="917 123 4567"
                aria-label="Mobile number"
                value={typed}
                disabled={locked}
                onChange={(event) => setTyped(event.target.value)}
              />
            </InputGroup>
            <FieldDescription>Another country? Type the whole number starting with +.</FieldDescription>
          </Field>
        ))}
        <div ref={captchaHost} />
      </div>
    </ChatCard>
  );
}
