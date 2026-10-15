// Site settings ------------------------------------------------------------
var WHATSAPP_NUMBER = "918547275320";
// Once Real View CRM is online, enquiries are also saved there as leads.
// Leave empty to only use WhatsApp.
var CRM_ENQUIRY_URL = "https://crm.realviewcreativestudio.in/enquiry";

// Mobile menu ---------------------------------------------------------------
var toggle = document.querySelector(".menu-toggle");
var links = document.querySelector(".links");
toggle.addEventListener("click", function () {
  var open = links.classList.toggle("open");
  toggle.setAttribute("aria-expanded", open);
});
links.addEventListener("click", function (e) {
  if (e.target.tagName === "A") links.classList.remove("open");
});

// WhatsApp links with a preset message -------------------------------------
document.querySelectorAll("a[data-wa-text]").forEach(function (a) {
  a.href = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(a.dataset.waText);
});

document.getElementById("year").textContent = new Date().getFullYear();

// Enquiry form: save to the CRM (if online) and open WhatsApp ---------------
var form = document.getElementById("enquiry");
var errorBox = form.querySelector(".form-error");

form.addEventListener("submit", function (e) {
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
  window.location.href = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(lines.join("\n"));
});

// Careers form: send the applicant's details on WhatsApp --------------------
var careers = document.getElementById("careers-form");
var careersError = careers.querySelector(".form-error");

careers.addEventListener("submit", function (e) {
  e.preventDefault();
  var data = new FormData(careers);
  var get = function (k) { return (data.get(k) || "").trim(); };
  if (!get("name") || !get("place") || get("phone").replace(/\D/g, "").length < 6 || !get("skills")) {
    careersError.textContent = "Please fill in your name, place, WhatsApp number and the services you can offer.";
    careersError.hidden = false;
    return;
  }
  careersError.hidden = true;
  var lines = ["Hi Real View Creative Studio,", "", "*Career application*",
               "Name: " + get("name"), "Place: " + get("place"), "WhatsApp: " + get("phone"),
               "", "Services I can offer:", get("skills")];
  window.location.href = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(lines.join("\n"));
});
