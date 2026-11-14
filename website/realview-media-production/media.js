// Realview Media Production page. Content comes from the CRM (Media Page) via /mp/data.json;
// the HTML already holds sensible defaults, so the page works even if the CRM can't be reached.
(function () {
  "use strict";
  var CRM = window.RV_MEDIA_CRM || "https://crm.realviewcreativestudio.in";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var S = {
    whatsapp: "+91 70255 04627", phone: "+91 70255 04627",
    wa_default: "Hi Azeez, I saw your work on the Realview Media Production website. I'd like to know more about your photography & videography packages.\n\nEvent: \nDate: \nLocation: ",
    wa_work: "Hi Azeez, I loved your work \"{work}\" on your website. I'd like a similar shoot for my event.\n\nDate: \nLocation: "
  };
  var works = [], current = "All";

  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function digits(n) { return String(n || "").replace(/\D/g, ""); }
  function waLink(text) {
    var d = digits(S.whatsapp);
    if (d.length === 10) d = "91" + d;
    return "https://wa.me/" + d + (text ? "?text=" + encodeURIComponent(text) : "");
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }
  function track_(ev, data) { try { if (window.fbq) fbq("track", ev, data || {}); } catch (e) { /* ignore */ } }
  function niceDate(d) {
    if (!d) return "";
    var t = new Date(d + "T00:00:00");
    return isNaN(t) ? "" : t.toLocaleDateString("en-IN", { month: "short", year: "numeric" });
  }

  // ------------------------------------------------------------ hero title: words come one by one
  function splitTitle() {
    var h = $(".hero-title");
    var words = h.textContent.trim().split(/\s+/);
    h.setAttribute("aria-label", h.textContent.trim());
    h.innerHTML = words.map(function (w, i) {
      return '<span class="w' + (i >= words.length - 2 ? " em" : "") + '" style="--i:' + (i + 1) + '" aria-hidden="true">' + esc(w) + "</span>";
    }).join(" ");
  }

  // ------------------------------------------------------------ settings from the CRM
  function applySettings(st) {
    Object.keys(st).forEach(function (k) { if (st[k] !== "" && st[k] != null) S[k] = st[k]; });
    $$("[data-s]").forEach(function (el) {
      var v = S[el.getAttribute("data-s")];
      if (typeof v === "string" && v) el.textContent = v;
    });
    if (st.hero_title) splitTitle();
    if (S.instagram) $$(".js-ig").forEach(function (a) { a.href = S.instagram; });
    if (S.youtube) $$(".js-yt").forEach(function (a) { a.href = S.youtube; });
    $$(".js-call").forEach(function (a) { a.href = "tel:+" + (digits(S.phone).length === 10 ? "91" : "") + digits(S.phone); });
    $$(".js-phone").forEach(function (a) { a.textContent = S.phone; });
    $$(".js-wanum").forEach(function (a) { a.textContent = S.whatsapp; });
    var handle = (S.instagram || "").match(/instagram\.com\/([^/?#]+)/);
    if (handle) $$(".hero-social .js-ig span, .book-contact .js-ig").forEach(function (el) { el.textContent = "@" + handle[1]; });
    if (st.photo) $$(".js-photo").forEach(function (img) { img.removeAttribute("srcset"); img.src = st.photo; });
    if (st.services && st.services.length) {
      var icons = [["save", "💌"], ["pre-", "💌"], ["film", "🎬"], ["video", "🎬"], ["engage", "🥂"], ["recep", "🥂"],
                   ["live", "🎤"], ["concert", "🎤"], ["show", "🎤"], ["birthday", "🎂"], ["corporate", "🏢"], ["baby", "👶"],
                   ["drone", "🚁"], ["product", "📦"], ["event", "🎉"], ["wedding", "💍"], ["photo", "📷"]];
      $(".svc-grid").innerHTML = st.services.map(function (s, i) {
        var t = s.title.toLowerCase(), ico = "📷";
        icons.some(function (k) { if (t.indexOf(k[0]) >= 0) { ico = k[1]; return true; } return false; });
        return '<article class="svc rv" style="--d:' + (i % 3) + '"><span class="svc-ico">' + ico + "</span><h3>" + esc(s.title) +
               "</h3><p>" + esc(s.text) + "</p></article>";
      }).join("");
    }
    if (st.packages && st.packages.length) {
      $("#packages").hidden = false;
      $(".pkg-grid").innerHTML = st.packages.map(function (p, i) {
        return '<div class="pkg rv" style="--d:' + i + '"><h3>' + esc(p.name) + '</h3><div class="price">' + esc(p.price) + "</div><ul>" +
          p.items.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
          '</ul><a class="mbtn mbtn-ghost js-wa" data-pkg="' + esc(p.name) + '" target="_blank" rel="noopener" href="#">Ask about ' + esc(p.name) + "</a></div>";
      }).join("");
    }
    if (st.faq && st.faq.length) {
      $("#faq").hidden = false;
      $(".faq-list").innerHTML = st.faq.map(function (f) {
        return '<div class="faq-item rv"><button type="button" aria-expanded="false">' + esc(f.q) +
               '</button><div class="faq-a"><div><p>' + esc(f.a) + "</p></div></div></div>";
      }).join("");
    }
    if (st.showreel) {
      var b = $(".js-reel");
      b.hidden = false;
      b.addEventListener("click", function () { openViewer({ title: "Showreel", kind: "youtube", lid: st.showreel, photos: [] }, 0); });
    }
    wireWa();
  }

  function wireWa() {
    $$(".js-wa").forEach(function (a) {
      var pkg = a.getAttribute("data-pkg");
      var text = pkg ? S.wa_default.replace(/\n\nEvent:/, "\nPackage: " + pkg + "\n\nEvent:") : S.wa_default;
      a.href = waLink(text);
      if (!a._t) { a._t = 1; a.addEventListener("click", function () { track_("Contact", { content_name: "WhatsApp" }); }); }
    });
  }

  // ------------------------------------------------------------ works
  function coverOf(w) {
    if (w.cover) return w.cover;
    if (w.photos && w.photos[0]) return { src: w.photos[0].thumb, w: w.photos[0].w, h: w.photos[0].h };
    return null;
  }
  function metaLine(w) { return [w.place, niceDate(w.date)].filter(Boolean).join(" · "); }

  function tileHtml(w, i) {
    var c = coverOf(w), ratio = c ? (c.h / c.w) : 0.75;
    if (w.kind === "youtube") ratio = Math.min(ratio, 0.75);
    ratio = Math.max(0.72, Math.min(ratio, 1.4));
    var extra = w.kind === "youtube" ? '<span class="play" aria-hidden="true"></span>'
      : w.kind === "instagram" ? '<span class="ig" aria-hidden="true"><svg viewBox="0 0 24 24"><use href="#i-ig"/></svg></span>'
      : (w.photos.length > 1 ? '<span class="count">🖼 ' + w.photos.length + "</span>" : "");
    return '<button type="button" class="tile rv" data-i="' + i + '" data-cat="' + esc(w.category) + '" aria-label="Open ' + esc(w.title) + '">' +
      '<div class="ph" style="aspect-ratio:1/' + ratio.toFixed(3) + '">' +
      (c ? '<img data-src="' + esc(c.src) + '" alt="' + esc(w.title) + '" decoding="async">' : "") +
      '<span class="shade"></span><span class="badge">' + esc(w.category) + "</span>" + extra +
      '<span class="meta"><b>' + esc(w.title) + "</b><span>" + esc(metaLine(w)) + "</span></span></div></button>";
  }

  function renderWorks() {
    var films = works.filter(function (w) { return w.kind === "youtube" && w.lid; });
    var insta = works.filter(function (w) { return w.kind === "instagram" && w.lid; });
    var album = works.filter(function (w) { return w.photos && w.photos.length; });
    var star = function (a, b) { return (b.featured ? 1 : 0) - (a.featured ? 1 : 0); };   // featured first, else as given
    films.sort(star); insta.sort(star); album.sort(star);
    renderFilms(films);
    renderInsta(insta);
    // photo gallery
    var box = $(".masonry");
    $("#work").hidden = album.length === 0;
    box.innerHTML = album.map(function (w) { return tileHtml(w, works.indexOf(w)); }).join("");
    var cats = ["All"].concat((DATA.settings.categories || []).filter(function (c) {
      return album.some(function (w) { return w.category === c; });
    }));
    var f = $(".filters");
    f.innerHTML = cats.length > 2 ? cats.map(function (c) {
      return '<button type="button" role="tab" aria-selected="' + (c === current) + '">' + esc(c) + "</button>";
    }).join("") : "";
    $$("button", f).forEach(function (b) { b.addEventListener("click", function () { filter(b.textContent); }); });
    $$(".masonry .tile[data-i]").forEach(function (t) {
      t.addEventListener("click", function () { openViewer(works[+t.getAttribute("data-i")], 0); });
    });
    // "See our work" goes to the first section that has something
    var first = !$("#films").hidden ? "#films" : !$("#instagram").hidden ? "#instagram" : !$("#work").hidden ? "#work" : "#about";
    $$(".js-seework, .scroll-cue").forEach(function (a) { a.setAttribute("href", first); });
    // hero background: newest featured / newest picture
    var heroW = works.filter(function (w) { return w.featured && coverOf(w); })[0] || works.filter(coverOf)[0];
    if (heroW) {
      var src = heroW.cover ? heroW.cover.big : heroW.photos[0].src;
      var im = new Image();
      im.onload = function () {
        if (im.naturalWidth <= 120 && src !== coverOf(heroW).src) { src = coverOf(heroW).src; im.src = src; return; }
        var h = $(".hero-img"); h.style.backgroundImage = "url('" + src + "')"; h.classList.add("on");
      };
      im.src = src;
    }
    lazyImages();
    reveal();
  }

  // ------------------------------------------------------------ swipe carousels (films, instagram)
  function carousel(root, onChange) {
    var sec = root.closest("section"), track = $(".car-track", root), dots = $(".car-dots", sec);
    var cards = $$(".card-c", track), idx = 0;
    dots.innerHTML = cards.length > 1 ? cards.map(function (_, i) { return '<i data-i="' + i + '"></i>'; }).join("") : "";
    var arrows = $(".car-arrows", sec);
    if (arrows) arrows.hidden = cards.length < 2;
    function go(i) {
      i = Math.max(0, Math.min(cards.length - 1, i));
      var c = cards[i];
      track.scrollTo({ left: c.offsetLeft - (track.clientWidth - c.offsetWidth) / 2, behavior: reduce ? "auto" : "smooth" });
    }
    function mark(i) {
      if (i === idx && dots.children[i] && dots.children[i].className) return;
      idx = i;
      $$("i", dots).forEach(function (d, k) { d.className = k === i ? "on" : ""; });
      cards.forEach(function (c, k) { c.classList.toggle("cur", k === i); });
      if (onChange) onChange(i, cards);
    }
    var t = null;
    track.addEventListener("scroll", function () {
      clearTimeout(t);
      t = setTimeout(function () {
        var mid = track.scrollLeft + track.clientWidth / 2, best = 0, bd = 1e9;
        cards.forEach(function (c, k) { var d = Math.abs(c.offsetLeft + c.offsetWidth / 2 - mid); if (d < bd) { bd = d; best = k; } });
        mark(best);
      }, 80);
    }, { passive: true });
    $$(".car-btn.prev", sec).forEach(function (b) { b.addEventListener("click", function () { go(idx - 1); }); });
    $$(".car-btn.next", sec).forEach(function (b) { b.addEventListener("click", function () { go(idx + 1); }); });
    dots.addEventListener("click", function (e) { var d = e.target.closest("i"); if (d) go(+d.getAttribute("data-i")); });
    track.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { e.preventDefault(); go(idx + 1); } else if (e.key === "ArrowLeft") { e.preventDefault(); go(idx - 1); }
    });
    // drag with the mouse on computers (phones swipe natively)
    var down = null;
    track.addEventListener("pointerdown", function (e) { if (e.pointerType === "mouse") down = { x: e.clientX, s: track.scrollLeft, moved: false }; });
    window.addEventListener("pointermove", function (e) {
      if (!down) return;
      var dx = e.clientX - down.x;
      if (Math.abs(dx) > 5) { down.moved = true; track.classList.add("drag"); track.scrollLeft = down.s - dx; }
    });
    window.addEventListener("pointerup", function () {
      if (!down) return;
      var moved = down.moved; down = null; track.classList.remove("drag");
      if (moved) { track._justDragged = true; setTimeout(function () { track._justDragged = false; }, 50); }
    });
    mark(0);
  }

  function renderFilms(films) {
    var sec = $("#films");
    sec.hidden = films.length === 0;
    if (!films.length) return;
    $(".car-track", sec).innerHTML = films.map(function (w, k) {
      return '<div class="card-c film rv" role="listitem" style="--d:' + Math.min(k, 3) + '" data-vid="' + esc(w.lid) + '">' +
        '<div class="film-media"><img alt="" decoding="async" loading="lazy" src="https://i.ytimg.com/vi/' + esc(w.lid) + '/maxresdefault.jpg"' +
        ' onload="if(this.naturalWidth<=120)this.src=\'https://i.ytimg.com/vi/' + esc(w.lid) + '/hqdefault.jpg\'"' +
        ' onerror="this.onerror=null;this.src=\'https://i.ytimg.com/vi/' + esc(w.lid) + '/hqdefault.jpg\'">' +
        '<button type="button" class="film-play" aria-label="Play ' + esc(w.title) + '"><span class="play"></span></button></div>' +
        '<div class="film-cap"><span class="badge">' + esc(w.category) + "</span><b>" + esc(w.title) + "</b><span>" + esc(metaLine(w)) + "</span>" +
        '<a class="film-wa js-wawork" target="_blank" rel="noopener" href="' + waLink(S.wa_work.replace(/\{work\}/g, w.title)) + '">' +
        '<svg viewBox="0 0 32 32"><use href="#i-wa"/></svg> Book a similar shoot</a></div></div>';
    }).join("");
    var track = $(".car-track", sec);
    function stopAll(except) {
      $$(".film", track).forEach(function (c) {
        if (c === except) return;
        var f = $("iframe", c);
        if (f) { f.remove(); c.classList.remove("playing"); }
      });
    }
    $$(".film", track).forEach(function (c) {
      $(".film-play", c).addEventListener("click", function () {
        if (track._justDragged) return;
        stopAll(c);
        var vid = c.getAttribute("data-vid");
        var f = document.createElement("iframe");
        f.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(vid) + "?autoplay=1&rel=0&modestbranding=1&playsinline=1";
        f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
        f.allowFullscreen = true;
        f.title = "Film";
        $(".film-media", c).appendChild(f);
        c.classList.add("playing");
        track.scrollTo({ left: c.offsetLeft - (track.clientWidth - c.offsetWidth) / 2, behavior: "smooth" });
        track_("ViewContent", { content_name: $("b", c).textContent, content_category: "Film" });
      });
    });
    $$(".js-wawork", track).forEach(function (a) { a.addEventListener("click", function () { track_("Contact", { content_name: "Film" }); }); });
    carousel($(".car", sec), function (i, cards) { stopAll(cards[i]); });
  }

  function renderInsta(posts) {
    var sec = $("#instagram");
    sec.hidden = posts.length === 0;
    if (!posts.length) return;
    var handle = (S.instagram || "").match(/instagram\.com\/([^/?#]+)/);
    if (handle) $$(".js-ighandle").forEach(function (el) { el.textContent = "@" + handle[1]; });
    var track = $(".car-track", sec);
    track.innerHTML = posts.map(function (w, k) {
      var reel = w.lid.indexOf("reel/") === 0;
      return '<div class="card-c ig-card rv' + (reel ? " reel" : "") + '" role="listitem" style="--d:' + Math.min(k, 3) + '" data-src="https://www.instagram.com/' +
        esc(w.lid) + '/embed/">' + '<div class="ig-skel"><span class="ig-ring"><svg viewBox="0 0 24 24"><use href="#i-ig"/></svg></span><b>' +
        esc(w.title) + "</b><i></i><i></i></div>" +
        '<a class="ig-open" href="https://www.instagram.com/' + esc(w.lid) + '/" target="_blank" rel="noopener">Open on Instagram ↗</a></div>';
    }).join("");
    // load an embed only when its card comes near the screen
    var load = function (c) {
      if (c._l) return;
      c._l = 1;
      var f = document.createElement("iframe");
      f.src = c.getAttribute("data-src");
      f.loading = "lazy";
      f.setAttribute("scrolling", "no");
      f.setAttribute("allowtransparency", "true");
      f.title = "Instagram post";
      f.onload = function () { c.classList.add("ld"); };
      c.insertBefore(f, c.firstChild);
    };
    var cards = $$(".ig-card", track);
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (es) {
        es.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); load(e.target); } });
      }, { rootMargin: "300px 600px" });
      cards.forEach(function (c) { io.observe(c); });
    } else cards.forEach(load);
    carousel($(".car", sec));
  }

  function filter(cat) {
    current = cat;
    $$(".filters button").forEach(function (b) { b.setAttribute("aria-selected", String(b.textContent === cat)); });
    var tiles = $$(".masonry .tile");
    tiles.forEach(function (t) { t.classList.add("out"); });
    setTimeout(function () {
      tiles.forEach(function (t) {
        var show = cat === "All" || t.getAttribute("data-cat") === cat;
        t.classList.toggle("gone", !show);
        t.classList.add("in");
      });
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        tiles.forEach(function (t) { if (!t.classList.contains("gone")) t.classList.remove("out"); });
      }); });
      lazyImages();
    }, reduce ? 0 : 260);
  }

  function lazyImages() {
    var imgs = $$("img[data-src]");
    var load = function (img) {
      var s = img.getAttribute("data-src");
      img.removeAttribute("data-src");
      img.onload = function () { img.classList.add("ld"); };
      img.src = s;
    };
    if (!("IntersectionObserver" in window)) { imgs.forEach(load); return; }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); load(e.target); } });
    }, { rootMargin: "400px 0px" });
    imgs.forEach(function (i) { io.observe(i); });
  }

  // ------------------------------------------------------------ viewer
  var lb = $(".lb"), stage = $(".lb-stage"), items = [], pos = 0, lbWork = null;

  function openViewer(w, start) {
    lbWork = w;
    items = [];
    if (w.kind === "youtube" && w.lid) items.push({ t: "yt", id: w.lid });
    if (w.kind === "instagram" && w.lid) items.push({ t: "ig", id: w.lid });
    (w.photos || []).forEach(function (p) { items.push({ t: "img", src: p.src }); });
    if (!items.length && w.link) { window.open(w.link, "_blank", "noopener"); return; }
    if (!items.length && w.cover) items.push({ t: "img", src: w.cover.src });
    $(".lb-title").textContent = w.title;
    $(".lb-meta").textContent = [w.category, metaLine(w), w.about].filter(Boolean).join(" · ");
    var wa = $(".lb-wa");
    wa.href = waLink(S.wa_work.replace(/\{work\}/g, w.title));
    lb.hidden = false;
    document.documentElement.style.overflow = "hidden";
    requestAnimationFrame(function () { lb.classList.add("open"); });
    show(start || 0);
    track_("ViewContent", { content_name: w.title, content_category: w.category || "Showreel" });
  }
  function show(i) {
    pos = (i + items.length) % items.length;
    var it = items[pos];
    stage.innerHTML = it.t === "yt"
      ? '<iframe src="https://www.youtube-nocookie.com/embed/' + encodeURIComponent(it.id) + '?autoplay=1&rel=0&modestbranding=1&playsinline=1" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen title="Video"></iframe>'
      : it.t === "ig"
      ? '<iframe class="ig" src="https://www.instagram.com/' + it.id + '/embed/captioned/" allowtransparency="true" allow="encrypted-media" scrolling="no" title="Instagram post"></iframe>'
      : '<img src="' + esc(it.src) + '" alt="">';
    $(".lb-count").textContent = items.length > 1 ? (pos + 1) + " / " + items.length : "";
    $$(".lb-nav").forEach(function (b) { b.hidden = items.length < 2; });
    var nx = items[pos + 1];
    if (nx && nx.t === "img") { var p = new Image(); p.src = nx.src; }
  }
  function closeViewer() {
    lb.classList.remove("open");
    stage.innerHTML = "";
    setTimeout(function () { lb.hidden = true; }, 200);
    document.documentElement.style.overflow = "";
  }
  $(".lb-x").addEventListener("click", closeViewer);
  $(".lb-prev").addEventListener("click", function () { show(pos - 1); });
  $(".lb-next").addEventListener("click", function () { show(pos + 1); });
  lb.addEventListener("click", function (e) { if (e.target === lb || e.target === stage) closeViewer(); });
  $(".lb-wa").addEventListener("click", function () { track_("Contact", { content_name: lbWork ? lbWork.title : "" }); });
  document.addEventListener("keydown", function (e) {
    if (lb.hidden) return;
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowRight") show(pos + 1);
    else if (e.key === "ArrowLeft") show(pos - 1);
  });
  var sx = null;
  stage.addEventListener("touchstart", function (e) { sx = e.touches[0].clientX; }, { passive: true });
  stage.addEventListener("touchend", function (e) {
    if (sx == null || items.length < 2) return;
    var dx = e.changedTouches[0].clientX - sx;
    if (Math.abs(dx) > 50) show(pos + (dx < 0 ? 1 : -1));
    sx = null;
  });

  // ------------------------------------------------------------ motion
  var revealIO = ("IntersectionObserver" in window && !reduce) ? new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      if (!e.isIntersecting) return;
      revealIO.unobserve(e.target);
      e.target.classList.add("in");
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.06 }) : null;
  function reveal() {
    $$(".rv:not(.in)").forEach(function (el, k) {
      if (!el.style.getPropertyValue("--d") && el.classList.contains("tile")) el.style.setProperty("--d", k % 3);
      if (revealIO) revealIO.observe(el); else el.classList.add("in");
    });
  }
  function motion() {
    var nav = $(".mnav"), photo = $(".portrait-frame img"), ticking = false;
    function onScroll() {
      nav.classList.toggle("solid", window.scrollY > 40);
      if (photo && !reduce) {
        var r = photo.getBoundingClientRect();
        if (r.bottom > 0 && r.top < innerHeight) photo.style.setProperty("--py", ((r.top + r.height / 2 - innerHeight / 2) * -0.035).toFixed(1) + "px");
      }
      ticking = false;
    }
    window.addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
    onScroll();
    document.addEventListener("pointermove", function (e) {
      var c = e.target.closest && e.target.closest(".svc");
      if (!c) return;
      var r = c.getBoundingClientRect();
      c.style.setProperty("--mx", (e.clientX - r.left) + "px");
      c.style.setProperty("--my", (e.clientY - r.top) + "px");
    });
    var steps = $(".steps");
    if ("IntersectionObserver" in window && !reduce) {
      var so = new IntersectionObserver(function (es) { if (es[0].isIntersecting) { steps.classList.add("drawn"); so.disconnect(); } }, { threshold: 0.3 });
      so.observe(steps);
    } else steps.classList.add("drawn");
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".faq-item button");
      if (!b) return;
      var it = b.parentNode, open = !it.classList.contains("open");
      it.classList.toggle("open", open);
      b.setAttribute("aria-expanded", String(open));
    });
  }

  // ------------------------------------------------------------ booking form
  function form() {
    var f = $(".book-form"), msg = $(".form-msg");
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var d = new FormData(f), name = (d.get("name") || "").trim(), phone = (d.get("phone") || "").trim();
      if (!name || digits(phone).length < 10) { msg.textContent = "Please add your name and WhatsApp number."; return; }
      var when = d.get("date") ? new Date(d.get("date") + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "long", year: "numeric" }) : "";
      if (when) d.set("date", when);
      var text = "Hi Azeez, I'd like to book a shoot with Realview Media Production.\n\nName: " + name + "\nEvent: " + d.get("event") +
        "\nDate: " + (when || "-") + "\nLocation: " + ((d.get("place") || "").trim() || "-") +
        ((d.get("message") || "").trim() ? "\n\n" + d.get("message").trim() : "");
      var win = window.open(waLink(text), "_blank", "noopener");
      if (!win) location.href = waLink(text);
      msg.textContent = "Opening WhatsApp… just tap Send. Thank you!";
      track_("Lead", { content_name: "Media Production booking", content_category: d.get("event") });
      try { fetch(CRM + "/mp/enquiry", { method: "POST", body: new URLSearchParams(d), mode: "cors", credentials: "omit" }); } catch (err) { /* WhatsApp is enough */ }
      f.reset();
    });
  }

  // ------------------------------------------------------------ start
  var DATA = { settings: {}, works: [] };
  splitTitle();
  wireWa();
  motion();
  form();
  reveal();
  var y = $("#year"); if (y) y.textContent = new Date().getFullYear();
  fetch(CRM + "/mp/data.json", { credentials: "omit" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d) { renderWorks(); return; }
      DATA = d; works = d.works || [];
      applySettings(d.settings || {});
      renderWorks();
    })
    .catch(function () { renderWorks(); });
})();
