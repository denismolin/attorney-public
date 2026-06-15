// Filtrage de la liste des mails par affaire + clic sur un point de la frise.
(function () {
  // Filtres affaire (cases à cocher) -> masque/affiche les lignes du tableau.
  const filters = document.querySelectorAll(".afilter");
  function applyFilters() {
    const active = new Set(
      Array.from(filters).filter((f) => f.checked).map((f) => f.value)
    );
    document.querySelectorAll("table.maillist tbody tr").forEach((tr) => {
      tr.style.display = active.has(tr.dataset.affaire) ? "" : "none";
    });
  }
  filters.forEach((f) => f.addEventListener("change", applyFilters));

  // Clic sur un point de la frise Plotly -> ouvre l'email correspondant.
  const plot = document.getElementById("timeline-plot");
  if (plot && plot.on) {
    plot.on("plotly_click", function (data) {
      const pt = data.points && data.points[0];
      if (!pt || !pt.customdata) return;
      const cd = pt.customdata;
      const target = cd.email_id || cd.doc_id;
      if (target) window.location.href = "/email/" + target;
    });
  }
})();
