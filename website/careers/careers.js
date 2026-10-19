// Careers form: checks the answers, then opens WhatsApp with them. Nothing is sent to any server.
(function () {
  var WHATSAPP_NUMBER = "918547275320";
  var form = document.getElementById("apply-form");
  if (!form) return;
  var errorBox = form.querySelector(".form-error");
  var okBox = form.querySelector(".form-success");
  function pixel() { return window.rvPixel || {track: function () {}, once: function () {}, custom: function () {}}; }

  // CareersStart: the first time someone types or ticks anything in the form.
  var started = false;
  function start() {
    if (started) return;
    started = true;
    pixel().custom("CareersStart", {page: "/careers/"});
  }
  form.addEventListener("input", start);
  form.addEventListener("change", start);

  // 10-digit Indian mobile number; +91 / 0 in front is fine.
  function indianMobile(value) {
    var d = (value || "").replace(/\D/g, "");
    if (d.length === 12 && d.indexOf("91") === 0) d = d.slice(2);
    else if (d.length === 11 && d.charAt(0) === "0") d = d.slice(1);
    return /^[6-9]\d{9}$/.test(d) ? d : "";
  }

  function el(name) { return form.elements.namedItem(name); }

  function fail(msg, field) {
    errorBox.textContent = msg;
    errorBox.hidden = false;
    okBox.hidden = true;
    if (field && field.focus) field.focus();
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var data = new FormData(form);
    var get = function (k) { return (data.get(k) || "").toString().trim(); };
    var jobTypes = data.getAll("job_type"), locations = data.getAll("location");
    var phone = indianMobile(get("whatsapp"));
    if (!get("name")) return fail("Please enter your name.", el("name"));
    if (!phone) return fail("Please enter a valid 10-digit WhatsApp number.", el("whatsapp"));
    if (!get("place")) return fail("Please enter your place.", el("place"));
    if (!get("role")) return fail("Please tell us the role or work you want to do.", el("role"));
    if (!jobTypes.length) return fail("Please choose at least one job type.", form.querySelector("[name=job_type]"));
    if (!locations.length) return fail("Please choose Office, Remote or both.", form.querySelector("[name=location]"));
    if (!get("experience")) return fail("Please choose your experience.", form.querySelector("[name=experience]"));
    errorBox.hidden = true;

    var lines = [
      "*Job Application – Real View Creative Studio*",
      "",
      "Name: " + get("name"),
      "WhatsApp: " + phone,
      "Place: " + get("place"),
      "Role: " + get("role"),
      "Job type: " + jobTypes.join(", "),
      "Work location: " + locations.join(", "),
      "Experience: " + get("experience")
    ];
    if (get("portfolio")) lines.push("Portfolio / Resume: " + get("portfolio"));
    if (get("about")) lines.push("", "About me:", get("about"));
    var url = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(lines.join("\n"));

    // No personal data in the event: only the role and the choices.
    pixel().once("apply", "SubmitApplication", {
      content_name: get("role").slice(0, 60), job_type: jobTypes.join(", "), work_location: locations.join(", ")
    });
    okBox.hidden = false;
    // A new tab keeps this page (and the thank-you note); if the browser blocks it, open WhatsApp here.
    var win = window.open(url, "_blank");
    if (win) win.opener = null;
    else setTimeout(function () { window.location.href = url; }, 300);
  });
})();
