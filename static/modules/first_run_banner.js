window.apiUrl = function (path) {
  return window.FT710Settings && FT710Settings.url
    ? FT710Settings.url(path)
    : path;
};

(function () {
  var banner = document.getElementById("first-run-banner");
  if (!banner || localStorage.getItem("mrrc_welcome_dismissed")) return;
  fetch(window.apiUrl("/api/setup"))
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (s) {
      if (!s || !s.auto_password) return;
      document.getElementById("fr-model").textContent = s.radio_model || "—";
      document.getElementById("fr-port").textContent = s.serial_port || "自动";
      banner.style.display = "block";
    })
    .catch(function () {});
  document.getElementById("fr-close").addEventListener("click", function () {
    banner.style.display = "none";
    localStorage.setItem("mrrc_welcome_dismissed", "1");
  });
})();
