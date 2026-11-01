import { useCallback, useEffect, useRef } from 'react';

// blob: previews of picked files: every one made through `create` is let go
// by `release`, or when the component goes.
export function useObjectUrls() {
  const urls = useRef<Set<string>>(new Set());

  const create = useCallback((file: File): string => {
    const url = URL.createObjectURL(file);
    urls.current.add(url);
    return url;
  }, []);

  const release = useCallback((url: string) => {
    if (urls.current.delete(url)) URL.revokeObjectURL(url);
  }, []);

  useEffect(() => {
    const held = urls.current;
    return () => { held.forEach((url) => URL.revokeObjectURL(url)); held.clear(); };
  }, []);

  return { create, release };
}
