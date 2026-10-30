'use client';
import { useEffect, useRef, useState } from 'react';
import { importLibrary, setOptions } from '@googlemaps/js-api-loader';
import { MapPinIcon } from 'lucide-react';
import type { ZoneShape } from './deliveryZones';

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
  // The store's delivery areas, shaded on the map.
  zones?: readonly ZoneShape[];
  dataComponent: string;
}

// Where a map opens when there is no pin yet: Metro Manila.
export const DEFAULT_MAP_START: MapPoint = { latitude: 14.5995, longitude: 120.9842 };

// The browser key: restricted to the app's websites and the Maps JavaScript
// API, so it is public by design.
const API_KEY = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ?? '';
export const MAPS_AVAILABLE = API_KEY !== '';
let optionsSet = false;

export function loadMaps(): Promise<google.maps.MapsLibrary> {
  if (!optionsSet) {
    setOptions({ key: API_KEY, v: 'weekly' });
    optionsSet = true;
  }
  return importLibrary('maps');
}

export function interaction(disabled: boolean): google.maps.MapOptions {
  // greedy: one finger drags the map on a phone, as the pin needs.
  return { gestureHandling: disabled ? 'none' : 'greedy', zoomControl: !disabled, keyboardShortcuts: !disabled };
}

// The look of a delivery area on a map.
export const ZONE_STYLE: google.maps.PolygonOptions = {
  strokeColor: '#2563EB',
  strokeOpacity: 0.9,
  strokeWeight: 2,
  fillColor: '#2563EB',
  fillOpacity: 0.15,
};

export function toLatLng(point: MapPoint): google.maps.LatLngLiteral {
  return { lat: point.latitude, lng: point.longitude };
}

// A Google map with a pin fixed at its center: the user moves the map until
// the pin sits on the spot. The Maps JavaScript API loads in the browser only.
export function MapPicker({ start, startZoom, target, onMove, onUserMove, disabled = false, zones, dataComponent }: MapPickerProps) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const onMoveRef = useRef(onMove);
  const startRef = useRef({ start, startZoom });
  const disabledRef = useRef(disabled);
  const onUserMoveRef = useRef(onUserMove);
  const zonesRef = useRef(zones);
  const [failed, setFailed] = useState(!MAPS_AVAILABLE);

  useEffect(() => { onMoveRef.current = onMove; }, [onMove]);
  useEffect(() => { onUserMoveRef.current = onUserMove; }, [onUserMove]);

  useEffect(() => {
    if (!MAPS_AVAILABLE) return;
    let cancelled = false;
    let listeners: google.maps.MapsEventListener[] = [];
    let shaded: google.maps.Polygon[] = [];
    loadMaps().then(({ Map, Polygon }) => {
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
      // Shaded only: a tap on an area still moves the map.
      shaded = (zonesRef.current ?? []).map((zone) => new Polygon({
        ...ZONE_STYLE,
        map: created,
        paths: zone.points.map(toLatLng),
        clickable: false,
      }));
      mapRef.current = created;
    }).catch((error: unknown) => {
      console.error('The map could not load', error);
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
      for (const listener of listeners) listener.remove();
      for (const polygon of shaded) polygon.setMap(null);
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
