// Music tab: 30 s previews, "+ Tunarr" (copies the search text, Tunarr opens in a new tab), ✕ with Undo, show more.
(() => {
  if (!document.querySelector(".mu")) return;
  const send = (url) => fetch(url, { method: "POST", headers: { "x-requested-with": "fetch" } }).then((r) => {
    if (r.status === 401) location.href = "/login";
    return r.json();
  });
  const toast = (msg) => {
    const t = document.getElementById("mu-toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500);
  };
  const copy = (text) => {   // http://pc.lan isn't a secure context, so navigator.clipboard may be missing
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).catch(() => {});
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (e) { /* best effort */ }
    ta.remove();
  };
  const audio = new Audio();
  let playing = null;
  audio.addEventListener("ended", () => playing && playing.classList.remove("playing"));
  const saved = new Map();   // hidden rows, for Undo

  document.addEventListener("click", async (e) => {
    const pv = e.target.closest("[data-preview]");
    if (pv) {
      if (playing === pv && !audio.paused) { audio.pause(); pv.classList.remove("playing"); return; }
      if (playing) playing.classList.remove("playing");
      audio.src = pv.dataset.preview; audio.play().catch(() => toast("Preview unavailable"));
      pv.classList.add("playing"); playing = pv;
      return;
    }
    const tn = e.target.closest(".mu-tunarr");
    if (tn) { copy(tn.dataset.q); return; }   // Tunarr runs ?q= itself; the copy is just a backup
    const no = e.target.closest("[data-mu-nope]");
    if (no) {
      const row = no.closest("[data-kind]"), d = row.dataset;
      no.disabled = true;
      const q = new URLSearchParams({ kind: d.kind, artist: d.artist, title: d.title || "" });
      const r = await send(`/music/nope?${q}`);
      if (!r.ok) { no.disabled = false; return; }
      saved.set(r.key, row.outerHTML);
      const what = d.kind === "artist" ? "Hidden — no more from this artist" : "Hidden — won't suggest it again";
      row.outerHTML = `<div class="mu-hidden" data-key="${r.key}">${what} <button class="btn" data-mu-undo>Undo</button></div>`;
      return;
    }
    const un = e.target.closest("[data-mu-undo]");
    if (un) {
      const box = un.closest(".mu-hidden"), key = box.dataset.key;
      await send(`/music/undo?${new URLSearchParams({ key })}`);
      box.outerHTML = saved.get(key) || "";
      return;
    }
    const more = e.target.closest("[data-mu-more]");
    if (more) { document.getElementById("mu-songs").classList.add("all"); more.remove(); }
  });
})();
