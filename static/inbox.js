// File de tri : emails reçus par la boîte de collecte, à classer dans un dossier.
(function () {
  const App = window.App;
  const esc = App.escapeHtml;
  const conf = window.INBOX;
  const list = document.getElementById("inbox-list");
  let view = "open";

  const STATUS = {
    pending: "À trier", needs_admin: "Dossier proposé", filed: "Classé, indexation en attente",
    indexed: "Classé", rejected: "Écarté", duplicate: "Doublon", quarantined: "Quarantaine", error: "Erreur",
  };
  const MODE = {
    rfc822: "original joint", eml_attachment: "original joint", inline: "original reconstitué",
    none: "message direct",
  };
  const caseName = (id) => (conf.cases.find((c) => c.case_id === id) || {}).name || id;

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso.slice(0, 10) : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
  }
  function fmtTs(ts) {
    return ts ? new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  }

  function suggestionHtml(it) {
    if (!it.suggested_case_id) {
      return "<p class='notice'>" + esc((it.reasons || [])[0] || "Aucun dossier proposé.") + "</p>";
    }
    const pct = Math.round((it.confidence || 0) * 100);
    const reasons = (it.reasons || []).map((r) => "<li>" + esc(r) + "</li>").join("");
    return "<div class='notice info'><strong>Dossier proposé : " + esc(caseName(it.suggested_case_id)) + "</strong> " +
      "<span class='muted'>(confiance " + pct + " %" + (it.deterministic ? ", indice vérifiable" : ", à vérifier") + ")</span>" +
      (reasons ? "<ul style='margin:0.3rem 0 0; padding-left:1.2rem;'>" + reasons + "</ul>" : "") + "</div>";
  }

  function actionsHtml(it) {
    if (it.status === "pending" || it.status === "needs_admin") {
      const options = conf.cases.map((c) =>
        "<option value='" + esc(c.case_id) + "'" + (c.case_id === it.suggested_case_id ? " selected" : "") + ">" + esc(c.name) + "</option>").join("");
      let html = "";
      if (it.status === "needs_admin") {
        html += "<p class='notice'>" + esc(it.decided_by || "Un utilisateur") + " propose un nouveau dossier : <strong>" +
          esc(it.proposed_case_name || "") + "</strong>.</p>";
      }
      html += "<div class='btn-row'>" +
        "<label class='sr-only' for='case-" + it.id + "'>Dossier</label>" +
        "<select class='eval-select' id='case-" + it.id + "' data-role='case'>" +
        (it.suggested_case_id ? "" : "<option value='' selected disabled>Choisir un dossier…</option>") + options + "</select>" +
        "<button type='button' data-act='file'>Classer</button>" +
        "<button type='button' class='btn-secondary' data-act='" + (conf.isAdmin ? "approve" : "propose") + "'>" +
        (conf.isAdmin ? "Créer un dossier…" : "Proposer un nouveau dossier…") + "</button>" +
        "<button type='button' class='btn-danger-outline' data-act='reject'>Écarter</button></div>";
      return html;
    }
    let html = "";
    if (it.case_id && (it.status === "filed" || it.status === "indexed")) {
      html += "<p class='muted'>Classé dans <strong>" + esc(caseName(it.case_id)) + "</strong> par " +
        esc(it.decided_by === "auto" ? "classement automatique" : (it.decided_by || "?")) + " le " + fmtTs(it.decided_at) + ".";
      if (it.status === "indexed" && it.doc_id) html += " <a href='" + App.api("/email/" + encodeURIComponent(it.doc_id)) + "'>Ouvrir l'email</a>";
      html += "</p>";
    } else if (it.error) {
      html += "<p class='muted'>" + esc(it.error) + "</p>";
    } else if (it.status === "rejected") {
      html += "<p class='muted'>Écarté par " + esc(it.decided_by || "?") + " le " + fmtTs(it.decided_at) + ".</p>";
    }
    if (conf.isAdmin && ["rejected", "quarantined", "error"].includes(it.status)) {
      html += "<div class='btn-row'><button type='button' class='btn-secondary' data-act='restore'>Remettre dans la file</button></div>";
    }
    return html;
  }

  function cardHtml(it) {
    const from = it.orig_from || it.forwarder || "Expéditeur inconnu";
    return "<article class='panel' data-id='" + it.id + "'>" +
      "<div class='page-head'><div style='min-width:0;'>" +
      "<h2 style='margin:0; overflow-wrap:anywhere;'>" + esc(it.subject || "(sans objet)") + "</h2>" +
      "<p class='muted' style='margin:0;'>" + esc(from) + (it.orig_date ? " · " + fmtDate(it.orig_date) : "") +
      (it.n_attachments ? " · " + it.n_attachments + " pièce(s) jointe(s)" : "") + "</p></div>" +
      "<span class='badge neutral'>" + esc(STATUS[it.status] || it.status) + "</span></div>" +
      "<p class='hint'>Transféré par " + esc(it.forwarder || "?") + " le " + fmtTs(it.received_at) +
      " · " + esc(MODE[it.unwrap_mode] || "") +
      (it.sender_verified ? "" : " · expéditeur non authentifié") + "</p>" +
      (it.forwarder_note ? "<p class='hint'>Mot d'accompagnement : « " + esc(it.forwarder_note.slice(0, 300)) + " »</p>" : "") +
      (it.preview ? "<details><summary>Aperçu du message</summary><p class='body-new' style='margin-top:0.4rem;'>" + esc(it.preview) + (it.preview.length >= 600 ? "…" : "") + "</p></details>" : "") +
      (["pending", "needs_admin"].includes(it.status) ? suggestionHtml(it) : "") +
      actionsHtml(it) + "</article>";
  }

  const EMPTY = {
    open: ["Rien à trier", "Les emails transférés à la boîte de collecte apparaîtront ici."],
    done: ["Aucun email traité", "Les emails classés ou écartés apparaîtront ici."],
    quarantine: ["Quarantaine vide", "Les messages d'expéditeurs non autorisés sont retenus ici."],
  };

  async function load() {
    list.innerHTML = "<p class='muted'><span class='spinner'></span> Chargement…</p>";
    try {
      const data = await App.requestJson("/api/inbox?view=" + view);
      const c = data.counts || {};
      const open = (c.pending || 0) + (c.needs_admin || 0);
      const q = (c.quarantined || 0) + (c.error || 0);
      document.getElementById("count-open").textContent = open ? "(" + open + ")" : "";
      const qEl = document.getElementById("count-quarantine");
      if (qEl) qEl.textContent = q ? "(" + q + ")" : "";
      if (!data.items.length) {
        list.innerHTML = "<div class='panel'><div class='empty-state'><strong>" + EMPTY[view][0] + "</strong><p>" + EMPTY[view][1] + "</p></div></div>";
        return;
      }
      list.innerHTML = data.items.map(cardHtml).join("");
    } catch (err) {
      list.innerHTML = "<p class='notice error'>" + esc(err.message) + "</p>";
    }
  }

  async function act(card, path, body, okMessage) {
    card.querySelectorAll("button, select").forEach((el) => (el.disabled = true));
    try {
      await App.requestJson("/api/inbox/" + card.dataset.id + path, { json: body || {} });
      App.toast(okMessage, { kind: "success" });
      load();
    } catch (err) {
      App.toast(err.message, { kind: "error" });
      card.querySelectorAll("button, select").forEach((el) => (el.disabled = false));
    }
  }

  // Saisie d'un nom de dossier (proposition par un utilisateur, création par un admin).
  function askCaseName(title, hint, okLabel) {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.className = "dialog";
      dlg.innerHTML = "<form method='dialog' class='stack'><h2>" + esc(title) + "</h2><p class='muted'>" + esc(hint) + "</p>" +
        "<label class='field'>Nom du dossier<input class='input' name='name' required maxlength='80'></label>" +
        "<div class='btn-row'><button type='button' class='btn-secondary' value='cancel'>Annuler</button>" +
        "<button type='submit'>" + esc(okLabel) + "</button></div></form>";
      document.body.appendChild(dlg);
      let value = null;
      dlg.querySelector("[value=cancel]").addEventListener("click", () => dlg.close());
      dlg.querySelector("form").addEventListener("submit", () => { value = dlg.querySelector("input").value.trim(); });
      dlg.addEventListener("close", () => { dlg.remove(); resolve(value); });
      dlg.showModal();
    });
  }

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const card = btn.closest("article");
    const action = btn.dataset.act;
    if (action === "file") {
      const caseId = card.querySelector("[data-role=case]").value;
      if (!caseId) { App.toast("Choisissez un dossier.", { kind: "error" }); return; }
      act(card, "/file", { case_id: caseId }, "Email classé dans « " + caseName(caseId) + " ». Il sera indexé dans quelques instants.");
    } else if (action === "reject") {
      if (await App.confirm("Écarter cet email ? Il ne sera ajouté à aucun dossier. Un administrateur peut le remettre dans la file.",
        { okLabel: "Écarter", danger: true })) act(card, "/reject", {}, "Email écarté.");
    } else if (action === "propose") {
      const name = await askCaseName("Proposer un nouveau dossier", "Un administrateur validera la création du dossier, puis l'email y sera classé.", "Proposer");
      if (name) act(card, "/propose-case", { name }, "Proposition envoyée aux administrateurs.");
    } else if (action === "approve") {
      const name = await askCaseName("Créer un dossier", "Le dossier est créé et cet email y est classé. Vous pourrez compléter sa fiche dans Dossiers.", "Créer et classer");
      if (name) act(card, "/approve-case", { name }, "Dossier « " + name + " » créé, email classé.");
    } else if (action === "restore") {
      act(card, "/restore", {}, "Email remis dans la file.");
    }
  });

  document.querySelectorAll(".tab-btn").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      view = tab.dataset.view;
      load();
    });
  });

  load();
})();
