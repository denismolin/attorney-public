// Réglages : base vectorielle et boîte de collecte.
(function () {
  const App = window.App;
  const $ = (id) => document.getElementById(id);

  function say(el, text, kind) {
    el.textContent = text;
    el.className = "hint " + (kind || "");
  }
  async function busy(btn, fn) {
    btn.disabled = true;
    try { return await fn(); } finally { btn.disabled = false; }
  }

  // ------------------------------------------------------- Base vectorielle
  const chromaMsg = $("chroma-msg");
  function chromaPayload() {
    return {
      host: $("chroma_host").value.trim(),
      port: parseInt($("chroma_port").value, 10) || 8000,
      ssl: $("chroma_ssl").checked,
      auth_header: $("chroma_auth_header").value,
    };
  }
  function describe(probe) {
    if (!probe.ok) return "Injoignable : " + probe.error;
    const c = probe.counts || {};
    return "Connexion établie" + (probe.version ? " (ChromaDB " + probe.version + ")" : "") +
      " — " + (c.documents || 0) + " documents, " + (c.chunks || 0) + " extraits.";
  }

  $("chroma-test").addEventListener("click", (e) => busy(e.target, async () => {
    say(chromaMsg, "Test en cours…");
    try {
      const probe = await App.requestJson("/api/settings/chroma/test", { json: chromaPayload() });
      say(chromaMsg, describe(probe), probe.ok ? "ok" : "ko");
    } catch (err) { say(chromaMsg, err.message, "ko"); }
  }));

  $("chroma-save").addEventListener("click", (e) => busy(e.target, async () => {
    say(chromaMsg, "Enregistrement…");
    try {
      const data = await App.requestJson("/api/settings/chroma", { json: chromaPayload() });
      $("chroma_host").value = data.chroma.host;
      $("chroma_port").value = data.chroma.port;
      $("chroma_auth_header").value = "";
      say(chromaMsg, (data.status === "reset" ? "Réglage de l'installation rétabli. " : "Enregistré. ") + describe(data.probe),
        data.probe.ok ? "ok" : "ko");
      if (data.probe.ok && !(data.probe.counts || {}).chunks) {
        App.toast("Cette base est vide. Copiez-y les vecteurs existants ou lancez une indexation.", { timeout: 9000 });
      }
    } catch (err) { say(chromaMsg, err.message, "ko"); }
  }));

  $("chroma-copy").addEventListener("click", async (e) => {
    const payload = chromaPayload();
    if (!payload.host) { say(chromaMsg, "Indiquez d'abord l'adresse de destination.", "ko"); return; }
    const yes = await App.confirm(
      "Copier tous les vecteurs de la base actuelle vers " + payload.host + ":" + payload.port +
      " ? Rien n'est recalculé et la base actuelle n'est pas modifiée. L'opération peut durer plusieurs minutes.",
      { okLabel: "Copier" });
    if (!yes) return;
    busy(e.target, async () => {
      say(chromaMsg, "Copie en cours… ne fermez pas cette page.");
      try {
        const r = await App.requestJson("/api/settings/chroma/copy", { json: payload });
        say(chromaMsg, "Copie " + (r.ok ? "terminée" : "incomplète") + " : " + r.documents.target + "/" + r.documents.source +
          " documents, " + r.chunks.target + "/" + r.chunks.source + " extraits. " +
          (r.ok ? "Enregistrez l'adresse pour utiliser cette base." : ""), r.ok ? "ok" : "ko");
      } catch (err) { say(chromaMsg, err.message, "ko"); }
    });
  });

  // ------------------------------------------------------ Boîte de collecte
  const mailMsg = $("mailbox-msg");
  function mailboxPayload() {
    return {
      imap_enabled: $("imap_enabled").checked,
      imap_host: $("imap_host").value.trim(),
      imap_port: parseInt($("imap_port").value, 10) || 993,
      imap_ssl: $("imap_ssl").checked,
      imap_user: $("imap_user").value.trim(),
      imap_password: $("imap_password").value,
      imap_folder: $("imap_folder").value.trim() || "INBOX",
      imap_poll_interval: parseInt($("imap_poll_interval").value, 10) || 300,
      imap_processed_action: $("imap_processed_action").value,
      imap_processed_folder: $("imap_processed_folder").value.trim(),
      mailbox_address: $("mailbox_address").value.trim(),
      mailbox_allowed_senders: $("mailbox_allowed_senders").value.trim(),
      mailbox_require_auth_results: $("mailbox_require_auth_results").checked,
      mailbox_autofile: $("mailbox_autofile").checked,
      mailbox_autofile_threshold: parseFloat($("mailbox_autofile_threshold").value) || 0.85,
    };
  }

  $("mailbox-test").addEventListener("click", (e) => busy(e.target, async () => {
    say(mailMsg, "Connexion à la boîte…");
    try {
      const r = await App.requestJson("/api/settings/mailbox/test", { json: mailboxPayload() });
      if (r.ok) say(mailMsg, "Connexion établie — " + r.messages + " message(s) dans le dossier." + (r.warning ? " " + r.warning : ""), r.warning ? "" : "ok");
      else say(mailMsg, "Échec : " + r.error, "ko");
    } catch (err) { say(mailMsg, err.message, "ko"); }
  }));

  $("mailbox-save").addEventListener("click", (e) => busy(e.target, async () => {
    say(mailMsg, "Enregistrement…");
    try {
      const data = await App.requestJson("/api/settings/mailbox", { json: mailboxPayload() });
      const pw = data.mailbox.imap_password || {};
      $("imap_password").value = "";
      $("imap_password").placeholder = pw.configured ? "configuré (" + pw.hint + ")" : "non configuré";
      say(mailMsg, "Enregistré. Le service de relève applique ce réglage à son prochain passage.", "ok");
    } catch (err) { say(mailMsg, err.message, "ko"); }
  }));
})();
