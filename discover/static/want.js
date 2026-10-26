// + Want options sheet (seasons · Tamil audio · quality), live download badges, ▶ trailer availability.
(() => {
  const H = { "x-requested-with": "fetch" };
  const get = (u) => fetch(u, { headers: H }).then((r) => {
    if (r.status === 401) location.href = "/login";
    return r.json();
  });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const gb = (x) => (Math.round(x * 10) / 10).toString();

  // ---------- sheet ----------
  let sheet = null;
  const close = () => { if (sheet) { sheet.remove(); sheet = null; document.body.classList.remove("wlock"); } };

  async function open(key, btn) {
    btn.disabled = true;
    let o;
    try { o = await get(`/want/options/${encodeURIComponent(key)}`); } finally { btn.disabled = false; }
    if (o.error) return;
    close();
    const tv = o.kind === "tv", ss = o.seasons || [];
    const chosen = new Set(o.opts.pick === "custom" ? o.opts.seasons
      : o.opts.pick === "latest" ? ss.slice(-1).map((s) => s.n)
      : o.opts.pick === "first" ? ss.slice(0, 1).map((s) => s.n) : ss.map((s) => s.n));
    let pick = o.opts.pick || "all";
    sheet = document.createElement("div");
    sheet.className = "wsheet";
    sheet.innerHTML = `<div class="wpanel" role="dialog" aria-modal="true" aria-label="Want options">
      <div class="wgrip"></div>
      <h4>${esc(o.title)}${o.year ? ` <span>${esc(o.year)}</span>` : ""}</h4>
      ${tv && ss.length ? `<div class="wsec"><div class="wrow"><b>Seasons</b>
          <span class="wquick"><button data-q="all">All</button><button data-q="latest">Latest</button><button data-q="first">First</button></span></div>
        <div class="wseasons">${ss.map((s) => `<label><input type="checkbox" value="${s.n}" ${chosen.has(s.n) ? "checked" : ""}>
          S${s.n}<small>${s.eps} ep${s.eps === 1 ? "" : "s"}</small></label>`).join("")}</div></div>` : ""}
      ${o.tamil_choice ? `<label class="wsec wtoggle"><input type="checkbox" name="tamil" ${o.opts.tamil ? "checked" : ""}>
        <span><b>Tamil audio if available</b><small>Prefers Tamil dubbed / multi-audio releases; falls back to the normal one.</small></span></label>` : ""}
      <div class="wsec"><div class="wrow"><b>Quality</b>
        <span class="wseg">${["1080", "720"].map((q) => `<label><input type="radio" name="q" value="${q}" ${o.opts.quality === q ? "checked" : ""}><span>${q}p</span></label>`).join("")}</span></div>
        <small class="west"></small></div>
      <div class="wbtns"><button class="btn" data-x>Cancel</button><button class="btn primary" data-ok>+ Want</button></div>
      <small class="wnote">Downloads run at full speed 22:00–09:00.</small></div>`;
    document.body.append(sheet);
    document.body.classList.add("wlock");
    const boxes = [...sheet.querySelectorAll(".wseasons input")];

    const est = () => {
      const q = sheet.querySelector("input[name=q]:checked")?.value || "1080";
      let t;
      if (!tv) t = `≈ ${gb(o.gb.movie[q])} GB`;
      else {
        const eps = boxes.length ? ss.filter((s, i) => boxes[i].checked).reduce((a, s) => a + s.eps, 0) : 0;
        t = `≈ ${gb(o.gb.ep[q])} GB/ep` + (eps ? ` × ${eps} eps ≈ ${gb(o.gb.ep[q] * eps)} GB` : "");
      }
      sheet.querySelector(".west").textContent = t;
      sheet.querySelectorAll(".wquick button").forEach((b) => b.classList.toggle("on", b.dataset.q === pick));
      const ok = sheet.querySelector("[data-ok]");
      ok.disabled = boxes.length > 0 && !boxes.some((b) => b.checked);
    };
    est();

    sheet.addEventListener("change", (e) => {
      if (e.target.closest(".wseasons")) {
        const n = boxes.filter((b) => b.checked).length;
        pick = n === boxes.length ? "all" : "custom";
      }
      est();
    });
    sheet.addEventListener("click", async (e) => {
      if (e.target === sheet || e.target.closest("[data-x]")) return close();
      const q = e.target.closest("[data-q]");
      if (q) {
        pick = q.dataset.q;
        boxes.forEach((b, i) => { b.checked = pick === "all" || (pick === "latest" ? i === boxes.length - 1 : i === 0); });
        return est();
      }
      const ok = e.target.closest("[data-ok]");
      if (!ok) return;
      ok.disabled = true;
      const body = {
        key, pick, seasons: boxes.filter((b) => b.checked).map((b) => +b.value),
        tamil: !!sheet.querySelector("input[name=tamil]")?.checked,
        quality: sheet.querySelector("input[name=q]:checked")?.value || "1080",
      };
      const r = await fetch("/want", { method: "POST", headers: { ...H, "content-type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 401) return (location.href = "/login");
      close();
      const card = document.querySelector(`.card[data-key="${CSS.escape(key)}"]`);
      if (card) {
        card.outerHTML = await fetch(`/card/${encodeURIComponent(key)}`).then((x) => x.text());
        status([key]);
      }
    });
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  // capture phase: runs before app.js's one-tap handler and stops it
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-want]");
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    open(b.dataset.want, b);
  }, true);

  // ---------- live badges + trailer availability ----------
  const keysOf = (sel) => [...new Set([...document.querySelectorAll(sel)].map((x) => x.dataset.wst || x.dataset.trailer))];
  async function status(keys) {
    for (let i = 0; i < keys.length; i += 100) {
      const st = await get(`/want/status?keys=${encodeURIComponent(keys.slice(i, i + 100).join(","))}`);
      for (const [k, d] of Object.entries(st)) {
        document.querySelectorAll(`[data-wst="${CSS.escape(k)}"]`).forEach((el) => {
          const b = el.querySelector(".wbadge");
          b.className = `wbadge ${d.state}`;
          b.textContent = d.label;
          el.querySelector(".wsize").textContent = d.size;
        });
      }
    }
  }
  async function trailers() {
    const keys = keysOf("[data-trailer]:not(.off)");
    for (let i = 0; i < keys.length; i += 60) {
      const t = await get(`/want/trailers?keys=${encodeURIComponent(keys.slice(i, i + 60).join(","))}`);
      for (const [k, ok] of Object.entries(t)) {
        if (!ok) document.querySelectorAll(`[data-trailer="${CSS.escape(k)}"]`).forEach((a) => a.classList.add("off"));
      }
    }
  }
  const keys = keysOf("[data-wst]");
  if (keys.length) status(keys).catch(() => {}).then(() => trailers().catch(() => {}));
})();
