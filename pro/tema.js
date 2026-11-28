document.getElementById("tema").addEventListener("click", function () {
  var claro = document.documentElement.getAttribute("data-theme") !== "light";
  if (claro) document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("pro_tema", claro ? "claro" : "escuro"); } catch (e) {}
});
var mais = document.getElementById("mais");
if (mais) mais.addEventListener("click", function () { document.body.classList.toggle("menu-aberto"); });
