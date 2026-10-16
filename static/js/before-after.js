// ---- Before / after ------------------------------------------------------
// Renders the unedited photo by asking for neutral values, so the comparison
// goes through exactly the same pipeline as the edit rather than a
// separately-cached image that might drift from it.
async function showOriginal() {
  if (!current || comparing) return;
  comparing = true;
  editedSrc = $("preview").src;
  $("compare-badge").classList.add("on");

  const res = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: current, adjustments: importValues || { ...neutral, monochrome: false } }),
  });
  const data = await res.json();
  // The key may already have been released while this was in flight.
  if (comparing && !data.error) $("preview").src = data.preview;
}

function showEdited() {
  if (!comparing) return;
  comparing = false;
  $("compare-badge").classList.remove("on");
  if (editedSrc) $("preview").src = editedSrc;
}
