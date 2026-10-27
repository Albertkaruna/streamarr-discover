// Release radar: Notify toggle, dismiss the "Arrived" banner, jump to a tile's card on /coming.
(() => {
  const H = { "x-requested-with": "fetch" };
  const post = (u) => fetch(u, { method: "POST", headers: H }).then((r) => {
    if (r.status === 401) location.href = "/login";
    return r.json();
  });
  document.addEventListener("click", async (e) => {
    const n = e.target.closest("[data-radar-notify]");
    if (n) {
      n.disabled = true;
      const j = await post(`/radar/notify/${encodeURIComponent(n.dataset.radarNotify)}`).catch(() => ({}));
      n.disabled = false;
      if (j.ok) { n.classList.toggle("on", j.on); n.lastChild.nodeValue = j.on ? "Notifying" : "Notify me"; }
      return;
    }
    const d = e.target.closest("[data-radar-dismiss]");
    if (d) { d.closest(".rd-arrived").remove(); post("/radar/dismiss"); }
  });
  // Tiles link to /coming#c-movie:123 — cards carry data-key, not ids (an id per card would clash on other pages)
  const go = () => {
    const k = decodeURIComponent(location.hash.slice(3));
    if (!location.hash.startsWith("#c-") || !k) return;
    const c = [...document.querySelectorAll(".card")].find((x) => x.dataset.key === k);
    if (c) { c.classList.add("rd-hit"); c.scrollIntoView({ block: "center" }); }
  };
  go();
  addEventListener("hashchange", go);
})();
