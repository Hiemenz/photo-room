// ---- Theme ----------------------------------------------------------------
function setTheme(name, remember = true) {
  document.documentElement.dataset.theme = name;
  $("theme-toggle").textContent = name === "light" ? "Dark" : "Light";
  if (remember) { try { localStorage.setItem("photoroom-theme", name); } catch (e) { /* private mode */ } }
}
$("theme-toggle").addEventListener("click", () =>
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark", false);
