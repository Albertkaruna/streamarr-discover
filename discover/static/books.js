// Book buttons (Books tab + search results): own endpoint, since books aren't TMDB items.
(() => {
  const saved = new Map();
  const ICON = {ebook: "📖", audiobook: "🎧", both: "📖🎧"};
  const tpl = document.getElementById("bk-want-tpl");
  const WANT = tpl ? tpl.innerHTML.trim() : "";
  const note = document.getElementById("bk-note");
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-bact]");
    if (!b) return;
    const el = b.closest("[data-bid]"), id = el.dataset.bid, act = b.dataset.bact;
    b.disabled = true;
    const r = await fetch(`/books/act/${act}/${encodeURIComponent(id)}`, {method: "POST",
      headers: {"x-requested-with": "fetch", "content-type": "application/json"},
      body: JSON.stringify({...JSON.parse(el.dataset.book || "{}"), media: b.dataset.media || "ebook"})});
    if (r.status === 401) { location.href = "/login"; return; }
    const res = await r.json();
    const wn = document.getElementById("bk-wn");
    if (wn) wn.textContent = res.wants;
    if (act === "dislike") {
      saved.set(id, el.outerHTML);
      el.outerHTML = `<div class="hidden-card" data-bid="${id}">Hidden — won't suggest this again
        <button class="btn" data-bact="undo">Undo</button></div>`;
    } else if (act === "undo") {
      el.outerHTML = saved.get(id) || "";
    } else if (el.tagName === "LI") {
      el.remove();
      document.querySelectorAll(`.card[data-bid="${id}"] [data-bact="unwant"]`).forEach((x) => { x.outerHTML = WANT; });
    } else {
      const on = act === "want";
      el.classList.toggle("wanted", on);
      const row = b.closest(".bk-wantrow") || b;
      row.outerHTML = on ? `<button class="btn on" data-bact="unwant" title="Remove from wanted">✓ Wanted ${ICON[b.dataset.media] || ""}</button>`
                         : WANT;
      if (on && note) { note.hidden = false; note.classList.add("flash"); setTimeout(() => note.classList.remove("flash"), 1200); }
    }
  });
})();
