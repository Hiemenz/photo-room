// ---- Presets -------------------------------------------------------------
async function loadPresets(selected) {
  const data = await (await fetch("/api/presets")).json();
  const sel = $("preset-list");
  sel.innerHTML = `<option value="">Presets…</option>`;
  for (const name of data.presets || []) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    sel.appendChild(opt);
  }
  if (selected) sel.value = selected;
  $("preset-del").disabled = !sel.value;
}

$("preset-list").addEventListener("change", async (e) => {
  const name = e.target.value;
  $("preset-del").disabled = !name;
  if (!name || !current) return;

  const data = await post("/api/preset_get", {
    name, baseline_temp: baselineTemp || 5500,
  });
  if (data.error) return setStatus(data.error, true);

  // A preset is tone and colour only; whatever crop this photo has stays.
  values = { ...values, ...data.adjustments };
  paintAll();
  schedulePreview();
  commit(`Preset: ${name}`);
  setStatus(`Applied preset '${name}' — not saved yet`);
});

$("preset-save").addEventListener("click", async () => {
  if (!current) return;
  const name = prompt("Name this preset:");
  if (!name) return;
  const data = await post("/api/preset_save", {
    name, adjustments: values, baseline_temp: baselineTemp || 5500,
  });
  if (data.error) return setStatus(data.error, true);
  await loadPresets(name);
  setStatus(data.message);
});

$("preset-del").addEventListener("click", async () => {
  const name = $("preset-list").value;
  if (!name) return;
  const data = await post("/api/preset_delete", { name });
  await loadPresets();
  setStatus(data.message);
});
