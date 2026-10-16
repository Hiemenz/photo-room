// ---- Actions -------------------------------------------------------------
async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

$("save").addEventListener("click", async () => {
  if (!current) return;
  setStatus("Saving…");
  const data = await post("/api/save", {
    path: current, adjustments: { ...values, _baseline_temp: baselineTemp },
  });
  if (data.error) return setStatus(data.error, true);
  $("source").textContent = "Showing previously saved edits";
  setStatus(data.message);
});

$("reset").addEventListener("click", async () => {
  if (!current) return;
  setStatus("Reverting…");
  const data = await post("/api/reset", { path: current });
  if (data.error) return setStatus(data.error, true);
  values = data.adjustments;
  neutral = { ...data.adjustments };
  paintAll();
  $("preview").src = data.preview;
  drawHistogram(data.histogram);
  $("source").textContent = "No saved edits";
  commit("Revert to original");
  setStatus(data.message);
});

$("recover").addEventListener("click", async () => {
  if (!current) return;
  setStatus("Extracting embedded original…");
  const data = await post("/api/recover", { path: current });
  if (data.error) return setStatus(data.error, true);
  setStatus(data.message);
  loadFiles();   // the recovered raw is a new file in the library
});
