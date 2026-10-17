import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parsedState, stateWith } from "@/test/fixtures";
import { jsonResponse, mockFetch, renderWithProviders } from "@/test/render";
import { AlertsBanner } from "./AlertsBanner";

const TURN_ON = "Turn on alerts on this device. It's how you'll hear when something opens.";
const IPHONE =
  "To get alerts on iPhone, add Waitlist to your Home Screen (Share → Add to Home Screen), then open it from there.";

/** A browser that can do Web Push, subscribed here or not; `subscribe` is what a new subscription returns. */
function pushCapable(subscribed: boolean) {
  const endpoint = { endpoint: "https://push.example/abc", keys: { p256dh: "k", auth: "a" } };
  const pushManager = {
    getSubscription: vi.fn(() => Promise.resolve(subscribed ? { toJSON: () => endpoint } : null)),
    subscribe: vi.fn(() => Promise.resolve({ toJSON: () => endpoint })),
  };
  const registration = { pushManager };
  vi.stubGlobal("PushManager", {});
  vi.stubGlobal("Notification", { requestPermission: () => Promise.resolve("granted") });
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: {
      getRegistration: () => Promise.resolve(registration),
      register: () => Promise.resolve(registration),
      ready: Promise.resolve(registration),
    },
  });
  return { pushManager, endpoint };
}

/** A browser without Web Push: an iPhone's Safari outside a home-screen app, or something older. */
function noPush(userAgent: string) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
}

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("the alerts banner", () => {
  it("asks to turn alerts on while this device has no subscription", async () => {
    pushCapable(false);
    renderWithProviders(<AlertsBanner state={parsedState()} />);
    expect(await screen.findByText(TURN_ON)).toBeInTheDocument();
  });

  it("isn't shown on a device that already gets alerts", async () => {
    const { pushManager } = pushCapable(true);
    renderWithProviders(<AlertsBanner state={parsedState()} />);
    await waitFor(() => expect(pushManager.getSubscription).toHaveBeenCalled());
    expect(screen.queryByText(TURN_ON)).toBeNull();
  });

  it("isn't shown to someone who watches nothing yet", () => {
    pushCapable(false);
    renderWithProviders(<AlertsBanner state={stateWith([])} />);
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("turns alerts on, then goes away", async () => {
    const { endpoint } = pushCapable(false);
    const { requests } = mockFetch(() => jsonResponse({}));
    const { user } = renderWithProviders(<AlertsBanner state={parsedState()} />);
    await user.click(await screen.findByRole("button", { name: "Turn on" }));
    expect(await screen.findByText("Alerts are on for this device")).toBeInTheDocument();
    expect(requests.find((r) => r.url === "/api/me/push-subscriptions")?.body).toEqual(endpoint);
    await waitFor(() => expect(screen.queryByText(TURN_ON)).toBeNull());
  });

  it("says how to get alerts on an iPhone outside a home-screen app", () => {
    noPush("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari");
    renderWithProviders(<AlertsBanner state={parsedState()} />);
    expect(screen.getByText(IPHONE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  });

  it("says nothing where push isn't possible and there's nothing to suggest", () => {
    noPush("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/115.0");
    renderWithProviders(<AlertsBanner state={parsedState()} />);
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("stays dismissed on this device", async () => {
    pushCapable(false);
    const { user, unmount } = renderWithProviders(<AlertsBanner state={parsedState()} />);
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(TURN_ON)).toBeNull();
    expect(localStorage.getItem("wl.alertsBannerDismissed")).toBe("1");
    unmount();
    renderWithProviders(<AlertsBanner state={parsedState()} />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(TURN_ON)).toBeNull();
  });

  it("works when storage is blocked", async () => {
    pushCapable(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { user } = renderWithProviders(<AlertsBanner state={parsedState()} />);
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(TURN_ON)).toBeNull();
  });
});
