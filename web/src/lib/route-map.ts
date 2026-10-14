/**
 * The trip sheet's route map: the bundled coast, berths and routes (route-map-data.json, written by
 * scripts/build_route_map.py) and the maths that frames them. Map points are Web Mercator pixels at the
 * data's zoom (about 12 m), counted from its top-left corner; screen points are the SVG's own units.
 *
 * The app loads this module only when it first shows a map (useRouteMap), so neither the data nor the
 * maths are in its main bundle.
 */

import data from "./route-map-data.json";
import type { Terminal } from "./schema";
import { destinations } from "./terminals";

export type Point = readonly [number, number];

export interface RouteMapData {
  zoom: number;
  origin: readonly number[];
  /** The map's width and height. */
  size: readonly number[];
  /** Rings of land: a first point, then the steps to each next point, flattened. */
  land: readonly (readonly number[])[];
  /** Each terminal's berth. */
  terminals: Readonly<Record<string, readonly number[]>>;
  /** "A-B" (codes sorted): a path through the water from A to B, stored like a ring. */
  routes: Readonly<Record<string, readonly number[]>>;
}

export const ROUTE_MAP: RouteMapData = data;

export function decode(steps: readonly number[]): Point[] {
  const points: Point[] = [];
  let x = 0;
  let y = 0;
  for (let i = 0; i + 1 < steps.length; i += 2) {
    x += steps[i] ?? 0;
    y += steps[i + 1] ?? 0;
    points.push([x, y]);
  }
  return points;
}

export function berth(data: RouteMapData, code: string): Point | undefined {
  const at = data.terminals[code];
  return at && at.length === 2 ? [at[0] ?? 0, at[1] ?? 0] : undefined;
}

/** The path from one terminal to another, if the map has one. */
export function routePath(data: RouteMapData, from: string, to: string): Point[] | undefined {
  const reversed = from > to;
  const steps = data.routes[reversed ? `${to}-${from}` : `${from}-${to}`];
  if (!steps) return undefined;
  const points = decode(steps);
  return reversed ? points.reverse() : points;
}

const coastPaths = new WeakMap<RouteMapData, string>();

/**
 * The land as one SVG path, in map points. Each ring is drawn as a quadratic B-spline: a curve through its
 * edges' midpoints with its points as control points. The build script checks the routes against this same
 * shape, so they stay in the water as drawn.
 */
export function coastPath(data: RouteMapData): string {
  let path = coastPaths.get(data);
  if (path === undefined) {
    path = data.land.map((steps) => smoothRing(decode(steps))).join("");
    coastPaths.set(data, path);
  }
  return path;
}

export function smoothRing(ring: readonly Point[]): string {
  const last = ring[ring.length - 1];
  if (!last || ring.length < 3) return "";
  const mid = (a: Point, b: Point) => `${(a[0] + b[0]) / 2} ${(a[1] + b[1]) / 2}`;
  const parts = [`M${mid(last, ring[0] ?? last)}`];
  ring.forEach((point, i) => {
    parts.push(`Q${point[0]} ${point[1]} ${mid(point, ring[(i + 1) % ring.length] ?? point)}`);
  });
  return `${parts.join("")}Z`;
}

export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export function boxAround(points: readonly Point[]): Box | undefined {
  if (points.length === 0) return undefined;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
}

export interface Frame {
  /** The SVG's size, in its own units. */
  width: number;
  height: number;
  /** Kept clear around what's framed, for markers and labels. */
  padding: number;
  /** The least the frame shows across, in map points: a short crossing still shows where it is. */
  minSpan: number;
}

/** Which part of the map the frame shows: screen = (map - corner) × scale. */
export interface View {
  scale: number;
  left: number;
  top: number;
}

/**
 * The view that fits `box` inside the frame's padding, centred, without zooming in past `minSpan`. When the
 * view is smaller than `limits` (the map's extent), it's moved inside it, so it never shows past the data.
 */
export function fitView(box: Box, frame: Frame, limits?: Box): View {
  const width = Math.max(box.right - box.left, 1);
  const height = Math.max(box.bottom - box.top, 1);
  const scale = Math.min(
    (frame.width - 2 * frame.padding) / width,
    (frame.height - 2 * frame.padding) / height,
    frame.width / frame.minSpan,
  );
  const shown = { width: frame.width / scale, height: frame.height / scale };
  let left = (box.left + box.right) / 2 - shown.width / 2;
  let top = (box.top + box.bottom) / 2 - shown.height / 2;
  if (limits) {
    left = within(left, limits.left, limits.right - shown.width);
    top = within(top, limits.top, limits.bottom - shown.height);
  }
  return { scale, left, top };
}

/** `value` moved into [low, high]; unchanged if that range is empty. */
function within(value: number, low: number, high: number): number {
  return low > high ? value : Math.min(Math.max(value, low), high);
}

export function toScreen(view: View, point: Point): Point {
  return [(point[0] - view.left) * view.scale, (point[1] - view.top) * view.scale];
}

/** An SVG path through screen points. */
export function linePath(points: readonly Point[]): string {
  return points.map((p, i) => `${i === 0 ? "M" : "L"}${round(p[0])} ${round(p[1])}`).join("");
}

const round = (value: number) => Math.round(value * 10) / 10;

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface PlacedLabel {
  /** Where the text's anchor sits on its baseline. */
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  box: Rect;
}

/** Roughly how wide a label is: the app's font, semibold, averages a little under 0.62 em a character. */
export function labelWidth(text: string, fontSize: number): number {
  return text.length * fontSize * 0.62;
}

/** How far labels keep from the frame's edge, halo included. */
const LABEL_INSET = 6;

interface Placement {
  /** Which way the label sits from its marker: a unit-ish vector. */
  dx: number;
  dy: number;
  anchor: PlacedLabel["anchor"];
  /** Placements to the sides read best; a small nudge towards them. */
  bias: number;
}

const PLACEMENTS: readonly Placement[] = [
  { dx: 1, dy: 0, anchor: "start", bias: 0 },
  { dx: -1, dy: 0, anchor: "end", bias: 0 },
  { dx: 0, dy: -1, anchor: "middle", bias: 1 },
  { dx: 0, dy: 1, anchor: "middle", bias: 1 },
  { dx: 0.7, dy: -0.7, anchor: "start", bias: 2 },
  { dx: -0.7, dy: -0.7, anchor: "end", bias: 2 },
  { dx: 0.7, dy: 0.7, anchor: "start", bias: 2 },
  { dx: -0.7, dy: 0.7, anchor: "end", bias: 2 },
];

/**
 * Where a marker's label goes: inside the frame, clear of `avoid` (other labels and markers), off the
 * `line` where it can be, and preferably on the side facing away from `away` (the other end of the route).
 */
export function placeLabel(
  text: string,
  at: Point,
  frame: { width: number; height: number },
  options: { fontSize: number; gap: number; avoid?: readonly Rect[]; line?: readonly Point[]; away?: Point },
): PlacedLabel {
  const { fontSize, gap, avoid = [], line = [], away } = options;
  const width = labelWidth(text, fontSize);
  const awayX = away ? at[0] - away[0] : 0;
  const awayY = away ? at[1] - away[1] : 0;
  const awayLength = Math.hypot(awayX, awayY) || 1;
  const scored = PLACEMENTS.map((placement) => {
    const label = labelAt(at, placement, width, fontSize, gap);
    const { box } = label;
    const overflow =
      Math.max(0, LABEL_INSET - box.left) +
      Math.max(0, box.right - (frame.width - LABEL_INSET)) +
      Math.max(0, LABEL_INSET - box.top) +
      Math.max(0, box.bottom - (frame.height - LABEL_INSET));
    const collisions = avoid.filter((other) => overlaps(box, other)).length;
    const crossings = line.filter((p) => inside(p, box, 2)).length;
    const facing = (placement.dx * awayX + placement.dy * awayY) / awayLength;
    return { label, score: overflow * 100 + collisions * 1000 + crossings * 20 - facing * 15 + placement.bias };
  });
  return scored.reduce((best, next) => (next.score < best.score ? next : best)).label;
}

function labelAt(at: Point, placement: Placement, width: number, fontSize: number, gap: number): PlacedLabel {
  const x = at[0] + placement.dx * gap;
  const y = at[1] + placement.dy * gap;
  const left = placement.anchor === "start" ? x : placement.anchor === "end" ? x - width : x - width / 2;
  // Beside the marker the text is centred on it; above or below, it sits just past the gap.
  const top = placement.dy < 0 ? y - fontSize : placement.dy > 0 ? y : y - fontSize / 2;
  return {
    x,
    y: top + fontSize * 0.8,
    anchor: placement.anchor,
    box: { left, top, right: left + width, bottom: top + fontSize },
  };
}

function overlaps(a: Rect, b: Rect): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

function inside(point: Point, box: Rect, margin: number): boolean {
  return (
    point[0] >= box.left - margin &&
    point[0] <= box.right + margin &&
    point[1] >= box.top - margin &&
    point[1] <= box.bottom + margin
  );
}

/** Points along a line no further apart than `spacing`, for checking what a label would cover. */
export function samplePoints(points: readonly Point[], spacing: number): Point[] {
  const samples: Point[] = [];
  points.forEach((point, i) => {
    const next = points[i + 1];
    samples.push(point);
    if (!next) return;
    const steps = Math.floor(Math.hypot(next[0] - point[0], next[1] - point[1]) / spacing);
    for (let step = 1; step < steps; step++) {
      const t = step / steps;
      samples.push([point[0] + (next[0] - point[0]) * t, point[1] + (next[1] - point[1]) * t]);
    }
  });
  return samples;
}

/** The map's frame: 16:9 suits a phone-width sheet. */
export const FRAME: Frame = { width: 400, height: 225, padding: 28, minSpan: 1700 };
export const LABEL_SIZE = 12.5;
const MARKER_CLEARANCE = 8;
const MARKER_GAP = 9;

export interface Stop {
  code: string;
  place: string;
  at: Point;
}

export interface LabelledStop extends Stop {
  label: PlacedLabel;
}

/** Everything the map draws, in screen points. */
export interface Scene {
  description: string;
  view: View;
  from: LabelledStop;
  /** Absent when only the departure is chosen. */
  to?: LabelledStop;
  /** The route between them, if the map has one. */
  route?: string;
  /** With only the departure chosen: where you can go from it, and the way there. */
  others: (Stop & { route?: string })[];
}

/**
 * What to draw for a trip from `from` to `to` (or, with no `to`, from `from` to anywhere it goes), or
 * undefined if the map doesn't know a terminal (BC Ferries can add one before the map does).
 */
export function routeScene(data: RouteMapData, from: string, to: string, terminals: Terminal[]): Scene | undefined {
  const byCode = new Map(terminals.map((t) => [t.code, t]));
  const place = (code: string) => byCode.get(code)?.place ?? code;
  const fromAt = berth(data, from);
  if (!fromAt) return undefined;
  const limits = { left: 0, top: 0, right: data.size[0] ?? 0, bottom: data.size[1] ?? 0 };

  if (to) {
    const toAt = berth(data, to);
    if (!toAt) return undefined;
    const path = routePath(data, from, to);
    const view = fitView(boxAround(path ?? [fromAt, toAt]) ?? pointBox(fromAt), FRAME, limits);
    const line = path?.map((p) => toScreen(view, p));
    const a = toScreen(view, fromAt);
    const b = toScreen(view, toAt);
    const markers = [markerBox(a), markerBox(b)];
    const options = { fontSize: LABEL_SIZE, gap: MARKER_GAP, line: line ? samplePoints(line, 4) : [] };
    const fromLabel = placeLabel(place(from), a, FRAME, { ...options, avoid: markers, away: b });
    const toLabel = placeLabel(place(to), b, FRAME, { ...options, avoid: [...markers, fromLabel.box], away: a });
    return {
      description: `Map of the route from ${place(from)} to ${place(to)}`,
      view,
      from: { code: from, place: place(from), at: a, label: fromLabel },
      to: { code: to, place: place(to), at: b, label: toLabel },
      route: line && linePath(line),
      others: [],
    };
  }

  const reachable = destinations(terminals, from).flatMap((t) => {
    const at = berth(data, t.code);
    return at ? [{ code: t.code, place: t.place, at, path: routePath(data, from, t.code) }] : [];
  });
  const framed = [fromAt, ...reachable.flatMap((stop) => stop.path ?? [stop.at])];
  const view = fitView(boxAround(framed) ?? pointBox(fromAt), FRAME, limits);
  const others = reachable.map((stop) => ({
    code: stop.code,
    place: stop.place,
    at: toScreen(view, stop.at),
    route: stop.path && linePath(stop.path.map((p) => toScreen(view, p))),
  }));
  const a = toScreen(view, fromAt);
  const lines = reachable.flatMap((stop) =>
    stop.path
      ? samplePoints(
          stop.path.map((p) => toScreen(view, p)),
          4,
        )
      : [],
  );
  const label = placeLabel(place(from), a, FRAME, {
    fontSize: LABEL_SIZE,
    gap: MARKER_GAP,
    avoid: [markerBox(a), ...others.map((stop) => markerBox(stop.at))],
    line: lines,
    away: others.length > 0 ? centre(others.map((stop) => stop.at)) : undefined,
  });
  return {
    description: others.length > 0 ? `Map of the routes from ${place(from)}` : `Map of ${place(from)}`,
    view,
    from: { code: from, place: place(from), at: a, label },
    others,
  };
}

function pointBox(point: Point): Box {
  return { left: point[0], top: point[1], right: point[0], bottom: point[1] };
}

function markerBox(at: Point): Rect {
  return {
    left: at[0] - MARKER_CLEARANCE,
    top: at[1] - MARKER_CLEARANCE,
    right: at[0] + MARKER_CLEARANCE,
    bottom: at[1] + MARKER_CLEARANCE,
  };
}

function centre(points: readonly Point[]): Point {
  const sum = points.reduce<[number, number]>((total, p) => [total[0] + p[0], total[1] + p[1]], [0, 0]);
  return [sum[0] / points.length, sum[1] / points.length];
}
