// Conseil : stratégie contradictoire, s'appuyant sur les agents Chat et Synthèse.
(function () {
  const topK = document.getElementById("adv-top-k");
  const expandExtra = document.getElementById("adv-expand-extra");
  const effort = document.getElementById("adv-effort");
  const LS_EFFORT = "case_adv_effort";

  // Le niveau de raisonnement est mémorisé d'une visite à l'autre.
  try {
    const saved = localStorage.getItem(LS_EFFORT);
    if (saved !== null) effort.value = saved;
  } catch (e) { /* stockage indisponible */ }
  effort.addEventListener("change", () => {
    try { localStorage.setItem(LS_EFFORT, effort.value); } catch (e) { /* ignore */ }
  });

  ChatCore.create({
    form: document.getElementById("advform"),
    input: document.getElementById("advquestion"),
    log: document.getElementById("advlog"),
    sendBtn: document.getElementById("advsendbtn"),
    clearBtn: document.getElementById("advclearbtn"),
    copyBtn: document.getElementById("advcopybtn"),
    provider: document.getElementById("adv-provider"),
    model: document.getElementById("adv-model"),
    promptButtons: document.querySelectorAll(".adv-prompt-btn"),
    storageKey: "case_advisor_history",
    ctxKey: "case_advisor_ctx",
    lsProvider: "case_adv_provider",
    lsModel: "case_adv_model",
    endpoint: "/api/advisor/stream",
    stream: true,
    botLabel: "Conseil",
    pendingLabel: "Initialisation…",
    emptyTitle: "Exposez un scénario",
    emptyHint: "Collez un dire de la partie adverse ou choisissez un scénario type. L'agent élabore une réponse stratégique et liste les pièces utiles.",
    extraPayload: () => ({
      top_k: parseInt(topK.value, 10) || 25,
      expand_extra: parseInt(expandExtra.value, 10) || 6,
      reasoning_effort: effort.value || null,
    }),
  });
})();
