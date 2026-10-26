// Swipe: the ✕ "why not?" sheet on every card, and the one-card deck on /swipe.
(() => {
  const hdr = { "x-requested-with": "fetch" };
  const go = (r) => { if (r.status === 401) location.href = "/login"; return r; };
  const post = (url) => fetch(url, { method: "POST", headers: hdr }).then(go);
  const enc = encodeURIComponent;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const SEEN = { loved: "❤️ Loved", ok: "🙂 OK", meh: "👎 Meh" };
  const REASONS = { slow: "🐢 Too slow", lang: "🗣️ Not my language", genre: "🎭 Not my genre",
                    enough: "🔁 Seen enough like it", no: "✋ Just no" };

  // ---------- bottom sheet → resolves with a choice key, or null ----------
  let sheetOpen = false;
  function sheet(title, withReasons) {
    return new Promise((done) => {
      const el = document.createElement("div");
      el.className = "sw-sheet";
      const btns = (o, cls) => Object.entries(o).map(([k, v]) => `<button class="${cls}" data-pick="${k}">${v}</button>`).join("");
      el.innerHTML = `<div class="sw-sheet-in" role="dialog" aria-label="Not interested">
        <header><b>${esc(title)}</b><button class="tx-x" data-pick="" aria-label="Close">✕</button></header>
        <h4>Seen it →</h4><div class="sw-seen">${btns(SEEN, "btn")}</div>
        ${withReasons ? `<h4>Not interested because…</h4><div class="sw-why">${btns(REASONS, "sw-chip")}</div>` : ""}
      </div>`;
      const close = (v) => { el.remove(); sheetOpen = false; document.removeEventListener("keydown", onKey); done(v); };
      const onKey = (e) => { if (e.key === "Escape") close(null); };
      el.addEventListener("click", (e) => {
        const b = e.target.closest("[data-pick]");
        if (b) close(b.dataset.pick || null);
        else if (e.target === el) close(null);
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(el);
      sheetOpen = true;
      el.querySelector(".sw-seen .btn").focus();
    });
  }

  // ---------- ✕ on cards: capture phase, so app.js's one-tap dislike never runs ----------
  window.addEventListener("click", async (e) => {
    const undo = e.target.closest(".hidden-card [data-act='undo']");
    if (undo) {                             // app.js does the real undo; we just forget the reason
      post(`/swipe/reason/${enc(undo.closest(".hidden-card").dataset.key)}?r=`);
      return;
    }
    const su = e.target.closest("[data-sw-undo]");
    if (su) {
      const ph = su.closest(".hidden-card"), key = ph.dataset.key;
      su.disabled = true;
      await post(`/swipe/undo/${enc(key)}`);
      ph.outerHTML = await fetch(`/card/${enc(key)}`, { headers: hdr }).then((r) => r.text());
      return;
    }
    const b = e.target.closest("[data-act='dislike']");
    const card = b && b.closest(".card");
    if (!card || !card.dataset.key) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const key = card.dataset.key;
    const pick = await sheet(card.querySelector("h3")?.textContent || "", true);
    if (!pick) return;
    if (SEEN[pick]) {                       // seen ≠ disliked: a rating (feeds taste), card just leaves
      await post(`/swipe/act/${pick}/${enc(key)}`);
      card.outerHTML = `<div class="hidden-card" data-key="${esc(key)}">Seen · ${SEEN[pick]}
        <button class="btn" data-sw-undo>Undo</button></div>`;
    } else {
      await post(`/swipe/reason/${enc(key)}?r=${pick}`);
      await post(`/act/dislike/${enc(key)}`);
      card.outerHTML = `<div class="hidden-card" data-key="${esc(key)}">Hidden — won't suggest this again
        <button class="btn" data-act="undo">Undo</button></div>`;      // same markup as app.js
    }
  }, true);

  // ---------- the deck ----------
  const root = document.getElementById("sw");
  if (!root) return;
  const stack = document.getElementById("sw-stack"), btnRow = document.getElementById("sw-btns");
  const undoBtn = document.getElementById("sw-undo"), todayEl = document.getElementById("sw-today");
  const kind = root.dataset.kind;
  const cards = JSON.parse(document.getElementById("sw-data").textContent);
  const loaded = new Set(cards.map((c) => c.key));
  const hist = [];
  let i = 0, busy = false, loading = false, ended = false;

  // picks: 👍 / ✕ / + Want; your own titles: Loved / Meh / OK
  const MAP = { pick: { right: "like", left: "dislike", up: "want" }, lib: { right: "loved", left: "meh", up: "ok" } };
  const STAMP = { like: "👍 Like", dislike: "✕ Nope", want: "+ Want", loved: "❤️ Loved", meh: "👎 Meh", ok: "🙂 OK" };
  const cur = () => cards[i];
  const poster = (c, w = "w342") => (c && c.poster ? `/img/${w}${c.poster}` : "");

  function cardHTML(c) {
    const meta = [c.year, c.kind === "tv" ? "Show" : "", c.genres.join(", ")].filter(Boolean).join(" · ");
    const m = MAP[c.lib ? "lib" : "pick"];
    return `${c.poster ? `<img src="${poster(c)}" alt="" draggable="false">` : `<div class="noposter">${esc(c.title)}</div>`}
      ${c.lib ? `<span class="sw-lib">In your library</span>` : ""}${c.rating ? `<span class="rate">★ ${c.rating.toFixed(1)}</span>` : ""}
      <i class="sw-stamp r">${STAMP[m.right]}</i><i class="sw-stamp l">${STAMP[m.left]}</i><i class="sw-stamp u">${STAMP[m.up]}</i>
      <div class="sw-info"><h2>${esc(c.title)}</h2><div class="meta">${esc(meta)}</div>${c.why ? `<p class="why">${esc(c.why)}</p>` : ""}</div>`;
  }

  function render() {
    stack.innerHTML = "";
    const c = cur();
    if (!c) {
      btnRow.innerHTML = "";
      stack.innerHTML = loading ? `<p class="sw-done"><span class="spin"></span></p>`
        : `<p class="sw-done">All caught up 🎉<br><a href="/${kind === "tv" ? "shows" : "movies"}">Back to picks</a></p>`;
      return;
    }
    if (cards[i + 1]) {
      const n = document.createElement("div");
      n.className = "sw-card next";
      n.innerHTML = cardHTML(cards[i + 1]);
      stack.appendChild(n);
    }
    const el = document.createElement("div");
    el.className = "sw-card top";
    el.innerHTML = cardHTML(c);
    stack.appendChild(el);
    drag(el);
    btnRow.innerHTML = c.lib
      ? `<button class="sw-b no" data-sw="left" title="Meh (←)">👎<small>Meh</small></button>
         <button class="sw-b" data-sw="up" title="OK (↑)">🙂<small>OK</small></button>
         <button class="sw-b yes" data-sw="right" title="Loved (→)">❤️<small>Loved</small></button>`
      : `<button class="sw-b no" data-sw="left" title="Not interested (←)">✕<small>Nope</small></button>
         <button class="sw-b" data-sw="seen" title="Seen it (S)">👁<small>Seen it</small></button>
         <button class="sw-b want" data-sw="up" title="Want (↑)">＋<small>Want</small></button>
         <button class="sw-b yes" data-sw="right" title="Like (→)">👍<small>Like</small></button>`;
    for (const k of [i + 1, i + 2]) if (cards[k]?.poster) new Image().src = poster(cards[k]);   // preload
    undoBtn.disabled = !hist.length;
    if (cards.length - i < 6) refill();
  }

  async function refill() {
    if (loading || ended) return;
    loading = true;
    try {
      const r = await fetch(`/swipe/deck?kind=${kind}&skip=${enc([...loaded].join(","))}`, { headers: hdr }).then(go);
      const d = await r.json();
      const fresh = d.cards.filter((c) => !loaded.has(c.key));
      fresh.forEach((c) => loaded.add(c.key));
      if (!fresh.length) ended = true;
      const wasEmpty = !cur();
      cards.push(...fresh);
      if (wasEmpty || !cards[i + 1]) { loading = false; render(); }
    } catch { ended = true; }
    loading = false;
  }

  async function send(c, action) {
    if (action === "want") await post(`/act/want/${enc(c.key)}`);    // the normal want flow (defaults + list sync)
    const r = await post(`/swipe/act/${action}/${enc(c.key)}`);
    if (r.ok) todayEl.textContent = (await r.json()).today;
  }

  async function commit(action, dir) {
    const c = cur();
    if (!c || busy) return;
    busy = true;
    const el = stack.querySelector(".sw-card.top");
    if (el) {
      const [x, y] = { right: [1, 0], left: [-1, 0], up: [0, -1], down: [0, 1] }[dir] || [0, 1];
      el.style.transition = "transform .25s ease-in, opacity .25s";
      el.style.transform = `translate(${x * 130}vw, ${y * 110}vh) rotate(${x * 25}deg)`;
      el.style.opacity = "0";
    }
    hist.push({ c, action });
    send(c, action).catch(() => {});
    await new Promise((r) => setTimeout(r, 200));
    i++;
    busy = false;
    render();
  }

  async function act(dir) {
    const c = cur();
    if (!c || busy || sheetOpen) return;
    if (dir === "seen") {
      const pick = await sheet(c.title, false);
      if (pick) commit(pick, pick === "meh" ? "left" : "right");
      return;
    }
    if (dir === "skip") return commit("skip", "down");
    commit(MAP[c.lib ? "lib" : "pick"][dir], dir);
  }

  async function undo() {
    const h = hist.pop();
    if (!h || busy) return;
    const r = await post(`/swipe/undo/${enc(h.c.key)}`);
    if (r.ok) todayEl.textContent = (await r.json()).today;
    cards.splice(i, 0, h.c);
    render();
  }

  // Pointer drag (touch + mouse). Horizontal/up past ~90px commits; otherwise it springs back.
  function drag(el) {
    let sx = 0, sy = 0, dx = 0, dy = 0, on = false;
    const stamps = { r: el.querySelector(".sw-stamp.r"), l: el.querySelector(".sw-stamp.l"), u: el.querySelector(".sw-stamp.u") };
    el.addEventListener("pointerdown", (e) => {
      if (busy || e.button > 0) return;
      on = true; sx = e.clientX; sy = e.clientY; dx = dy = 0;
      el.setPointerCapture(e.pointerId);
      el.style.transition = "none";
    });
    el.addEventListener("pointermove", (e) => {
      if (!on) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      el.style.transform = `translate(${dx}px, ${Math.min(dy, 40)}px) rotate(${dx / 18}deg)`;
      const up = -dy > Math.abs(dx);
      stamps.r.style.opacity = !up ? Math.max(0, dx / 90) : 0;
      stamps.l.style.opacity = !up ? Math.max(0, -dx / 90) : 0;
      stamps.u.style.opacity = up ? Math.max(0, -dy / 90) : 0;
    });
    const end = () => {
      if (!on) return;
      on = false;
      const up = -dy > Math.abs(dx);
      if (up && -dy > 90) return act("up");
      if (!up && Math.abs(dx) > 90) return act(dx > 0 ? "right" : "left");
      el.style.transition = "transform .2s";
      el.style.transform = "";
      Object.values(stamps).forEach((s) => (s.style.opacity = 0));
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  }

  btnRow.addEventListener("click", (e) => { const b = e.target.closest("[data-sw]"); if (b) act(b.dataset.sw); });
  undoBtn.addEventListener("click", undo);
  document.getElementById("sw-skip").addEventListener("click", () => act("skip"));
  document.addEventListener("keydown", (e) => {
    if (sheetOpen || e.ctrlKey || e.metaKey || e.altKey || /input|textarea/i.test(e.target.tagName)) return;
    const k = { ArrowRight: "right", ArrowLeft: "left", ArrowUp: "up", s: "seen", " ": "skip", ArrowDown: "skip" }[e.key];
    if (k) { e.preventDefault(); if (k !== "seen" || !cur()?.lib) act(k); }
    else if (e.key === "u" || e.key === "Backspace") { e.preventDefault(); undo(); }
  });
  render();
})();
