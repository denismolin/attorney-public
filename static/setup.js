// Assistant d'installation : une étape affichée à la fois, avancement mémorisé côté serveur.
(function () {
  const App = window.App;
  const $ = (id) => document.getElementById(id);
  const steps = window.SETUP.steps;

  function show(step) {
    document.querySelectorAll("[data-step]").forEach((s) => (s.hidden = s.dataset.step !== step));
    const idx = steps.indexOf(step);
    document.querySelectorAll("[data-step-dot]").forEach((dot, i) => {
      dot.className = i < idx ? "done" : i === idx ? "current" : "";
    });
    const first = document.querySelector('[data-step="' + step + '"] input:not([type=radio]):not([type=checkbox]):not([disabled])');
    if (first) first.focus();
  }

  async function goTo(step) {
    await App.requestJson("/api/setup/step", { json: { step } });
    if (step === "done") { window.location.href = App.api("/accueil"); return; }
    show(step);
  }

  function say(el, text, kind) { el.textContent = text; el.className = "hint " + (kind || ""); }
  async function busy(btn, fn) {
    btn.disabled = true;
    try { await fn(); } catch (err) { App.toast(err.message, { kind: "error" }); } finally { btn.disabled = false; }
  }

  document.querySelectorAll("[data-skip]").forEach((btn) =>
    btn.addEventListener("click", () => busy(btn, () => goTo(btn.dataset.skip))));

  // ---- 1. Compte administrateur
  const formAdmin = $("form-admin");
  if (formAdmin) {
    formAdmin.addEventListener("submit", async (e) => {
      e.preventDefault();
      const alertEl = formAdmin.querySelector("[role=alert]");
      const btn = formAdmin.querySelector("button[type=submit]");
      alertEl.hidden = true;
      btn.disabled = true;
      try {
        const body = {};
        new FormData(formAdmin).forEach((v, k) => (body[k] = v));
        await App.requestJson("/api/setup/admin", { json: body });
        window.location.reload();      // recharge avec la session admin : étapes suivantes
      } catch (err) {
        alertEl.textContent = err.message;
        alertEl.hidden = false;
        btn.disabled = false;
      }
    });
  }

  // ---- 2. Base vectorielle
  const storageMsg = $("storage-msg");
  document.querySelectorAll('input[name="storage"]').forEach((r) =>
    r.addEventListener("change", () => { $("storage-remote").hidden = r.value !== "remote" || !r.checked; }));
  if ($("storage-next")) {
    $("storage-next").addEventListener("click", (e) => busy(e.target, async () => {
      const remote = document.querySelector('input[name="storage"]:checked').value === "remote";
      say(storageMsg, "Test de la connexion…");
      if (remote) {
        const payload = {
          host: $("chroma_host").value.trim(), port: parseInt($("chroma_port").value, 10) || 8000,
          ssl: $("chroma_ssl").checked, auth_header: $("chroma_auth_header").value,
        };
        if (!payload.host) { say(storageMsg, "Indiquez l'adresse de la machine.", "ko"); return; }
        try {
          await App.requestJson("/api/settings/chroma", { json: payload });
        } catch (err) { say(storageMsg, err.message, "ko"); return; }
      } else {
        const probe = await App.requestJson("/api/settings/chroma/test", { json: {} });
        if (!probe.ok) {
          say(storageMsg, "La base locale ne répond pas (" + probe.error + "). Vérifiez que le service « chromadb » est démarré, ou passez cette étape.", "ko");
          return;
        }
      }
      await goTo("models");
    }));
  }

  // ---- 3. Modèles
  const modelsMsg = $("models-msg");
  const DEFAULT_MODELS = {
    chat: { openai: "gpt-4o", mistral: "mistral-large-latest", vllm: "auto" },
    embed: { openai: "text-embedding-3-large", mistral: "mistral-embed" },
  };
  function modelsPayload() {
    const p = {
      chat_provider: $("chat_provider").value, chat_model: $("chat_model").value.trim(),
      vllm_base_url: $("vllm_base_url").value.trim(),
      keys: { openai: $("key_openai").value.trim(), mistral: $("key_mistral").value.trim(), anthropic: $("key_anthropic").value.trim() },
    };
    if (!$("embed_provider").disabled) {
      p.embed_provider = $("embed_provider").value;
      p.embed_model = $("embed_model").value.trim();
    }
    return p;
  }
  async function loadChatModels() {
    const list = $("chat-models");
    try {
      const data = await App.requestJson("/api/chat/models/" + encodeURIComponent($("chat_provider").value));
      list.innerHTML = (data.models || []).map((m) => "<option value='" + App.escapeHtml(m) + "'>").join("");
    } catch (err) { list.innerHTML = ""; }
  }
  if ($("models-next")) {
    $("chat_provider").addEventListener("change", () => {
      $("chat_model").value = DEFAULT_MODELS.chat[$("chat_provider").value] || "";
      loadChatModels();
    });
    $("embed_provider").addEventListener("change", () => {
      $("embed_model").value = DEFAULT_MODELS.embed[$("embed_provider").value] || "";
    });
    loadChatModels();
    $("models-next").addEventListener("click", (e) => busy(e.target, async () => {
      say(modelsMsg, "Enregistrement…");
      const payload = modelsPayload();
      if (!payload.chat_model) { say(modelsMsg, "Indiquez le modèle de chat.", "ko"); return; }
      await App.requestJson("/api/settings", { json: payload });
      const wanted = [...new Set([payload.chat_provider, payload.embed_provider || $("embed_provider").value])];
      const failed = [];
      for (const provider of wanted) {
        say(modelsMsg, "Test de " + provider + "…");
        const r = await App.requestJson("/api/settings/test", { json: { provider } });
        if (!r.ok) failed.push(provider + " : " + r.error);
      }
      if (failed.length) { say(modelsMsg, "Connexion impossible — " + failed.join(" ; "), "ko"); return; }
      await goTo("mailbox");
    }));
  }

  // ---- 4. Boîte de collecte
  const mailMsg = $("mailbox-msg");
  if ($("mailbox-next")) {
    $("mailbox-next").addEventListener("click", (e) => busy(e.target, async () => {
      const payload = {
        imap_host: $("imap_host").value.trim(), imap_port: parseInt($("imap_port").value, 10) || 993,
        imap_user: $("imap_user").value.trim(), imap_password: $("imap_password").value,
        mailbox_allowed_senders: $("mailbox_allowed_senders").value.trim(), imap_ssl: true,
      };
      say(mailMsg, "Connexion à la boîte…");
      const r = await App.requestJson("/api/settings/mailbox/test", { json: payload });
      if (!r.ok) { say(mailMsg, "Échec : " + r.error, "ko"); return; }
      if (!payload.mailbox_allowed_senders) {
        say(mailMsg, "Indiquez qui peut transférer des emails : sans cette liste, tout irait en quarantaine.", "ko");
        return;
      }
      await App.requestJson("/api/settings/mailbox", {
        json: Object.assign({}, payload, { imap_enabled: true, mailbox_address: payload.imap_user }),
      });
      await goTo("case");
    }));
  }

  // ---- 5. Premier dossier
  const formCase = $("form-case");
  if (formCase) {
    formCase.addEventListener("submit", (e) => {
      e.preventDefault();
      busy(formCase.querySelector("button[type=submit]"), async () => {
        const body = {};
        new FormData(formCase).forEach((v, k) => (body[k] = String(v).trim()));
        await App.requestJson("/api/cases/default", { method: "PUT", json: body });
        await goTo("done");
      });
    });
  }

  show(window.SETUP.step);
})();
