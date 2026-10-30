'use client';
import { useEffect, useRef, useState } from 'react';
import { importLibrary, setOptions } from '@googlemaps/js-api-loader';
import { MapPinIcon } from 'lucide-react';

export interface MapPoint {
  latitude: number;
  longitude: number;
}

interface MapPickerProps {
  // Where the map opens.
  start: MapPoint;
  startZoom: number;
  // Moves the map there whenever it changes (a "Use where I am").
  target: MapPoint | null;
  // The point under the pin, after every move.
  onMove: (point: MapPoint) => void;
  // The user dragged or zoomed the map (a resize's pan is not one; a
  // `target` move zooms, so it counts too).
  onUserMove?: () => void;
  disabled?: boolean;
  dataComponent: string;
}

// Where a map opens when there is no pin yet: Metro Manila.
export const DEFAULT_MAP_START: MapPoint = { latitude: 14.5995, longitude: 120.9842 };

// The browser key: restricted to the app's websites and the Maps JavaScript
// API, so it is public by design.
const API_KEY = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ?? '';
let optionsSet = false;

function loadMaps(): Promise<google.maps.MapsLibrary> {
  if (!optionsSet) {
    setOptions({ key: API_KEY, v: 'weekly' });
    optionsSet = true;
  }
  return importLibrary('maps');
}

function interaction(disabled: boolean): google.maps.MapOptions {
  // greedy: one finger drags the map on a phone, as the pin needs.
  return { gestureHandling: disabled ? 'none' : 'greedy', zoomControl: !disabled, keyboardShortcuts: !disabled };
}

// A Google map with a pin fixed at its center: the user moves the map until
// the pin sits on the spot. The Maps JavaScript API loads in the browser only.
export function MapPicker({ start, startZoom, target, onMove, onUserMove, disabled = false, dataComponent }: MapPickerProps) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const onMoveRef = useRef(onMove);
  const startRef = useRef({ start, startZoom });
  const disabledRef = useRef(disabled);
  const onUserMoveRef = useRef(onUserMove);
  const [failed, setFailed] = useState(API_KEY === '');

  useEffect(() => { onMoveRef.current = onMove; }, [onMove]);
  useEffect(() => { onUserMoveRef.current = onUserMove; }, [onUserMove]);

  useEffect(() => {
    if (API_KEY === '') return;
    let cancelled = false;
    let listeners: google.maps.MapsEventListener[] = [];
    loadMaps().then(({ Map }) => {
      if (cancelled || !container.current) return;
      const { start: point, startZoom: zoom } = startRef.current;
      const created = new Map(container.current, {
        center: { lat: point.latitude, lng: point.longitude },
        zoom,
        disableDefaultUI: true,
        clickableIcons: false,
        ...interaction(disabledRef.current),
      });
      listeners = [
        created.addListener('dragstart', () => onUserMoveRef.current?.()),
        created.addListener('zoom_changed', () => onUserMoveRef.current?.()),
        created.addListener('idle', () => {
          const center = created.getCenter();
          if (center) onMoveRef.current({ latitude: center.lat(), longitude: center.lng() });
        }),
      ];
      mapRef.current = created;
    }).catch((error: unknown) => {
      console.error('The map could not load', error);
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
      for (const listener of listeners) listener.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!target) return;
    mapRef.current?.setCenter({ lat: target.latitude, lng: target.longitude });
    mapRef.current?.setZoom(17);
  }, [target]);

  useEffect(() => {
    disabledRef.current = disabled;
    mapRef.current?.setOptions(interaction(disabled));
  }, [disabled]);

  return (
    <div data-component={dataComponent} className="relative isolate h-64 w-full overflow-hidden bg-muted sm:h-72">
      {failed ? (
        <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-muted-foreground text-sm">
          The map is unavailable right now. Use where you are instead.
        </p>
      ) : (
        <>
          <div ref={container} className="absolute inset-0" role="application" aria-label="Map. Move it until the pin sits on the spot." />
          <MapPinIcon
            aria-hidden="true"
            className="pointer-events-none absolute top-1/2 left-1/2 z-[500] size-9 -translate-x-1/2 -translate-y-full fill-primary text-primary-foreground drop-shadow-md"
          />
        </>
      )}
    </div>
  );
}
