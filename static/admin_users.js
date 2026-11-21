const _root = (window.APP_ROOT || "").replace(/\/$/, ""); function api(path) { return _root + path; }

// Gestion des utilisateurs : création, réinitialisation de mot de passe, suppression.
(function () {
  const createBtn = document.getElementById("create-btn");
  const createMsg = document.getElementById("create-msg");
  const reveal = document.getElementById("password-reveal");
  const revealValue = document.getElementById("password-reveal-value");

  function showPassword(password) {
    revealValue.textContent = password;
    reveal.style.display = "block";
  }

  if (createBtn) {
    createBtn.addEventListener("click", async function () {
      const username = document.getElementById("new-username").value.trim();
      const role = document.getElementById("new-role").value;
      if (!username) {
        createMsg.textContent = "Identifiant requis.";
        createMsg.className = "ko";
        return;
      }
      createBtn.disabled = true;
      createMsg.textContent = "Création…";
      createMsg.className = "muted";
      try {
        const resp = await fetch(api("/api/users"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username, role }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          createMsg.textContent = "Erreur : " + (data.error || resp.status);
          createMsg.className = "ko";
        } else {
          createMsg.textContent = "✓ Utilisateur créé.";
          createMsg.className = "ok";
          showPassword(data.password);
          setTimeout(() => window.location.reload(), 1500);
        }
      } catch (err) {
        createMsg.textContent = "Erreur réseau : " + err;
        createMsg.className = "ko";
      } finally {
        createBtn.disabled = false;
      }
    });
  }

  document.querySelectorAll(".reset-btn").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const id = btn.getAttribute("data-id");
      if (!(await App.confirm("Réinitialiser le mot de passe de cet utilisateur ? Un nouveau mot de passe sera généré ; il devra en choisir un autre à sa prochaine connexion.", { okLabel: "Réinitialiser" }))) return;
      btn.disabled = true;
      try {
        const resp = await fetch(api("/api/users/" + id + "/reset-password"), { method: "POST" });
        const data = await resp.json();
        if (!resp.ok) {
          App.toast(App.errorText(data.error) || ("Erreur " + resp.status), { kind: "error" });
        } else {
          showPassword(data.password);
        }
      } catch (err) {
        App.toast("Erreur réseau : " + err, { kind: "error" });
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll(".delete-btn").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const id = btn.getAttribute("data-id");
      if (!(await App.confirm("Supprimer cet utilisateur ? Son accès est retiré immédiatement.", { okLabel: "Supprimer", danger: true }))) return;
      btn.disabled = true;
      try {
        const resp = await fetch(api("/api/users/" + id + "/delete"), { method: "POST" });
        const data = await resp.json();
        if (!resp.ok) {
          App.toast(App.errorText(data.error) || ("Erreur " + resp.status), { kind: "error" });
        } else {
          window.location.reload();
        }
      } catch (err) {
        App.toast("Erreur réseau : " + err, { kind: "error" });
      } finally {
        btn.disabled = false;
      }
    });
  });
})();
