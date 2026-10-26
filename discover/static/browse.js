// Give each card an id so the Hidden-gems strip can jump to it (#c-movie:123) and :target highlights it.
const bzIds = () => document.querySelectorAll(".card[data-key]").forEach((c) => { c.id = "c-" + c.dataset.key; });
bzIds();
if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
// Cards get re-rendered after + Want / 👍 (outerHTML swap) — re-tag them.
new MutationObserver(bzIds).observe(document.querySelector("main") || document.body, { childList: true, subtree: true });
