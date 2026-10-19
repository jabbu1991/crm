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

// "Book a CRM demo" opens WhatsApp (a normal link) and counts as Schedule.
var demo = document.getElementById("crm-demo");
if (demo) demo.addEventListener("click", function () { pixel().once("schedule", "Schedule", {content_name: "CRM Demo"}); });

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
