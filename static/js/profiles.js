// ---- Camera profiles (Develop > Profile) ------------------------------------
// A profile is the base look applied right after the camera-matrix conversion and before every
// slider (profiles.py). Choosing one is an ordinary edit: it sets values.camera_profile, previews
// through the normal path and records one history step. Imported .dcp / .icc files live on the
// server in the profiles folder; the page only sends the file once, as base64.
const profileState = { list: [], thumbFor: null };

function profileNote(msg, isErr = false) {
  const el = $("profile-note"); el.textContent = msg || ""; el.classList.toggle("err", !!isErr);
}

function profileThumb(p) {
  if (!current || sourceKind === "image") return "";   // profiles are raw-only
  const q = new URLSearchParams({ path: current, id: p.id, baseline_temp: baselineTemp || 5500, v: p.rev || "" });
  return `/api/profiles/thumb?${q}`;
}

async function loadProfiles() {
  try { profileState.list = (await (await fetch("/api/profiles")).json()).profiles || []; }
  catch (e) { profileState.list = []; }
  renderProfiles();
}

function renderProfiles() {
  const sel = $("profile-select"), grid = $("profile-grid");
  sel.innerHTML = ""; grid.innerHTML = "";
  const add = (value, label) => { const o = document.createElement("option"); o.value = value; o.textContent = label; sel.appendChild(o); };
  add("", "None (camera matrix only)");
  profileState.thumbFor = current;
  const none = makeTile("None", "", null, () => chooseProfile(""));
  none.classList.add("none");
  grid.appendChild(none);
  for (const p of profileState.list) {
    add(p.id, p.builtin ? p.name : `${p.name} (imported)`);
    grid.appendChild(makeTile(p.name, p.id, profileThumb(p), () => chooseProfile(p.id)));
  }
  paintProfiles();
}

// Called by paintAll(): keep the panel in step with `values` (history jumps, a new photo, reset).
function paintProfiles() {
  if (!$("profile-select")) return;
  // Like Lightroom: JPEG/TIFF/PNG/HEIC have no camera profile; the panel is hidden for them.
  const rawOnly = sourceKind === "image";
  $("profile-panel").hidden = rawOnly;
  if (rawOnly) return;
  if (current !== profileState.thumbFor && profileState.list.length) {
    profileState.thumbFor = current;
    const imgs = document.querySelectorAll("#profile-grid .look-tile:not(.none) img");
    profileState.list.forEach((p, i) => { if (imgs[i]) imgs[i].src = profileThumb(p); });
  }
  const id = values.camera_profile || "";
  const entry = profileState.list.find(p => p.id === id);
  $("profile-select").value = entry ? id : "";
  document.querySelectorAll("#profile-grid .look-tile").forEach(t => t.classList.toggle("active", t.dataset.id === id));
  $("profile-delete").disabled = !entry || entry.builtin;
  const foreign = values.camera_profile_foreign;
  profileNote(id && !entry ? "This photo uses a camera profile that is not in your profile folder. Import the .dcp file again."
              : foreign && !id ? `Profile in the sidecar: ${foreign} (not available here; kept as is until you pick another)`
              : entry && entry.kind === "dcp" ? "Hue/saturation tables and tone curve applied; the profile's colour matrices are not used on an already-converted image."
              : "", id && !entry);
}

async function chooseProfile(id) {
  if (!current || sourceKind === "image") return;
  await commitChain;
  values.camera_profile = id;
  if (id) delete values.camera_profile_foreign;
  const name = id ? (profileState.list.find(p => p.id === id)?.name || id) : "none";
  paintProfiles(); schedulePreview();
  commit(id ? `Profile: ${name}` : "Profile: none");
  setStatus(id ? `Profile '${name}' — not saved yet` : "Removed profile — not saved yet");
}

$("profile-select").addEventListener("change", () => chooseProfile($("profile-select").value));
$("profile-import").addEventListener("click", () => $("profile-file").click());
$("profile-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const bytes = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const d = await post("/api/profiles/import", { name: file.name, data: btoa(bin) });
  if (d.error) { profileNote(d.error, true); return setStatus(d.error, true); }
  profileState.list = d.profiles; renderProfiles();
  profileNote(d.message);
});
$("profile-delete").addEventListener("click", async () => {
  const p = profileState.list.find(x => x.id === values.camera_profile);
  if (!p || p.builtin) return;
  if (!confirm(`Delete profile '${p.name}'? Photos that use it fall back to no profile.`)) return;
  const d = await post("/api/profiles/delete", { id: p.id });
  if (d.profiles) { profileState.list = d.profiles; renderProfiles(); }
});

loadProfiles();
