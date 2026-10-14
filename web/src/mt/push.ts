/** Turning alerts on for this device: a service worker and a Web Push subscription (§6.7, §10 item 6). */

export function keyBytes(base64url: string): Uint8Array {
  const padded = base64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64url.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

export const pushSupported = (): boolean =>
  "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

/** Asks for permission and subscribes; returns the subscription for the server, or null if refused. */
export async function subscribe(applicationServerKey: string): Promise<PushSubscriptionJSON | null> {
  if (!pushSupported()) return null;
  if ((await Notification.requestPermission()) !== "granted") return null;
  const registration = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(applicationServerKey),
    }));
  return subscription.toJSON();
}
