(function () {
  var e = new URLSearchParams(location.search).get("e");
  var box = document.getElementById("err");
  var exp = document.getElementById("expired");
  if (e === "expire") {
    exp.classList.add("show");
  } else if (e) {
    box.textContent = e === "bloque"
      ? "Trop de tentatives. Réessayez dans 15 minutes."
      : "Identifiant ou mot de passe incorrect.";
    box.classList.add("show");
  }
  fetch("/api/public").then(function (r) { return r.json(); }).then(function (d) {
    if (d.demo) document.getElementById("demo").classList.add("show");
    var c = d.contact || {};
    if (c.phone) {
      var digits = c.phone.replace(/[^0-9]/g, "");
      document.getElementById("expWho").textContent = c.name || "votre fournisseur";
      var call = document.getElementById("expCall");
      call.href = "tel:" + c.phone.replace(/\s/g, "");
      call.textContent = c.phone;
      var wa = document.getElementById("expWa");
      wa.href = "https://wa.me/" + digits + "?text=" + encodeURIComponent("Bonjour, je voudrais renouveler mon abonnement ADER LINK.");
      document.getElementById("expContact").hidden = false;
    }
  }).catch(function () {});
})();
