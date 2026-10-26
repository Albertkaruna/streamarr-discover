// Buttons: post the action, then swap in the re-rendered card. ✕ leaves an "Undo" placeholder.
const post = (url) => fetch(url, { method: "POST", headers: { "x-requested-with": "fetch" } }).then((r) => {
  if (r.status === 401) location.href = "/login";
  return r;
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (b) {
    const card = b.closest(".card, .hidden-card");
    const key = card.dataset.key, act = b.dataset.act;
    b.disabled = true;
    await post(`/act/${act}/${encodeURIComponent(key)}`);
    if (act === "dislike") {
      card.outerHTML = `<div class="hidden-card" data-key="${key}">Hidden — won't suggest this again
        <button class="btn" data-act="undo">Undo</button></div>`;
    } else if (act === "unwant" && location.pathname === "/wanted") {
      card.remove();
    } else {
      const html = await fetch(`/card/${encodeURIComponent(key)}`).then((r) => r.text());
      card.outerHTML = html;
    }
    return;
  }
  if (e.target.closest("[data-refresh]")) {
    await post("/refresh");
    location.reload();
  }
});

// While a refresh runs, show its progress and reload when it's done.
const st = document.getElementById("status");
if (st && st.dataset.running === "1") {
  const tick = setInterval(async () => {
    const s = await fetch("/api/status").then((r) => r.json());
    if (!s.running) { clearInterval(tick); location.reload(); }
    else document.getElementById("step").textContent = s.step + "…";
  }, 3000);
}
