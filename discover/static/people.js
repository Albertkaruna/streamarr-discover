// Person pages: tap the bio to expand; "Open" on a light row fetches the full card (so + Want works) into the grid.
(() => {
  document.addEventListener("click", async (e) => {
    const bio = e.target.closest("[data-pp-bio]");
    if (bio) { bio.classList.toggle("open"); return; }
    const b = e.target.closest("[data-pp-load]");
    if (!b) return;
    b.disabled = true;
    const r = await fetch(`/person/card/${encodeURIComponent(b.dataset.ppLoad)}?why=${encodeURIComponent(b.dataset.why || "")}`,
      { headers: { "x-requested-with": "fetch" } });
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) { b.textContent = "Not found"; return; }
    const sec = b.closest(".pp-sec");
    let grid = sec.querySelector(".pp-grid");
    if (!grid) {                       // section had no cards yet → make the grid above the rows
      grid = document.createElement("div");
      grid.className = "grid pp-grid";
      sec.querySelector(".pp-rows").before(grid);
    }
    grid.insertAdjacentHTML("beforeend", await r.text());
    const li = b.closest("li"), ol = li.parentElement;
    li.remove();
    if (!ol.children.length) ol.remove();
    grid.lastElementChild.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
})();
