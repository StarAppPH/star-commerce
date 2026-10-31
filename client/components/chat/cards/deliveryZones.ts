import { DELIVERY_ZONE_BORDER_TOLERANCE } from '@/lib/generated/serverConstants';
import type { MapPoint } from './MapPicker';

// A store's delivery zones (server objects/store.py): up to 10 closed shapes
// of 3 to 100 corners in drawing order, the last joining the first, never
// overlapping (a shared border is fine). The server checks them again; these
// only spare the user a refused save.
export const DELIVERY_ZONES_MAX = 10;
export const DELIVERY_ZONE_CORNERS_MIN = 3;
export const DELIVERY_ZONE_CORNERS_MAX = 100;

export interface ZoneShape {
  readonly points: readonly MapPoint[];
}

type Pair = readonly [number, number];

// Whether two points are the same corner.
export function samePoint(a: MapPoint, b: MapPoint): boolean {
  return a.latitude === b.latitude && a.longitude === b.longitude;
}

function orientation(a: Pair, b: Pair, c: Pair): number {
  const cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  return Math.sign(cross);
}

// The shape check (zoneProblem) stays exact on purpose; the border and overlap
// checks use the tolerant nearSegment.
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

function pair(point: MapPoint): Pair {
  return [point.latitude, point.longitude];
}

function sides(corners: readonly MapPoint[]): [Pair, Pair][] {
  return corners.map((corner, i) => [pair(corner), pair(corners[(i + 1) % corners.length])]);
}

// Whether the point lies on the segment a-b, within
// DELIVERY_ZONE_BORDER_TOLERANCE of it.
function nearSegment(point: Pair, a: Pair, b: Pair): boolean {
  const tolerance = DELIVERY_ZONE_BORDER_TOLERANCE;
  // Beyond the segment's box widened by the tolerance: no need to measure.
  if (!(Math.min(a[0], b[0]) - tolerance <= point[0] && point[0] <= Math.max(a[0], b[0]) + tolerance
    && Math.min(a[1], b[1]) - tolerance <= point[1] && point[1] <= Math.max(a[1], b[1]) + tolerance)) {
    return false;
  }
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const length = dx * dx + dy * dy;
  const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length));
  const ex = point[0] - (a[0] + t * dx);
  const ey = point[1] - (a[1] + t * dy);
  return ex * ex + ey * ey <= tolerance * tolerance;
}

// Whether two segments cross through each other: touching at a corner or
// running along each other (a shared border) is no crossing, nor is an end
// that lies on the other segment.
function segmentsCross(p1: Pair, p2: Pair, q1: Pair, q2: Pair): boolean {
  const o = [orientation(p1, p2, q1), orientation(p1, p2, q2), orientation(q1, q2, p1), orientation(q1, q2, p2)];
  if (o.includes(0) || o[0] === o[1] || o[2] === o[3]) return false;
  return !(nearSegment(q1, p1, p2) || nearSegment(q2, p1, p2) || nearSegment(p1, q1, q2) || nearSegment(p2, q1, q2));
}

function onBorder(point: MapPoint, corners: readonly MapPoint[]): boolean {
  const p = pair(point);
  return sides(corners).some(([a, b]) => nearSegment(p, a, b));
}

// Whether the point lies inside the shape, its border excluded.
export function strictlyInside(point: MapPoint, corners: readonly MapPoint[]): boolean {
  return !onBorder(point, corners) && insideZone(point, corners);
}

// Whether the line from `from` to `to` cuts through the shape's sides.
export function lineCrossesZone(from: MapPoint, to: MapPoint, corners: readonly MapPoint[]): boolean {
  return sides(corners).some(([a, b]) => segmentsCross(pair(from), pair(to), a, b));
}

// Whether two zone shapes share any ground (the server's zonesOverlap).
export function zonesOverlap(first: readonly MapPoint[], second: readonly MapPoint[]): boolean {
  const key = (corners: readonly MapPoint[]) => new Set(corners.map((c) => `${c.latitude},${c.longitude}`));
  const a = key(first);
  const b = key(second);
  if (a.size === b.size && [...a].every((corner) => b.has(corner))) return true;
  if (sides(first).some(([p1, p2]) => sides(second).some(([q1, q2]) => segmentsCross(p1, p2, q1, q2)))) return true;
  return [[first, second], [second, first]].some(([one, other]) => {
    const probes = [...one, ...sides(one).map(([p, q]) => ({ latitude: (p[0] + q[0]) / 2, longitude: (p[1] + q[1]) / 2 }))];
    return probes.some((probe) => strictlyInside(probe, other));
  });
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

// The zone a pin falls in, its border included: the first one listed, as the
// server picks (deliveryZoneAt). Null outside every zone.
export function zoneAt<Z extends ZoneShape>(point: MapPoint, zones: readonly Z[]): Z | null {
  return zones.find((zone) => onBorder(point, zone.points) || insideZone(point, zone.points)) ?? null;
}
