// Synthèse : narratif sourcé sur l'ensemble du dossier, réponse en flux.
(function () {
  const topK = document.getElementById("synth-top-k");
  const expandExtra = document.getElementById("synth-expand-extra");
  ChatCore.create({
    form: document.getElementById("synthform"),
    input: document.getElementById("synthquestion"),
    log: document.getElementById("synthlog"),
    sendBtn: document.getElementById("synthsendbtn"),
    clearBtn: document.getElementById("synthclearbtn"),
    copyBtn: document.getElementById("synthcopybtn"),
    provider: document.getElementById("synth-provider"),
    model: document.getElementById("synth-model"),
    promptButtons: document.querySelectorAll(".synth-prompt-btn"),
    storageKey: "case_synthesis_history",
    ctxKey: "case_synthesis_ctx",
    lsProvider: "case_synth_provider",
    lsModel: "case_synth_model",
    endpoint: "/api/synthesis/stream",
    stream: true,
    botLabel: "Synthèse",
    pendingLabel: "Initialisation…",
    emptyTitle: "Demandez une synthèse",
    emptyHint: "Choisissez un prompt suggéré ou décrivez le récit à construire. Chaque affirmation sera rattachée à sa source.",
    extraPayload: () => ({
      top_k: parseInt(topK.value, 10) || 25,
      expand_extra: parseInt(expandExtra.value, 10) || 6,
    }),
  });
})();
