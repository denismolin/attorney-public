// État du service de relève de la boîte de collecte (page Indexation).
(function () {
  const App = window.App;
  const esc = App.escapeHtml;
  const health = document.getElementById("mailbox-health");
  const logbox = document.getElementById("mailbox-log");
  if (!health) return;

  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "jamais";
  const row = (state, label, value) =>
    "<li><span class='dot " + state + "'></span><span>" + esc(label) + "</span><span class='val'>" + esc(value) + "</span></li>";

  async function refresh() {
    try {
      const d = await App.requestJson("/api/mailbox/status");
      const st = d.state || {}, c = d.counts || {};
      let html = row(st.daemon_alive ? "ok" : "ko", "Service de relève",
        st.daemon_alive ? "actif (" + (st.daemon_host || "?") + ")" : "arrêté — démarrez le service « maild »");
      if (!d.enabled) {
        html += row("", "Relève de la boîte", "désactivée dans les réglages");
      } else {
        html += row(st.last_error ? "ko" : (st.last_ok_at ? "ok" : ""), "Dernière relève",
          st.last_error ? "échec : " + st.last_error : when(st.last_ok_at));
        html += row(d.allowed_senders ? "ok" : "ko", "Expéditeurs autorisés",
          d.allowed_senders ? String(d.allowed_senders) : "aucun : tout va en quarantaine");
      }
      html += row((c.pending || c.needs_admin) ? "warn-dot" : "", "En attente de tri",
        String((c.pending || 0) + (c.needs_admin || 0)) + ((c.needs_admin || 0) ? " (dont " + c.needs_admin + " dossier(s) proposé(s))" : ""));
      if (c.filed) html += row("warn-dot", "Classés, indexation en attente", String(c.filed));
      if (c.quarantined) html += row("warn-dot", "En quarantaine", String(c.quarantined));
      if (c.error) html += row("ko", "En erreur", String(c.error));
      health.innerHTML = html;
      logbox.textContent = (d.log || []).map((l) =>
        new Date(l.ts * 1000).toLocaleTimeString("fr-FR") + "  " + (l.level === "info" ? "" : "[" + l.level + "] ") + l.message).join("\n");
    } catch (err) {
      health.innerHTML = "<li class='ko'>" + esc(err.message) + "</li>";
    }
  }
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
})();
