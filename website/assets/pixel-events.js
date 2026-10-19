/* Meta Pixel events for www.realviewcreativestudio.in (home page and /careers/).
 * The base code (fbq init + PageView) is in each page's <head>; this file adds the other events.
 * Every call is guarded with `if (window.fbq)`, so the site works when the pixel is blocked.
 * Never put personal data (name, phone, message text) in event parameters.
 * Debug: add ?pixeldebug=1 to the address to log every event with console.debug.
 */
(function () {
  var DEBUG = /[?&]pixeldebug=1\b/.test(location.search);
  var PAGE = location.pathname;
  var fired = {};

  function track(kind, name, params) {
    if (DEBUG) console.debug("[pixel]", kind, name, params || {});
    if (window.fbq) window.fbq(kind, name, params || {});
  }
  function once(key, kind, name, params) {
    if (fired[key]) return;
    fired[key] = true;
    track(kind, name, params);
  }

  // Used by the page scripts (forms, demo button).
  window.rvPixel = {
    track: function (name, params) { track("track", name, params); },
    custom: function (name, params) { track("trackCustom", name, params); },
    once: function (key, name, params) { once(key, "track", name, params); }
  };
  if (DEBUG) console.debug("[pixel]", "track", "PageView", {note: "sent by the base code in <head>", pixel_loaded: !!window.fbq});

  // ViewContent: a section is half visible (once per section). Sections carry data-track-section="Name".
  var sections = document.querySelectorAll("[data-track-section]");
  if ("IntersectionObserver" in window && sections.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var name = e.target.getAttribute("data-track-section");
        once("vc:" + name, "track", "ViewContent", {content_name: name, content_category: "Section"});
        io.unobserve(e.target);
      });
    }, {threshold: 0.5});
    sections.forEach(function (s) { io.observe(s); });
  }
  // A whole page counted as content (e.g. <body data-track-page="Careers Page">).
  var pageName = document.body.getAttribute("data-track-page");
  if (pageName) once("vc:page", "track", "ViewContent", {content_name: pageName});

  // Clicks: Contact (WhatsApp / Call / Email), ServiceClick, OutboundClick (social networks).
  var SOCIAL = {"instagram.com": "Instagram", "facebook.com": "Facebook", "fb.com": "Facebook",
                "linkedin.com": "LinkedIn", "youtube.com": "YouTube", "youtu.be": "YouTube"};
  function label(el) {
    return (el.getAttribute("data-track-label") || el.getAttribute("aria-label") || el.textContent || "")
      .replace(/\s+/g, " ").trim().slice(0, 60) || "link";
  }
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("a, button, [data-service]") : null;
    if (!el) return;
    var service = el.closest("[data-service]");
    if (service) track("trackCustom", "ServiceClick", {service: service.getAttribute("data-service")});
    var href = el.getAttribute("href") || "";
    var method = /^https:\/\/(wa\.me|api\.whatsapp\.com|web\.whatsapp\.com)\//.test(href) || /^whatsapp:/.test(href) ? "WhatsApp"
      : /^tel:/.test(href) ? "Call" : /^mailto:/.test(href) ? "Email" : "";
    if (method && !el.hasAttribute("data-no-contact")) track("track", "Contact", {content_name: label(el), method: method});
    var host = (el.hostname || "").replace(/^www\./, "");
    for (var h in SOCIAL) {
      if (host === h || host.slice(-h.length - 1) === "." + h) { track("trackCustom", "OutboundClick", {network: SOCIAL[h]}); break; }
    }
  }, true);

  // ScrollDepth at 50% and 90% of the page.
  function onScroll() {
    var doc = document.documentElement;
    var max = doc.scrollHeight - window.innerHeight;
    var pct = max <= 0 ? 100 : (window.scrollY / max) * 100;
    [50, 90].forEach(function (p) {
      if (pct >= p) once("sd:" + p, "trackCustom", "ScrollDepth", {percent: p, page: PAGE});
    });
    if (fired["sd:90"]) window.removeEventListener("scroll", onScroll);
  }
  window.addEventListener("scroll", onScroll, {passive: true});

  // TimeOnPage at 30 s and 60 s of visible time (the clock pauses while the tab is hidden).
  var visibleSeconds = 0;
  var timer = setInterval(function () {
    if (document.visibilityState !== "visible") return;
    visibleSeconds += 1;
    if (visibleSeconds === 30) once("t:30", "trackCustom", "TimeOnPage", {seconds: 30, page: PAGE});
    if (visibleSeconds === 60) { once("t:60", "trackCustom", "TimeOnPage", {seconds: 60, page: PAGE}); clearInterval(timer); }
  }, 1000);
})();
