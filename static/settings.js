// Réglages : enregistre providers/modèles/clés et teste les providers.
(function () {
  const savebtn = document.getElementById("savebtn");
  const savemsg = document.getElementById("savemsg");

  function val(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : "";
  }

  function collect() {
    return {
      embed_provider: val("embed_provider"),
      chat_provider: val("chat_provider"),
      embed_model: val("embed_model"),
      chat_model: val("chat_model"),
      mistral_chat_model: val("mistral_chat_model"),
      vllm_base_url: val("vllm_base_url"),
      keys: {
        openai: val("key_openai"),
        anthropic: val("key_anthropic"),
        mistral: val("key_mistral"),
        vllm: val("key_vllm"),
      },
    };
  }

  if (savebtn) {
    savebtn.addEventListener("click", async function () {
      savebtn.disabled = true;
      savemsg.textContent = "Enregistrement…";
      savemsg.className = "muted";
      try {
        const resp = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collect()),
        });
        const data = await resp.json();
        if (!resp.ok) {
          savemsg.textContent = "Erreur : " + (data.error || resp.status);
          savemsg.className = "ko";
        } else {
          savemsg.textContent = "✓ Enregistré (appliqué immédiatement).";
          savemsg.className = "ok";
          // Vide les champs clé (déjà persistés) et rafraîchit les placeholders.
          ["openai", "anthropic", "mistral", "vllm"].forEach((prov) => {
            const el = document.getElementById("key_" + prov);
            const k = (data.settings && data.settings.keys && data.settings.keys[prov]) || {};
            if (el) {
              el.value = "";
              el.placeholder = k.configured
                ? "configuré (" + k.hint + ")"
                : prov === "vllm" ? "optionnel" : "non configuré";
            }
          });
        }
      } catch (err) {
        savemsg.textContent = "Erreur réseau : " + err;
        savemsg.className = "ko";
      } finally {
        savebtn.disabled = false;
      }
    });
  }

  document.querySelectorAll(".test-btn").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const provider = btn.getAttribute("data-provider");
      const out = document.querySelector('.test-result[data-provider="' + provider + '"]');
      if (out) { out.textContent = " test…"; out.className = "test-result muted"; }
      btn.disabled = true;
      try {
        const resp = await fetch("/api/settings/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider }),
        });
        const data = await resp.json();
        if (out) {
          if (data.ok) {
            out.textContent = " ✓ OK — " + data.n_models + " modèle(s)";
            out.className = "test-result ok";
          } else {
            out.textContent = " ✗ " + (data.error || "échec");
            out.className = "test-result ko";
          }
        }
      } catch (err) {
        if (out) { out.textContent = " ✗ " + err; out.className = "test-result ko"; }
      } finally {
        btn.disabled = false;
      }
    });
  });
})();
