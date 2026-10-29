'use client';
import 'leaflet/dist/leaflet.css';
import { useEffect, useRef } from 'react';
import type { Map as LeafletMap } from 'leaflet';
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

const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// A map with a pin fixed at its center: the user moves the map until the pin
// sits on the spot. Leaflet loads in the browser only.
export function MapPicker({ start, startZoom, target, onMove, onUserMove, disabled = false, dataComponent }: MapPickerProps) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<LeafletMap | null>(null);
  const onMoveRef = useRef(onMove);
  const startRef = useRef({ start, startZoom });

  const onUserMoveRef = useRef(onUserMove);

  useEffect(() => { onMoveRef.current = onMove; }, [onMove]);
  useEffect(() => { onUserMoveRef.current = onUserMove; }, [onUserMove]);

  useEffect(() => {
    let cancelled = false;
    let map: LeafletMap | null = null;
    import('leaflet').then((L) => {
      if (cancelled || !container.current) return;
      const { start: point, startZoom: zoom } = startRef.current;
      map = L.map(container.current, { zoomControl: true }).setView([point.latitude, point.longitude], zoom);
      L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map);
      const created = map;
      created.on('dragstart zoomstart', () => onUserMoveRef.current?.());
      created.on('moveend', () => {
        const center = created.getCenter();
        onMoveRef.current({ latitude: center.lat, longitude: center.lng });
      });
      mapRef.current = created;
    });
    return () => {
      cancelled = true;
      map?.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (target) mapRef.current?.setView([target.latitude, target.longitude], 17);
  }, [target]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const handlers = [map.dragging, map.scrollWheelZoom, map.touchZoom, map.doubleClickZoom, map.keyboard];
    for (const handler of handlers) {
      if (disabled) handler.disable(); else handler.enable();
    }
  }, [disabled]);

  return (
    <div data-component={dataComponent} className="relative isolate h-64 w-full overflow-hidden bg-muted sm:h-72">
      <div ref={container} className="absolute inset-0" role="application" aria-label="Map. Move it until the pin sits on the spot." />
      <MapPinIcon
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-1/2 z-[500] size-9 -translate-x-1/2 -translate-y-full fill-primary text-primary-foreground drop-shadow-md"
      />
    </div>
  );
}
