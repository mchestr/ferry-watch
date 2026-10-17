// Waitlist's service worker: shows Web Push alerts. It never sees the app's token; the push service
// delivers the (encrypted) message and the browser decrypts it before this runs.
self.addEventListener("push", (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch {
    message = {};
  }
  const title = typeof message.title === "string" ? message.title : "Waitlist";
  const body = typeof message.body === "string" ? message.body : "";
  event.waitUntil(self.registration.showNotification(title, { body, data: { url: message.url }, tag: title }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Only ever open a page on this site, whatever the message said.
  let url = new URL("/", self.location.origin);
  try {
    const target = new URL(event.notification.data?.url ?? "/", self.location.origin);
    if (target.origin === self.location.origin) url = target;
  } catch {
    // keep the dashboard
  }
  event.waitUntil(self.clients.openWindow(url.href));
});
