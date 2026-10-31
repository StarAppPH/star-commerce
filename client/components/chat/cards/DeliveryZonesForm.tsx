'use client';
import { useEffect, useRef, useState } from 'react';
import { importLibrary } from '@googlemaps/js-api-loader';
import { MapIcon, PlusIcon, Trash2Icon, Undo2Icon } from 'lucide-react';
import type { DeliveryZoneResponse } from '@/app/schemas/DeliveryZoneResponseSchema';
import type { NewDeliveryZone } from '@/app/schemas/NewDeliveryZoneSchema';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Field, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import { useSnackbar } from '@/lib/SnackbarContext';
import { cn } from '@/lib/utils';
import { ChatCard, SentNote } from './ChatCard';
import {
  DELIVERY_ZONES_MAX, DELIVERY_ZONE_CORNERS_MAX, DELIVERY_ZONE_CORNERS_MIN, lineCrossesZone, samePoint, strictlyInside, zoneProblem, zonesOverlap,
} from './deliveryZones';
import { zoneRuleText } from './format';
import { DEFAULT_MAP_START, MAPS_AVAILABLE, ZONE_STYLE, interaction, loadMaps, toLatLng, type MapPoint } from './MapPicker';
import type { ComponentProps } from './types';

type Corners = readonly MapPoint[];

// The server's limits on a zone's name, days and amounts (objects/store.py).
const NAME_MAX = 40;
const DAYS_MAX = 365;
const AMOUNT_MAX = 10_000_000;

// A zone as the form edits it: its shape and its policy as typed.
interface ZoneDraft {
  key: number;
  points: Corners;
  name: string;
  deliveryFee: string;
  deliveryDays: string;
  minimumOrder: string;
  freeDeliveryFrom: string;
}

type PolicyField = 'name' | 'deliveryFee' | 'deliveryDays' | 'minimumOrder' | 'freeDeliveryFrom';

// Each zone's color on the map, by its place in the list.
const ZONE_COLORS = ['#2563EB', '#16A34A', '#D97706', '#9333EA', '#0891B2', '#DB2777', '#65A30D', '#EA580C', '#4F46E5', '#0D9488'];
const DRAFT_COLOR = '#DC2626';
// A tap this many pixels from another zone's corner lands on that corner, so
// neighbouring zones share a border exactly (and near the first corner, on it).
const SNAP_PIXELS = 16;

function zoneColor(index: number): string {
  return ZONE_COLORS[index % ZONE_COLORS.length];
}

function amountText(value: number | null): string {
  return value === null ? '' : String(value);
}

function fromResponse(zone: DeliveryZoneResponse, key: number): ZoneDraft {
  return {
    key,
    points: zone.points,
    name: zone.name,
    deliveryFee: String(zone.deliveryFee),
    deliveryDays: String(zone.deliveryDays),
    minimumOrder: amountText(zone.minimumOrder),
    freeDeliveryFrom: amountText(zone.freeDeliveryFrom),
  };
}

function parseAmount(text: string): number | null {
  if (text.trim() === '') return null;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 && value <= AMOUNT_MAX ? value : Number.NaN;
}

// A zone's policy as sent, or what is missing from it.
function zonePolicy(zone: ZoneDraft): { policy: NewDeliveryZone } | { problem: string } {
  const name = zone.name.trim();
  if (name === '') return { problem: 'needs a name' };
  const fee = parseAmount(zone.deliveryFee);
  if (fee === null || Number.isNaN(fee)) return { problem: 'needs a delivery fee (0 for free)' };
  const days = Number(zone.deliveryDays);
  if (zone.deliveryDays.trim() === '' || !Number.isInteger(days) || days < 0 || days > DAYS_MAX) {
    return { problem: 'needs the days a delivery takes (0 for same day)' };
  }
  const minimumOrder = parseAmount(zone.minimumOrder);
  if (Number.isNaN(minimumOrder)) return { problem: 'has a minimum order that is not an amount' };
  const freeDeliveryFrom = parseAmount(zone.freeDeliveryFrom);
  if (Number.isNaN(freeDeliveryFrom)) return { problem: 'has a free delivery total that is not an amount' };
  return { policy: { name, points: [...zone.points], deliveryFee: fee, deliveryDays: days, minimumOrder, freeDeliveryFrom } };
}

// Meters per screen pixel at this zoom and latitude (Web Mercator).
function metersPerPixel(zoom: number, latitude: number): number {
  return (156543.03392 * Math.cos((latitude * Math.PI) / 180)) / 2 ** zoom;
}

function metersBetween(a: MapPoint, b: MapPoint): number {
  const meanLatitude = ((a.latitude + b.latitude) / 2) * (Math.PI / 180);
  const dx = (b.longitude - a.longitude) * 111320 * Math.cos(meanLatitude);
  const dy = (b.latitude - a.latitude) * 110540;
  return Math.hypot(dx, dy);
}

// The target corner nearest the tap, when it is within SNAP_PIXELS.
function snapped(point: MapPoint, targets: Corners, zoom: number): MapPoint {
  const reach = SNAP_PIXELS * metersPerPixel(zoom, point.latitude);
  let best: MapPoint | null = null;
  let bestMeters = reach;
  for (const target of targets) {
    const meters = metersBetween(point, target);
    if (meters <= bestMeters) {
      best = target;
      bestMeters = meters;
    }
  }
  return best ?? point;
}

interface MapLibraries {
  Polygon: typeof google.maps.Polygon;
  Polyline: typeof google.maps.Polyline;
  Marker: typeof google.maps.Marker;
  SymbolPath: typeof google.maps.SymbolPath;
}

function cornerMarker(libraries: MapLibraries, map: google.maps.Map, point: MapPoint, color: string, size: number, title?: string): google.maps.Marker {
  return new libraries.Marker({
    map,
    position: toLatLng(point),
    clickable: title !== undefined,
    cursor: title !== undefined ? 'pointer' : undefined,
    title,
    icon: {
      path: libraries.SymbolPath.CIRCLE,
      scale: size,
      fillColor: color,
      fillOpacity: 1,
      strokeColor: '#FFFFFF',
      strokeWeight: 2,
    },
  });
}

interface ZoneMapProps {
  start: MapPoint;
  startZoom: number;
  // The closed zones.
  zones: readonly Corners[];
  // The zone being drawn: its corners so far (null: not drawing).
  draft: Corners | null;
  selected: number | null;
  // A zone the last check found in the way, outlined in red.
  conflict: number | null;
  onMapClick: (point: MapPoint) => void;
  // The first corner was tapped: close the zone.
  onFirstCorner: () => void;
  onZoneClick: (index: number) => void;
  disabled: boolean;
  dataComponent: string;
}

// A Google map the admin draws delivery zones on: a tap places a corner (on
// another zone's corner when it is near one), a tap on the first corner closes
// the zone; a tap on a closed zone selects it.
function ZoneMap({
  start, startZoom, zones, draft, selected, conflict, onMapClick, onFirstCorner, onZoneClick, disabled, dataComponent,
}: ZoneMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const startRef = useRef({ start, startZoom });
  const disabledRef = useRef(disabled);
  const handlers = useRef({ onMapClick, onFirstCorner, onZoneClick });
  const snapTargets = useRef<Corners>([]);
  const [map, setMap] = useState<google.maps.Map | null>(null);
  const [libraries, setLibraries] = useState<MapLibraries | null>(null);
  const [failed, setFailed] = useState(!MAPS_AVAILABLE);

  useEffect(() => { handlers.current = { onMapClick, onFirstCorner, onZoneClick }; }, [onMapClick, onFirstCorner, onZoneClick]);
  // The other zones' corners, and while drawing the first corner, so a tap near it closes the zone.
  useEffect(() => { snapTargets.current = draft?.length ? [...zones.flat(), draft[0]] : zones.flat(); }, [zones, draft]);

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
        zoomControl: true,
        clickableIcons: false,
        draggableCursor: 'crosshair',
        ...interaction(disabledRef.current),
      });
      listener = created.addListener('click', (event: google.maps.MapMouseEvent) => {
        if (!event.latLng) return;
        const tapped = { latitude: event.latLng.lat(), longitude: event.latLng.lng() };
        handlers.current.onMapClick(snapped(tapped, snapTargets.current, created.getZoom() ?? zoom));
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
      const color = zoneColor(index);
      const polygon = new libraries.Polygon({
        ...ZONE_STYLE,
        strokeColor: index === conflict ? DRAFT_COLOR : color,
        fillColor: color,
        ...(index === selected || index === conflict ? { strokeWeight: 4, fillOpacity: 0.35 } : {}),
        map,
        paths: points.map(toLatLng),
        // While drawing, a tap inside a zone reaches the map (and is refused there).
        clickable: !drawing && !disabled,
      });
      polygon.addListener('click', () => handlers.current.onZoneClick(index));
      return polygon;
    });
    // While drawing, the other zones' corners show where a shared border can start.
    const corners = drawing
      ? zones.flatMap((points, index) => points.map((point) => {
        const marker = cornerMarker(libraries, map, point, zoneColor(index), 4, 'Use this corner');
        marker.addListener('click', () => handlers.current.onMapClick(point));
        return marker;
      }))
      : [];
    return () => {
      for (const polygon of polygons) polygon.setMap(null);
      for (const marker of corners) marker.setMap(null);
    };
  }, [map, libraries, zones, selected, conflict, drawing, disabled]);

  useEffect(() => {
    if (!map || !libraries || !draft || draft.length === 0) return undefined;
    const line = new libraries.Polyline({
      map,
      path: draft.map(toLatLng),
      strokeColor: DRAFT_COLOR,
      strokeWeight: 2,
      clickable: false,
    });
    const corners = draft.map((point, index) => cornerMarker(libraries, map, point, DRAFT_COLOR, index === 0 ? 8 : 5, index === 0 ? 'Close the zone' : undefined));
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
          The map is unavailable right now, so zones can’t be drawn. Try again later.
        </p>
      ) : (
        <div ref={container} className="absolute inset-0" role="application" aria-label="Map. Tap to place the corners of a delivery zone." />
      )}
    </div>
  );
}

interface ZonePolicyFieldsProps {
  zone: ZoneDraft;
  index: number;
  currency: string;
  disabled: boolean;
  onChange: (field: PolicyField, value: string) => void;
}

// The selected zone's own delivery policy.
function ZonePolicyFields({ zone, index, currency, disabled, onChange }: ZonePolicyFieldsProps) {
  const input = (field: PolicyField, label: string, placeholder: string, inputMode: 'decimal' | 'numeric' | 'text') => (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Input
        data-component={`deliveryZonesForm${field[0].toUpperCase()}${field.slice(1)}`}
        aria-label={`Zone ${index + 1}: ${label}`}
        placeholder={placeholder}
        inputMode={inputMode}
        maxLength={field === 'name' ? NAME_MAX : undefined}
        value={zone[field]}
        disabled={disabled}
        onChange={(event) => onChange(field, event.target.value)}
      />
    </Field>
  );
  return (
    <div data-component="deliveryZonesFormPolicy" className="grid grid-cols-2 gap-3 px-4 pb-3">
      <div className="col-span-2">{input('name', 'Zone name', 'e.g. Makati', 'text')}</div>
      {input('deliveryFee', `Delivery fee (${currency})`, '0 for free', 'decimal')}
      {input('deliveryDays', 'Days to deliver', '0 for same day', 'numeric')}
      {input('minimumOrder', `Minimum order (${currency})`, 'Optional', 'decimal')}
      {input('freeDeliveryFrom', `Free delivery from (${currency})`, 'Optional', 'decimal')}
    </div>
  );
}

// The admin draws where the store delivers: zones on the map that never
// overlap (they may share a border), each with its own delivery policy, saved
// together (setMyDeliveryZones); then the chat goes on.
export function DeliveryZonesForm({ component, answered, busy, onAnswer }: ComponentProps<'deliveryZonesForm'>) {
  const { userData, adminRequest } = useAuth();
  const userId = userData?._id ?? null;
  const { server } = useServer();
  const { showApiResponseSnackbar, showSnackbar } = useSnackbar();
  const [zones, setZones] = useState<ZoneDraft[]>(() => component.zones.map(fromResponse));
  const [nextKey, setNextKey] = useState<number>(component.zones.length);
  const [draft, setDraft] = useState<Corners | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [conflict, setConflict] = useState<number | null>(null);
  const [working, setWorking] = useState(false);
  const locked = answered || busy || working;
  const opening = component.storeLocation ?? zones[0]?.points[0] ?? DEFAULT_MAP_START;
  const shapes = zones.map((zone) => zone.points);
  const checked = zones.map(zonePolicy);
  const firstProblem = checked.findIndex((result) => 'problem' in result);

  const refuse = (message: string, zoneIndex: number | null) => {
    setConflict(zoneIndex);
    showSnackbar(message, 'error');
  };

  const startZone = () => {
    setSelected(null);
    setConflict(null);
    setDraft([]);
  };

  const closeZone = () => {
    if (!draft) return;
    const problem = zoneProblem(draft);
    if (problem) {
      refuse(`This zone can’t be closed: ${problem}.`, null);
      return;
    }
    const clash = shapes.findIndex((shape) => zonesOverlap(draft, shape));
    if (clash >= 0) {
      refuse(`This zone overlaps ${zones[clash].name || `zone ${clash + 1}`}. Zones can share a border, never overlap.`, clash);
      return;
    }
    setZones((current) => [...current, {
      key: nextKey, points: draft, name: '', deliveryFee: '', deliveryDays: '', minimumOrder: '', freeDeliveryFrom: '',
    }]);
    setNextKey((key) => key + 1);
    setSelected(zones.length);
    setConflict(null);
    setDraft(null);
  };

  const placeCorner = (point: MapPoint) => {
    if (locked) return;
    if (draft === null) {
      setSelected(null);
      return;
    }
    // The first corner again (also when shared with another zone's corner) closes the zone.
    if (draft.length > 0 && samePoint(point, draft[0])) {
      closeZone();
      return;
    }
    if (draft.some((corner) => samePoint(corner, point))) {
      refuse('That corner is already in this zone. Tap the first corner to close it.', null);
      return;
    }
    if (draft.length >= DELIVERY_ZONE_CORNERS_MAX) {
      refuse(`A zone has at most ${DELIVERY_ZONE_CORNERS_MAX} corners: close it.`, null);
      return;
    }
    const inside = shapes.findIndex((shape) => strictlyInside(point, shape));
    if (inside >= 0) {
      refuse(`That spot is inside ${zones[inside].name || `zone ${inside + 1}`}. Place corners outside it, or tap its corners to share its border.`, inside);
      return;
    }
    const last = draft[draft.length - 1];
    const crossed = last ? shapes.findIndex((shape) => lineCrossesZone(last, point, shape)) : -1;
    if (crossed >= 0) {
      refuse(`That line cuts through ${zones[crossed].name || `zone ${crossed + 1}`}. Go around it, or along its corners.`, crossed);
      return;
    }
    setConflict(null);
    setDraft([...draft, point]);
  };

  const updateZone = (index: number, field: PolicyField, value: string) => {
    setZones((current) => current.map((zone, i) => (i === index ? { ...zone, [field]: value } : zone)));
  };

  const deleteZone = () => {
    if (selected === null) return;
    setZones((current) => current.filter((_, index) => index !== selected));
    setSelected(null);
    setConflict(null);
  };

  const save = async () => {
    if (userId === null || draft !== null) return;
    const policies: NewDeliveryZone[] = [];
    for (const [index, result] of checked.entries()) {
      if ('problem' in result) {
        setSelected(index);
        refuse(`${zones[index].name.trim() || `Zone ${index + 1}`} ${result.problem}.`, null);
        return;
      }
      policies.push(result.policy);
    }
    const names = policies.map((policy) => policy.name.toLocaleLowerCase());
    const repeated = names.findIndex((name, index) => names.indexOf(name) !== index);
    if (repeated >= 0) {
      setSelected(repeated);
      refuse(`Two zones are called ${policies[repeated].name}: give each its own name.`, null);
      return;
    }
    setWorking(true);
    try {
      const res = await adminRequest(() => server.setMyDeliveryZones(userId, policies));
      showApiResponseSnackbar(res);
      if (res.status === 200) {
        setZones(res.data.deliveryZones.map(fromResponse));
        setSelected(null);
        onAnswer({ kind: 'deliveryZones', formId: component.formId });
      }
    } finally {
      setWorking(false);
    }
  };

  let note: string;
  if (draft !== null) {
    note = `Tap the map to place corners (${draft.length} so far). Tap another zone’s corner to share its border. Tap the first corner or Close zone to finish.`;
  } else if (zones.length === 0) {
    note = 'No zones yet: customers can’t order delivery until you draw one. Tap Add zone, then tap the map around the area you deliver to.';
  } else {
    note = 'Customers can pin a delivery only inside a zone, and pay that zone’s fee. Tap a zone to edit its details.';
  }

  return (
    <ChatCard
      dataComponent="deliveryZonesForm"
      icon={<MapIcon />}
      title="Where do you deliver?"
      footer={answered ? <SentNote text="Delivery zones saved" /> : draft !== null ? (
        <>
          <Button data-component="deliveryZonesFormCancel" variant="ghost" className="me-auto" disabled={locked} onClick={() => { setDraft(null); setConflict(null); }}>
            Cancel
          </Button>
          <Button data-component="deliveryZonesFormUndo" variant="outline" disabled={locked || draft.length === 0} onClick={() => setDraft(draft.slice(0, -1))}>
            <Undo2Icon />
            Undo corner
          </Button>
          <Button data-component="deliveryZonesFormClose" disabled={locked || draft.length < DELIVERY_ZONE_CORNERS_MIN} onClick={closeZone}>
            Close zone
          </Button>
        </>
      ) : (
        <>
          {selected !== null && (
            <Button data-component="deliveryZonesFormDelete" variant="destructive-outline" className="me-auto" disabled={locked} onClick={deleteZone}>
              <Trash2Icon />
              Delete zone
            </Button>
          )}
          <Button
            data-component="deliveryZonesFormAdd"
            variant="outline"
            disabled={locked || !MAPS_AVAILABLE || zones.length >= DELIVERY_ZONES_MAX}
            onClick={startZone}
          >
            <PlusIcon />
            Add zone
          </Button>
          <Button data-component="deliveryZonesFormSave" loading={working} disabled={busy || userId === null || firstProblem >= 0} onClick={save}>
            Save zones
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
            zones={shapes}
            draft={draft}
            selected={selected}
            conflict={conflict}
            onMapClick={placeCorner}
            onFirstCorner={() => { if (draft?.length) placeCorner(draft[0]); }}
            onZoneClick={(index) => { if (!locked) setSelected(index === selected ? null : index); }}
            disabled={locked}
          />
        </div>
      </div>
      {zones.length > 0 && (
        <ul data-component="deliveryZonesFormList" className="flex flex-col gap-1 px-3 pb-3">
          {zones.map((zone, index) => {
            const result = checked[index];
            return (
              <li key={zone.key}>
                <button
                  type="button"
                  data-component="deliveryZonesFormZone"
                  disabled={locked || draft !== null}
                  onClick={() => setSelected(index === selected ? null : index)}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm',
                    index === selected ? 'bg-muted' : 'hover:bg-muted/60',
                  )}
                >
                  <span aria-hidden="true" className="size-3 shrink-0 rounded-sm" style={{ backgroundColor: zoneColor(index) }} />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="font-medium">{zone.name.trim() || `Zone ${index + 1}`}</span>
                    {'policy' in result && (
                      <span className="text-muted-foreground">
                        {' · '}
                        {zoneRuleText({ ...result.policy, minimumOrder: result.policy.minimumOrder ?? null, freeDeliveryFrom: result.policy.freeDeliveryFrom ?? null }, component.currency)}
                      </span>
                    )}
                  </span>
                  {'problem' in result && <Badge variant="warning" size="sm">Needs details</Badge>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {selected !== null && zones[selected] && !answered && (
        <ZonePolicyFields
          zone={zones[selected]}
          index={selected}
          currency={component.currency}
          disabled={locked}
          onChange={(field, value) => updateZone(selected, field, value)}
        />
      )}
      {!answered && selected === null && firstProblem >= 0 && draft === null && (
        <p data-component="deliveryZonesFormMissing" className="px-4 pb-3 text-muted-foreground text-xs">
          Fill in every zone’s details to save: tap a zone marked Needs details.
        </p>
      )}
    </ChatCard>
  );
}
