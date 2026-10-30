'use client';
import { useEffect, useRef, useState } from 'react';
import { importLibrary } from '@googlemaps/js-api-loader';
import { MapIcon, PlusIcon, Trash2Icon, Undo2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { ChatCard, SentNote } from './ChatCard';
import { DELIVERY_ZONES_MAX, DELIVERY_ZONE_CORNERS_MAX, DELIVERY_ZONE_CORNERS_MIN, zoneProblem } from './deliveryZones';
import { DEFAULT_MAP_START, MAPS_AVAILABLE, ZONE_STYLE, interaction, loadMaps, toLatLng, type MapPoint } from './MapPicker';
import type { ComponentProps } from './types';

type Corners = readonly MapPoint[];

interface MapLibraries {
  Polygon: typeof google.maps.Polygon;
  Polyline: typeof google.maps.Polyline;
  Marker: typeof google.maps.Marker;
  SymbolPath: typeof google.maps.SymbolPath;
}

interface ZoneMapProps {
  start: MapPoint;
  startZoom: number;
  // The closed areas.
  zones: readonly Corners[];
  // The area being drawn: its corners so far (null: not drawing).
  draft: Corners | null;
  selected: number | null;
  onMapClick: (point: MapPoint) => void;
  // The first corner was tapped: close the area.
  onFirstCorner: () => void;
  onZoneClick: (index: number) => void;
  disabled: boolean;
  dataComponent: string;
}

const DRAFT_COLOR = '#DC2626';

function corner(libraries: MapLibraries, map: google.maps.Map, point: MapPoint, first: boolean): google.maps.Marker {
  return new libraries.Marker({
    map,
    position: toLatLng(point),
    clickable: first,
    cursor: first ? 'pointer' : undefined,
    title: first ? 'Close the area' : undefined,
    icon: {
      path: libraries.SymbolPath.CIRCLE,
      scale: first ? 8 : 5,
      fillColor: DRAFT_COLOR,
      fillOpacity: 1,
      strokeColor: '#FFFFFF',
      strokeWeight: 2,
    },
  });
}

// A Google map the admin draws delivery areas on: a tap places a corner, a
// tap on the first corner closes the area; a tap on a closed area selects it.
function ZoneMap({ start, startZoom, zones, draft, selected, onMapClick, onFirstCorner, onZoneClick, disabled, dataComponent }: ZoneMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const startRef = useRef({ start, startZoom });
  const disabledRef = useRef(disabled);
  const handlers = useRef({ onMapClick, onFirstCorner, onZoneClick });
  const [map, setMap] = useState<google.maps.Map | null>(null);
  const [libraries, setLibraries] = useState<MapLibraries | null>(null);
  const [failed, setFailed] = useState(!MAPS_AVAILABLE);

  useEffect(() => { handlers.current = { onMapClick, onFirstCorner, onZoneClick }; }, [onMapClick, onFirstCorner, onZoneClick]);

  useEffect(() => {
    if (!MAPS_AVAILABLE) return;
    let cancelled = false;
    let listener: google.maps.MapsEventListener | null = null;
    Promise.all([loadMaps(), importLibrary('marker'), importLibrary('core')]).then(([maps, markers, core]) => {
      if (cancelled || !container.current) return;
      const { start: point, startZoom: zoom } = startRef.current;
      const created = new maps.Map(container.current, {
        center: toLatLng(point),
        zoom,
        disableDefaultUI: true,
        clickableIcons: false,
        draggableCursor: 'crosshair',
        ...interaction(disabledRef.current),
      });
      listener = created.addListener('click', (event: google.maps.MapMouseEvent) => {
        if (event.latLng) handlers.current.onMapClick({ latitude: event.latLng.lat(), longitude: event.latLng.lng() });
      });
      setLibraries({ Polygon: maps.Polygon, Polyline: maps.Polyline, Marker: markers.Marker, SymbolPath: core.SymbolPath });
      setMap(created);
    }).catch((error: unknown) => {
      console.error('The map could not load', error);
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
      listener?.remove();
    };
  }, []);

  useEffect(() => {
    disabledRef.current = disabled;
    map?.setOptions({ ...interaction(disabled), draggableCursor: disabled ? undefined : 'crosshair' });
  }, [map, disabled]);

  const drawing = draft !== null;

  useEffect(() => {
    if (!map || !libraries) return undefined;
    const polygons = zones.map((points, index) => {
      const polygon = new libraries.Polygon({
        ...ZONE_STYLE,
        ...(index === selected ? { strokeWeight: 4, fillOpacity: 0.35 } : {}),
        map,
        paths: points.map(toLatLng),
        // While drawing, a tap inside an area places a corner instead.
        clickable: !drawing && !disabled,
      });
      polygon.addListener('click', () => handlers.current.onZoneClick(index));
      return polygon;
    });
    return () => { for (const polygon of polygons) polygon.setMap(null); };
  }, [map, libraries, zones, selected, drawing, disabled]);

  useEffect(() => {
    if (!map || !libraries || !draft || draft.length === 0) return undefined;
    const line = new libraries.Polyline({
      map,
      path: draft.map(toLatLng),
      strokeColor: DRAFT_COLOR,
      strokeWeight: 2,
      clickable: false,
    });
    const corners = draft.map((point, index) => corner(libraries, map, point, index === 0));
    corners[0].addListener('click', () => handlers.current.onFirstCorner());
    return () => {
      line.setMap(null);
      for (const marker of corners) marker.setMap(null);
    };
  }, [map, libraries, draft]);

  return (
    <div data-component={dataComponent} className="relative isolate h-72 w-full overflow-hidden bg-muted sm:h-80">
      {failed ? (
        <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-muted-foreground text-sm">
          The map is unavailable right now, so areas can’t be drawn. Try again later.
        </p>
      ) : (
        <div ref={container} className="absolute inset-0" role="application" aria-label="Map. Tap to place the corners of a delivery area." />
      )}
    </div>
  );
}

function areasText(count: number): string {
  if (count === 0) return 'No areas yet: you deliver anywhere.';
  return `${count} area${count > 1 ? 's' : ''}. Customers can pin a delivery only inside one.`;
}

// The admin draws where the store delivers: areas on the map, each closed,
// saved together (setMyDeliveryZones); then the chat goes on.
export function DeliveryZonesForm({ component, answered, busy, onAnswer }: ComponentProps<'deliveryZonesForm'>) {
  const { userData, adminRequest } = useAuth();
  const userId = userData?._id ?? null;
  const { server } = useServer();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const [zones, setZones] = useState<readonly Corners[]>(() => component.deliveryZones.map((zone) => zone.points));
  const [draft, setDraft] = useState<Corners | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [working, setWorking] = useState(false);
  const locked = answered || busy || working;
  const opening = component.storeLocation ?? zones[0]?.[0] ?? DEFAULT_MAP_START;

  const startArea = () => {
    setSelected(null);
    setDraft([]);
  };

  const closeArea = () => {
    if (!draft) return;
    const problem = zoneProblem(draft);
    if (problem) {
      showSnackbar(`This area can’t be saved: ${problem}.`, 'error');
      return;
    }
    setZones((current) => [...current, draft]);
    setDraft(null);
  };

  const placeCorner = (point: MapPoint) => {
    if (locked) return;
    if (draft === null) {
      setSelected(null);
      return;
    }
    if (draft.length >= DELIVERY_ZONE_CORNERS_MAX) {
      showSnackbar(`An area has at most ${DELIVERY_ZONE_CORNERS_MAX} corners: close it.`, 'error');
      return;
    }
    setDraft([...draft, point]);
  };

  const deleteArea = () => {
    if (selected === null) return;
    setZones((current) => current.filter((_, index) => index !== selected));
    setSelected(null);
  };

  const save = async () => {
    if (userId === null || draft !== null) return;
    setWorking(true);
    try {
      const res = await adminRequest(() => server.setMyDeliveryZones(userId, zones.map((points) => ({ points: [...points] }))));
      showApiResponseSnackbar(res);
      if (res.status === 200) {
        setZones(res.data.deliveryZones.map((zone) => zone.points));
        onAnswer({ kind: 'deliveryZones', formId: component.formId });
      }
    } finally {
      setWorking(false);
    }
  };

  const note = draft === null
    ? areasText(zones.length) + (zones.length > 0 && !answered ? ' Tap an area to select it.' : '')
    : `Tap the map to place corners (${draft.length} so far). Tap the first corner or Close area to finish.`;

  return (
    <ChatCard
      dataComponent="deliveryZonesForm"
      icon={<MapIcon />}
      title="Where do you deliver?"
      footer={answered ? <SentNote text="Delivery areas saved" /> : draft !== null ? (
        <>
          <Button data-component="deliveryZonesFormCancel" variant="ghost" className="me-auto" disabled={locked} onClick={() => setDraft(null)}>
            Cancel
          </Button>
          <Button data-component="deliveryZonesFormUndo" variant="outline" disabled={locked || draft.length === 0} onClick={() => setDraft(draft.slice(0, -1))}>
            <Undo2Icon />
            Undo corner
          </Button>
          <Button data-component="deliveryZonesFormClose" disabled={locked || draft.length < DELIVERY_ZONE_CORNERS_MIN} onClick={closeArea}>
            Close area
          </Button>
        </>
      ) : (
        <>
          {selected !== null && (
            <Button data-component="deliveryZonesFormDelete" variant="destructive-outline" className="me-auto" disabled={locked} onClick={deleteArea}>
              <Trash2Icon />
              Delete area
            </Button>
          )}
          <Button
            data-component="deliveryZonesFormAdd"
            variant="outline"
            disabled={locked || !MAPS_AVAILABLE || zones.length >= DELIVERY_ZONES_MAX}
            onClick={startArea}
          >
            <PlusIcon />
            Add area
          </Button>
          <Button data-component="deliveryZonesFormSave" loading={working} disabled={busy || userId === null} onClick={save}>
            Save areas
          </Button>
        </>
      )}
    >
      <p data-component="deliveryZonesFormNote" className="px-4 pt-3 text-muted-foreground text-sm">{note}</p>
      <div className="p-3">
        <div className="overflow-hidden rounded-lg border">
          <ZoneMap
            dataComponent="deliveryZonesFormMap"
            start={opening}
            startZoom={component.storeLocation || zones.length > 0 ? 12 : 11}
            zones={zones}
            draft={draft}
            selected={selected}
            onMapClick={placeCorner}
            onFirstCorner={closeArea}
            onZoneClick={(index) => { if (!locked) setSelected(index === selected ? null : index); }}
            disabled={locked}
          />
        </div>
      </div>
    </ChatCard>
  );
}
