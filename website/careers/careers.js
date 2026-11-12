// Careers form: checks the answers, saves them in our CRM (Job Applications) and shows "submitted".
// A WhatsApp button then lets the applicant tell us; if saving fails, that button carries the whole application.
(function () {
  var WHATSAPP_NUMBER = "918547275320";
  var CRM_URL = "https://crm.realviewcreativestudio.in/job-application";
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

    if (data.get("website")) return;   // bot

    var full = [
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
    if (get("portfolio")) full.push("Portfolio / Resume / Instagram: " + get("portfolio"));
    if (get("about")) full.push("", "About me:", get("about"));
    var short = "Hi, I have submitted my application for *" + get("role") + "* on your website. My name is *" + get("name") + "*.";
    var btn = form.querySelector("button[type=submit]");
    var waBtn = document.getElementById("apply-wa");

    function done(saved) {
      btn.disabled = false; btn.textContent = "Submit application";
      waBtn.href = "https://wa.me/" + WHATSAPP_NUMBER + "?text=" + encodeURIComponent(saved ? short : full.join("\n"));
      if (saved) {
        okBox.querySelector("p").innerHTML = "<b>✅ Application submitted successfully.</b> Thank you! We will contact you soon.";
        form.querySelectorAll("input, textarea").forEach(function (x) { x.disabled = true; });
        btn.style.display = "none";
        // No personal data in the event: only the role and the choices.
        pixel().once("apply", "SubmitApplication", {
          content_name: get("role").slice(0, 60), job_type: jobTypes.join(", "), work_location: locations.join(", ")
        });
      } else {
        okBox.querySelector("p").innerHTML = "<b>⚠️ We could not save it right now.</b> Please send your application on WhatsApp instead.";
      }
      okBox.hidden = false;
      okBox.scrollIntoView({behavior: "smooth", block: "center"});
    }
    btn.disabled = true; btn.textContent = "Submitting…";
    var body = new FormData(form);
    body.set("whatsapp", phone);
    fetch(CRM_URL, {method: "POST", body: body})
      .then(function (r) { done(r.ok); })
      .catch(function () { done(false); });
  });
})();
