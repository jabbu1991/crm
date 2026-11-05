// Site settings ------------------------------------------------------------
var WHATSAPP_NUMBER = "918547275320";
// Once Real View CRM is online, enquiries are also saved there as leads.
// Leave empty to only use WhatsApp.
var CRM_ENQUIRY_URL = "https://crm.realviewcreativestudio.in/enquiry";

// Mobile menu ---------------------------------------------------------------
var toggle = document.querySelector(".menu-toggle");
var links = document.querySelector(".links");
if (toggle && links) {
  toggle.addEventListener("click", function () {
    var open = links.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open);
  });
  links.addEventListener("click", function (e) {
    if (e.target.tagName === "A") links.classList.remove("open");
  });
}

// Meta Pixel helpers (assets/pixel-events.js); harmless if the pixel is blocked.
function pixel() { return window.rvPixel || {track: function () {}, once: function () {}, custom: function () {}}; }

// WhatsApp links with a preset message -------------------------------------
document.querySelectorAll("a[data-wa-text]").forEach(function (a) {
  a.href = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(a.dataset.waText);
});

var year = document.getElementById("year");
if (year) year.textContent = new Date().getFullYear();

// "Book a demo" buttons open WhatsApp (a normal link) and count as Schedule, once per product.
// Each button carries data-demo="<Product> Demo" (e.g. "CRM Demo", "Order Tracker Demo").
document.querySelectorAll("a[data-demo]").forEach(function (a) {
  a.addEventListener("click", function () {
    var name = a.getAttribute("data-demo");
    pixel().once("schedule:" + name, "Schedule", {content_name: name});
  });
});

// Enquiry form: save to the CRM (if online) and open WhatsApp ---------------
var form = document.getElementById("enquiry");
if (form) form.addEventListener("submit", function (e) {
  var errorBox = form.querySelector(".form-error");
  e.preventDefault();
  var data = new FormData(form);
  var name = (data.get("name") || "").trim();
  var phone = (data.get("phone") || "").trim();
  if (!name || phone.replace(/\D/g, "").length < 6) {
    errorBox.textContent = "Please enter your name and a phone or WhatsApp number.";
    errorBox.hidden = false;
    return;
  }
  errorBox.hidden = true;
  if (data.get("website")) return; // bot

  if (CRM_ENQUIRY_URL) {
    try {
      // Fire-and-forget; keepalive lets it finish while WhatsApp opens.
      fetch(CRM_ENQUIRY_URL, { method: "POST", body: data, mode: "no-cors", keepalive: true });
    } catch (err) { /* WhatsApp still works */ }
  }

  var lines = ["Hi Real View Creative Studio,", "", "Name: " + name, "Phone: " + phone];
  if (data.get("business")) lines.push("Business: " + data.get("business"));
  if (data.get("service")) lines.push("Service: " + data.get("service"));
  if (data.get("message")) lines.push("", data.get("message"));
  pixel().once("lead", "Lead", {content_name: "Website Contact Form"});
  var url = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(lines.join("\n"));
  setTimeout(function () { window.location.href = url; }, 300); // a moment for the pixel request
});

// Motion: things below the screen come up when scrolled to (light: only opacity/transform) ------
(function () {
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!("IntersectionObserver" in window) || reduce) {
    document.querySelectorAll(".steps.line").forEach(function (s) { s.classList.add("drawn"); });
    return;
  }
  var groups = ".cards, .steps, .highlights, .pills, .faq, .contact-list, .approach";
  var items = document.querySelectorAll(
    ".section .kicker, .section h2, .section .big, .mission, .card, .steps li, .approach-item, .pills li, " +
    ".faq-item, .contact-list li, .careers-teaser > .btn, .more-link, .cta-band h2, .cta-band .btn, .phone");
  var screenH = window.innerHeight;
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      var el = e.target;
      io.unobserve(el);
      el.classList.add("in");
      // afterwards give the element its own hover transitions back
      setTimeout(function () { el.classList.remove("rv", "in"); el.style.removeProperty("--d"); }, 1400);
    });
  }, {rootMargin: "0px 0px -8% 0px", threshold: 0.08});
  items.forEach(function (el) {
    if (el.closest(".hero, .product-hero, .careers-hero, .stories-band") || el.getBoundingClientRect().top < screenH) return;
    var group = el.parentElement && el.parentElement.closest(groups);
    var d = group ? Array.prototype.indexOf.call(el.parentElement.children, el) : 0;
    el.style.setProperty("--d", Math.min(d, 6));
    el.classList.add("rv");
    io.observe(el);
  });
  // the process line draws itself; animations pause when they are off the screen
  var draw = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("drawn"); draw.unobserve(e.target); } });
  }, {threshold: 0.3});
  document.querySelectorAll(".steps.line").forEach(function (s) { draw.observe(s); });
  var pause = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { e.target.classList.toggle("paused", !e.isIntersecting); });
  });
  document.querySelectorAll(".phone, .stories-band").forEach(function (el) { pause.observe(el); });
})();
