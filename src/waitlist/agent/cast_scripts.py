"""The fixed scripts a stream session runs in the checkout's frames (DESIGN §6.4, PROTOCOL.md §5.3.2).

Every page access of the streamed viewer goes through these seven constant scripts, run in an isolated world of
each frame (`Page.createIsolatedWorld`), which the page's own script can't see. `call()` applies one to
arguments formatted from Python ints and finite floats only, so no string from the viewer (or the page) can ever
become part of an expression. They run with `Runtime.evaluate` and `timeout: 100`, except `STORE_OWNER`, which
is run once per frame with `Runtime.callFunctionOn` on the frame's owner element, resolved into its parent's
world, to keep it there.

The scripts only read the DOM (`STORE_OWNER` and `FIELDS` write only to the world's own global) and every one
bounds what it returns before it leaves the renderer (C39): a page can make its DOM as big as it likes, but no
more than the budget ever reaches the agent. FIELDS stops after 500 candidates or 30 ms, returns at most 100
fields and 48 KiB, cuts labels and `autocomplete` to 64 code points, enumerated attributes to 16 and values to
10, and lists at most 400 options a select.

Reference: the design's round-2 scripts, with the same names, bounds and return shapes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Final

MAX_SCAN: Final = 500  # candidate elements looked at per frame
MAX_FIELDS: Final = 100  # fields returned per frame
MAX_OPTIONS: Final = 400  # options listed per select
BUDGET: Final = 48 * 1024  # serialized characters per frame result
TIME_MS: Final = 30  # a script stops after this long (the evaluation's 100 ms timeout is the backstop)
MAX_OWNERS: Final = 64  # owner iframes kept in one world
MAX_POINT_NUMBERS: Final = 2048  # numbers OWNER_HIT takes in one call


def _fill(script: str) -> str:
    """A script with its bounds written in (as @NAME@, since % and {} are JavaScript's own)."""
    values = {
        "OPTIONS": MAX_OPTIONS,
        "SCAN": MAX_SCAN,
        "TIME": TIME_MS,
        "BUDGET": BUDGET,
        "FIELDS": MAX_FIELDS,
        "OWNERS": MAX_OWNERS,
        "NUMBERS": MAX_POINT_NUMBERS,
    }
    for name, value in values.items():
        script = script.replace(f"@{name}@", str(value))
    if "@" in script:
        raise ValueError("a script placeholder left unfilled")
    return script


# Shared by FIELDS and FOCUS: the clip (what of this frame can be on screen: the main frame's visual viewport, or
# this frame's own viewport), and one element described as a field. Not a script on its own.
_COMMON = r"""
  const SKIP = new Set(['hidden', 'checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'range',
    'color']);
  const top = window.top === window, vv = window.visualViewport;
  const W = innerWidth, H = innerHeight;
  const CX0 = top && vv ? vv.offsetLeft : 0, CY0 = top && vv ? vv.offsetTop : 0;
  const CX1 = top && vv ? CX0 + vv.width : W, CY1 = top && vv ? CY0 + vv.height : H;
  const cut = (v, n) => String(v == null ? '' : v).slice(0, n);
  const low = (v) => cut(v, 16).toLowerCase();
  const st = globalThis.__wl || (globalThis.__wl = {revs: new Map(), owners: new Map()});
  const kindOf = (el) => {
    const tag = el.tagName;
    if (tag === 'INPUT') return SKIP.has(low(el.type)) ? '' : 'i';
    if (tag === 'TEXTAREA') return 'a';
    if (tag === 'SELECT') return 's';
    return el.isContentEditable ? 'e' : '';
  };
  // A field's description: its box in this frame's client coordinates, and a 3x3 mask of the cells of its
  // visible part where a click would reach it and nothing else (in this frame). Null when it can't be seen.
  const describe = (el, tag) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right <= CX0 || r.bottom <= CY0 || r.left >= CX1 || r.top >= CY1) {
      return null;
    }
    const x0 = Math.max(r.left, CX0), y0 = Math.max(r.top, CY0);
    const x1 = Math.min(r.right, CX1), y1 = Math.min(r.bottom, CY1);
    let m = 0;
    for (let j = 0; j < 9; j++) {
      const px = x0 + (x1 - x0) * (2 * (j % 3) + 1) / 6, py = y0 + (y1 - y0) * (2 * ((j / 3) | 0) + 1) / 6;
      const hit = document.elementFromPoint(px, py);
      if (hit && (hit === el || el.contains(hit))) m |= 1 << j;
    }
    if (!m) return null;
    const max = (tag === 'i' || tag === 'a') && el.maxLength > 0 ? Math.min(el.maxLength, 524288) : null;
    const f = {tag, type: tag === 'i' ? low(el.type) : '', im: low(el.getAttribute('inputmode')),
      ac: cut(el.getAttribute('autocomplete'), 64).toLowerCase(), ekh: low(el.getAttribute('enterkeyhint')),
      cap: low(el.getAttribute('autocapitalize')), max, ro: !!(el.readOnly || el.disabled || el.matches(':disabled')),
      r: [r.left, r.top, r.width, r.height], m};
    if (tag === 's') {
      const n = el.options.length, opts = [];
      for (let k = 0; k < Math.min(n, @OPTIONS@); k++) {
        const o = el.options[k], g = o.parentElement;
        opts.push([cut(o.label, 64), !!(o.disabled || (g && g.tagName === 'OPTGROUP' && g.disabled))]);
      }
      f.n = Math.min(n, 1000000); f.opts = opts; f.sel = el.selectedIndex; f.ot = n > opts.length;
    } else if (f.type === 'date' || f.type === 'time') {
      f.v = cut(el.value, 10);
    }
    return f;
  };
  const clip = [CX0, CY0, CX1 - CX0, CY1 - CY0];
  const view = top && vv ? [vv.pageLeft, vv.pageTop, vv.offsetLeft, vv.offsetTop, vv.scale] : null;
"""

# The editable fields of this frame that a click can reach, for the field map. Keeps this evaluation's elements
# (with the select labels it reported) under `ev`, and those of `keep1` and `keep2` (the map revisions the agent
# still holds), for CLICK_POINTS and IS_FOCUSED. `own` says which of the owner iframes kept in this world
# intersect the clip: the child frames worth evaluating. `slow`: it stopped for time.
FIELDS: Final = _fill(
    r"""function (ev, keep1, keep2) {
  const T0 = performance.now();"""
    + _COMMON
    + r"""
  const out = [], kept = [];
  let size = 0, trunc = false, slow = false, scanned = 0;
  for (const el of document.querySelectorAll('input,textarea,select,[contenteditable]')) {
    if (++scanned > @SCAN@) { trunc = true; break; }
    if (performance.now() - T0 > @TIME@) { trunc = true; slow = true; break; }
    const tag = kindOf(el);
    if (!tag) continue;
    const f = describe(el, tag);
    if (!f) continue;
    f.i = kept.length;
    let add = JSON.stringify(f).length;
    if (size + add > @BUDGET@ && f.opts) {
      delete f.opts; f.ot = true; add = JSON.stringify(f).length;
    }
    if (size + add > @BUDGET@ || out.length >= @FIELDS@) { trunc = true; break; }
    size += add;
    out.push(f);
    kept.push([el, f.opts || null, f.n || 0]);
  }
  st.revs.set(ev, kept);
  for (const k of [...st.revs.keys()]) if (k !== ev && k !== keep1 && k !== keep2) st.revs.delete(k);
  const own = [];
  for (const [k, o] of st.owners) {
    if (own.length >= @OWNERS@) break;
    const b = o.isConnected ? o.getBoundingClientRect() : null;
    own.push([k, b && b.right > CX0 && b.bottom > CY0 && b.left < CX1 && b.top < CY1 ? 1 : 0]);
  }
  const d = document.documentElement;
  return {f: out, trunc, slow, clip, vv: view, cw: d ? d.scrollWidth : 0, ch: d ? d.scrollHeight : 0, own};
}"""
)

# Run with Runtime.callFunctionOn on an owner iframe resolved into its parent's world: keep it as owner `k`.
STORE_OWNER: Final = _fill(
    r"""function (k) {
  const st = globalThis.__wl || (globalThis.__wl = {revs: new Map(), owners: new Map()});
  for (const [j, o] of st.owners) if (!o.isConnected) st.owners.delete(j);
  if (st.owners.size >= @OWNERS@) return false;
  st.owners.set(k, this);
  return true;
}"""
)

# In a parent's world, about its owner iframe `k`: the iframe's content-box origin in the parent's client
# coordinates; for each child point (child client coordinates, flat [x, y, x, y, ...]) whether a click there
# would reach the iframe (and, in the main frame, lie in the visual viewport); whether the iframe has focus here;
# and whether its box intersects the clip. Null when the owner is gone.
OWNER_HIT: Final = _fill(
    r"""function (k, pts) {
  const st = globalThis.__wl, el = st && st.owners.get(k);
  if (!el || !el.isConnected) return null;
  const top = window.top === window, vv = window.visualViewport;
  const X0 = top && vv ? vv.offsetLeft : 0, Y0 = top && vv ? vv.offsetTop : 0;
  const X1 = top && vv ? X0 + vv.width : innerWidth, Y1 = top && vv ? Y0 + vv.height : innerHeight;
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  const ox = r.left + el.clientLeft + (parseFloat(s.paddingLeft) || 0);
  const oy = r.top + el.clientTop + (parseFloat(s.paddingTop) || 0);
  const h = [], n = Math.min(pts.length, @NUMBERS@), T0 = performance.now();
  let slow = false;
  for (let j = 0; j + 1 < n; j += 2) {
    if (slow || performance.now() - T0 > @TIME@) { slow = true; h.push(0); continue; }
    const x = ox + pts[j], y = oy + pts[j + 1];
    h.push(x >= X0 && y >= Y0 && x < X1 && y < Y1 && document.elementFromPoint(x, y) === el ? 1 : 0);
  }
  const vis = r.right > X0 && r.bottom > Y0 && r.left < X1 && r.top < Y1;
  return {o: [ox, oy], h, focused: document.activeElement === el, vis, slow};
}"""
)

# Where to click element `i` of evaluation `ev` so that the element itself gets the click (mode 0: the centre,
# then 9 sample points; mode 1: the first segment of a date or time field only), after checking it's still the
# element the map described: connected, with the same options. Also its selected index or value now.
CLICK_POINTS: Final = r"""function (ev, i, mode) {
  const st = globalThis.__wl, kept = st && st.revs.get(ev);
  if (!kept || i < 0 || i >= kept.length) return {why: 'gone'};
  const [el, labels, n] = kept[i];
  if (!el.isConnected) return {why: 'gone'};
  if (labels) {
    if (el.options.length !== n) return {why: 'changed'};
    for (let k = 0; k < labels.length; k++) {
      const o = el.options[k], g = o.parentElement;
      const disabled = !!(o.disabled || (g && g.tagName === 'OPTGROUP' && g.disabled));
      if (String(o.label).slice(0, 64) !== labels[k][0] || disabled !== labels[k][1]) return {why: 'changed'};
    }
  }
  const r = el.getBoundingClientRect(), W = innerWidth, H = innerHeight;
  const at = mode === 1 ? [[Math.min(12, r.width / 4) / r.width, 0.5]]
    : [[0.5, 0.5], [1 / 6, 1 / 6], [1 / 2, 1 / 6], [5 / 6, 1 / 6], [1 / 6, 1 / 2], [5 / 6, 1 / 2], [1 / 6, 5 / 6],
       [1 / 2, 5 / 6], [5 / 6, 5 / 6]];
  const pts = [];
  for (const [a, b] of at) {
    const x = r.left + r.width * a, y = r.top + r.height * b;
    if (x < 0 || y < 0 || x >= W || y >= H) continue;
    if (document.elementFromPoint(x, y) === el) pts.push(x, y);
  }
  return {pts, sel: labels ? el.selectedIndex : -1, v: labels ? '' : String(el.value).slice(0, 10)};
}"""

# Whether element `i` of evaluation `ev` is its document's focused element (before any key is sent to it).
IS_FOCUSED: Final = r"""function (ev, i) {
  const st = globalThis.__wl, kept = st && st.revs.get(ev);
  return !!(kept && kept[i] && document.activeElement === kept[i][0]);
}"""

# This frame's focused element: as a field (with the clip, and in the main frame its visual viewport), `{frame: k}`
# when it's an owner iframe kept here as `k` (-1 for one that isn't), or null when nothing editable has focus.
FOCUS: Final = _fill(
    r"""function () {"""
    + _COMMON
    + r"""
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
    for (const [k, o] of st.owners) if (o === el) return {frame: k, vv: view};
    return {frame: -1, vv: view};
  }
  const tag = kindOf(el);
  if (!tag) return null;
  const f = describe(el, tag);
  if (f && f.opts && JSON.stringify(f).length > @BUDGET@) { delete f.opts; f.ot = true; }
  return {f, clip, vv: view};
}"""
)

# The main frame's window and visual viewport, and the browser's locale (for `date`).
VIEWPORT: Final = r"""function () {
  const vv = window.visualViewport;
  return [innerWidth, innerHeight, devicePixelRatio, vv.pageLeft, vv.pageTop, vv.offsetLeft, vv.offsetTop,
          vv.scale, String(navigator.language).slice(0, 16)];
}"""

# The scripts run with Runtime.evaluate, by name (STORE_OWNER is the one run with callFunctionOn).
SCRIPTS: Final = {
    "FIELDS": FIELDS,
    "OWNER_HIT": OWNER_HIT,
    "CLICK_POINTS": CLICK_POINTS,
    "IS_FOCUSED": IS_FOCUSED,
    "FOCUS": FOCUS,
    "VIEWPORT": VIEWPORT,
}

Argument = int | float | Sequence[int | float]
_ARGUMENTS = re.compile(r"[-+0-9.e,\[\]]*")
_SAFE_INTEGER = 2**53


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"a script argument must be a number, not {type(value).__name__}")
    if isinstance(value, int):
        if abs(value) >= _SAFE_INTEGER:
            raise ValueError("an integer script argument out of range")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("a script argument must be finite")
    return repr(value)


def _argument(value: object) -> str:
    if isinstance(value, list | tuple):
        if len(value) > MAX_POINT_NUMBERS:
            raise ValueError(f"more than {MAX_POINT_NUMBERS} numbers in one script argument")
        return "[" + ",".join(_number(v) for v in value) + "]"
    return _number(value)


def call(script: str, *args: Argument) -> str:
    """The expression that applies one of the fixed scripts to these arguments: ints, finite floats, or lists of
    them, and nothing else (a str, even a digit, is refused)."""
    if script not in SCRIPTS.values():
        raise ValueError("not one of the fixed scripts")
    return f"({script})({','.join(_argument(a) for a in args)})"


def script_of(expression: str) -> str | None:
    """The name of the fixed script an expression applies, if it is exactly such a call (made by `call()`):
    the allowlist's check on every `Runtime.evaluate` (C34)."""
    for name, script in SCRIPTS.items():
        head = f"({script})("
        if expression.startswith(head) and expression.endswith(")"):
            arguments = expression[len(head) : -1]
            return name if _ARGUMENTS.fullmatch(arguments) else None
    return None
