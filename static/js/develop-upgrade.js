// ---- Edit version: offer to upgrade an edit made with the old colour maths ----
// An edit saved before linear-light editing (edit_version 1) keeps rendering with the
// old maths, so it looks exactly as it did; this banner is the only way it changes.
function refreshUpgradeBanner() {
  const banner = $("upgrade-banner");
  if (!banner) return;
  banner.hidden = !(values && Number(values.edit_version || 2) < 2);
}

$("upgrade-edit").addEventListener("click", async () => {
  if (!current) return;
  const button = $("upgrade-edit");
  button.disabled = true;
  setStatus("Upgrading the edit…");
  try {
    await commitChain;
    // The state before the upgrade stays in History, so Undo/jump brings the old look back.
    const data = await post("/api/upgrade_edit", {
      path: current, adjustments: { ...values, _baseline_temp: baselineTemp },
    });
    if (data.error) return setStatus(data.error, true);
    values = data.adjustments;
    paintAll();
    $("preview").src = data.preview;
    drawHistogram(data.histogram);
    commit("Upgrade to linear-light colour");
    const r = data.report || {};
    setStatus(`Upgraded. Average difference from the old look: ${r.mean_difference_after} of 255 levels `
      + `(a plain re-map would have been ${r.mean_difference_before}). Not saved yet.`);
  } finally {
    button.disabled = false;
  }
});
