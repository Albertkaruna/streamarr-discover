// Taste: one-tap library ratings (tap the active one again to clear) and the "?" explain sheet on cards.
(() => {
  const hdr = { "x-requested-with": "fetch" };
  const go = (r) => { if (r.status === 401) location.href = "/login"; return r; };

  const close = () => document.querySelector(".tx-sheet")?.remove();

  document.addEventListener("click", async (e) => {
    const rb = e.target.closest("[data-rate]");
    if (rb) {
      const tile = rb.closest(".tx-tile");
      const r = tile.dataset.r === rb.dataset.rate ? "" : rb.dataset.rate;
      rb.disabled = true;
      const res = await fetch(`/rate/${encodeURIComponent(tile.dataset.key)}?r=${r}`, { method: "POST", headers: hdr }).then(go);
      rb.disabled = false;
      if (!res.ok) return;
      const d = await res.json();
      tile.dataset.r = d.r || "";
      tile.classList.toggle("rated", !!d.r);
      tile.querySelectorAll("[data-rate]").forEach((b) => b.classList.toggle("on", b.dataset.rate === d.r));
      const p = document.getElementById("tx-progress");
      if (p) {
        p.querySelector("b").textContent = d.rated;
        p.querySelector("i").style.width = (d.total ? (100 * d.rated) / d.total : 0) + "%";
      }
      return;
    }
    if (e.target.closest("[data-explain]")) {
      const key = e.target.closest("[data-key]").dataset.key;
      const res = await fetch(`/explain/${encodeURIComponent(key)}`, { headers: hdr }).then(go);
      if (!res.ok) return;
      close();
      const sheet = document.createElement("div");
      sheet.className = "tx-sheet";
      sheet.innerHTML = await res.text();
      document.body.appendChild(sheet);
      sheet.querySelector(".tx-x")?.focus();
      return;
    }
    if (e.target.closest("[data-tx-close]") || e.target.classList.contains("tx-sheet")) close();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();
