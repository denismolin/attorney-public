// Base path — injected by the template as window.APP_ROOT (e.g. "/attorney")
const _root = (window.APP_ROOT || "").replace(/\/$/, "");
function api(path) { return _root + path; }

// ─── Upload de fichiers ──────────────────────────────────────────────────────
function handleDrop(event) {
  event.preventDefault();
  event.currentTarget.style.background = "";
  uploadFiles(Array.from(event.dataTransfer.files));
}

(function setupUpload() {
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  const folderBtn = document.getElementById("folder-select-btn");
  const folderInput = document.getElementById("folder-input");
  const uploadResult = document.getElementById("upload-result");

  if (!dropZone || !fileInput) return;

  dropZone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) uploadFiles(Array.from(fileInput.files));
    fileInput.value = "";
  });

  if (folderBtn && folderInput) {
    folderBtn.addEventListener("click", () => folderInput.click());
    folderInput.addEventListener("change", () => {
      const emlFiles = Array.from(folderInput.files).filter((f) =>
        f.name.toLowerCase().endsWith(".eml")
      );
      if (emlFiles.length) uploadFiles(emlFiles);
      else {
        uploadResult.style.display = "block";
        uploadResult.innerHTML = '<span class="ko">Aucun fichier .eml trouvé dans ce dossier.</span>';
      }
      folderInput.value = "";
    });
  }

  const uploadProgress = document.getElementById("upload-progress");
  const uploadBar = document.getElementById("upload-bar");
  const uploadProgressText = document.getElementById("upload-progress-text");

  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m > 0 ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
  }

  window.uploadFiles = function (files) {
    if (!files.length) return;
    uploadResult.style.display = "none";
    uploadProgress.style.display = "block";
    uploadBar.style.width = "0%";
    const startTime = Date.now();
    uploadProgressText.textContent = `Envoi de ${files.length} fichier(s)… 0 % — écoulé 0 s`;

    const form = new FormData();
    files.forEach((f) => form.append("files", f));

    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", api("/api/upload"));

      xhr.upload.addEventListener("progress", (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        const elapsed = (Date.now() - startTime) / 1000;
        uploadBar.style.width = pct + "%";
        let text = `Envoi de ${files.length} fichier(s)… ${pct} % — écoulé ${fmtDuration(elapsed)}`;
        if (e.loaded > 0 && e.loaded < e.total) {
          const rate = e.loaded / elapsed; // octets/s
          const remaining = (e.total - e.loaded) / rate;
          if (isFinite(remaining)) text += ` — restant ≈ ${fmtDuration(remaining)}`;
        }
        uploadProgressText.textContent = text;
      });

      xhr.addEventListener("load", () => {
        uploadProgress.style.display = "none";
        uploadResult.style.display = "block";
        let data;
        try {
          data = JSON.parse(xhr.responseText);
        } catch (err) {
          uploadResult.innerHTML = `<span class="ko">Erreur réseau : ${escapeHtml(String(err))}</span>`;
          resolve();
          return;
        }
        let html = "";
        if (data.accepted && data.accepted.length) {
          html += `<span class="ok">✓ ${data.accepted.length} fichier(s) accepté(s) :</span><ul style="margin:0.2rem 0 0.4rem 1rem;">`;
          data.accepted.forEach((n) => { html += `<li><code>${escapeHtml(n)}</code></li>`; });
          html += "</ul>";
        }
        if (data.rejected && data.rejected.length) {
          html += `<span class="ko">✗ ${data.rejected.length} refusé(s) :</span><ul style="margin:0.2rem 0 0 1rem;">`;
          data.rejected.forEach((r) => { html += `<li><code>${escapeHtml(r.name)}</code> — ${escapeHtml(r.reason)}</li>`; });
          html += "</ul>";
        }
        if (data.error && !html) html = `<span class="ko">${escapeHtml(data.error)}</span>`;
        if (data.accepted && data.accepted.length) {
          html += `<p class="muted" style="margin-top:0.4rem;">Lancez l'indexation pour les intégrer.</p>`;
        }
        uploadResult.innerHTML = html;
        resolve();
      });

      xhr.addEventListener("error", () => {
        uploadProgress.style.display = "none";
        uploadResult.style.display = "block";
        uploadResult.innerHTML = `<span class="ko">Erreur réseau lors de l'envoi.</span>`;
        resolve();
      });

      xhr.send(form);
    });
  };

  function escapeHtml(s) {
    const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
  }
})();

// Admin : déclenche l'indexation et poll la progression.
(function () {
  const startbtn = document.getElementById("startbtn");
  const resetbtn = document.getElementById("resetbtn");
  const resetmsg = document.getElementById("resetmsg");
  const progress = document.getElementById("progress");
  const phase = document.getElementById("phase");
  const counter = document.getElementById("counter");
  const bar = document.getElementById("bar");
  const currentdoc = document.getElementById("currentdoc");
  const logbox = document.getElementById("logbox");
  const enrichProvider = document.getElementById("enrich-provider");
  const enrichModel = document.getElementById("enrich-model");
  let pollTimer = null;

  const LS_PROVIDER = "case_enrich_provider";
  const LS_MODEL = "case_enrich_model";

  function saveSelection() {
    if (enrichProvider) localStorage.setItem(LS_PROVIDER, enrichProvider.value);
    if (enrichModel) localStorage.setItem(LS_MODEL, enrichModel.value);
  }

  function loadModels(restoreModel) {
    const provider = enrichProvider ? enrichProvider.value : "";
    if (!provider || !enrichModel) return;
    enrichModel.innerHTML = '<option value="">Chargement…</option>';
    enrichModel.disabled = true;
    fetch(api("/api/eval/models/" + encodeURIComponent(provider)))
      .then((r) => r.json())
      .then((data) => {
        enrichModel.innerHTML = "";
        const models = data.models || [];
        if (!models.length) {
          enrichModel.innerHTML = '<option value="">auto</option>';
        } else {
          models.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            if (restoreModel && m === restoreModel) opt.selected = true;
            enrichModel.appendChild(opt);
          });
        }
        enrichModel.disabled = false;
        saveSelection();
      })
      .catch(() => {
        enrichModel.innerHTML = '<option value="">auto</option>';
        enrichModel.disabled = false;
      });
  }

  function applyProvider(provider) {
    if (!enrichProvider) return;
    const opt = enrichProvider.querySelector(`option[value="${CSS.escape(provider)}"]`);
    if (opt) enrichProvider.value = provider;
  }

  if (enrichProvider) {
    enrichProvider.addEventListener("change", () => {
      saveSelection();
      loadModels(null);
    });
  }

  if (enrichModel) {
    enrichModel.addEventListener("change", saveSelection);
  }

  // Charge le statut d'abord, pour savoir si une indexation est en cours,
  // puis initialise les selects avec le bon provider/modèle.
  fetch(api("/api/index/status"))
    .then((r) => r.json())
    .then((status) => {
      // Priorité : provider/modèle de l'indexation en cours (ou dernière).
      // Fallback : localStorage, puis valeur par défaut du template.
      const activeProvider = status.enrich_provider || localStorage.getItem(LS_PROVIDER);
      const activeModel = status.enrich_model || localStorage.getItem(LS_MODEL);

      if (activeProvider) applyProvider(activeProvider);
      loadModels(activeModel);

      if (status.running) {
        startbtn.disabled = true;
        startbtn.textContent = "Indexation en cours…";
        pollTimer = setInterval(poll, 1000);
        render(status);
      } else if (status.total) {
        render(status);
        startbtn.textContent = "Relancer l'indexation";
      }
    })
    .catch(() => {
      // Fallback silencieux : charger les modèles avec les valeurs localStorage.
      const savedProvider = localStorage.getItem(LS_PROVIDER);
      if (savedProvider) applyProvider(savedProvider);
      loadModels(localStorage.getItem(LS_MODEL));
    });

  function render(status) {
    progress.style.display = "block";
    phase.textContent = status.phase || "—";
    const cur = status.current || 0;
    const tot = status.total || 0;
    counter.textContent = tot ? `${cur} / ${tot}` + (status.errors ? ` · ${status.errors} erreurs` : "") : "";
    bar.style.width = tot ? Math.round((cur / tot) * 100) + "%" : "0%";
    currentdoc.textContent = status.current_doc || "";
    if (status.log) {
      logbox.textContent = status.log
        .map((l) => {
          const t = new Date((l.ts || 0) * 1000).toLocaleTimeString();
          const prefix = l.level === "error" ? "⚠ " : "";
          return `[${t}] ${prefix}${l.message}`;
        })
        .join("\n");
      logbox.scrollTop = logbox.scrollHeight;
    }
  }

  async function poll() {
    try {
      const resp = await fetch(api("/api/index/status"));
      const status = await resp.json();
      render(status);
      if (!status.running) {
        clearInterval(pollTimer);
        pollTimer = null;
        startbtn.disabled = false;
        startbtn.textContent = "Relancer l'indexation";
      }
    } catch (e) {
      /* ignore une erreur transitoire de polling */
    }
  }

  startbtn.addEventListener("click", async function () {
    startbtn.disabled = true;
    startbtn.textContent = "Indexation en cours…";
    try {
      const provider = enrichProvider ? enrichProvider.value : null;
      const model = enrichModel ? (enrichModel.value || null) : null;
      const payload = { enrich_provider: provider, enrich_model: model };
      const resp = await fetch(api("/api/index/start"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error || "Impossible de démarrer l'indexation.");
        startbtn.disabled = false;
        startbtn.textContent = "Lancer l'indexation";
        return;
      }
      if (!pollTimer) pollTimer = setInterval(poll, 1000);
      poll();
    } catch (e) {
      alert("Erreur réseau : " + e);
      startbtn.disabled = false;
    }
  });

  // ─── Migration / sauvegarde ───────────────────────────────────────────────
  const exportbtn = document.getElementById("exportbtn");
  const importbtn = document.getElementById("importbtn");
  const importInput = document.getElementById("import-input");
  const exportSources = document.getElementById("export-sources");
  const migratemsg = document.getElementById("migratemsg");

  function showMigrate(html) {
    if (!migratemsg) return;
    migratemsg.style.display = "block";
    migratemsg.innerHTML = html;
  }

  if (exportbtn) {
    exportbtn.addEventListener("click", function () {
      const sources = exportSources && exportSources.checked ? "1" : "0";
      showMigrate('<span class="muted">Préparation de l\'archive… (le téléchargement démarre dès qu\'elle est prête, cela peut prendre un moment)</span>');
      // Téléchargement direct via navigation : le serveur renvoie le .zip en pièce jointe.
      window.location.href = api("/api/migrate/export?sources=") + sources;
      setTimeout(() => showMigrate('<span class="muted">Si rien ne se télécharge, vérifiez que ChromaDB est joignable et qu\'aucune indexation n\'est en cours.</span>'), 4000);
    });
  }

  if (importbtn && importInput) {
    importbtn.addEventListener("click", () => importInput.click());
    importInput.addEventListener("change", async function () {
      const file = importInput.files[0];
      importInput.value = "";
      if (!file) return;
      if (!confirm("Importer « " + file.name + " » ?\nCela ÉCRASE la base SQLite et les collections ChromaDB actuelles (une sauvegarde du SQLite est faite automatiquement).")) return;
      importbtn.disabled = true;
      exportbtn.disabled = true;
      showMigrate('<span class="muted">Import en cours… (réinjection des vecteurs, ne fermez pas la page)</span>');
      const form = new FormData();
      form.append("file", file);
      try {
        const resp = await fetch(api("/api/migrate/import"), { method: "POST", body: form });
        const data = await resp.json();
        if (!resp.ok) {
          showMigrate('<span class="ko">Erreur : ' + escapeHtmlMig(data.error || resp.status) + "</span>");
        } else {
          const r = data.restored || {}, e = data.expected || {};
          let html = '<span class="ok">✓ Import terminé.</span><br>' +
            "Documents : " + (r.documents ?? "?") + " / " + (e.documents ?? "?") + " · " +
            "Chunks : " + (r.chunks ?? "?") + " / " + (e.chunks ?? "?");
          if (data.warnings && data.warnings.length) {
            html += '<br><span class="ko">⚠ ' + data.warnings.map(escapeHtmlMig).join("<br>⚠ ") + "</span>";
          }
          html += '<br><span class="muted">Rechargez les pages pour voir les données importées.</span>';
          showMigrate(html);
        }
      } catch (err) {
        showMigrate('<span class="ko">Erreur réseau : ' + escapeHtmlMig(String(err)) + "</span>");
      } finally {
        importbtn.disabled = false;
        exportbtn.disabled = false;
      }
    });
  }

  function escapeHtmlMig(s) {
    const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
  }

  resetbtn.addEventListener("click", async function () {
    if (!confirm("Supprimer TOUTES les données indexées (ChromaDB + registre) ?\nIl faudra relancer l'indexation complète.")) return;
    resetbtn.disabled = true;
    startbtn.disabled = true;
    resetmsg.style.display = "block";
    resetmsg.textContent = "Réinitialisation en cours…";
    try {
      const resp = await fetch(api("/api/index/reset"), { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) {
        resetmsg.textContent = "Erreur : " + (data.error || resp.status);
      } else {
        resetmsg.textContent =
          `Index vidé — ${data.deleted_documents} documents et ${data.deleted_chunks} chunks supprimés. Vous pouvez relancer l'indexation.`;
        progress.style.display = "none";
        startbtn.textContent = "Lancer l'indexation";
      }
    } catch (e) {
      resetmsg.textContent = "Erreur réseau : " + e;
    } finally {
      resetbtn.disabled = false;
      startbtn.disabled = false;
    }
  });

})();
