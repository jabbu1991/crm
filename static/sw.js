/* Real View CRM: shows "New online order" notifications on staff phones, even when the screen is off. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {title: "New online order", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "🔔 New online order", {
    body: d.body || "", tag: d.tag || "order", renotify: true, requireInteraction: true,
    vibrate: [400, 150, 400, 150, 400], icon: "/static/favicon.png", badge: "/static/favicon.png",
    data: {url: d.url || "/cafe/orders"}
  }));
});

self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || "/cafe/orders";
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url.indexOf(self.location.origin) === 0 && "focus" in list[i]) { list[i].navigate(url); return list[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
