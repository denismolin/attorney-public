// Socle commun à toutes les pages : URLs sous préfixe, requêtes, notifications,
// confirmation, thème, menu mobile. Exposé sous `window.App` (aucun nom global
// n'est déclaré : les scripts de page gardent leur propre `api()`).
(function () {
  const root = (window.APP_ROOT || "").replace(/\/$/, "");
  const activeCase = window.ACTIVE_CASE || "default";

  function api(path) { return root + path; }

  // Sûr dans le texte ET dans une valeur d'attribut (guillemets échappés).
  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g,
      (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  // fetch() avec le dossier actif en en-tête et un traitement uniforme des refus.
  async function request(path, opts) {
    opts = Object.assign({}, opts);
    opts.headers = Object.assign({ "X-Case-Id": activeCase }, opts.headers || {});
    if (opts.json !== undefined) {
      opts.method = opts.method || "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    const resp = await fetch(api(path), opts);
    if (resp.status === 401) {
      // Session expirée : retour à la connexion, puis à la page courante.
      window.location.href = api("/login") + "?next=" + encodeURIComponent(window.location.pathname + window.location.search);
      throw new Error("Session expirée");
    }
    if (resp.status === 403) {
      const clone = resp.clone();
      const data = await clone.json().catch(() => ({}));
      if (data.error === "password_change_required") {
        window.location.href = api("/account/password");
        throw new Error("Changement de mot de passe requis");
      }
    }
    return resp;
  }

  // Requête JSON : renvoie les données, ou lève une Error au message lisible.
  async function requestJson(path, opts) {
    const resp = await request(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(errorText(data.error) || ("Erreur " + resp.status));
    return data;
  }

  const ERROR_LABELS = {
    unauthorized: "Session expirée. Reconnectez-vous.",
    forbidden: "Action réservée aux administrateurs.",
    not_found: "Élément introuvable.",
  };
  function errorText(code) { return ERROR_LABELS[code] || code || ""; }

  // ------------------------------------------------------------ Notifications
  function toast(message, opts) {
    opts = opts || {};
    const host = document.getElementById("toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = "toast " + (opts.kind || "");
    const text = document.createElement("span");
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Fermer");
    close.textContent = "×";
    close.addEventListener("click", () => el.remove());
    el.append(text, close);
    host.appendChild(el);
    const ms = opts.timeout === undefined ? (opts.kind === "error" ? 9000 : 4500) : opts.timeout;
    if (ms) setTimeout(() => el.remove(), ms);
    return el;
  }

  // ------------------------------------------------------------- Confirmation
  // Remplace window.confirm : renvoie une promesse résolue à true/false.
  function confirmDialog(message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      if (typeof HTMLDialogElement === "undefined") { resolve(window.confirm(message)); return; }
      const dlg = document.createElement("dialog");
      dlg.className = "dialog";
      const h = document.createElement("h2");
      h.textContent = opts.title || "Confirmer";
      const p = document.createElement("p");
      p.textContent = message;
      const row = document.createElement("div");
      row.className = "btn-row";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn-secondary";
      cancel.textContent = opts.cancelLabel || "Annuler";
      const ok = document.createElement("button");
      ok.type = "button";
      if (opts.danger) ok.className = "btn-danger";
      ok.textContent = opts.okLabel || "Confirmer";
      row.append(cancel, ok);
      dlg.append(h, p, row);
      document.body.appendChild(dlg);
      let result = false;
      cancel.addEventListener("click", () => dlg.close());
      ok.addEventListener("click", () => { result = true; dlg.close(); });
      dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
      dlg.showModal();
      (opts.danger ? cancel : ok).focus();
    });
  }

  function copyText(text, feedbackEl) {
    return navigator.clipboard.writeText(text).then(() => {
      if (!feedbackEl) return;
      const old = feedbackEl.textContent;
      feedbackEl.textContent = "✓";
      setTimeout(() => (feedbackEl.textContent = old), 1200);
    }).catch(() => toast("Copie impossible : le navigateur a refusé l'accès au presse-papiers.", { kind: "error" }));
  }

  // Remplit un <select> avec les modèles d'un provider (repli « auto » si indisponible).
  function loadModels(select, provider, restoreModel) {
    if (!select || !provider) return Promise.resolve([]);
    select.innerHTML = '<option value="">Chargement…</option>';
    select.disabled = true;
    return request("/api/chat/models/" + encodeURIComponent(provider))
      .then((r) => r.json())
      .then((data) => {
        const models = data.models || [];
        select.innerHTML = "";
        if (!models.length) {
          select.innerHTML = '<option value="">auto</option>';
          if (data.error) select.title = data.error;
        } else {
          select.title = "";
          models.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            if (restoreModel && m === restoreModel) opt.selected = true;
            select.appendChild(opt);
          });
        }
        return models;
      })
      .catch(() => { select.innerHTML = '<option value="">auto</option>'; return []; })
      .finally(() => { select.disabled = false; });
  }

  // Clé de stockage propre au dossier actif. Le dossier par défaut garde la clé
  // historique, pour que les conversations existantes restent visibles.
  function caseKey(base) {
    return activeCase && activeCase !== "default" ? base + "::" + activeCase : base;
  }

  // ------------------------------------------------------- Thème, menu mobile
  function currentTheme() {
    const set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    const btn = document.querySelector('[data-action="nav-open"]');
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    if (action === "theme") {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("ui_theme", next); } catch (err) { /* stockage indisponible */ }
    } else if (action === "nav-open") {
      setNav(!document.body.classList.contains("nav-open"));
    } else if (action === "nav-close") {
      setNav(false);
    }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNav(false); });

  // Changement de dossier : enregistré côté serveur, puis la page est rechargée
  // pour que tout (listes, conversations, prompts) reflète le nouveau dossier.
  const caseSelect = document.getElementById("case-select");
  if (caseSelect) {
    caseSelect.addEventListener("change", async () => {
      caseSelect.disabled = true;
      try {
        await requestJson("/api/cases/active", { json: { case_id: caseSelect.value } });
        window.location.reload();
      } catch (err) {
        caseSelect.disabled = false;
        caseSelect.value = activeCase;
        toast("Changement de dossier impossible : " + err.message, { kind: "error" });
      }
    });
  }

  window.App = {
    root, activeCase, api, escapeHtml, request, requestJson, errorText,
    toast, confirm: confirmDialog, copyText, loadModels, caseKey,
  };
})();
