// Panel d'évaluation RAG — logique JS
(function () {
  "use strict";

  // =========================================================================
  // 1. TAB SWITCHING
  // =========================================================================
  function tabShow(name) {
    document.querySelectorAll(".tab-content").forEach(function (el) {
      el.style.display = "none";
    });
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.classList.remove("active");
    });
    var content = document.getElementById("tab-" + name);
    if (content) content.style.display = "block";
    var btn = document.querySelector('[data-tab="' + name + '"]');
    if (btn) btn.classList.add("active");

    // Charger les données à l'activation de l'onglet
    if (name === "dataset") loadQuestions();
    if (name === "evaluate") { loadRunsList(); updateEvalQCount(); }
    if (name === "report") loadRunsForReport();
  }

  document.querySelectorAll(".tab-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      tabShow(btn.dataset.tab);
    });
  });

  // =========================================================================
  // 2. UTILITAIRES
  // =========================================================================
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function badge(val, type) {
    return '<span class="badge ' + esc(type) + '">' + esc(val) + "</span>";
  }

  var CAT_LABELS = {
    factual: "Factuel", procedural: "Procédural", comparative: "Comparatif",
    temporal: "Temporel", synthetic: "Synthétique"
  };
  var DIFF_LABELS = { easy: "Facile", medium: "Moyen", hard: "Difficile" };
  var DIFF_CLASSES = { easy: "autre", medium: "succession", hard: "dette" };

  function fmtTs(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString("fr-FR");
  }

  // =========================================================================
  // 3. MODÈLES — chargement dynamique par provider
  // =========================================================================
  function loadModels(providerSelectId, modelSelectId) {
    var provider = document.getElementById(providerSelectId).value;
    var sel = document.getElementById(modelSelectId);
    if (!provider) return;
    sel.innerHTML = '<option value="">Chargement…</option>';
    sel.disabled = true;
    fetch("/api/eval/models/" + encodeURIComponent(provider))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        sel.innerHTML = "";
        var models = data.models || [];
        if (!models.length) {
          sel.innerHTML = '<option value="auto">auto</option>';
        } else {
          models.forEach(function (m) {
            var opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            sel.appendChild(opt);
          });
        }
        sel.disabled = false;
      })
      .catch(function () {
        sel.innerHTML = '<option value="auto">auto</option>';
        sel.disabled = false;
      });
  }

  document.getElementById("gen-provider").addEventListener("change", function () {
    loadModels("gen-provider", "gen-model");
  });
  document.getElementById("eval-provider").addEventListener("change", function () {
    loadModels("eval-provider", "eval-model");
  });

  // Charger les modèles pour le provider sélectionné par défaut au démarrage
  if (document.getElementById("gen-provider").value) {
    loadModels("gen-provider", "gen-model");
  }
  if (document.getElementById("eval-provider").value) {
    loadModels("eval-provider", "eval-model");
  }

  // =========================================================================
  // 4. GÉNÉRATION
  // =========================================================================
  var genPollTimer = null;

  document.getElementById("gen-start-btn").addEventListener("click", startGeneration);
  document.getElementById("gen-cancel-btn").addEventListener("click", cancelGeneration);

  function startGeneration() {
    var provider = document.getElementById("gen-provider").value;
    var model = document.getElementById("gen-model").value || "auto";
    var n = parseInt(document.getElementById("gen-n").value, 10) || 10;
    var categories = Array.from(document.querySelectorAll(".cat-check:checked")).map(function (c) { return c.value; });
    var difficulties = Array.from(document.querySelectorAll(".diff-check:checked")).map(function (c) { return c.value; });

    if (!provider) { alert("Choisissez un provider."); return; }
    if (categories.length === 0) { alert("Choisissez au moins une catégorie."); return; }
    if (difficulties.length === 0) { alert("Choisissez au moins une difficulté."); return; }

    document.getElementById("gen-start-btn").disabled = true;
    document.getElementById("gen-cancel-btn").style.display = "inline-block";
    document.getElementById("gen-progress").style.display = "block";
    document.getElementById("gen-phase").textContent = "Démarrage…";
    document.getElementById("gen-counter").textContent = "";
    document.getElementById("gen-bar").style.width = "0%";
    document.getElementById("gen-logbox").textContent = "";

    fetch("/api/eval/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider, model: model, n_questions: n, categories: categories, difficulties: difficulties })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) {
        alert(data.error);
        document.getElementById("gen-start-btn").disabled = false;
        document.getElementById("gen-cancel-btn").style.display = "none";
        return;
      }
      if (!genPollTimer) genPollTimer = setInterval(pollGenStatus, 1500);
      pollGenStatus();
    }).catch(function (e) {
      alert("Erreur réseau : " + e);
      document.getElementById("gen-start-btn").disabled = false;
    });
  }

  function pollGenStatus() {
    fetch("/api/eval/gen_status").then(function (r) { return r.json(); }).then(function (status) {
      document.getElementById("gen-phase").textContent = status.phase || "—";
      var cur = status.current || 0;
      var tot = status.total || 0;
      document.getElementById("gen-counter").textContent = tot ? cur + " / " + tot : "";
      document.getElementById("gen-bar").style.width = tot ? Math.round((cur / tot) * 100) + "%" : "0%";
      if (status.log) {
        document.getElementById("gen-logbox").textContent = status.log
          .map(function (l) { return "[" + new Date((l.ts || 0) * 1000).toLocaleTimeString() + "] " + l.message; })
          .join("\n");
        document.getElementById("gen-logbox").scrollTop = 99999;
      }
      if (!status.running) {
        clearInterval(genPollTimer);
        genPollTimer = null;
        document.getElementById("gen-start-btn").disabled = false;
        document.getElementById("gen-cancel-btn").style.display = "none";
        loadQuestions(); // Rafraîchir le dataset
        updateDatasetCount();
      }
    }).catch(function () {});
  }

  function cancelGeneration() {
    fetch("/api/eval/gen_cancel", { method: "POST" }).catch(function () {});
    document.getElementById("gen-cancel-btn").disabled = true;
  }

  // =========================================================================
  // 5. DATASET CRUD
  // =========================================================================
  function loadQuestions() {
    var cat = document.getElementById("filter-category").value;
    var diff = document.getElementById("filter-difficulty").value;
    var url = "/api/eval/questions";
    var params = [];
    if (cat) params.push("category=" + encodeURIComponent(cat));
    if (diff) params.push("difficulty=" + encodeURIComponent(diff));
    if (params.length) url += "?" + params.join("&");

    fetch(url).then(function (r) { return r.json(); }).then(function (rows) {
      renderQuestionsTable(rows);
      updateDatasetCount(rows.length);
    }).catch(function (e) {
      document.getElementById("questions-tbody").innerHTML =
        '<tr><td colspan="6" class="ko">Erreur chargement : ' + esc(String(e)) + "</td></tr>";
    });
  }

  function updateDatasetCount(n) {
    if (n === undefined) {
      fetch("/api/eval/questions").then(function (r) { return r.json(); }).then(function (rows) {
        var el = document.getElementById("dataset-count");
        if (el) el.textContent = rows.length;
      }).catch(function () {});
      return;
    }
    var el = document.getElementById("dataset-count");
    if (el) el.textContent = n;
  }

  function updateEvalQCount() {
    fetch("/api/eval/questions").then(function (r) { return r.json(); }).then(function (rows) {
      var el = document.getElementById("eval-q-count");
      if (el) el.textContent = "toutes (" + rows.length + ")";
    }).catch(function () {});
  }

  function renderQuestionsTable(rows) {
    var tbody = document.getElementById("questions-tbody");
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;">Aucune question. Générez ou importez un dataset.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(function (q) {
      var catLabel = CAT_LABELS[q.category] || q.category;
      var diffClass = DIFF_CLASSES[q.difficulty] || "autre";
      var diffLabel = DIFF_LABELS[q.difficulty] || q.difficulty;
      return "<tr>" +
        "<td>" + q.id + "</td>" +
        "<td class='eval-cell-wrap'>" + esc(q.question) + "</td>" +
        "<td class='eval-cell-wrap muted'>" + esc((q.expected || "").substring(0, 120)) + "…</td>" +
        "<td>" + badge(catLabel, "autre") + "</td>" +
        "<td>" + badge(diffLabel, diffClass) + "</td>" +
        "<td style='white-space:nowrap;'>" +
        "<button class='btn-secondary btn-sm' onclick='window._evalEditQ(" + q.id + ")'>✏</button> " +
        "<button class='btn-secondary btn-sm' onclick='window._evalDeleteQ(" + q.id + ")'>🗑</button>" +
        "</td></tr>";
    }).join("");
  }

  window._evalDeleteQ = function (id) {
    if (!confirm("Supprimer la question #" + id + " ?")) return;
    fetch("/api/eval/questions/" + id, { method: "DELETE" }).then(function () { loadQuestions(); updateDatasetCount(); });
  };

  window._evalEditQ = function (id) {
    fetch("/api/eval/questions?category=&difficulty=").then(function (r) { return r.json(); }).then(function (rows) {
      var q = rows.find(function (r) { return r.id === id; });
      if (!q) return;
      document.getElementById("edit-q-id").value = q.id;
      document.getElementById("edit-question").value = q.question;
      document.getElementById("edit-expected").value = q.expected;
      document.getElementById("edit-category").value = q.category;
      document.getElementById("edit-difficulty").value = q.difficulty;
      document.getElementById("edit-notes").value = q.notes || "";
      document.getElementById("edit-modal").style.display = "flex";
    });
  };

  document.getElementById("edit-save-btn").addEventListener("click", function () {
    var id = parseInt(document.getElementById("edit-q-id").value, 10);
    var payload = {
      question: document.getElementById("edit-question").value,
      expected: document.getElementById("edit-expected").value,
      category: document.getElementById("edit-category").value,
      difficulty: document.getElementById("edit-difficulty").value,
      notes: document.getElementById("edit-notes").value,
    };
    fetch("/api/eval/questions/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function () {
      document.getElementById("edit-modal").style.display = "none";
      loadQuestions();
    });
  });

  document.getElementById("edit-cancel-btn").addEventListener("click", function () {
    document.getElementById("edit-modal").style.display = "none";
  });

  document.getElementById("apply-filters").addEventListener("click", loadQuestions);

  document.getElementById("add-question-btn").addEventListener("click", function () {
    var q = document.getElementById("add-question").value.trim();
    var e = document.getElementById("add-expected").value.trim();
    if (!q || !e) { alert("Question et réponse attendue requis."); return; }
    fetch("/api/eval/questions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q, expected: e,
        category: document.getElementById("add-category").value,
        difficulty: document.getElementById("add-difficulty").value,
      }),
    }).then(function (r) { return r.json(); }).then(function () {
      document.getElementById("add-question").value = "";
      document.getElementById("add-expected").value = "";
      loadQuestions();
      updateDatasetCount();
    });
  });

  // Export
  document.getElementById("export-btn").addEventListener("click", function () {
    window.location.href = "/api/eval/export";
  });

  // Effacer tout le dataset
  document.getElementById("clear-dataset-btn").addEventListener("click", function () {
    var count = parseInt(document.getElementById("dataset-count").textContent, 10) || 0;
    if (!confirm("Supprimer les " + count + " questions du dataset ? Cette action est irréversible.")) return;
    fetch("/api/eval/questions/clear", { method: "DELETE" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        loadQuestions();
        updateDatasetCount(0);
      })
      .catch(function (err) { alert("Erreur : " + err); });
  });

  // Import
  document.getElementById("import-file").addEventListener("change", function (e) {
    var file = e.target.files[0];
    if (!file) return;
    var formData = new FormData();
    formData.append("file", file);
    fetch("/api/eval/import", { method: "POST", body: formData })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        alert("Import : " + data.imported + " questions importées, " + (data.skipped || 0) + " ignorées.");
        loadQuestions();
        updateDatasetCount();
      })
      .catch(function (err) { alert("Erreur import : " + err); });
    e.target.value = "";
  });

  // =========================================================================
  // 6. ÉVALUATION
  // =========================================================================
  var evalPollTimers = {};

  // Slider threshold
  var thresholdEl = document.getElementById("eval-threshold");
  var thresholdVal = document.getElementById("eval-threshold-val");
  thresholdEl.addEventListener("input", function () { thresholdVal.textContent = thresholdEl.value; });

  document.getElementById("eval-start-btn").addEventListener("click", startEval);

  function startEval() {
    var provider = document.getElementById("eval-provider").value;
    var model = document.getElementById("eval-model").value || "auto";
    var threshold = parseFloat(document.getElementById("eval-threshold").value);
    if (!provider) { alert("Choisissez un provider."); return; }

    document.getElementById("eval-start-btn").disabled = true;

    fetch("/api/eval/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider, model: model, threshold: threshold }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      document.getElementById("eval-start-btn").disabled = false;
      if (data.error) { alert(data.error); return; }
      var runId = data.run_id;
      addActiveRunWidget(runId, provider, model);
      evalPollTimers[runId] = setInterval(function () { pollRunStatus(runId); }, 2000);
      pollRunStatus(runId);
    }).catch(function (e) {
      document.getElementById("eval-start-btn").disabled = false;
      alert("Erreur réseau : " + e);
    });
  }

  function addActiveRunWidget(runId, provider, model) {
    var container = document.getElementById("eval-active-runs");
    var div = document.createElement("div");
    div.id = "run-widget-" + runId;
    div.className = "eval-run-widget";
    div.innerHTML =
      "<strong>" + esc(provider) + "/" + esc(model) + "</strong> " +
      '<span class="muted">' + runId.substring(0, 8) + "…</span>" +
      '<div><span id="run-phase-' + runId + '">démarrage…</span> ' +
      '<span id="run-counter-' + runId + '" class="muted"></span></div>' +
      '<div class="progressbar"><div id="run-bar-' + runId + '"></div></div>' +
      '<button class="btn-secondary btn-sm" onclick="window._cancelRun(\'' + runId + '\')">Annuler</button>';
    container.appendChild(div);
  }

  function pollRunStatus(runId) {
    fetch("/api/eval/run_status/" + runId).then(function (r) { return r.json(); }).then(function (run) {
      var phaseEl = document.getElementById("run-phase-" + runId);
      var counterEl = document.getElementById("run-counter-" + runId);
      var barEl = document.getElementById("run-bar-" + runId);
      if (phaseEl) phaseEl.textContent = run.phase || "—";
      if (counterEl) counterEl.textContent = run.total ? run.current + " / " + run.total : "";
      if (barEl) barEl.style.width = run.total ? Math.round((run.current / run.total) * 100) + "%" : "0%";

      if (!run.running) {
        clearInterval(evalPollTimers[runId]);
        delete evalPollTimers[runId];
        loadRunsList();
        loadRunsForReport();
        var widget = document.getElementById("run-widget-" + runId);
        if (widget) widget.remove();
      }
    }).catch(function () {});
  }

  window._cancelRun = function (runId) {
    if (!confirm("Annuler ce run ?")) return;
    fetch("/api/eval/run_cancel/" + runId, { method: "POST" }).catch(function () {});
  };

  function loadRunsList() {
    fetch("/api/eval/runs").then(function (r) { return r.json(); }).then(function (runs) {
      var el = document.getElementById("runs-list");
      if (!runs.length) { el.textContent = "Aucun run pour l'instant."; return; }
      el.innerHTML = runs.map(function (r) {
        var statusCls = r.phase === "terminé" ? "ok" : (r.cancelled ? "ko" : "muted");
        return "<div class='eval-run-row'>" +
          "<code>" + r.run_id.substring(0, 8) + "…</code> " +
          "<span class='muted'>" + fmtTs(r.started_at) + "</span> " +
          "<strong>" + esc(r.provider) + "/" + esc(r.model) + "</strong> " +
          "<span class='" + statusCls + "'>" + esc(r.phase) + "</span> " +
          r.current + "/" + r.total + " questions" +
          "</div>";
      }).join("");
    }).catch(function () {});
  }

  // =========================================================================
  // 7. RAPPORT
  // =========================================================================
  var currentRunId = null;

  function loadRunsForReport() {
    fetch("/api/eval/runs").then(function (r) { return r.json(); }).then(function (runs) {
      var sel = document.getElementById("report-run-select");
      var prev = sel.value;
      sel.innerHTML = '<option value="">— Choisir un run —</option>';
      runs.forEach(function (r) {
        var opt = document.createElement("option");
        opt.value = r.run_id;
        opt.textContent = r.run_id.substring(0, 8) + " — " + r.provider + "/" + r.model +
          " — " + fmtTs(r.started_at) + " (" + r.phase + ")";
        sel.appendChild(opt);
      });
      if (prev) sel.value = prev;
    }).catch(function () {});
  }

  document.getElementById("report-load-btn").addEventListener("click", function () {
    var runId = document.getElementById("report-run-select").value;
    if (!runId) { alert("Choisissez un run."); return; }
    loadReport(runId);
  });

  document.getElementById("report-apply-filters").addEventListener("click", function () {
    if (currentRunId) loadReport(currentRunId);
  });

  function loadReport(runId) {
    currentRunId = runId;
    var cat = document.getElementById("report-filter-cat").value;
    var verdict = document.getElementById("report-filter-verdict").value;
    var diff = document.getElementById("report-filter-diff").value;
    var params = ["category=" + encodeURIComponent(cat), "verdict=" + encodeURIComponent(verdict), "difficulty=" + encodeURIComponent(diff)];
    var url = "/api/eval/results/" + runId + "?" + params.join("&");

    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      renderSummaryCards(data.summary);
      renderDistributionChart(data.summary.distribution);
      renderResultsTable(data.results);
      loadTrendChart();
      document.getElementById("report-content").style.display = "block";
      document.getElementById("report-empty").style.display = "none";
      document.getElementById("dl-md-btn").style.display = "inline-block";
      document.getElementById("dl-html-btn").style.display = "inline-block";
    }).catch(function (e) {
      alert("Erreur chargement rapport : " + e);
    });
  }

  function renderSummaryCards(summary) {
    var scoreColor = summary.avg_score >= 7 ? "#2e7d32" : summary.avg_score >= 5 ? "#e65100" : "#c62828";
    var passColor = summary.pass_rate >= 70 ? "#2e7d32" : summary.pass_rate >= 50 ? "#e65100" : "#c62828";
    document.getElementById("summary-cards").innerHTML =
      '<div class="eval-card"><strong>' + summary.total + '</strong><span>Questions</span></div>' +
      '<div class="eval-card"><strong style="color:' + scoreColor + '">' + summary.avg_score + '/10</strong><span>Score moyen</span></div>' +
      '<div class="eval-card"><strong style="color:' + passColor + '">' + summary.pass_rate + ' %</strong><span>Taux de réussite</span></div>';
  }

  function renderDistributionChart(distribution) {
    var labels = ["0–2", "2–4", "4–6", "6–8", "8–10"];
    var colors = ["#c62828", "#e65100", "#f9a825", "#43a047", "#1565c0"];
    Plotly.newPlot("chart-dist", [{
      type: "bar", x: labels, y: distribution || [0, 0, 0, 0, 0],
      marker: { color: colors },
    }], {
      margin: { t: 10, b: 30, l: 30, r: 10 },
      paper_bgcolor: "transparent", plot_bgcolor: "transparent",
      yaxis: { tickformat: "d" },
    }, { responsive: true, displayModeBar: false });
  }

  function loadTrendChart() {
    fetch("/api/eval/runs").then(function (r) { return r.json(); }).then(function (runs) {
      var finished = runs.filter(function (r) { return r.phase === "terminé"; }).reverse();
      if (!finished.length) return;
      var promises = finished.map(function (r) {
        return fetch("/api/eval/results/" + r.run_id).then(function (res) { return res.json(); });
      });
      Promise.all(promises).then(function (dataArr) {
        var xs = dataArr.map(function (d, i) { return fmtTs(finished[i].started_at); });
        var ys = dataArr.map(function (d) { return d.summary.avg_score; });
        Plotly.newPlot("chart-trend", [{
          type: "scatter", mode: "lines+markers",
          x: xs, y: ys,
          marker: { color: "#2c7fb8", size: 8 },
          line: { color: "#2c7fb8" },
        }], {
          margin: { t: 10, b: 60, l: 40, r: 10 },
          paper_bgcolor: "transparent", plot_bgcolor: "transparent",
          yaxis: { range: [0, 10], title: "Score moyen" },
          xaxis: { tickangle: -30 },
        }, { responsive: true, displayModeBar: false });
      });
    }).catch(function () {});
  }

  function renderResultsTable(results) {
    var tbody = document.getElementById("results-tbody");
    if (!results.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;">Aucun résultat pour ce filtre.</td></tr>';
      return;
    }
    tbody.innerHTML = results.map(function (r) {
      var verdictCls = r.verdict === "pass" ? "ok" : "ko";
      var scoreColor = r.score >= 7 ? "#2e7d32" : r.score >= 5 ? "#e65100" : "#c62828";
      var scoreCell = '<strong style="color:' + scoreColor + '">' + (r.score !== null ? r.score : "—") + '</strong>';
      var metrics =
        '<span title="Fidélité">' + (r.faithfulness !== null ? r.faithfulness : "—") + '</span> / ' +
        '<span title="Pertinence">' + (r.relevance !== null ? r.relevance : "—") + '</span> / ' +
        '<span title="Complétude">' + (r.completeness !== null ? r.completeness : "—") + '</span>';
      // Ligne principale
      var mainRow = "<tr class='eval-result-main'>" +
        "<td rowspan='3' style='vertical-align:top; font-weight:bold;'>" + r.question_id + "</td>" +
        "<td colspan='5' class='eval-cell-wrap' style='font-weight:500;'>" + esc(r.question) + "</td>" +
        "<td rowspan='3' style='vertical-align:top; text-align:center;'>" + scoreCell +
          "<br><small class='" + verdictCls + "'>" + (r.verdict || "—") + "</small>" +
          "<br><small class='muted' style='font-size:.75rem;'>" + metrics + "</small>" +
        "</td>" +
        "</tr>";
      // Ligne réponse attendue / obtenue
      var pairRow = "<tr class='eval-result-pair'>" +
        "<td colspan='2' style='background:#f0fff0; vertical-align:top;'>" +
          "<small class='muted'>Attendu</small><br>" +
          "<span style='font-size:.85rem;'>" + esc(r.expected || "") + "</span>" +
        "</td>" +
        "<td colspan='3' style='background:#fff8f0; vertical-align:top;'>" +
          "<small class='muted'>Réponse RAG</small><br>" +
          "<span style='font-size:.85rem;'>" + esc(r.actual || "") + "</span>" +
        "</td>" +
        "</tr>";
      // Ligne raisonnement juge
      var reasonRow = "<tr class='eval-result-reason'>" +
        "<td colspan='5' style='background:#f5f5f5; font-size:.8rem; color:#555;'>" +
          "<strong>Juge :</strong> " + esc(r.reasoning || "—") +
        "</td>" +
        "</tr>";
      return mainRow + pairRow + reasonRow;
    }).join("");
  }

  // Téléchargements
  document.getElementById("dl-md-btn").addEventListener("click", function () {
    if (currentRunId) window.location.href = "/api/eval/download/" + currentRunId + "?format=md";
  });
  document.getElementById("dl-html-btn").addEventListener("click", function () {
    if (currentRunId) window.location.href = "/api/eval/download/" + currentRunId + "?format=html";
  });

  // =========================================================================
  // INIT
  // =========================================================================
  loadQuestions();
  updateDatasetCount();

  // Reprendre le polling si une génération était en cours au chargement
  fetch("/api/eval/gen_status").then(function (r) { return r.json(); }).then(function (status) {
    if (status.running) {
      document.getElementById("gen-start-btn").disabled = true;
      document.getElementById("gen-cancel-btn").style.display = "inline-block";
      document.getElementById("gen-progress").style.display = "block";
      if (!genPollTimer) genPollTimer = setInterval(pollGenStatus, 1500);
      pollGenStatus();
    }
  }).catch(function () {});

})();
