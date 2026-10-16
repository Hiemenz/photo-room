// ---- Library -------------------------------------------------------------
async function loadFiles() {
  const res = await fetch("/api/files");
  const data = await res.json();
  const list = $("file-list");
  list.innerHTML = "";

  if (!data.files.length) {
    list.innerHTML = `<div class="file" style="cursor:default">No DNG or CR2 files found</div>`;
    return;
  }
  files = data.files;
  data.files.forEach((f, i) => {
    const el = document.createElement("div");
    el.className = "file" + (f === current ? " active" : "");
    el.textContent = f;
    el.title = f;
    el.dataset.path = f;
    el.addEventListener("click", (e) => handleFileClick(e, f, i));
    list.appendChild(el);
  });
  updateSelectionUI();
}
