/* 📥 Lead Capture helpers: Paste & Fill, Pick from Contacts, Duplicate Alert, Quick Add. */
(function () {
  "use strict";
  var RVC = window.RVC = {};

  // ------------------------------------------------------------ Paste & Fill
  var PLACE_WORDS = /\b(place|location|city|town|district|from|loc|address|area|stay(?:ing)? at)\s*[:\-–]\s*(.+)/i;
  var NAME_WORDS = /\b(name|customer|contact person|cust)\s*[:\-–]\s*(.+)/i;
  var SKIP = /(https?:\/\/|www\.|@|\d{5,}|^(hi|hello|hai|dear|sir|madam|good (morning|evening|afternoon)|thanks|thank you|regards|ok|okay)\b)/i;

  RVC.parse = function (text) {
    var out = {name: "", phone: "", email: "", place: "", notes: ""};
    text = String(text || "").replace(/\r/g, "").trim();
    if (!text) return out;
    var email = text.match(/[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}/i);
    if (email) out.email = email[0];
    // Phone: the first run of 10–13 digits (spaces, dashes, brackets and +91 allowed inside).
    var phones = text.match(/(?:\+?\d[\d\s\-().]{8,18}\d)/g) || [];
    for (var i = 0; i < phones.length; i++) {
      var d = phones[i].replace(/\D/g, "");
      if (d.length >= 10 && d.length <= 13) {
        if (d.length === 12 && d.indexOf("91") === 0) d = d.slice(2);
        else if (d.length === 11 && d[0] === "0") d = d.slice(1);
        out.phone = d.length === 10 ? d : "+" + d;
        break;
      }
    }
    var lines = text.split(/\n+/).map(function (l) { return l.trim(); }).filter(Boolean);
    lines.forEach(function (l) {
      var m;
      if (!out.place && (m = l.match(PLACE_WORDS))) out.place = m[2].trim().slice(0, 80);
      if (!out.name && (m = l.match(NAME_WORDS))) out.name = m[2].trim().slice(0, 80);
    });
    if (!out.name) {
      // WhatsApp forward "[10:15, 12/11/2026] Rahul K: message" or a vCard "FN:Rahul K".
      var wa = text.match(/\]\s*([^:\n]{2,40}):/) || text.match(/^FN[:;](.+)$/mi);
      if (wa && !/\d{6,}/.test(wa[1])) out.name = wa[1].trim();
    }
    if (!out.name) {
      for (var j = 0; j < lines.length; j++) {
        var l = lines[j].replace(/^[\-*•]\s*/, "");
        if (l.length >= 2 && l.length <= 40 && !SKIP.test(l) && /^[A-Za-zഀ-ൿ .'\-]+$/.test(l)
            && l.split(/\s+/).length <= 4) { out.name = l; break; }
      }
    }
    out.notes = text.slice(0, 1500);
    return out;
  };

  // Put parsed values into a form's empty fields (or all of them when force).
  RVC.fill = function (form, data, force) {
    var map = {customer_name: data.name, phone: data.phone, email: data.email, place: data.place};
    Object.keys(map).forEach(function (k) {
      var el = form.querySelector('[name="' + k + '"]');
      if (el && map[k] && (force || !el.value)) { el.value = map[k]; el.dispatchEvent(new Event("input", {bubbles: true})); }
    });
    var wa = form.querySelector('[name="whatsapp"]');
    if (wa && data.phone && !wa.value) wa.value = data.phone;
    var notes = form.querySelector('[name="notes"]') || form.querySelector('[name="requirement"]');
    if (notes && data.notes && !notes.value && data.notes.replace(/\s/g, "").length > 25) notes.value = data.notes;
  };

  // ------------------------------------------------------------ Pick from Contacts
  RVC.canPickContact = !!(navigator.contacts && navigator.contacts.select);
  RVC.pickContact = function (form) {
    if (!RVC.canPickContact) { alert("Your phone's browser does not allow picking contacts. Use Android Chrome."); return; }
    navigator.contacts.select(["name", "tel", "email"], {multiple: false}).then(function (list) {
      if (!list || !list.length) return;
      var c = list[0];
      var raw = (c.tel && c.tel[0]) || "";
      var d = raw.replace(/\D/g, "");
      if (d.length === 12 && d.indexOf("91") === 0) d = d.slice(2);
      RVC.fill(form, {name: (c.name && c.name[0]) || "", phone: d.length === 10 ? d : raw.trim(),
                      email: (c.email && c.email[0]) || ""}, true);
    }).catch(function () {});
  };

  // ------------------------------------------------------------ Duplicate Alert
  RVC.watchDup = function (input, box, url, exclude) {
    if (!input || !box || !url) return;
    var t = null, last = "";
    function check() {
      var v = input.value.replace(/\D/g, "");
      if (v.length < 8 || v === last) { if (v.length < 8) { box.hidden = true; last = ""; } return; }
      last = v;
      fetch(url + "?phone=" + encodeURIComponent(input.value) + (exclude ? "&exclude=" + exclude : ""),
            {credentials: "same-origin"}).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.matches.length && !j.hidden) { box.hidden = true; return; }
        box.innerHTML = "";
        var h = document.createElement("b"); h.textContent = "⚠️ Already in the CRM:"; box.appendChild(h);
        j.matches.forEach(function (m) {
          var row = document.createElement("div");
          var a = document.createElement("a"); a.href = m.url; a.target = "_blank"; a.textContent = m.name;
          row.appendChild(a);
          row.appendChild(document.createTextNode(" · " + [m.status, m.assigned, m.when].filter(Boolean).join(" · ")));
          box.appendChild(row);
        });
        if (j.hidden) {
          var hd = document.createElement("div");
          hd.textContent = j.hidden + " more with other staff. Ask your admin before adding.";
          box.appendChild(hd);
        }
        box.hidden = false;
      }).catch(function () {});
    }
    input.addEventListener("input", function () { clearTimeout(t); t = setTimeout(check, 500); });
    input.addEventListener("blur", check);
    if (input.value) check();
  };

  // ------------------------------------------------------------ toolbar (Paste & Fill + Contacts) on a form
  RVC.tools = function (form) {
    var bar = form.querySelector(".cap-tools");
    if (!bar || form.dataset.capInit) return;
    form.dataset.capInit = "1";
    var pasteBtn = bar.querySelector("[data-cap=paste]"), contactBtn = bar.querySelector("[data-cap=contacts]");
    var area = bar.querySelector(".cap-paste");
    if (contactBtn) {
      if (!RVC.canPickContact) contactBtn.hidden = true;
      contactBtn.addEventListener("click", function () { RVC.pickContact(form); });
    }
    if (pasteBtn && area) {
      var ta = area.querySelector("textarea");
      pasteBtn.addEventListener("click", function () {
        area.hidden = !area.hidden;
        if (!area.hidden) {
          ta.focus();
          if (navigator.clipboard && navigator.clipboard.readText) {
            navigator.clipboard.readText().then(function (t) { if (t && !ta.value) ta.value = t; }).catch(function () {});
          }
        }
      });
      area.querySelector("[data-cap=fill]").addEventListener("click", function () {
        RVC.fill(form, RVC.parse(ta.value), true);
        area.hidden = true; ta.value = "";
      });
    }
  };

  // ------------------------------------------------------------ Quick Add window
  function tomorrow() {
    var d = new Date(); d.setDate(d.getDate() + 1);
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
  }

  RVC.initQuick = function () {
    var dlg = document.getElementById("qa-dialog");
    if (!dlg) return;
    var form = dlg.querySelector("form"), msg = dlg.querySelector(".qa-msg"), dupBox = dlg.querySelector(".cap-dup");
    var saveBtn = form.querySelector(".qa-save"), confirm = form.querySelector('[name="confirm_duplicate"]');
    var lastProject = "";
    try { lastProject = localStorage.getItem("qa-project") || ""; } catch (e) {}
    RVC.tools(form);
    RVC.watchDup(form.querySelector('[name="phone"]'), dupBox, dlg.dataset.dup);
    RVC.openQuick = function (text) {
      form.reset(); confirm.value = ""; msg.textContent = ""; msg.className = "qa-msg"; dupBox.hidden = true;
      var sel = form.querySelector('[name="project_id"]');
      if (sel && lastProject && sel.querySelector('option[value="' + lastProject + '"]')) sel.value = lastProject;
      var fu = form.querySelector(".qa-fu"); if (fu) fu.value = tomorrow();
      if (text) RVC.fill(form, RVC.parse(text), true);
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
      setTimeout(function () { (form.querySelector('[name="customer_name"]').value
        ? form.querySelector('[name="phone"]') : form.querySelector('[name="customer_name"]')).focus(); }, 50);
    };
    document.querySelectorAll("[data-quick-add]").forEach(function (b) {
      b.addEventListener("click", function (e) { e.preventDefault(); RVC.openQuick(); });
    });
    dlg.querySelector(".qa-close").addEventListener("click", function () { dlg.close ? dlg.close() : dlg.removeAttribute("open"); });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      saveBtn.disabled = true; msg.textContent = "Saving…"; msg.className = "qa-msg";
      fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin"})
        .then(function (r) { return r.json(); }).then(function (j) {
          saveBtn.disabled = false;
          if (j.ok) {
            try { localStorage.setItem("qa-project", form.querySelector('[name="project_id"]').value); } catch (e2) {}
            lastProject = form.querySelector('[name="project_id"]').value;
            msg.className = "qa-msg ok";
            msg.innerHTML = "";
            msg.appendChild(document.createTextNode("✅ Saved: " + j.name + " · "));
            var a = document.createElement("a"); a.href = j.url; a.textContent = "Open lead"; msg.appendChild(a);
            var keep = form.querySelector('[name="project_id"]').value;
            form.reset(); confirm.value = ""; dupBox.hidden = true;
            form.querySelector('[name="project_id"]').value = keep;
            var fu2 = form.querySelector(".qa-fu"); if (fu2) fu2.value = tomorrow();
            form.querySelector('[name="customer_name"]').focus();
          } else if (j.duplicate) {
            msg.className = "qa-msg warn";
            msg.textContent = "⚠️ This customer may already be in the CRM (see above). Tap Save again to add anyway.";
            confirm.value = "1";
          } else {
            msg.className = "qa-msg err"; msg.textContent = j.error || "Could not save.";
          }
        }).catch(function () { saveBtn.disabled = false; msg.className = "qa-msg err"; msg.textContent = "No connection. Try again."; });
    });
    form.querySelector('[name="phone"]').addEventListener("input", function () { confirm.value = ""; });
    if (window.RV_SHARED !== undefined) RVC.openQuick(window.RV_SHARED);
  };

  document.addEventListener("DOMContentLoaded", function () {
    RVC.initQuick();
    document.querySelectorAll("form[data-capture]").forEach(function (f) {
      if (f.closest("#qa-dialog")) return;
      RVC.tools(f);
      if (f.dataset.dup) RVC.watchDup(f.querySelector('[name="phone"]'), f.querySelector(".cap-dup"), f.dataset.dup, f.dataset.exclude);
    });
  });
})();
