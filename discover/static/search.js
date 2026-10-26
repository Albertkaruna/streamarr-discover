// Search as you type (debounced) — swaps only the results, keeps the URL shareable.
(() => {
  const box = document.getElementById("sr-q"), out = document.getElementById("sr-results");
  if (!box || !out) return;
  const kind = document.getElementById("sr-type");
  let timer, seq = 0;
  const run = async () => {
    const q = box.value.trim(), t = kind.value, my = ++seq;
    history.replaceState(null, "", `/search?q=${encodeURIComponent(q)}&type=${t}`);
    document.querySelectorAll(".sr-kinds .chip").forEach((a) => (a.href = `?q=${encodeURIComponent(q)}&type=${a.dataset.kind}`));
    out.classList.add("loading");
    const r = await fetch(`/search?q=${encodeURIComponent(q)}&type=${t}&partial=1`, { headers: { "x-requested-with": "fetch" } });
    if (r.status === 401) { location.href = "/login"; return; }
    if (my === seq) { out.innerHTML = await r.text(); out.classList.remove("loading"); }   // ignore late, stale answers
  };
  box.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 450); });
  box.form.addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(timer); run(); box.blur(); });
})();
