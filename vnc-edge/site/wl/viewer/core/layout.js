// Where the image goes on the viewer's screen, and how a point there maps to the pod's page (PROTOCOL.md §1,
// §8.1). DOM-free.
//
// The viewer asks for its own layout viewport (clamped), and the agent sends images of the page's viewport,
// `w`×`h` frame px. Each image is drawn into a box of `w·k`×`h·k` CSS px centred on the viewer's screen, with
// `k = min(1, innerWidth / w, innerHeight / h)`: never magnified, shrunk to fit if the viewer is smaller than the
// image (a window wider than 1920 px, or a rotation the pod hasn't caught up with yet), letterboxed around.

export const LIMITS = Object.freeze({ minW: 240, maxW: 1920, minH: 240, maxH: 1600, minDpr: 0.5, maxDpr: 5 });

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

/** The viewport the viewer asks for in `hello` and `resize`. */
export function viewport(innerWidth, innerHeight, devicePixelRatio) {
  return {
    w: clamp(Math.round(innerWidth), LIMITS.minW, LIMITS.maxW),
    h: clamp(Math.round(innerHeight), LIMITS.minH, LIMITS.maxH),
    dpr: clamp(devicePixelRatio, LIMITS.minDpr, LIMITS.maxDpr),
  };
}

/** The box a `w`×`h` image is drawn in on a `viewWidth`×`viewHeight` screen (CSS px). */
export function box(viewWidth, viewHeight, w, h) {
  const k = Math.min(1, viewWidth / w, viewHeight / h);
  const width = w * k;
  const height = h * k;
  return { x: (viewWidth - width) / 2, y: (viewHeight - height) / 2, width, height, k, w, h };
}

/** The frame px under a point of the screen, or null when the point isn't on the image. */
export function toFrame(at, px, py) {
  const x = (px - at.x) / at.k;
  const y = (py - at.y) / at.k;
  return x >= 0 && y >= 0 && x <= at.w && y <= at.h ? [x, y] : null;
}

/** The point of the screen showing a frame px. */
export function toView(at, x, y) {
  return [at.x + x * at.k, at.y + y * at.k];
}

/** A canvas's backing store for a `width`×`height` CSS px element: one pixel per device pixel. */
export function backing(width, height, devicePixelRatio) {
  return [Math.max(1, Math.round(width * devicePixelRatio)), Math.max(1, Math.round(height * devicePixelRatio))];
}
