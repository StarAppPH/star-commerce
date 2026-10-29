'use client';
import { useState } from 'react';
import { LocateFixedIcon, MapPinIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { MapPicker, type MapPoint } from './MapPicker';
import type { ComponentProps } from './types';

// Where the map opens when the store has no pin yet: Metro Manila.
const DEFAULT_START: MapPoint = { latitude: 14.5995, longitude: 120.9842 };

// Pin the store: move the map until the pin sits on the door.
export function LocationForm({ component, answered, busy, onAnswer }: ComponentProps<'locationForm'>) {
  const { showSnackbar } = useSnackbar();
  const start = component.location ?? DEFAULT_START;
  const [point, setPoint] = useState<MapPoint>(start);
  const [target, setTarget] = useState<MapPoint | null>(null);
  const [locating, setLocating] = useState(false);

  const useWhereIAm = () => {
    if (!('geolocation' in navigator)) {
      showSnackbar('This browser can’t share its location. Move the map instead.', 'warning');
      return;
    }
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocating(false);
        const here = { latitude: position.coords.latitude, longitude: position.coords.longitude };
        setTarget(here);
        setPoint(here);
      },
      () => {
        setLocating(false);
        showSnackbar('We couldn’t get your location. Allow location access, or move the map.', 'warning');
      },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  };

  return (
    <ChatCard
      dataComponent="locationForm"
      icon={<MapPinIcon />}
      title="Pin your store"
      footer={answered ? <SentNote text="Pin sent" /> : (
        <>
          <span className="me-auto text-muted-foreground text-xs tabular-nums">
            {point.latitude.toFixed(5)}, {point.longitude.toFixed(5)}
          </span>
          <Button data-component="locationFormHere" variant="outline" loading={locating} disabled={busy} onClick={useWhereIAm}>
            <LocateFixedIcon />
            Use where I am
          </Button>
          <Button
            data-component="locationFormSave"
            disabled={busy}
            onClick={() => onAnswer({ kind: 'location', formId: component.formId, ...point })}
          >
            Save pin
          </Button>
        </>
      )}
    >
      {component.address && <p className="px-4 pt-3 text-muted-foreground text-sm">Near {component.address}</p>}
      <div className="p-3">
        <div className="overflow-hidden rounded-lg border">
          <MapPicker
            dataComponent="locationFormMap"
            start={start}
            startZoom={component.location ? 17 : 12}
            target={target}
            onMove={setPoint}
            disabled={answered || busy}
          />
        </div>
      </div>
    </ChatCard>
  );
}
