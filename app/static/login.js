(function () {
  var e = new URLSearchParams(location.search).get("e");
  var box = document.getElementById("err");
  if (e) {
    box.textContent = e === "bloque"
      ? "Trop de tentatives. Réessayez dans 15 minutes."
      : "Identifiant ou mot de passe incorrect.";
    box.classList.add("show");
  }
  fetch("/api/public").then(function (r) { return r.json(); }).then(function (d) {
    if (d.demo) document.getElementById("demo").classList.add("show");
  }).catch(function () {});
})();
