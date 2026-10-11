import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

// jsdom doesn't implement these; Radix and the app's hooks touch them.
class ResizeObserverStub {
  observe() {
    // nothing to observe in jsdom
  }
  unobserve() {
    // nothing to observe in jsdom
  }
  disconnect() {
    // nothing to observe in jsdom
  }
}
Object.defineProperty(window, "ResizeObserver", { configurable: true, writable: true, value: ResizeObserverStub });

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

Element.prototype.hasPointerCapture = () => false;
Element.prototype.releasePointerCapture = () => undefined;
Element.prototype.scrollIntoView = () => undefined;
