'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useAuth } from '../../lib/AuthContext';

interface OverlayInfo {
  top: number;
  left: number;
  width: number;
  height: number;
  label: string;
  targetElement: Element;
}

/** Walk up from an element to find the nearest data-component attribute */
function findDataComponent(el: Element): { element: Element; label: string } | null {
  let current: Element | null = el;
  while (current) {
    const label = current.getAttribute('data-component');
    if (label) return { element: current, label };
    current = current.parentElement;
  }
  return null;
}

/** Get the React fiber from a DOM element */
function getReactFiber(element: Element): Record<string, unknown> | null {
  const key = Object.keys(element).find(
    (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
  );
  if (!key) return null;
  return (element as unknown as Record<string, unknown>)[key] as Record<string, unknown>;
}

/** Walk up fibers to find the nearest component name */
function getComponentName(fiber: Record<string, unknown>): string | null {
  let current: Record<string, unknown> | null = fiber;
  while (current) {
    const fiberType = current.type;
    if (typeof fiberType === 'function' || (typeof fiberType === 'object' && fiberType !== null)) {
      const ct = fiberType as Record<string, unknown>;
      const name = (ct.displayName as string) || (ct.name as string);
      if (name && name !== 'RoleGuard') return name;
    }
    current = current.return as Record<string, unknown> | null;
  }
  return null;
}

function Overlay({ info }: { info: OverlayInfo }) {
  const tooltipAbove = info.top > 28;

  return (
    <div
      style={{
        position: 'fixed',
        top: info.top,
        left: info.left,
        width: info.width,
        height: info.height,
        border: '2px solid #3b82f6',
        borderRadius: 3,
        pointerEvents: 'none',
        zIndex: 2147483647,
        background: 'rgba(59,130,246,0.05)',
      }}
    >
      <div
        style={{
          position: 'absolute',
          ...(tooltipAbove ? { bottom: '100%', marginBottom: 2 } : { top: '100%', marginTop: 2 }),
          left: 0,
          background: '#1e40af',
          color: 'white',
          padding: '3px 8px',
          fontSize: 12,
          fontFamily: 'ui-monospace, monospace',
          fontWeight: 500,
          whiteSpace: 'nowrap',
          borderRadius: 4,
          lineHeight: '18px',
          boxShadow: '0 2px 8px rgba(0,0,0,0.2)',
        }}
      >
        {info.label}
      </div>
    </div>
  );
}

function ComponentInspector() {
  const [overlay, setOverlay] = useState<OverlayInfo | null>(null);
  const overlayRef = useRef<OverlayInfo | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!e.altKey) {
      if (overlayRef.current) {
        overlayRef.current = null;
        setOverlay(null);
      }
      return;
    }

    const target = document.elementFromPoint(e.clientX, e.clientY);
    if (!target) return;
    if (rootRef.current?.contains(target)) return;

    const match = findDataComponent(target);
    let label: string;
    let highlightEl: Element;

    if (match) {
      label = match.label;
      highlightEl = match.element;
    } else {
      // Fallback: use React fiber component name
      const fiber = getReactFiber(target);
      if (!fiber) {
        overlayRef.current = null;
        setOverlay(null);
        return;
      }
      const name = getComponentName(fiber);
      if (!name) {
        overlayRef.current = null;
        setOverlay(null);
        return;
      }
      label = name;
      highlightEl = target;
    }

    const rect = highlightEl.getBoundingClientRect();
    const newOverlay: OverlayInfo = {
      top: rect.top,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      label,
      targetElement: highlightEl,
    };
    overlayRef.current = newOverlay;
    setOverlay(newOverlay);
  }, []);

  const handleKeyUp = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Alt') {
      overlayRef.current = null;
      setOverlay(null);
    }
  }, []);

  const handleClick = useCallback((e: MouseEvent) => {
    if (!e.altKey) return;
    const current = overlayRef.current;
    if (!current) return;

    e.preventDefault();
    e.stopPropagation();

    // Resolve component name from fiber
    let componentName: string | null = null;
    const fiber = getReactFiber(current.targetElement);
    if (fiber) componentName = getComponentName(fiber);

    // Check if label came from data-component or fiber fallback
    const hasDataComponent = current.targetElement.closest('[data-component]') !== null;

    const clipboard: Record<string, string | null> = { component: componentName };
    if (hasDataComponent) {
      clipboard['data-component'] = current.label;
    }

    const json = JSON.stringify(clipboard);
    navigator.clipboard.writeText(json);
  }, []);

  useEffect(() => {
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('click', handleClick, true);
    document.addEventListener('keyup', handleKeyUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('click', handleClick, true);
      document.removeEventListener('keyup', handleKeyUp);
    };
  }, [handleMouseMove, handleClick, handleKeyUp]);

  if (!overlay) return null;
  return createPortal(
    <div ref={rootRef}>
      <Overlay info={overlay} />
    </div>,
    document.body
  );
}

/** Any user in development; only a superAdmin in production. Must render inside AuthProvider. */
export function DevClickToComponent() {
  const { userData } = useAuth();
  const allowed = process.env.NODE_ENV === 'development' || userData?.userType === 'superAdmin';
  return allowed ? <ComponentInspector /> : null;
}
