// Instagram stories on the home page. The CRM saves a copy of every story (Super Admin -> Instagram) and
// gives them here newest first; they stay even after they leave Instagram. Nothing shows if there are none.
(function () {
  var FEED = window.RV_STORIES_FEED || "https://crm.realviewcreativestudio.in/ig/stories.json";
  var PHOTO_MS = 5000;
  var band = document.getElementById("stories");
  if (!band || !window.fetch) return;
  var row = band.querySelector(".stories-row");
  var list = [], profile = "https://www.instagram.com/realview_creative_studio/";

  function seenSet() {
    try { return JSON.parse(localStorage.getItem("rvStoriesSeen") || "[]"); } catch (e) { return []; }
  }
  function markSeen(id) {
    try {
      var s = seenSet();
      if (s.indexOf(id) < 0) { s.push(id); localStorage.setItem("rvStoriesSeen", JSON.stringify(s.slice(-200))); }
    } catch (e) { /* private mode: fine */ }
  }
  function label(st) {
    if (st.title) return st.title;
    var d = new Date(st.posted);
    if (isNaN(d)) return "Story";
    var days = Math.floor((Date.now() - d) / 86400000);
    if (days < 1) return "Today";
    if (days < 2) return "Yesterday";
    return d.toLocaleDateString("en-IN", {day: "numeric", month: "short"});
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function render() {
    var seen = seenSet();
    row.innerHTML = "";
    list.forEach(function (st, i) {
      var b = el("button", "story" + (seen.indexOf(st.id) >= 0 ? " seen" : ""));
      b.type = "button";
      b.setAttribute("role", "listitem");
      b.setAttribute("aria-label", "Open story: " + label(st));
      var ring = el("span", "story-ring");
      if (st.thumb) {
        var img = el("img");
        img.src = st.thumb; img.alt = ""; img.loading = "lazy"; img.decoding = "async"; img.width = 72; img.height = 72;
        ring.appendChild(img);
      } else {
        ring.appendChild(el("b", null, "▶"));
      }
      if (st["new"]) ring.appendChild(el("i", "new", "NEW"));
      b.appendChild(ring);
      var t = el("span");
      t.textContent = label(st);
      b.appendChild(t);
      b.addEventListener("click", function () { open(i); });
      row.appendChild(b);
    });
    band.hidden = list.length === 0;
  }

  // ---------------------------------------------------------------- full-screen viewer
  var sv, frame, bars, media, nameBox, cur = -1, timer = null, started = 0, elapsed = 0, paused = false, raf = 0;

  function build() {
    sv = el("div", "sv");
    sv.setAttribute("role", "dialog");
    sv.setAttribute("aria-modal", "true");
    sv.setAttribute("aria-label", "Instagram stories");
    frame = el("div", "sv-frame");
    bars = el("div", "sv-bars");
    var top = el("div", "sv-top");
    top.appendChild(Object.assign(el("img"), {src: "/assets/logo-mark.png", alt: ""}));
    nameBox = el("div", null);
    top.appendChild(nameBox);
    var close = el("button", "sv-close", "×");
    close.type = "button"; close.setAttribute("aria-label", "Close");
    close.addEventListener("click", function (e) { e.stopPropagation(); shut(); });
    top.appendChild(close);
    media = el("div", "sv-media");
    media.style.height = "100%";
    var cta = el("a", "btn btn-primary sv-cta", "Follow on Instagram");
    cta.href = profile; cta.target = "_blank"; cta.rel = "noopener";
    cta.addEventListener("click", function (e) { e.stopPropagation(); });
    frame.appendChild(media); frame.appendChild(bars); frame.appendChild(top); frame.appendChild(cta);
    var prev = el("button", "sv-nav sv-prev", "‹"), next = el("button", "sv-nav sv-next", "›");
    prev.type = next.type = "button";
    prev.setAttribute("aria-label", "Previous"); next.setAttribute("aria-label", "Next");
    prev.addEventListener("click", function (e) { e.stopPropagation(); go(cur - 1); });
    next.addEventListener("click", function (e) { e.stopPropagation(); go(cur + 1); });
    var wrap = el("div", "sv-wrap");
    wrap.appendChild(frame); wrap.appendChild(prev); wrap.appendChild(next);
    sv.appendChild(wrap);
    // tap left third = back, elsewhere = next; press and hold = pause
    var down = 0;
    frame.addEventListener("pointerdown", function (e) {
      if (e.target.closest("a, button")) { down = 0; return; }
      down = Date.now(); pause(true);
    });
    frame.addEventListener("pointerup", function (e) {
      if (!down) return;
      pause(false);
      if (Date.now() - down > 300) return;              // it was a hold
      var r = frame.getBoundingClientRect();
      go(e.clientX - r.left < r.width / 3 ? cur - 1 : cur + 1);
    });
    sv.addEventListener("click", function (e) { if (e.target === sv) shut(); });
    document.addEventListener("keydown", function (e) {
      if (cur < 0) return;
      if (e.key === "Escape") shut();
      else if (e.key === "ArrowRight") go(cur + 1);
      else if (e.key === "ArrowLeft") go(cur - 1);
    });
    document.body.appendChild(sv);
  }

  function open(i) {
    if (!sv) build();
    document.documentElement.style.overflow = "hidden";
    sv.style.display = "grid";
    requestAnimationFrame(function () { sv.classList.add("open"); });
    if (window.rvPixel) window.rvPixel.custom("InstagramStoryOpen", {content_name: "Website stories"});
    go(i);
  }

  function shut() {
    stop();
    cur = -1;
    media.innerHTML = "";
    sv.classList.remove("open");
    sv.style.display = "none";
    document.documentElement.style.overflow = "";
    render();
  }

  function stop() {
    clearTimeout(timer);
    cancelAnimationFrame(raf);
  }

  function setBar(p) {
    var b = bars.children[cur];
    if (b) b.firstChild.style.transform = "scaleX(" + Math.max(0, Math.min(1, p)) + ")";
  }

  function go(i) {
    stop();
    if (i < 0) i = 0;
    if (i >= list.length) { shut(); return; }
    cur = i;
    var st = list[i];
    markSeen(st.id);
    bars.innerHTML = "";
    list.forEach(function (_, k) {
      var b = el("i", k < i ? "done" : "");
      b.appendChild(el("em"));
      bars.appendChild(b);
    });
    nameBox.innerHTML = "<b>realview_creative_studio</b> <small></small>";
    nameBox.querySelector("small").textContent = label(st);
    media.innerHTML = "";
    elapsed = 0; paused = false;
    if (st.type === "video") {
      var v = el("video");
      v.src = st.src; v.playsInline = true; v.autoplay = true; v.preload = "auto";
      v.setAttribute("playsinline", "");
      if (st.thumb) v.poster = st.thumb;
      v.addEventListener("timeupdate", function () { if (v.duration) setBar(v.currentTime / v.duration); });
      v.addEventListener("ended", function () { go(cur + 1); });
      v.addEventListener("error", function () { timer = setTimeout(function () { go(cur + 1); }, 1500); });
      media.appendChild(v);
      var p = v.play();
      if (p && p.catch) p.catch(function () { v.muted = true; v.play(); });   // sound blocked: play muted
    } else {
      var img = el("img");
      img.alt = st.title || "Instagram story";
      img.onload = function () { if (cur === i) tick(); };
      img.onerror = function () { go(cur + 1); };
      img.src = st.src;
      media.appendChild(img);
    }
    var nx = list[i + 1];
    if (nx && nx.type === "image") { var pre = new Image(); pre.src = nx.src; }   // next one loads early
  }

  function tick() {
    started = performance.now() - elapsed;
    (function step(now) {
      if (paused) return;
      elapsed = now - started;
      setBar(elapsed / PHOTO_MS);
      if (elapsed >= PHOTO_MS) { go(cur + 1); return; }
      raf = requestAnimationFrame(step);
    })(performance.now());
  }

  function pause(on) {
    if (cur < 0) return;
    var v = media.querySelector("video");
    if (v) { if (on) v.pause(); else v.play().catch(function () {}); return; }
    if (on) { paused = true; cancelAnimationFrame(raf); }
    else if (paused) { paused = false; tick(); }
  }

  // ---------------------------------------------------------------- load (after the page is ready, so the
  // first paint is never slowed down)
  function load() {
    fetch(FEED, {mode: "cors", credentials: "omit"})
      .then(function (r) { return r.ok ? r.json() : {stories: []}; })
      .then(function (d) {
        list = (d && d.stories) || [];
        if (d && d.profile) profile = d.profile;
        render();
      })
      .catch(function () { /* CRM unreachable: the section simply stays hidden */ });
  }
  if (document.readyState === "complete") setTimeout(load, 300);
  else window.addEventListener("load", function () { setTimeout(load, 300); });
})();
