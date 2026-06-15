// Chatbot Synthèse — narratif sourcé sur l'historique complet.
(function () {
  const form = document.getElementById("synthform");
  const input = document.getElementById("synthquestion");
  const log = document.getElementById("synthlog");
  const btn = document.getElementById("synthsendbtn");
  const clearbtn = document.getElementById("synthclearbtn");
  const copybtn = document.getElementById("synthcopybtn");
  const synthProvider = document.getElementById("synth-provider");
  const synthModel = document.getElementById("synth-model");
  const synthTopK = document.getElementById("synth-top-k");
  const synthExpandExtra = document.getElementById("synth-expand-extra");
  const promptBtns = document.querySelectorAll(".synth-prompt-btn");

  const STORAGE_KEY = "case_synthesis_history";
  const CTX_KEY = "case_synthesis_ctx";
  const LS_SYNTH_PROVIDER = "case_synth_provider";
  const LS_SYNTH_MODEL = "case_synth_model";

  let history = loadHistory();
  let sessionCtx = loadCtx();

  function loadHistory() {
    try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]"); }
    catch (e) { return []; }
  }
  function saveHistory() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history)); }
    catch (e) {}
  }
  function loadCtx() {
    try { return JSON.parse(sessionStorage.getItem(CTX_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function saveCtx() {
    try { sessionStorage.setItem(CTX_KEY, JSON.stringify(sessionCtx)); }
    catch (e) {}
  }

  // --- Provider / modèle ---
  function saveSynthSelection() {
    if (synthProvider) localStorage.setItem(LS_SYNTH_PROVIDER, synthProvider.value);
    if (synthModel) localStorage.setItem(LS_SYNTH_MODEL, synthModel.value);
  }

  function loadSynthModels(restoreModel) {
    const provider = synthProvider ? synthProvider.value : "";
    if (!provider || !synthModel) return;
    synthModel.innerHTML = '<option value="">Chargement…</option>';
    synthModel.disabled = true;
    fetch("/api/chat/models/" + encodeURIComponent(provider))
      .then((r) => r.json())
      .then((data) => {
        synthModel.innerHTML = "";
        const models = data.models || [];
        if (!models.length) {
          synthModel.innerHTML = '<option value="">auto</option>';
        } else {
          models.forEach((m) => {
            const opt = document.createElement("option");
            opt.value = m;
            opt.textContent = m;
            if (restoreModel && m === restoreModel) opt.selected = true;
            synthModel.appendChild(opt);
          });
        }
        synthModel.disabled = false;
        saveSynthSelection();
      })
      .catch(() => {
        synthModel.innerHTML = '<option value="">auto</option>';
        synthModel.disabled = false;
      });
  }

  if (synthProvider) {
    const savedProvider = localStorage.getItem(LS_SYNTH_PROVIDER);
    if (savedProvider) {
      const opt = synthProvider.querySelector(`option[value="${CSS.escape(savedProvider)}"]`);
      if (opt) synthProvider.value = savedProvider;
    }
    synthProvider.addEventListener("change", () => { saveSynthSelection(); loadSynthModels(null); });
  }
  if (synthModel) synthModel.addEventListener("change", saveSynthSelection);
  loadSynthModels(localStorage.getItem(LS_SYNTH_MODEL));

  // --- Boutons de prompts suggérés ---
  promptBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      input.value = btn.dataset.prompt || btn.textContent.trim();
      input.focus();
    });
  });

  // --- Rendu markdown ---
  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function markdownToHtml(text) {
    let s = escapeHtml(text);
    s = s.replace(/```[\s\S]*?```/g, (m) => {
      const inner = m.slice(3, -3).replace(/^[^\n]*\n?/, "");
      return "<pre><code>" + inner + "</code></pre>";
    });
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Titres ## et ###
    s = s.replace(/^### (.+)$/gm, "<h4>$1</h4>");
    s = s.replace(/^## (.+)$/gm, "<h3>$1</h3>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/((?:^|\n)[*-] .+)+/g, (block) => {
      const items = block.trim().split(/\n[*-] /).map((x) => x.replace(/^[*-] /, ""));
      return "<ul>" + items.map((x) => "<li>" + x + "</li>").join("") + "</ul>";
    });
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

  function addBubble(role, contentHtml, rawText) {
    const div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "user" : "bot");
    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = contentHtml;
    div.appendChild(body);
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

  function renderAll() {
    log.innerHTML = "";
    history.forEach((m) => {
      if (m.role === "user") {
        addBubble("user", escapeHtml(m.content).replace(/\n/g, "<br>"), m.content);
      } else {
        addBubble("bot", markdownToHtml(m.content) + renderSources(m.sources), m.content);
      }
    });
  }
  renderAll();

  function conversationToText() {
    return history
      .map((m) => (m.role === "user" ? "Vous : " : "Synthèse : ") + m.content)
      .join("\n\n");
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;

    addBubble("user", escapeHtml(question).replace(/\n/g, "<br>"), question);
    history.push({ role: "user", content: question });
    saveHistory();
    input.value = "";
    btn.disabled = true;

    // Bulle bot avec zone de raisonnement + zone de réponse
    const bubbleDiv = document.createElement("div");
    bubbleDiv.className = "msg bot";
    const bubbleBody = document.createElement("div");
    bubbleBody.className = "msg-body";

    // Bloc raisonnement (masqué par défaut, visible quand du thinking arrive)
    const thinkWrap = document.createElement("details");
    thinkWrap.className = "synth-thinking";
    const thinkSummary = document.createElement("summary");
    thinkSummary.textContent = "Raisonnement…";
    thinkWrap.appendChild(thinkSummary);
    const thinkContent = document.createElement("div");
    thinkContent.className = "synth-thinking-body";
    thinkWrap.appendChild(thinkContent);
    thinkWrap.style.display = "none";

    // Indicateur d'étape
    const stepEl = document.createElement("div");
    stepEl.className = "synth-step";
    stepEl.textContent = "Initialisation…";

    // Zone de réponse
    const answerEl = document.createElement("div");
    answerEl.className = "synth-answer";

    bubbleBody.appendChild(thinkWrap);
    bubbleBody.appendChild(stepEl);
    bubbleBody.appendChild(answerEl);
    bubbleDiv.appendChild(bubbleBody);

    const copyBtn = document.createElement("button");
    copyBtn.className = "msg-copy";
    copyBtn.title = "Copier ce message";
    copyBtn.textContent = "⧉";
    bubbleDiv.appendChild(copyBtn);
    log.appendChild(bubbleDiv);
    bubbleDiv.scrollIntoView({ behavior: "smooth", block: "end" });

    let fullText = "";
    let hasThinking = false;

    try {
      const priorHistory = history.slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
      const provider = synthProvider ? synthProvider.value : null;
      const model = synthModel ? (synthModel.value || null) : null;
      const topK = synthTopK ? (parseInt(synthTopK.value, 10) || 25) : 25;
      const expandExtra = synthExpandExtra ? (parseInt(synthExpandExtra.value, 10) || 6) : 6;

      const resp = await fetch("/api/synthesis/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          history: priorHistory,
          session_ctx: sessionCtx,
          chat_provider: provider,
          chat_model: model,
          top_k: topK,
          expand_extra: expandExtra,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        stepEl.style.display = "none";
        answerEl.innerHTML = "<span class='ko'>" + escapeHtml(err.error || "Erreur serveur") + "</span>";
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n\n");
        buf = lines.pop(); // garde le fragment incomplet

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }

          if (evt.type === "step") {
            stepEl.textContent = evt.text;
            stepEl.style.display = "";
          } else if (evt.type === "thinking") {
            if (!hasThinking) {
              hasThinking = true;
              thinkWrap.style.display = "";
              thinkWrap.open = true;
            }
            thinkContent.textContent += evt.text;
            thinkContent.scrollTop = thinkContent.scrollHeight;
          } else if (evt.type === "text") {
            stepEl.style.display = "none";
            fullText += evt.text;
            answerEl.innerHTML = markdownToHtml(fullText);
            bubbleDiv.scrollIntoView({ behavior: "smooth", block: "end" });
          } else if (evt.type === "sources") {
            // Réponse complète — fermer le thinking, afficher les sources
            if (hasThinking) thinkWrap.open = false;
            answerEl.innerHTML = markdownToHtml(fullText) + renderSources(evt.sources || []);
            history.push({ role: "assistant", content: fullText, sources: evt.sources || [] });
            saveHistory();
            if (evt.session_ctx) { sessionCtx = evt.session_ctx; saveCtx(); }
            copyBtn.addEventListener("click", () => copyText(fullText, copyBtn));
          } else if (evt.type === "error") {
            stepEl.style.display = "none";
            answerEl.innerHTML = "<span class='ko'>" + escapeHtml(evt.text) + "</span>";
          }
        }
      }
    } catch (err) {
      stepEl.style.display = "none";
      answerEl.innerHTML = "<span class='ko'>Erreur réseau : " + escapeHtml(String(err)) + "</span>";
    } finally {
      btn.disabled = false;
      input.focus();
    }
  });

  clearbtn.addEventListener("click", function () {
    if (history.length && !confirm("Effacer toute la conversation ?")) return;
    history = [];
    sessionCtx = {};
    saveHistory();
    saveCtx();
    log.innerHTML = "";
    input.focus();
  });

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
