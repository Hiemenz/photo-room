// ---- Camera card banner -------------------------------------------------------------
// `photo-room watch-cards` imports a card in its own process and writes progress to a
// file in the library. This polls /api/cards/status and shows it. When an import ends the
// server rescans the library, so the new photos arrive through the live grid feed.
const cardUi = { shown: null, dismissed: null, timer: null };
const CARD_TITLES = {
  detected: "Card found", scanning: "Reading the card", importing: "Importing from card",
  verifying: "Verifying the copy", deleting: "Removing verified photos from the card",
  ejecting: "Ejecting the card", done: "Card imported", done_with_errors: "Card needs attention",
  error: "Card import stopped", stale: "Card watcher stopped",
};

function cardPaint(d) {
  const panel = $("card-panel");
  const live = d.state && d.state !== "idle";
  if (!live || cardUi.dismissed === d.id || (d.updated && Date.now() / 1000 - d.updated > 600 && !["detected", "scanning", "importing", "verifying", "deleting", "ejecting"].includes(d.state))) {
    panel.hidden = true; return;
  }
  const finished = ["done", "done_with_errors", "error", "stale"].includes(d.state);
  panel.hidden = false;
  panel.classList.toggle("bad", ["done_with_errors", "error", "stale"].includes(d.state));
  $("card-title").textContent = `${CARD_TITLES[d.state] || "Card"}${d.label ? `: ${d.label}` : ""}`;
  $("card-count").textContent = d.total ? `${d.done || 0} of ${d.total}` : "";
  const bar = $("card-bar");
  bar.hidden = finished;
  bar.max = Math.max(1, d.total || 1); bar.value = d.done || 0;
  $("card-msg").textContent = finished ? (d.message || "") :
    [d.name, d.copied !== undefined ? `${d.copied} copied, ${d.duplicates || 0} already in library, ${d.failed || 0} failed` : ""].filter(Boolean).join(" - ");
  $("card-close").hidden = !finished;
  cardUi.shown = d.id;
}

async function cardPoll() {
  try {
    const r = await fetch("/api/cards/status", { cache: "no-store" });
    if (r.ok) cardPaint(await r.json());
  } catch (e) { /* editor restarting; try again */ }
  cardUi.timer = setTimeout(cardPoll, 2000);
}
$("card-close").addEventListener("click", () => { cardUi.dismissed = cardUi.shown; $("card-panel").hidden = true; });
cardPoll();
