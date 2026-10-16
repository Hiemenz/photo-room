// ---- Selection -----------------------------------------------------------
function updateSelectionUI() {
  document.querySelectorAll(".file").forEach(el => {
    el.classList.toggle("selected", selected.has(el.dataset.path));
  });
  syncGridState();
  const n = selected.size;
  $("batch").disabled = n === 0 || !current;
  $("batch").textContent = n
    ? (clipboard ? `Paste settings to ${n} selected` : `Apply edit to ${n} selected`)
    : "Apply edit to selected";
  $("sel-count").innerHTML = n
    ? `${n} selected &middot; <a href="#" id="clear-sel" style="color:var(--accent)">clear</a>`
    : `Click to open &middot; <kbd>cmd</kbd>-click to select`;
  const clear = $("clear-sel");
  if (clear) clear.addEventListener("click", (e) => {
    e.preventDefault(); selected.clear(); updateSelectionUI();
  });
}

function handleFileClick(e, path, index) {
  if (e.metaKey || e.ctrlKey) {
    e.preventDefault();
    selected.has(path) ? selected.delete(path) : selected.add(path);
    lastIndex = index;
    updateSelectionUI();
  } else if (e.shiftKey && lastIndex >= 0) {
    e.preventDefault();
    const [a, b] = [Math.min(lastIndex, index), Math.max(lastIndex, index)];
    for (let i = a; i <= b; i++) selected.add(files[i]);
    updateSelectionUI();
  } else {
    openFile(path);
  }
}

$("batch").addEventListener("click", async () => {
  if (!current || !selected.size) return;
  const n = selected.size;
  const source = clipboard ? clipboard.values : values;
  setStatus(`Applying to ${n} file(s)…`);
  const data = await post("/api/batch_apply", {
    paths: [...selected], adjustments: source, baseline_temp: baselineTemp,
  });
  if (data.error) return setStatus(data.error, true);
  setStatus(data.message, data.failed.length > 0);
  if (data.failed.length) {
    console.warn("Batch apply failures:", data.failed);
  }
  selected.clear();
  updateSelectionUI();
});
