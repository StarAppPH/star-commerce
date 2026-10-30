import type { MapPoint } from './MapPicker';

// A store's delivery areas (server objects/store.py): up to 10 closed shapes
// of 3 to 100 corners in drawing order, the last joining the first. The
// server checks them again; these only spare the user a refused save.
export const DELIVERY_ZONES_MAX = 10;
export const DELIVERY_ZONE_CORNERS_MIN = 3;
export const DELIVERY_ZONE_CORNERS_MAX = 100;

export interface ZoneShape {
  readonly points: readonly MapPoint[];
}

type Pair = readonly [number, number];

function orientation(a: Pair, b: Pair, c: Pair): number {
  const cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  return Math.sign(cross);
}

function onSegment(a: Pair, b: Pair, c: Pair): boolean {
  return Math.min(a[0], b[0]) <= c[0] && c[0] <= Math.max(a[0], b[0])
    && Math.min(a[1], b[1]) <= c[1] && c[1] <= Math.max(a[1], b[1]);
}

function segmentsMeet(p1: Pair, p2: Pair, q1: Pair, q2: Pair): boolean {
  const o1 = orientation(p1, p2, q1);
  const o2 = orientation(p1, p2, q2);
  const o3 = orientation(q1, q2, p1);
  const o4 = orientation(q1, q2, p2);
  if (o1 !== o2 && o3 !== o4) return true;
  return (o1 === 0 && onSegment(p1, p2, q1)) || (o2 === 0 && onSegment(p1, p2, q2))
    || (o3 === 0 && onSegment(q1, q2, p1)) || (o4 === 0 && onSegment(q1, q2, p2));
}

// What is wrong with an area's shape (the server's deliveryZoneProblem); null
// when it is a closed shape whose sides never cross.
export function zoneProblem(corners: readonly MapPoint[]): string | null {
  if (corners.length < DELIVERY_ZONE_CORNERS_MIN || corners.length > DELIVERY_ZONE_CORNERS_MAX) {
    return `draw ${DELIVERY_ZONE_CORNERS_MIN} to ${DELIVERY_ZONE_CORNERS_MAX} corners`;
  }
  const pairs: Pair[] = corners.map((corner) => [corner.latitude, corner.longitude]);
  if (new Set(pairs.map((pair) => pair.join(','))).size !== pairs.length) return 'a corner is placed twice';
  const count = pairs.length;
  for (let i = 0; i < count; i += 1) {
    for (let j = i + 1; j < count; j += 1) {
      // Neighbouring sides share a corner, which is no crossing.
      if (j === i + 1 || (i === 0 && j === count - 1)) continue;
      if (segmentsMeet(pairs[i], pairs[(i + 1) % count], pairs[j], pairs[(j + 1) % count])) {
        return 'its sides cross: draw it without crossing lines';
      }
    }
  }
  const area = pairs.reduce((sum, a, i) => {
    const b = pairs[(i + 1) % count];
    return sum + a[0] * b[1] - b[0] * a[1];
  }, 0);
  return area === 0 ? 'its corners lie in one line' : null;
}

function insideZone(point: MapPoint, corners: readonly MapPoint[]): boolean {
  let inside = false;
  for (let i = 0; i < corners.length; i += 1) {
    const a = corners[i];
    const b = corners[(i + 1) % corners.length];
    if ((a.latitude > point.latitude) !== (b.latitude > point.latitude)) {
      const crossing = a.longitude + ((point.latitude - a.latitude) * (b.longitude - a.longitude)) / (b.latitude - a.latitude);
      if (point.longitude < crossing) inside = !inside;
    }
  }
  return inside;
}

// Whether a store with these areas delivers to the point: anywhere without one.
export function insideZones(point: MapPoint, zones: readonly ZoneShape[] | null | undefined): boolean {
  if (!zones || zones.length === 0) return true;
  return zones.some((zone) => insideZone(point, zone.points));
}
