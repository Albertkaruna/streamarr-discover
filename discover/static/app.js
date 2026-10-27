// Buttons: post the action, then swap in the re-rendered card. ✕ leaves an "Undo" placeholder.
const post = (url) => fetch(url, { method: "POST", headers: { "x-requested-with": "fetch" } }).then((r) => {
  if (r.status === 401) location.href = "/login";
  return r;
});

async function onAct(b) {
  const card = b.closest(".card, .hidden-card");
  const key = card.dataset.key, act = b.dataset.act;
  b.disabled = true;
  await post(`/act/${act}/${encodeURIComponent(key)}`);
  if (act === "dislike") {
    card.outerHTML = `<div class="hidden-card" data-key="${key}"><span>Hidden — won't suggest this again</span>
      <button class="btn sm" data-act="undo">Undo</button></div>`;
  } else if (act === "unwant" && location.pathname === "/wanted") {
    card.remove();
  } else {
    card.outerHTML = await fetch(`/card/${encodeURIComponent(key)}`).then((r) => r.text());
  }
}

document.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]");
  if (act) return onAct(act);
  const rail = e.target.closest("[data-rail]");
  if (rail) {
    const row = rail.closest(".sec").querySelector(".rail");
    return row.scrollBy({ left: Number(rail.dataset.rail) * row.clientWidth * 0.85, behavior: "smooth" });
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
