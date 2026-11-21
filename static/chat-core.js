// Moteur commun aux trois conversations (Chat, Synthèse, Conseil) : historique,
// rendu markdown et sources, choix du modèle, envoi (JSON ou flux SSE), arrêt.
// Chaque page l'instancie avec ses éléments et ses particularités (cf. chat.js).
window.ChatCore = (function () {
  const App = window.App;
  const escapeHtml = App.escapeHtml;

  function markdownToHtml(text) {
    let s = escapeHtml(text);
    const blocks = [];
    const placeholder = (html) => { blocks.push(html); return "\x00" + (blocks.length - 1) + "\x00"; };
    s = s.replace(/```[\s\S]*?```/g, (m) => {
      const inner = m.slice(3, -3).replace(/^[^\n]*\n?/, "");
      return placeholder("<pre><code>" + inner + "</code></pre>");
    });
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/^### (.+)$/gm, "<h4>$1</h4>");
    s = s.replace(/^## (.+)$/gm, "<h3>$1</h3>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/((?:^|\n)\|.+)+/g, (block) => {
      const lines = block.trim().split("\n").filter(l => l.trim().startsWith("|"));
      if (lines.length < 2) return block;
      const isAlignRow = (l) => /^\|[\s|:-]+\|$/.test(l.trim());
      let rows = lines.filter((l, i) => i === 0 || !isAlignRow(l));
      const parseRow = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const heads = parseRow(rows[0]);
      const body = rows.slice(1);
      let html = "<table><thead><tr>" + heads.map(h => "<th>" + h + "</th>").join("") + "</tr></thead><tbody>";
      body.forEach(r => {
        const cells = parseRow(r);
        if (cells.every(c => !c)) return;
        html += "<tr>" + cells.map(c => "<td>" + c + "</td>").join("") + "</tr>";
      });
      return placeholder(html + "</tbody></table>");
    });
    s = s.replace(/((?:^|\n)[*-] .+)+/g, (block) => {
      const items = block.trim().split(/\n[*-] /).map((x) => x.replace(/^[*-] /, ""));
      return placeholder("<ul>" + items.map((x) => "<li>" + x + "</li>").join("") + "</ul>");
    });
    s = s.replace(/\n/g, "<br>");
    s = s.replace(/\x00(\d+)\x00/g, (_, i) => blocks[+i]);
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
      html += "<a href='" + App.api("/email/" + encodeURIComponent(target)) + "'>" + escapeHtml(label) + "</a>";
    });
    return html + "</div>";
  }

  function readJson(key, fallback) {
    try { return JSON.parse(sessionStorage.getItem(key) || "") || fallback; } catch (e) { return fallback; }
  }
  function writeJson(key, value) {
    try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* quota dépassé : on ignore */ }
  }

  /**
   * o = {
   *   form, input, log, sendBtn, clearBtn, copyBtn, provider, model,   // éléments
   *   storageKey, ctxKey, lsProvider, lsModel,                          // stockage
   *   endpoint, stream,            // route ; true = flux SSE, false = réponse JSON
   *   botLabel, sendLabel, pendingLabel, timeoutMs,
   *   emptyTitle, emptyHint,
   *   extraPayload(),              // champs ajoutés à la requête
   *   promptButtons,               // NodeList de boutons [data-prompt]
   * }
   */
  function create(o) {
    const storageKey = App.caseKey(o.storageKey);
    const ctxKey = App.caseKey(o.ctxKey);
    let history = readJson(storageKey, []);   // [{role:'user'|'assistant', content, sources?}]
    let sessionCtx = readJson(ctxKey, {});
    let controller = null;                    // requête en cours (null = au repos)
    let stoppedByUser = false;
    const sendLabel = o.sendLabel || o.sendBtn.textContent;

    o.log.setAttribute("role", "log");
    o.log.setAttribute("aria-live", "polite");

    // --- Provider / modèle. Le choix n'est pas mémorisé d'une visite à l'autre :
    // la page démarre toujours sur le modèle par défaut du serveur.
    const defaultModel = typeof DEFAULT_CHAT_MODEL !== "undefined" ? DEFAULT_CHAT_MODEL : null;
    if (o.provider) {
      try { localStorage.removeItem(o.lsProvider); localStorage.removeItem(o.lsModel); } catch (e) { /* ignore */ }
      o.provider.addEventListener("change", () => App.loadModels(o.model, o.provider.value, null));
      App.loadModels(o.model, o.provider.value, defaultModel);
    }

    (o.promptButtons || []).forEach((b) => {
      b.addEventListener("click", () => {
        o.input.value = b.dataset.prompt || b.textContent.trim();
        o.input.focus();
      });
    });

    // --- Rendu
    function showEmpty() {
      if (history.length || o.log.querySelector(".empty-state")) return;
      const el = document.createElement("div");
      el.className = "empty-state";
      el.innerHTML = "<strong>" + escapeHtml(o.emptyTitle || "Aucun message") + "</strong>" +
        "<p>" + escapeHtml(o.emptyHint || "") + "</p>";
      o.log.appendChild(el);
    }
    function clearEmpty() {
      const el = o.log.querySelector(".empty-state");
      if (el) el.remove();
    }

    // Bulle de message. `getText` fournit le texte à copier au moment du clic.
    function addBubble(role, contentHtml, getText) {
      clearEmpty();
      const div = document.createElement("div");
      div.className = "msg " + (role === "user" ? "user" : "bot");
      const body = document.createElement("div");
      body.className = "msg-body";
      body.innerHTML = contentHtml;
      div.appendChild(body);
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "msg-copy";
      copy.title = "Copier ce message";
      copy.setAttribute("aria-label", "Copier ce message");
      copy.textContent = "⧉";
      copy.addEventListener("click", () => {
        const text = getText();
        if (text) App.copyText(text, copy);
      });
      div.appendChild(copy);
      o.log.appendChild(div);
      div.scrollIntoView({ behavior: "smooth", block: "end" });
      return { div, body };
    }

    const userHtml = (text) => escapeHtml(text).replace(/\n/g, "<br>");

    function renderAll() {
      o.log.innerHTML = "";
      history.forEach((m) => {
        if (m.role === "user") addBubble("user", userHtml(m.content), () => m.content);
        else addBubble("bot", markdownToHtml(m.content) + renderSources(m.sources), () => m.content);
      });
      showEmpty();
    }
    renderAll();

    function setBusy(busy) {
      o.sendBtn.textContent = busy ? "Arrêter" : sendLabel;
      o.sendBtn.classList.toggle("btn-danger-outline", busy);
      o.input.disabled = busy;
      if (!busy) o.input.focus();
    }

    function errorHtml(msg) { return "<span class='ko'>" + escapeHtml(msg) + "</span>"; }

    // --- Envoi
    async function send(question) {
      addBubble("user", userHtml(question), () => question);
      history.push({ role: "user", content: question });
      writeJson(storageKey, history);
      o.input.value = "";

      let fullText = "";
      let answered = false;          // une réponse (même partielle) a été enregistrée
      const bubble = addBubble("bot", "", () => fullText);
      const think = document.createElement("details");
      think.className = "synth-thinking";
      think.hidden = true;
      think.innerHTML = "<summary>Raisonnement…</summary><div class='synth-thinking-body'></div>";
      const thinkBody = think.lastChild;
      const tools = document.createElement("div");
      tools.className = "adv-tools";
      tools.hidden = true;
      const step = document.createElement("div");
      step.className = "synth-step";
      step.textContent = o.pendingLabel || "Recherche en cours…";
      const answer = document.createElement("div");
      answer.className = "synth-answer";
      bubble.body.append(think, tools, step, answer);

      function finish(sources, ctx, note) {
        think.open = false;
        step.hidden = true;
        answer.innerHTML = markdownToHtml(fullText) + (note ? "<p class='muted'><em>" + escapeHtml(note) + "</em></p>" : "") + renderSources(sources || []);
        history.push({ role: "assistant", content: fullText, sources: sources || [] });
        writeJson(storageKey, history);
        if (ctx) { sessionCtx = ctx; writeJson(ctxKey, sessionCtx); }
        answered = true;
      }
      function fail(msg) {
        step.hidden = true;
        answer.innerHTML = (fullText ? markdownToHtml(fullText) : "") + errorHtml(msg);
      }

      controller = new AbortController();
      stoppedByUser = false;
      let timedOut = false;
      let elapsed = 0;
      const base = step.textContent;
      const ticker = setInterval(() => { elapsed++; if (!fullText) step.textContent = base + " (" + elapsed + " s)"; }, 1000);
      const timeoutId = o.timeoutMs ? setTimeout(() => { timedOut = true; controller.abort(); }, o.timeoutMs) : null;
      setBusy(true);

      try {
        // L'historique part SANS le dernier tour utilisateur (déjà passé en `question`).
        const prior = history.slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
        const payload = Object.assign({
          question, history: prior, session_ctx: sessionCtx,
          chat_provider: o.provider ? o.provider.value : null,
          chat_model: o.model ? (o.model.value || null) : null,
        }, o.extraPayload ? o.extraPayload() : {});
        const resp = await App.request(o.endpoint, { json: payload, signal: controller.signal });

        if (!o.stream) {
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok) { fail(App.errorText(data.error) || "Erreur serveur"); return; }
          fullText = data.answer || "(pas de réponse)";
          finish(data.sources, data.session_ctx);
          return;
        }

        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          fail(App.errorText(err.error) || "Erreur serveur");
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
          buf = lines.pop();   // garde le fragment incomplet
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
            if (evt.type === "step") {
              step.textContent = evt.text;
              step.hidden = false;
            } else if (evt.type === "thinking") {
              if (think.hidden) { think.hidden = false; think.open = true; }
              thinkBody.textContent += evt.text;
              thinkBody.scrollTop = thinkBody.scrollHeight;
            } else if (evt.type === "tool") {
              tools.hidden = false;
              const t = document.createElement("div");
              t.className = "adv-tool-item";
              t.textContent = evt.text;
              tools.appendChild(t);
            } else if (evt.type === "text") {
              step.hidden = true;
              fullText += evt.text;
              answer.innerHTML = markdownToHtml(fullText);
              bubble.div.scrollIntoView({ behavior: "smooth", block: "end" });
            } else if (evt.type === "sources") {
              finish(evt.sources, evt.session_ctx);
            } else if (evt.type === "error") {
              fail(evt.text);
            }
          }
        }
        // Flux clos sans événement final : on garde ce qui a été reçu.
        if (!answered && fullText && !answer.querySelector(".ko")) finish([], null, "Réponse interrompue.");
      } catch (err) {
        if (err.name === "AbortError") {
          if (timedOut) fail("Délai dépassé (> " + Math.round(o.timeoutMs / 60000) + " min). Réessayez.");
          else if (fullText) finish([], null, "Réponse interrompue.");
          else fail("Demande annulée.");
        } else {
          fail("Erreur réseau : " + String(err.message || err));
        }
      } finally {
        clearInterval(ticker);
        if (timeoutId) clearTimeout(timeoutId);
        controller = null;
        // Sans réponse, le tour utilisateur est retiré de l'historique : la question
        // suivante ne doit pas partir avec deux tours utilisateur consécutifs.
        if (!answered) { history.pop(); writeJson(storageKey, history); }
        setBusy(false);
      }
    }

    o.form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (controller) { stoppedByUser = true; controller.abort(); return; }
      const question = o.input.value.trim();
      if (question) send(question);
    });

    // Entrée envoie, Maj+Entrée va à la ligne (zones de texte multi-lignes).
    if (o.input.tagName === "TEXTAREA") {
      o.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); o.form.requestSubmit(); }
      });
    }

    o.clearBtn.addEventListener("click", async function () {
      if (controller) return;
      if (history.length && !(await App.confirm("Effacer toute la conversation ?", { okLabel: "Effacer", danger: true }))) return;
      history = [];
      sessionCtx = {};
      writeJson(storageKey, history);
      writeJson(ctxKey, sessionCtx);
      renderAll();
      o.input.focus();
    });

    o.copyBtn.addEventListener("click", function () {
      if (!history.length) { App.toast("Rien à copier pour l'instant."); return; }
      const text = history
        .map((m) => (m.role === "user" ? "Vous : " : (o.botLabel || "Assistant") + " : ") + m.content)
        .join("\n\n");
      App.copyText(text).then(() => App.toast("Conversation copiée.", { kind: "success" }));
    });
  }

  return { create, markdownToHtml, renderSources };
})();
