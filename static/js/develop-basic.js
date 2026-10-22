// ---- Build sliders -------------------------------------------------------
function buildSlider(c, host) {
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  wrap.id = `ctrl-${c.key}`;
  wrap.innerHTML = `
    <div class="ctrl-head">
      <label for="s-${c.key}">${c.label}</label>
      <span class="val" id="v-${c.key}" title="Click to reset">0</span>
    </div>
    <input type="range" id="s-${c.key}"
           min="${c.min}" max="${c.max}" step="${c.step}" value="${c.default}">`;
  host.appendChild(wrap);

  const slider = $(`s-${c.key}`);
  slider.addEventListener("input", () => {
    values[c.key] = parseFloat(slider.value);
    paintValue(c);
    if (c.key.startsWith("grade_")) paintWheels();
    schedulePreview();
  });
  // Click the number to return that one slider to its unedited value.
  $(`v-${c.key}`).addEventListener("click", () => {
    if (!current) return;
    values[c.key] = neutral[c.key];
    slider.value = neutral[c.key];
    paintValue(c);
    if (c.key.startsWith("grade_")) paintWheels();
    schedulePreview();
    commit(`Reset ${c.label}`);
  });
}

// Every slider release is a committed step in the history.
$("controls").addEventListener("change", (e) => {
  const el = e.target;
  if (!current || el.type !== "range") return;
  const name = el.closest(".ctrl")?.querySelector("label")?.textContent || "Slider";
  commit(`${name} ${el.value}`);
});

for (const c of CONTROLS) buildSlider(c, $("sliders"));
for (const c of GLOBAL_CONTROLS) buildSlider(c, $("globals"));

// Black and white sits with the global controls: it applies after every
// other adjustment, so the colour sliders act as channel mixers for it.
const bwWrap = document.createElement("label");
bwWrap.className = "toggle";
bwWrap.innerHTML = `<input type="checkbox" id="s-monochrome"><span>Black &amp; white</span>`;
$("globals").appendChild(bwWrap);
$("s-monochrome").addEventListener("change", (e) => {
  values.monochrome = e.target.checked;
  syncMixerView();
  schedulePreview();
  commit(e.target.checked ? "Black & white on" : "Black & white off");
});

function paintValue(c) {
  const v = values[c.key] ?? c.default;
  const el = $(`v-${c.key}`);
  if (c.key === "color_temp") el.textContent = `${Math.round(v)}K`;
  else if (c.key === "strength") el.textContent = `${Math.round(v)}%`;
  else if (c.key === "sharpen_radius") el.textContent = v.toFixed(1);
  else if (c.key === "exposure") el.textContent = (v > 0 ? "+" : "") + v.toFixed(2);
  else el.textContent = (v > 0 ? "+" : "") + Math.round(v);
  $(`ctrl-${c.key}`).classList.toggle("changed", Math.abs(v - neutral[c.key]) > 1e-6);
}
