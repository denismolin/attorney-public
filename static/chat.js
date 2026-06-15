// Chat multi-turn : conserve l'historique, l'envoie au serveur, gère clear + copie.
(function () {
  const form = document.getElementById("chatform");
  const input = document.getElementById("question");
  const log = document.getElementById("chatlog");
  const btn = document.getElementById("sendbtn");
  const clearbtn = document.getElementById("clearbtn");
  const copybtn = document.getElementById("copybtn");
  const chatProvider = document.getElementById("chat-provider");
  const chatModel = document.getElementById("chat-model");

  const STORAGE_KEY = "case_chat_history";
  const CTX_KEY = "case_session_ctx";
  const LS_CHAT_PROVIDER = "case_chat_provider";
  const LS_CHAT_MODEL = "case_chat_model";
  // history : [{role:'user'|'assistant', content, sources?}]
  let history = loadHistory();
  let sessionCtx = loadCtx();

  function loadHistory() {
    try {
      return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
    } catch (e) {
      return [];
    }
  }
  function saveHistory() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history));
    } catch (e) {
      /* quota dépassé : on ignore */
    }
  }
  function loadCtx() {
    try {
      return JSON.parse(sessionStorage.getItem(CTX_KEY) || "{}");
    } catch (e) {
      return {};
    }
  }
  function saveCtx() {
    try {
      sessionStorage.setItem(CTX_KEY, JSON.stringify(sessionCtx));
    } catch (e) {
      /* quota dépassé : on ignore */
    }
  }

  // --- Gestion provider/modèle ---
  function saveChatSelection() {
    if (chatProvider) localStorage.setItem(LS_CHAT_PROVIDER, chatProvider.value);
    if (chatModel) localStorage.setItem(LS_CHAT_MODEL, chatModel.value);
  }

  function loadChatModels(restoreModel) {
    const provider = chatProvider ? chatProvider.value : "";
    if (!provider || !chatModel) return;
    chatModel.innerHTML = '<option value="">Chargement…</option>';
    chatModel.disabled = true;
    fetch("/api/chat/models/" + encodeURIComponent(provider))
      .then((r) => r.json())
      .then((data) => {
        chatModel.innerHTML = "";
        const models = data.models || [];
        if (!models.length) {
          chatModel.innerHTML = '<option value="">auto</option>';
        } else {
          models.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            if (restoreModel && m === restoreModel) opt.selected = true;
            chatModel.appendChild(opt);
          });
        }
        chatModel.disabled = false;
        saveChatSelection();
      })
      .catch(() => {
        chatModel.innerHTML = '<option value="">auto</option>';
        chatModel.disabled = false;
      });
  }

  if (chatProvider) {
    const savedProvider = localStorage.getItem(LS_CHAT_PROVIDER);
    if (savedProvider) {
      const opt = chatProvider.querySelector(`option[value="${CSS.escape(savedProvider)}"]`);
      if (opt) chatProvider.value = savedProvider;
    }
    chatProvider.addEventListener("change", () => {
      saveChatSelection();
      loadChatModels(null);
    });
  }
  if (chatModel) {
    chatModel.addEventListener("change", saveChatSelection);
  }
  loadChatModels(localStorage.getItem(LS_CHAT_MODEL));

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // Convertit le sous-ensemble markdown utilisé par le LLM en HTML sûr.
  function markdownToHtml(text) {
    // 1. Échappe d'abord le HTML brut
    let s = escapeHtml(text);
    // 2. Blocs de code (```...```) → <pre><code>
    s = s.replace(/```[\s\S]*?```/g, (m) => {
      const inner = m.slice(3, -3).replace(/^[^\n]*\n?/, ""); // retire la ligne de langue
      return "<pre><code>" + inner + "</code></pre>";
    });
    // 3. Code inline `...`
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // 4. **gras**
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // 5. *italique*
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    // 6. Listes à puces : lignes commençant par "- " ou "* "
    s = s.replace(/((?:^|\n)[*-] .+)+/g, (block) => {
      const items = block.trim().split(/\n[*-] /).map((x) => x.replace(/^[*-] /, ""));
      return "<ul>" + items.map((x) => "<li>" + x + "</li>").join("") + "</ul>";
    });
    // 7. Sauts de ligne restants → <br> (hors blocs pre)
    s = s.replace(/(?<!<\/pre>)\n(?!<pre)/g, "<br>");
    return s;
  }

  function renderSources(sources) {
    if (!sources || !sources.length) return "";
    let html = "<div class='sources'><strong>Sources :</strong>";
    sources.forEach((s) => {
      const label =
        (s.date ? s.date.substring(0, 10) : "?") +
        " — " + (s.correspondent || "?") +
        " — " + (s.title || "");
      const target = s.email_id || s.doc_id;
      html += "<a href='/email/" + target + "'>" + escapeHtml(label) + "</a>";
    });
    html += "</div>";
    return html;
  }

  // Crée la bulle d'un message (avec bouton copier individuel).
  function addBubble(role, contentHtml, rawText) {
    const div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "user" : "bot");
    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = contentHtml;
    div.appendChild(body);
    // Bouton copier ce message
    const copy = document.createElement("button");
    copy.className = "msg-copy";
    copy.title = "Copier ce message";
    copy.textContent = "⧉";
    copy.addEventListener("click", () => copyText(rawText, copy));
    div.appendChild(copy);
    log.appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "end" });
    return body;
  }

  function copyText(text, feedbackEl) {
    navigator.clipboard.writeText(text).then(() => {
      if (feedbackEl) {
        const old = feedbackEl.textContent;
        feedbackEl.textContent = "✓";
        setTimeout(() => (feedbackEl.textContent = old), 1200);
      }
    });
  }

  // Rejoue l'historique sauvegardé au chargement de la page.
  function renderAll() {
    log.innerHTML = "";
    history.forEach((m) => {
      if (m.role === "user") {
        addBubble("user", escapeHtml(m.content), m.content);
      } else {
        const html = markdownToHtml(m.content) + renderSources(m.sources);
        addBubble("bot", html, m.content);
      }
    });
  }
  renderAll();

  // Construit le texte intégral de la conversation (pour copier-coller).
  function conversationToText() {
    return history
      .map((m) => (m.role === "user" ? "Vous : " : "Assistant : ") + m.content)
      .join("\n\n");
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    // Affiche + mémorise la question
    addBubble("user", escapeHtml(question), question);
    history.push({ role: "user", content: question });
    saveHistory();
    input.value = "";
    btn.disabled = true;

    const pendingBody = addBubble("bot", "<em>Recherche en cours…</em>", "");

    try {
      // On envoie l'historique SANS le dernier tour user (déjà passé en 'question').
      const priorHistory = history.slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
      const provider = chatProvider ? chatProvider.value : null;
      const model = chatModel ? (chatModel.value || null) : null;
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history: priorHistory, session_ctx: sessionCtx, chat_provider: provider, chat_model: model }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        pendingBody.innerHTML = "<span class='ko'>" + escapeHtml(data.error || "Erreur") + "</span>";
        return;
      }
      const answer = data.answer || "(pas de réponse)";
      pendingBody.innerHTML = markdownToHtml(answer) + renderSources(data.sources);
      history.push({ role: "assistant", content: answer, sources: data.sources || [] });
      saveHistory();
      // Met à jour le contexte de session
      if (data.session_ctx) {
        sessionCtx = data.session_ctx;
        saveCtx();
      }
    } catch (err) {
      pendingBody.innerHTML = "<span class='ko'>Erreur réseau : " + escapeHtml(String(err)) + "</span>";
    } finally {
      btn.disabled = false;
      input.focus();
    }
  });

  // Effacer l'historique + contexte de session (conserve le choix de provider/modèle)
  clearbtn.addEventListener("click", function () {
    if (history.length && !confirm("Effacer toute la conversation ?")) return;
    history = [];
    sessionCtx = {};
    saveHistory();
    saveCtx();
    log.innerHTML = "";
    input.focus();
  });

  // Copier toute la conversation
  copybtn.addEventListener("click", function () {
    if (!history.length) {
      copybtn.textContent = "Rien à copier";
      setTimeout(() => (copybtn.textContent = "📋 Copier"), 1200);
      return;
    }
    copyText(conversationToText(), null);
    const old = copybtn.textContent;
    copybtn.textContent = "✓ Copié";
    setTimeout(() => (copybtn.textContent = old), 1200);
  });
})();
