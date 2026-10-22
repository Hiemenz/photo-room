// ---- Library backup -----------------------------------------------------------------
// "Back up" snapshots the catalog, flags, history, sidecars and cull data to the folder
// saved in settings (asked for the first time). On load, a toast reminds you when the
// reminder interval has passed. Settings: dest, interval_days, keep, include_raws, zip, auto.
async function backupCall(path, body) {
  const r = await fetch(path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

async function backupNow() {
  const btn = $("lib-backup");
  try {
    const st = await backupCall("/api/backup/status");
    let dest;
    if (!st.settings.dest) {
      dest = (window.prompt("Folder to keep backups in (for example /Volumes/Backup/photo-room):") || "").trim();
      if (!dest) return;
    }
    btn.disabled = true;
    const r = await backupCall("/api/backup/run", dest ? { dest } : {});
    toast("Library backed up", { kind: "info", why: `${r.files} files to ${r.path}` });
  } catch (e) {
    toastError("Backup", e);
  } finally {
    btn.disabled = false;
  }
}
$("lib-backup").addEventListener("click", backupNow);

(async () => {
  try {
    const st = await backupCall("/api/backup/status");
    if (st.due && !st.settings.auto) {
      toast("Back up your library?", { kind: "info", ms: 20000,
        why: `It has been ${st.settings.interval_days}+ days since the last backup.`,
        action: { label: "Back up now", run: backupNow } });
    }
  } catch (e) { /* no reminder */ }
})();
