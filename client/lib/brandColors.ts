'use client';
import { useEffect } from 'react';

// A #RRGGBB color's relative luminance (WCAG 2).
function luminance(hex: string): number {
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

function contrast(a: number, b: number): number {
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

// The text color that reads best on `hex`: white or near-black.
export function readableOn(hex: string): string {
  const background = luminance(hex);
  return contrast(background, 1) >= contrast(background, luminance('#171717')) ? '#FFFFFF' : '#171717';
}

// The shop, and the admin's chat, drawn in the store's main brand color
// (primaryColor): buttons, the user's messages, the map pin and the store's
// mark take it, with a readable text color on them. Set on the root
// element, so dialogs and sheets (rendered outside the shop) take it too;
// none keeps the app's own.
export function useBrandColor(primaryColor: string | null): void {
  useEffect(() => {
    if (!primaryColor) return;
    const root = document.documentElement.style;
    root.setProperty('--primary', primaryColor);
    root.setProperty('--primary-foreground', readableOn(primaryColor));
    return () => {
      root.removeProperty('--primary');
      root.removeProperty('--primary-foreground');
    };
  }, [primaryColor]);
}
