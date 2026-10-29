'use client';
import { useState, type ReactNode } from 'react';
import { LocateFixedIcon, MapPinIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ChatCard, SentNote } from './ChatCard';
import { DEFAULT_MAP_START, MapPicker, type MapPoint } from './MapPicker';
import { useWhereIAm } from './useWhereIAm';

interface PinCardProps {
  // The card's data-component; its map and buttons add Map, Here and Send.
  dataComponent: string;
  title: string;
  // Above the map: what the pin is near, what it is for.
  note?: ReactNode;
  // The pin so far: the map opens there (else Metro Manila).
  start: MapPoint | null;
  startZoom: number;
  sendLabel: string;
  sentText: string;
  answered: boolean;
  busy: boolean;
  onSend: (point: MapPoint) => void;
}

// A map with a pin to move onto a spot, "Use where I am" and a send button:
// the store's pin and a delivery's.
export function PinCard({
  dataComponent, title, note, start, startZoom, sendLabel, sentText, answered, busy, onSend,
}: PinCardProps) {
  const opening = start ?? DEFAULT_MAP_START;
  const [point, setPoint] = useState<MapPoint>(opening);
  const [target, setTarget] = useState<MapPoint | null>(null);
  const { locating, locate } = useWhereIAm((here) => {
    setTarget(here);
    setPoint(here);
  });

  return (
    <ChatCard
      dataComponent={dataComponent}
      icon={<MapPinIcon />}
      title={title}
      footer={answered ? <SentNote text={sentText} /> : (
        <>
          <span className="me-auto text-muted-foreground text-xs tabular-nums">
            {point.latitude.toFixed(5)}, {point.longitude.toFixed(5)}
          </span>
          <Button data-component={`${dataComponent}Here`} variant="outline" loading={locating} disabled={busy} onClick={locate}>
            <LocateFixedIcon />
            Use where I am
          </Button>
          <Button data-component={`${dataComponent}Send`} disabled={busy} onClick={() => onSend(point)}>
            {sendLabel}
          </Button>
        </>
      )}
    >
      {note && <p className="px-4 pt-3 text-muted-foreground text-sm">{note}</p>}
      <div className="p-3">
        <div className="overflow-hidden rounded-lg border">
          <MapPicker
            dataComponent={`${dataComponent}Map`}
            start={opening}
            startZoom={startZoom}
            target={target}
            onMove={setPoint}
            disabled={answered || busy}
          />
        </div>
      </div>
    </ChatCard>
  );
}
