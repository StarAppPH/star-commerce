'use client';
import { useEffect, useRef, useState } from 'react';
import { CheckIcon, CopyIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface CopyButtonProps {
  value: string;
  dataComponent: string;
  // Read by screen readers, e.g. "Copy 0012 3456 7890".
  label: string;
  text?: string;
  variant?: 'ghost' | 'outline';
  size?: 'xs' | 'sm';
}

// Copies `value`, then says "Copied" for 2 seconds.
export function CopyButton({ value, dataComponent, label, text = 'Copy', variant = 'ghost', size = 'xs' }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      console.error('Copying failed:', error instanceof Error ? error.name : 'unknown');
    }
  };

  return (
    <Button data-component={dataComponent} variant={variant} size={size} onClick={copy} aria-label={label}>
      {copied ? <CheckIcon /> : <CopyIcon />}
      {copied ? 'Copied' : text}
    </Button>
  );
}
