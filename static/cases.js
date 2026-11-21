// Gestion des dossiers (administrateurs) : création, fiche, participants, archivage.
(function () {
  const App = window.App;
  const panel = document.getElementById("new-case-panel");
  const form = document.getElementById("new-case-form");

  document.getElementById("new-case-btn").addEventListener("click", () => {
    panel.hidden = false;
    form.elements.name.focus();
  });
  document.getElementById("new-case-cancel").addEventListener("click", () => { panel.hidden = true; });

  function formData(f) {
    const out = {};
    new FormData(f).forEach((v, k) => { out[k] = String(v).trim(); });
    return out;
  }

  async function run(btn, fn, okMessage) {
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> ' + App.escapeHtml(label); }
    try {
      await fn();
      if (okMessage) App.toast(okMessage, { kind: "success" });
      return true;
    } catch (err) {
      App.toast(err.message, { kind: "error" });
      return false;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await run(form.querySelector("button[type=submit]"),
      () => App.requestJson("/api/cases", { json: formData(form) }));
    if (ok) window.location.reload();
  });

  document.querySelectorAll(".case-card").forEach((card) => {
    const caseId = card.dataset.case;
    const base = "/api/cases/" + encodeURIComponent(caseId);

    card.querySelector(".case-edit").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const ok = await run(f.querySelector("button[type=submit]"),
        () => App.requestJson(base, { method: "PUT", json: formData(f) }), "Dossier enregistré.");
      if (ok) card.querySelector("h2").firstChild.textContent = f.elements.name.value + " ";
    });

    card.querySelector(".participant-add").addEventListener("submit", async (e) => {
      e.preventDefault();
      const ok = await run(e.target.querySelector("button[type=submit]"),
        () => App.requestJson(base + "/participants", { json: formData(e.target) }));
      if (ok) window.location.reload();
    });

    card.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "del-participant") {
        const li = btn.closest("li");
        const ok = await run(btn, () => App.requestJson(base + "/participants/" + li.dataset.id, { method: "DELETE" }));
        if (ok) li.remove();
      } else if (act === "archive") {
        const yes = await App.confirm(
          "Archiver ce dossier ? Il disparaît du sélecteur ; ses emails et son index sont conservés et il peut être réactivé.",
          { okLabel: "Archiver", danger: true });
        if (!yes) return;
        if (await run(btn, () => App.requestJson(base, { method: "PUT", json: { status: "archived" } }))) window.location.reload();
      } else if (act === "restore") {
        if (await run(btn, () => App.requestJson(base, { method: "PUT", json: { status: "active" } }))) window.location.reload();
      }
    });
  });
})();
