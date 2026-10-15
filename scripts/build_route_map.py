"""Build the trip sheet's route map from Natural Earth's coastline (docs/multi-tenant/design-reference.md §10).

    uv run --with shapely==2.2.0 --with scikit-image==0.26.0 --with pillow==12.3.0 \
        python scripts/build_route_map.py <natural earth dir>

<natural earth dir> holds ne_10m_land.geojson and ne_10m_minor_islands.geojson, downloaded from SOURCES (public
domain). Their sha256 are checked, so a changed download fails the build instead of quietly changing the map.
Writes web/src/lib/route-map-data.json, which the app loads only when it shows a map. Everything in it is in
whole Web Mercator pixels at zoom 13 (about 12 m here), counted from the data's top-left corner:

- ``land``: the coast inside the service area, as rings: a first point, then the steps to each next point. The
  app draws each ring smoothed (a quadratic B-spline through its edges' midpoints), and every check below runs
  against that smoothed shape, not the raw one.
- ``terminals``: each terminal's berth, from TERMINALS. A berth more than 1.5 km from the coast fails the build:
  either its coordinates or the coastline is wrong (OFF_THE_COAST_M lists the few known gaps in the coastline).
  Natural Earth leaves out most small bays and harbours, so a berth can fall inside the land as drawn: it's then
  drawn just off the nearest shore, so its marker sits on the coast and its routes don't start overland.
- ``routes``: a path through the water for every route BC Ferries books, keyed "A-B" (codes sorted) and running
  from A to B, stored like a ring. Each is the cheapest way across a water grid where keeping clear of the
  shore costs less, smoothed so it reads as a ferry's course, then checked to stay off the land except near
  its two ends.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from PIL import Image, ImageDraw
from scipy import ndimage, sparse
from scipy.sparse.linalg import spsolve
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, shape
from shapely.geometry.polygon import orient
from skimage.graph import MCP_Geometric

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web/src/lib/route-map-data.json"

_NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
SOURCES = {
    "ne_10m_land.geojson": (
        f"{_NE}/ne_10m_land.geojson",
        "1ac90796408bc6ad6911d69448485d3c4dbf2190370080368a09976e1c9f7416",
    ),
    "ne_10m_minor_islands.geojson": (
        f"{_NE}/ne_10m_minor_islands.geojson",
        "8c933ca7a4760256bdc46408355706e39764b0fa01c160c28888b90b4faec29f",
    ),
}

ZOOM = 13
WORLD = 256 * 2**ZOOM  # the world's width in pixels at ZOOM
# West, south, east, north: BC Ferries' service area with room around it. Inland it reaches far enough east
# that the 16:9 frame around the tallest routes stays inside the data; that costs nothing, as there's no coast.
BOUNDS = (-134.0, 48.0, -118.0, 55.0)
EARTH_CIRCUMFERENCE_M = 40_075_016.686

# Each terminal's berth (lat, lon) and the terminals BC Ferries books to from it (its booking form on 2026-10-11,
# tests/fixtures/flow/route.html). The coordinates are the berths' own, not moved onto the coastline: Natural
# Earth draws it with points about 1.5 km apart, so a berth can sit some way inside or off it (one inside it is
# drawn off the nearest shore: place_berths).
TERMINALS: dict[str, tuple[float, float, str]] = {
    "TSA": (49.0066, -123.1306, "DUK NAN PLH POB PSB PST PVB SWB"),  # Vancouver (Tsawwassen)
    "HSB": (49.3742, -123.2734, "BOW LNG NAN"),  # Vancouver (Horseshoe Bay)
    "BOW": (49.3790, -123.3322, "HSB"),  # Bowen Island (Snug Cove)
    "SWB": (48.6888, -123.4108, "FUL PLH POB PSB PST PVB TSA"),  # Victoria (Swartz Bay)
    "NAH": (49.1656, -123.9338, "GAB"),  # Nanaimo (Nanaimo Harbour)
    "NAN": (49.1934, -123.9551, "HSB"),  # Nanaimo (Departure Bay)
    "DUK": (49.1628, -123.8911, "TSA"),  # Nanaimo (Duke Point)
    "CMX": (49.7406, -124.9226, "PWR"),  # Comox (Little River)
    "PPH": (50.7243, -127.4612, "BEC KLE PBB POF PPR SHW"),  # Port Hardy (Bear Cove)
    "BTW": (48.5743, -123.4648, "MIL"),  # Brentwood Bay
    "BKY": (49.5269, -124.8513, "DNM"),  # Buckley Bay
    "CAM": (50.0272, -125.2435, "QDR"),  # Campbell River
    "CHM": (48.9249, -123.7151, "PEN THT"),  # Chemainus
    "CFT": (48.8657, -123.6372, "VES"),  # Crofton
    "MIL": (48.6512, -123.5524, "BTW"),  # Mill Bay
    "MCN": (50.5918, -127.0846, "ALR SOI"),  # Port McNeill
    "LNG": (49.4346, -123.4714, "HSB"),  # Sunshine Coast (Langdale)
    "PWR": (49.8353, -124.5273, "CMX TEX"),  # Powell River (Westview)
    "SLT": (49.7821, -124.1745, "ERL"),  # Powell River (Saltery Bay)
    "ERL": (49.7531, -124.0084, "SLT"),  # Sunshine Coast (Earls Cove)
    "TEX": (49.7951, -124.6202, "PWR"),  # Texada Island (Blubber Bay)
    "POB": (48.7981, -123.3163, "PLH PSB PST PVB SWB TSA"),  # Pender Island (Otter Bay)
    "PSB": (48.8768, -123.3148, "PLH POB PST PVB SWB TSA"),  # Galiano Island (Sturdies Bay)
    "PVB": (48.8446, -123.3224, "PLH POB PSB PST SWB TSA"),  # Mayne Island (Village Bay)
    "PST": (48.7992, -123.2027, "PLH POB PSB PVB SWB TSA"),  # Saturna Island (Lyall Harbour)
    "GAB": (49.1784, -123.8597, "NAH"),  # Gabriola Island (Descanso Bay)
    "PEN": (48.9705, -123.6585, "CHM THT"),  # Penelakut Island (Telegraph Harbour)
    "PLH": (48.8508, -123.4494, "POB PSB PST PVB SWB TSA"),  # Salt Spring Island (Long Harbour)
    "FUL": (48.7689, -123.4506, "SWB"),  # Salt Spring Island (Fulford Harbour)
    "VES": (48.8809, -123.5718, "CFT"),  # Salt Spring Island (Vesuvius Bay)
    "THT": (48.9790, -123.6815, "CHM PEN"),  # Thetis Island (Preedy Harbour)
    "ALR": (50.5866, -126.9300, "MCN SOI"),  # Cormorant Island (Alert Bay)
    "COR": (50.1082, -125.0522, "HRB"),  # Cortes Island (Whaletown)
    "DNM": (49.5322, -124.8208, "BKY"),  # Denman Island West
    "DNE": (49.5040, -124.7360, "HRN"),  # Denman Island East (Gravelly Bay)
    "HRN": (49.5133, -124.6985, "DNE"),  # Hornby Island (Shingle Spit)
    "SOI": (50.6290, -127.0195, "ALR MCN"),  # Malcolm Island (Sointula)
    "HRB": (50.1027, -125.2110, "COR"),  # Quadra Island (Heriot Bay)
    "QDR": (50.0446, -125.2187, "CAM"),  # Quadra Island (Quathiaski Cove)
    "BEC": (52.3795, -126.7690, "PBB POF PPH SHW"),  # Bella Coola
    "PBB": (52.1375, -128.1430, "BEC KLE POF PPH PPR SHW"),  # Bella Bella (McLoughlin Bay)
    "POF": (52.3528, -127.6930, "BEC PBB PPH SHW"),  # Ocean Falls
    "SHW": (52.1476, -128.0887, "BEC PBB POF PPH"),  # Shearwater
    "PPR": (54.2942, -130.3375, "KLE PBB PPH PSK"),  # Prince Rupert
    "KLE": (52.5930, -128.5215, "PBB PPH PPR"),  # Klemtu
    "PSK": (53.2443, -132.0106, "ALF PPR"),  # Graham Island (Skidegate)
    "ALF": (53.2100, -131.9958, "PSK"),  # Moresby Island (Alliford Bay)
}

MAX_BERTH_TO_COAST_M = 1500
# Berths further from Natural Earth's coast than that, and why.
OFF_THE_COAST_M = {
    "TSA": 3000,  # at the end of a 3 km causeway, which Natural Earth leaves out
    "DUK": 2500,  # Natural Earth leaves out the Duke Point peninsula
    "ALR": 3000,  # Natural Earth leaves out Cormorant Island
    "POF": 2500,  # Natural Earth's Cousins Inlet stops short of Ocean Falls
}
# How far into the water a berth inside the drawn land is drawn, past the nearest shore.
SHORE_OFFSET_M = 60.0
# Rings smaller than this (about 150 m across) are dropped: they're specks at any scale the app shows.
MIN_RING_AREA = 150.0
SIMPLIFY = 1.5  # pixels: drops points that sit on a straight line once rounded
CURVE_SAMPLES = 6  # points per smoothed edge when checking against the smoothed coast


class BuildError(Exception):
    pass


# --- Projection --------------------------------------------------------------------------------------------------


def project(lon: float, lat: float) -> tuple[float, float]:
    """Web Mercator pixels at ZOOM, from the world's top-left corner."""
    s = math.sin(math.radians(lat))
    return (lon + 180) / 360 * WORLD, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD


ORIGIN = tuple(math.floor(v) for v in project(BOUNDS[0], BOUNDS[3]))
SIZE = tuple(math.ceil(v) - o for v, o in zip(project(BOUNDS[2], BOUNDS[1]), ORIGIN, strict=True))


def to_map(lon: float, lat: float) -> tuple[float, float]:
    x, y = project(lon, lat)
    return x - ORIGIN[0], y - ORIGIN[1]


def latitude(y: float) -> float:
    """The latitude of a map y."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + ORIGIN[1]) / WORLD))))


def metres_per_pixel(y: float) -> float:
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(latitude(y))) / WORLD


# --- Land --------------------------------------------------------------------------------------------------------


def check_sources(directory: Path) -> list[Path]:
    paths = []
    for name, (url, sha256) in SOURCES.items():
        path = directory / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as e:
            raise BuildError(f"{path}: {e.strerror} (download it from {url})") from e
        if digest != sha256:
            raise BuildError(f"{path}: sha256 {digest}, expected {sha256} (from {url})")
        paths.append(path)
    return paths


def load_land(paths: list[Path]) -> list[Polygon]:
    """Natural Earth's land and minor islands in the service area, in map pixels, rounded and simplified."""
    area = box(*BOUNDS)
    parts = []
    for path in paths:
        for feature in json.loads(path.read_text())["features"]:
            geometry = shape(feature["geometry"])
            if geometry.intersects(area):
                parts.append(geometry.intersection(area))
    land = shapely.transform(shapely.unary_union(parts), _to_map_array)
    land = shapely.set_precision(land, 1.0).simplify(SIMPLIFY)
    polygons = [
        orient(Polygon(p.exterior, [h for h in p.interiors if Polygon(h).area >= MIN_RING_AREA]))
        for p in _polygons(land)
        if p.area >= MIN_RING_AREA
    ]
    # Biggest first, so drawing them in order never paints a lake over an island in it.
    return sorted(polygons, key=lambda p: -p.area)


def _to_map_array(lon_lat: np.ndarray) -> np.ndarray:
    s = np.sin(np.radians(lon_lat[:, 1]))
    x = (lon_lat[:, 0] + 180) / 360 * WORLD - ORIGIN[0]
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD - ORIGIN[1]
    return np.column_stack([x, y])


def _polygons(geometry: Any) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return [g for part in getattr(geometry, "geoms", []) for g in _polygons(part)]


def ring_points(ring: Any) -> np.ndarray:
    """A ring's points as integers, without the closing repeat of the first. Points on the map's edge, where
    the land was cut off, come twice: the smoothed ring then runs straight along the edge and turns sharply
    at its corners instead of rounding them off."""
    points = np.asarray(ring.coords, dtype=float)[:-1].round().astype(np.int64)
    x, y = points[:, 0], points[:, 1]
    on_edge = (x <= 1) | (y <= 1) | (x >= SIZE[0] - 1) | (y >= SIZE[1] - 1)
    return np.repeat(points, np.where(on_edge, 2, 1), axis=0)


def smooth_ring(points: np.ndarray, samples: int = CURVE_SAMPLES) -> np.ndarray:
    """The ring as the app draws it: a quadratic B-spline (each point a control point between its edges'
    midpoints), sampled `samples` times per point."""
    p = points.astype(float)
    before = (np.roll(p, 1, axis=0) + p) / 2
    after = (p + np.roll(p, -1, axis=0)) / 2
    t = np.linspace(0, 1, samples, endpoint=False)[:, None, None]
    curve = (1 - t) ** 2 * before + 2 * (1 - t) * t * p + t**2 * after
    return curve.transpose(1, 0, 2).reshape(-1, 2)


def smoothed(polygons: list[Polygon]) -> list[Polygon]:
    """The land as the app draws it."""
    shapes = []
    for polygon in polygons:
        holes = [smooth_ring(ring_points(h)) for h in polygon.interiors]
        shapes.append(shapely.make_valid(Polygon(smooth_ring(ring_points(polygon.exterior)), holes)))
    return [p for s in shapes for p in _polygons(s)]


# --- Terminals ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Berth:
    code: str
    x: float
    y: float
    coast_m: float  # how far the berth is from the drawn coast
    inland: bool  # whether it's inside the drawn land (it's then drawn off the nearest shore: x and y)


def place_berths(land: shapely.Geometry) -> dict[str, Berth]:
    coast = land.boundary
    berths = {}
    problems = []
    for code, (lat, lon, _) in TERMINALS.items():
        x, y = (round(v) for v in to_map(lon, lat))
        point = Point(x, y)
        coast_m = coast.distance(point) * metres_per_pixel(y)
        inland = land.contains(point)
        if inland:
            x, y = _off_the_shore(coast, point)
            if land.contains(Point(x, y)):
                problems.append(f"{code} can't be drawn off the shore")
        berths[code] = Berth(code, x, y, coast_m, inland)
        if coast_m > OFF_THE_COAST_M.get(code, MAX_BERTH_TO_COAST_M):
            problems.append(f"{code} is {coast_m:.0f} m from the coast")
    if problems:
        raise BuildError("; ".join(problems))
    return berths


def _off_the_shore(coast: shapely.Geometry, point: Point) -> tuple[int, int]:
    """Where a berth inside the land is drawn: SHORE_OFFSET_M into the water past the nearest shore."""
    (x0, y0), (x1, y1) = shapely.shortest_line(point, coast).coords
    offset = SHORE_OFFSET_M / metres_per_pixel(y1) / math.hypot(x1 - x0, y1 - y0)
    return round(x1 + (x1 - x0) * offset), round(y1 + (y1 - y0) * offset)


def route_pairs() -> list[tuple[str, str]]:
    pairs = set()
    for code, (_, _, dests) in TERMINALS.items():
        for dest in dests.split():
            if dest not in TERMINALS:
                raise BuildError(f"{code} books to {dest}, which has no berth")
            pairs.add((min(code, dest), max(code, dest)))
    return sorted(pairs)


# --- Routes ------------------------------------------------------------------------------------------------------

# Water near the shore costs more to cross, up to 1 + SHORE_COST, falling off with the distance from it over a
# sixtieth of the route's extent (within these limits): routes keep to the middle of channels and still cross
# open water straight.
SHORE_COST = 2.0
SHORE_FALLOFF_M = (300.0, 1500.0)
# Land near a route's own ends can be crossed (at this cost) to reach the water, out to the nearest water plus
# END_MARGIN_M: a berth can sit inside the drawn coast.
END_LAND_COST = 8.0
END_MARGIN_M = 400.0
MAX_TO_WATER_M = 3000.0  # how far a berth or via point can be from the grid's water
MIN_SMOOTHING = 3.0  # cells: enough to hide the grid's steps
GRID_CELLS = 1800  # cells across a route's grid, within the limits below
CELL_M = (50.0, 150.0)
# Some routes keep to sheltered channels that a shortest path would skip; these points steer them through.
_FITZ_HUGH_SOUND = (51.80, -127.90)
_LAMA_PASSAGE = (52.1380, -128.1365)  # mid-channel, off Bella Bella
_MILBANKE_SOUND = (52.33, -128.55)
_FINLAYSON_CHANNEL = (52.50, -128.47)
# Graham Reach, Fraser Reach, Wright Sound, Grenville Channel and Chatham Sound.
_NORTH_OF_KLEMTU = [(53.00, -128.52), (53.20, -128.80), (53.33, -129.15), (53.70, -129.80)]
_TO_PRINCE_RUPERT = [*_NORTH_OF_KLEMTU, (54.10, -130.30)]
VIA: dict[tuple[str, str], list[tuple[float, float]]] = {
    # Out past Entrance Island, north of Gabriola: Gabriola Passage, south of it, is too narrow for a ferry.
    ("DUK", "TSA"): [(49.215, -123.805)],
    # The Inside Passage, not the open water outside it.
    ("PPH", "PPR"): [_FITZ_HUGH_SOUND, _LAMA_PASSAGE, _MILBANKE_SOUND, _FINLAYSON_CHANNEL, *_TO_PRINCE_RUPERT],
    ("KLE", "PPH"): [_FINLAYSON_CHANNEL, _MILBANKE_SOUND, _LAMA_PASSAGE, _FITZ_HUGH_SOUND],
    ("KLE", "PPR"): _TO_PRINCE_RUPERT,
    ("PBB", "PPR"): [_MILBANKE_SOUND, _FINLAYSON_CHANNEL, *_TO_PRINCE_RUPERT],
}


@dataclass(frozen=True, slots=True)
class Route:
    points: np.ndarray
    smoothing: float
    clearance_m: float


@dataclass(frozen=True, slots=True)
class Grid:
    x0: float
    y0: float
    cell: float  # pixels
    land: np.ndarray  # bool, [row, column]
    shore_m: np.ndarray  # metres from each cell to the nearest land cell

    def index(self, x: float, y: float) -> tuple[int, int]:
        return int((y - self.y0) // self.cell), int((x - self.x0) // self.cell)

    def centre(self, row: int, column: int) -> tuple[float, float]:
        return self.x0 + (column + 0.5) * self.cell, self.y0 + (row + 0.5) * self.cell


def make_grid(land: list[Polygon], window: tuple[float, float, float, float], cell: float) -> Grid:
    x0, y0, x1, y1 = window
    width, height = math.ceil((x1 - x0) / cell), math.ceil((y1 - y0) / cell)
    image = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(image)
    area = box(*window)

    def pixels(ring: Any) -> list[tuple[float, float]]:
        return [((x - x0) / cell - 0.5, (y - y0) / cell - 0.5) for x, y in ring.coords]

    for polygon in land:  # biggest first, so a lake never covers an island in it
        if polygon.intersects(area):
            draw.polygon(pixels(polygon.exterior), fill=1)
            for hole in polygon.interiors:
                draw.polygon(pixels(hole), fill=0)
    # A cell touching the land counts as land, so a path between cell centres keeps clear of the exact coast.
    grid_land = ndimage.binary_dilation(np.array(image, dtype=bool), structure=np.ones((3, 3), dtype=bool))
    m = metres_per_pixel((y0 + y1) / 2) * cell
    return Grid(x0, y0, cell, grid_land, ndimage.distance_transform_edt(~grid_land) * m)


@dataclass(frozen=True, slots=True)
class GridPath:
    """The cheapest way across a route's grid, before smoothing."""

    points: np.ndarray  # evenly spaced, from berth to berth
    grid: Grid
    ends: shapely.Geometry  # the circles around the berths where the path may cross land
    falloff_m: float
    extent: float  # pixels: the larger side of the berths' (and via points') bounding box
    mpp: float  # metres per pixel


def grid_path(a: Berth, b: Berth, land: list[Polygon]) -> GridPath:
    via = [to_map(lon, lat) for lat, lon in VIA.get((a.code, b.code), [])]
    xs, ys = [a.x, b.x, *(p[0] for p in via)], [a.y, b.y, *(p[1] for p in via)]
    mpp = metres_per_pixel((a.y + b.y) / 2)
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    margin = max(0.3 * extent, 20_000 / mpp)
    window = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    cell = min(max((extent + 2 * margin) / GRID_CELLS, CELL_M[0] / mpp), CELL_M[1] / mpp)
    grid = make_grid(land, window, cell)

    falloff = min(max(extent * mpp / 60, SHORE_FALLOFF_M[0]), SHORE_FALLOFF_M[1])
    cost = 1 + SHORE_COST * np.exp(-grid.shore_m / falloff)
    cost[grid.land] = np.inf
    rows, columns = np.indices(grid.land.shape)
    stops, zones = [], []
    for berth in (a, b):
        row, column = grid.index(berth.x, berth.y)
        water = _nearest_water(grid, (row, column), MAX_TO_WATER_M / mpp)
        if water is None:
            raise BuildError(f"{berth.code}: no water within {MAX_TO_WATER_M:.0f} m of the berth")
        radius = math.dist(water, (row, column)) * cell * mpp + END_MARGIN_M
        near = np.hypot(rows - row, columns - column) * cell * mpp <= radius
        cost[near & grid.land] = END_LAND_COST
        stops.append((row, column))
        zones.append(Point(berth.x, berth.y).buffer(radius / mpp))
    for (lat, lon), point in zip(VIA.get((a.code, b.code), []), via, strict=True):
        water = _nearest_water(grid, grid.index(*point), MAX_TO_WATER_M / mpp)
        if water is None:
            raise BuildError(f"{a.code}-{b.code}: the via point {lat}, {lon} is too far from the water")
        stops.insert(-1, water)

    raw: list[tuple[float, float]] = [(a.x, a.y)]
    for start, end in itertools.pairwise(stops):
        mcp = MCP_Geometric(cost, fully_connected=True)
        costs, _ = mcp.find_costs([start], [end])
        if not np.isfinite(costs[end]):
            raise BuildError(f"{a.code}-{b.code}: no way through the water")
        raw.extend(grid.centre(*cell_index) for cell_index in mcp.traceback(end)[1:-1])
    raw.append((b.x, b.y))
    return GridPath(_resample(np.array(raw), cell), grid, shapely.union_all(zones), falloff, extent, mpp)


def find_route(a: Berth, b: Berth, land: list[Polygon], land_union: shapely.Geometry) -> Route:
    """The grid path, smoothed: freely in open water, less where the smoothed line would come too close to the
    shore, and less everywhere if that isn't enough."""
    found = grid_path(a, b, land)
    path, grid = found.points, found.grid
    grid_clearance = _clearance_m(path, found.ends, land_union, found.mpp)
    needed = min(max(found.extent * found.mpp / 100, 100.0), 0.7 * grid_clearance)
    tolerance = max(found.extent / 2500, 1.0)
    free = shapely.contains_xy(found.ends, path[:, 0], path[:, 1])  # near the berths, nothing holds it back
    shore = np.where(free, np.inf, _shore_m(grid, path))
    smoothing = max(0.06 * float(np.sum(np.hypot(*np.diff(path, axis=0).T))) / grid.cell, MIN_SMOOTHING)  # points
    while True:
        # Stiffer where the shore is near, and stiffer again around any point that comes too close.
        # Never so stiff that the grid's steps show: every bend spreads over a few cells at least.
        stiffest = max(smoothing / MIN_SMOOTHING, 1.0) ** 4
        weight = np.minimum(1 + (found.falloff_m / np.maximum(shore, 1.0)) ** 4, stiffest)
        reach = int(smoothing)
        for _ in range(12):
            candidate = _smooth(path, weight, smoothing)
            free_now = shapely.contains_xy(found.ends, candidate[:, 0], candidate[:, 1])
            close = np.flatnonzero(~free_now & (_shore_m(grid, candidate) < needed))
            if close.size == 0:
                break
            for i in close:
                around = slice(max(i - reach, 0), i + reach + 1)
                weight[around] = np.minimum(weight[around] * 4, stiffest)
        points = np.asarray(LineString(candidate).simplify(tolerance).coords).round()
        clearance = _clearance_m(points, found.ends, land_union, found.mpp)
        if clearance >= needed or smoothing <= MIN_SMOOTHING:
            break
        smoothing = max(smoothing * 0.6, MIN_SMOOTHING)
    if clearance < min(needed, 0.5 * grid_clearance) or clearance <= 0:
        raise BuildError(f"{a.code}-{b.code}: the path runs onto the land ({clearance:.0f} m clear)")
    return Route(points.astype(np.int64), smoothing * grid.cell, clearance)


def _shore_m(grid: Grid, points: np.ndarray) -> np.ndarray:
    """How far each point is from the land, by the grid."""
    rows = np.clip(((points[:, 1] - grid.y0) // grid.cell).astype(int), 0, grid.land.shape[0] - 1)
    columns = np.clip(((points[:, 0] - grid.x0) // grid.cell).astype(int), 0, grid.land.shape[1] - 1)
    return grid.shore_m[rows, columns]


def _nearest_water(grid: Grid, at: tuple[int, int], within: float) -> tuple[int, int] | None:
    """The water cell nearest to a cell, if there's one within `within` pixels."""
    reach = math.ceil(within / grid.cell)
    top, left = max(at[0] - reach, 0), max(at[1] - reach, 0)
    rows, columns = np.nonzero(~grid.land[top : at[0] + reach + 1, left : at[1] + reach + 1])
    if rows.size == 0:
        return None
    distances = np.hypot(rows + top - at[0], columns + left - at[1])
    nearest = int(np.argmin(distances))
    if distances[nearest] * grid.cell > within:
        return None
    return int(rows[nearest] + top), int(columns[nearest] + left)


def _resample(path: np.ndarray, spacing: float) -> np.ndarray:
    """Points evenly spaced along the path, keeping its ends."""
    steps = np.hypot(*np.diff(path, axis=0).T)
    along = np.concatenate([[0], np.cumsum(steps)])
    count = max(int(along[-1] / spacing), 2)
    at = np.linspace(0, along[-1], count + 1)
    return np.column_stack([np.interp(at, along, path[:, 0]), np.interp(at, along, path[:, 1])])


def _smooth(path: np.ndarray, weight: np.ndarray, length: float) -> np.ndarray:
    """A smoothing spline through the path with its ends pinned. `length` (in points) sets how far a bend
    spreads; a point's weight, how closely the spline keeps to it."""
    n = len(path)
    second = sparse.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(n - 2, n))
    system = (sparse.diags(weight) + length**4 * (second.T @ second)).tolil()
    rhs = weight[:, None] * path
    for end in (0, n - 1):  # the ends stay where they are
        system.rows[end], system.data[end] = [end], [1.0]
        rhs[end] = path[end]
    solve = system.tocsc()
    return np.column_stack([spsolve(solve, rhs[:, i]) for i in (0, 1)])


def _clearance_m(points: np.ndarray, zones: shapely.Geometry, land: shapely.Geometry, mpp: float) -> float:
    """How close the path comes to the land, away from its ends."""
    outside = LineString(points).difference(zones)
    return math.inf if outside.is_empty else float(outside.distance(land)) * mpp


# --- Output ------------------------------------------------------------------------------------------------------


def steps(points: np.ndarray) -> list[int]:
    """A first point, then the steps to each next one, flattened."""
    points = np.asarray(points, dtype=np.int64)
    return [int(v) for v in np.concatenate([points[:1], np.diff(points, axis=0)]).ravel()]


def build(ne_dir: Path, out: Path) -> str:
    land = load_land(check_sources(ne_dir))
    drawn = smoothed(land)
    drawn_union = shapely.union_all(drawn)
    berths = place_berths(drawn_union)
    rings = [ring_points(r) for p in land for r in (p.exterior, *p.interiors)]
    routes = {}
    report = []
    for a, b in route_pairs():
        route = find_route(berths[a], berths[b], drawn, drawn_union)
        routes[f"{a}-{b}"] = steps(route.points)
        report.append(
            f"{a}-{b}: {len(route.points)} points, smoothing {route.smoothing:.0f} px, "
            f"{route.clearance_m:.0f} m clear of the shore"
        )
    data = {
        "source": "Natural Earth 10m land and minor islands (public domain)",
        "zoom": ZOOM,
        "origin": list(ORIGIN),
        "size": list(SIZE),
        "land": [steps(r) for r in rings],
        "terminals": {code: [round(berths[code].x), round(berths[code].y)] for code in sorted(berths)},
        "routes": routes,
    }
    text = json.dumps(data, separators=(",", ":")) + "\n"
    out.write_text(text)
    far = sorted((b for b in berths.values() if b.coast_m > 500), key=lambda b: -b.coast_m)
    report += [
        f"{b.code}: berth {b.coast_m:.0f} m {'inside the coast (drawn off the shore)' if b.inland else 'off the coast'}"
        for b in far
    ]
    report.append(
        f"{out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}: {sum(len(r) for r in rings)} coast points "
        f"in {len(rings)} rings, {len(routes)} routes, {len(text) / 1024:.0f} KB "
        f"({len(gzip.compress(text.encode(), 9)) / 1024:.0f} KB gzipped)"
    )
    return "\n".join(report)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the trip sheet's route map data.")
    parser.add_argument("ne_dir", type=Path, help="holds Natural Earth's ne_10m_land and ne_10m_minor_islands")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    try:
        report = build(args.ne_dir, args.out)
    except (BuildError, OSError) as e:
        sys.exit(f"build_route_map.py: {e}")
    sys.stdout.write(report + "\n")


if __name__ == "__main__":
    main()
