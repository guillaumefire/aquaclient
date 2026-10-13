(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  let clientActif = null;
  let visiteActive = null;
  let plusProcheHorsRayon = null;
  let intentForce = null;
  let gpsOverride = null;
  let suggestTimer = null;
  let suggestIndex = -1;
  let suggestAbort = null;
  let recording = false;

  const screens = {
    home: $("#screen-home"),
    confirm: $("#screen-confirm"),
    type: $("#screen-type"),
    none: $("#screen-none"),
    liste: $("#screen-liste"),
    carte: $("#screen-carte"),
    agenda: $("#screen-agenda"),
    factures: $("#screen-factures"),
    dashboard: $("#screen-dashboard"),
    visite: $("#screen-visite"),
    nouveau: $("#screen-nouveau"),
    loading: $("#screen-loading"),
  };

  const TYPE_LABELS = {
    depannage: "Dépannage",
    entretien_simple: "Entretien classique",
    entretien_complet: "Entretien complet",
    hivernage: "Hivernage",
  };

  /** @type {{id:string,label:string,photos:Array}[]} */
  let hivernageSteps = [];
  /** @type {Record<string, Array>} photos serveur par instruction_id */
  let hivernagePhotosByInst = {};
  /** @type {Record<string, {localId:string,file:File,url:string}[]>} photos locales avant création client */
  let pendingPhotosByInst = {};
  let dashFiltre = "tous";

  function newInstructionId() {
    if (crypto?.randomUUID) return crypto.randomUUID();
    return `hiv_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  }

  function normalizeHivernageSteps(list) {
    const raw = Array.isArray(list) ? list : [];
    return raw
      .map((item) => {
        if (item && typeof item === "object") {
          const label = String(item.label || item.texte || "").trim();
          const id = String(item.id || "").trim() || newInstructionId();
          // garder les lignes vides en édition (nouvelles étapes)
          return {
            id,
            label,
            photos: Array.isArray(item.photos) ? item.photos : [],
          };
        }
        const label = String(item || "").trim();
        if (!label) return null;
        return { id: newInstructionId(), label, photos: [] };
      })
      .filter(Boolean);
  }

  function collectHivernageFromEditor() {
    const root = $("#hivernage-editor");
    if (!root) return [];
    return $$(".hiv-step", root)
      .map((row) => {
        const id = row.dataset.id || newInstructionId();
        const label = ($("input[type='text']", row)?.value || "").trim();
        if (!label) return null;
        return { id, label };
      })
      .filter(Boolean);
  }

  function clearPendingPhotos() {
    Object.values(pendingPhotosByInst).forEach((arr) => {
      (arr || []).forEach((p) => {
        if (p.url) URL.revokeObjectURL(p.url);
      });
    });
    pendingPhotosByInst = {};
  }

  function openPhotoLightbox(url) {
    const box = $("#photo-lightbox");
    const img = $("#photo-lightbox-img");
    if (!box || !img) return;
    img.src = url;
    box.hidden = false;
  }

  function closePhotoLightbox() {
    const box = $("#photo-lightbox");
    const img = $("#photo-lightbox-img");
    if (img) img.removeAttribute("src");
    if (box) box.hidden = true;
  }

  function renderHivernageEditor() {
    const root = $("#hivernage-editor");
    if (!root) return;

    root.innerHTML = "";
    if (!hivernageSteps.length) {
      const empty = document.createElement("p");
      empty.className = "muted muted--sm";
      empty.textContent = "Aucune étape pour l’instant — ajoute-en une ci-dessous.";
      root.appendChild(empty);
      return;
    }

    hivernageSteps.forEach((step) => {
      const photos = [
        ...(step.photos || []),
        ...(hivernagePhotosByInst[step.id] || []),
      ];
      const seen = new Set();
      const uniq = photos.filter((p) => {
        const pid = String(p.id || "");
        if (!pid || seen.has(pid)) return false;
        seen.add(pid);
        return true;
      });
      const pending = pendingPhotosByInst[step.id] || [];

      const row = document.createElement("div");
      row.className = "hiv-step";
      row.dataset.id = step.id;
      row.innerHTML =
        `<div class="hiv-step-row">` +
        `<input type="text" value="${escapeHtml(step.label)}" placeholder="Instruction…" />` +
        `<button type="button" class="hiv-step-remove" data-act="remove">Retirer</button>` +
        `</div>` +
        `<div class="hiv-photos" data-photos></div>`;

      const photosEl = $("[data-photos]", row);
      uniq.forEach((ph) => {
        const url = ph.url || `/api/photos/${ph.id}/file`;
        const fig = document.createElement("div");
        fig.className = "hiv-photo";
        fig.innerHTML =
          `<img src="${escapeHtml(url)}" alt="" loading="lazy" />` +
          `<button type="button" class="hiv-photo-del" data-photo-id="${escapeHtml(ph.id)}" title="Supprimer">×</button>`;
        $("img", fig)?.addEventListener("click", () => openPhotoLightbox(url));
        $(".hiv-photo-del", fig)?.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          if (!window.confirm("Supprimer cette photo ?")) return;
          try {
            await api(`/api/photos/${ph.id}`, { method: "DELETE" });
            hivernagePhotosByInst[step.id] = (hivernagePhotosByInst[step.id] || []).filter(
              (x) => x.id !== ph.id
            );
            step.photos = (step.photos || []).filter((x) => x.id !== ph.id);
            renderHivernageEditor();
          } catch (err) {
            setFeedback($("#nouveau-feedback"), err.message, false);
          }
        });
        photosEl.appendChild(fig);
      });

      pending.forEach((ph) => {
        const fig = document.createElement("div");
        fig.className = "hiv-photo";
        fig.innerHTML =
          `<img src="${escapeHtml(ph.url)}" alt="" />` +
          `<button type="button" class="hiv-photo-del" title="Retirer">×</button>`;
        $("img", fig)?.addEventListener("click", () => openPhotoLightbox(ph.url));
        $(".hiv-photo-del", fig)?.addEventListener("click", (ev) => {
          ev.stopPropagation();
          pendingPhotosByInst[step.id] = (pendingPhotosByInst[step.id] || []).filter(
            (x) => x.localId !== ph.localId
          );
          URL.revokeObjectURL(ph.url);
          renderHivernageEditor();
        });
        photosEl.appendChild(fig);
      });

      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "hiv-add-photo";
      addBtn.textContent = "＋ Photo";
      addBtn.addEventListener("click", () => pickAndUploadPhoto(step.id));
      photosEl.appendChild(addBtn);

      $(".hiv-step-remove", row)?.addEventListener("click", () => {
        const kept = collectHivernageFromEditor().filter((s) => s.id !== step.id);
        // conserver aussi l'étape courante si label encore vide (pas dans collect)
        hivernageSteps = kept.length
          ? kept
          : hivernageSteps.filter((s) => s.id !== step.id);
        const pend = pendingPhotosByInst[step.id] || [];
        pend.forEach((p) => URL.revokeObjectURL(p.url));
        delete pendingPhotosByInst[step.id];
        renderHivernageEditor();
      });

      // sync label live dans hivernageSteps
      $("input[type='text']", row)?.addEventListener("input", (ev) => {
        const s = hivernageSteps.find((x) => x.id === step.id);
        if (s) s.label = ev.target.value;
      });

      root.appendChild(row);
    });
  }

  async function uploadPhotoFile(clientId, instructionId, file) {
    const fd = new FormData();
    fd.append("instruction_id", instructionId);
    fd.append("file", file);
    const res = await fetch(`/api/clients/${clientId}/photos`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = err.detail;
      const msg =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d) => d.msg || d).join(", ")
            : `Erreur ${res.status}`;
      throw new Error(msg);
    }
    return res.json();
  }

  async function flushPendingPhotos(clientId) {
    const entries = Object.entries(pendingPhotosByInst);
    if (!entries.length) return;
    for (const [instructionId, list] of entries) {
      for (const ph of list || []) {
        try {
          const photo = await uploadPhotoFile(clientId, instructionId, ph.file);
          if (!hivernagePhotosByInst[instructionId]) hivernagePhotosByInst[instructionId] = [];
          hivernagePhotosByInst[instructionId].push(photo);
        } catch (err) {
          setFeedback($("#nouveau-feedback"), err.message || String(err), false);
        }
        if (ph.url) URL.revokeObjectURL(ph.url);
      }
    }
    pendingPhotosByInst = {};
  }

  async function pickAndUploadPhoto(instructionId) {
    // Sync labels depuis l'éditeur
    const live = collectHivernageFromEditor();
    const fromLive = live.find((s) => s.id === instructionId);
    const fromState = hivernageSteps.find((s) => s.id === instructionId);
    const label = (fromLive?.label || fromState?.label || "").trim();
    if (!label) {
      setFeedback(
        $("#nouveau-feedback"),
        "Renseignez le texte de l'étape avant d'ajouter une photo.",
        false
      );
      return;
    }
    // garder les labels à jour
    hivernageSteps = hivernageSteps.map((s) => {
      const m = live.find((x) => x.id === s.id);
      return m ? { ...s, label: m.label } : s;
    });

    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      const clientId = ($("#edit-client-id")?.value || "").trim();
      if (!clientId) {
        if (!pendingPhotosByInst[instructionId]) pendingPhotosByInst[instructionId] = [];
        pendingPhotosByInst[instructionId].push({
          localId: newInstructionId(),
          file,
          url: URL.createObjectURL(file),
        });
        renderHivernageEditor();
        setFeedback($("#nouveau-feedback"), "Photo prête — elle sera envoyée à l’enregistrement.", true);
        return;
      }
      setFeedback($("#nouveau-feedback"), "Envoi de la photo…", null);
      try {
        await api(`/api/clients/${clientId}`, {
          method: "PUT",
          body: JSON.stringify(buildClientPayloadFromForm()),
        });
        const photo = await uploadPhotoFile(clientId, instructionId, file);
        if (!hivernagePhotosByInst[instructionId]) hivernagePhotosByInst[instructionId] = [];
        hivernagePhotosByInst[instructionId].push(photo);
        renderHivernageEditor();
        setFeedback($("#nouveau-feedback"), "Photo enregistrée.", true);
      } catch (err) {
        setFeedback($("#nouveau-feedback"), err.message || String(err), false);
      }
    };
    input.click();
  }

  function buildClientPayloadFromForm() {
    const form = $("#form-nouveau");
    const latVal = $("#input-lat").value.trim();
    const lonVal = $("#input-lon").value.trim();
    const typeEl = form.querySelector('input[name="type_entretien"]:checked');
    const freqEl = form.querySelector('input[name="frequence_passage"]:checked');
    const typeEntretien = typeEl ? typeEl.value : "entretien_simple";
    const payload = {
      nom: form.nom.value.trim(),
      prenom: form.prenom.value.trim(),
      adresse: form.adresse.value.trim() || `GPS ${latVal}, ${lonVal}`,
      pompe: form.pompe.value.trim(),
      filtre: form.filtre.value.trim(),
      robot: form.robot.value.trim(),
      skimmer: form.skimmer.value.trim(),
      autres: form.autres.value.trim(),
      vanne_vidange: form.vanne_vidange.checked,
      injecteurs: form.injecteurs.checked,
      type_entretien: typeEntretien,
      frequence_passage:
        typeEntretien === "entretien_complet" && freqEl ? freqEl.value : "",
      date_debut_passage:
        typeEntretien === "entretien_complet"
          ? ($("#input-date-passage")?.value || "").trim()
          : "",
      creer_compte: !!$("#chk-creer-compte")?.checked,
      email: ($("#input-email")?.value || "").trim(),
      telephone: ($("#input-telephone")?.value || "").trim(),
      password: ($("#input-password")?.value || "").trim(),
      checklist_hivernage: collectHivernageFromEditor(),
      piscine_hivernee: !!$("#chk-piscine-hivernee")?.checked,
    };
    if (latVal && lonVal) {
      payload.latitude = parseFloat(latVal.replace(",", "."));
      payload.longitude = parseFloat(lonVal.replace(",", "."));
    } else if (gpsOverride) {
      payload.latitude = gpsOverride.latitude;
      payload.longitude = gpsOverride.longitude;
    }
    return payload;
  }

  async function loadClientPhotos(clientId) {
    hivernagePhotosByInst = {};
    if (!clientId) return;
    try {
      const res = await api(`/api/clients/${clientId}/photos`);
      (res.photos || []).forEach((ph) => {
        const iid = ph.instruction_id;
        if (!hivernagePhotosByInst[iid]) hivernagePhotosByInst[iid] = [];
        hivernagePhotosByInst[iid].push(ph);
      });
    } catch {
      /* ignore */
    }
  }

  function show(name) {
    const leavingVisite =
      screens.visite.classList.contains("screen--active") && name !== "visite";
    Object.values(screens).forEach((el) => el.classList.remove("screen--active"));
    screens[name].classList.add("screen--active");
    if (leavingVisite) stopWakeListen();
  }

  function setFeedback(el, message, ok) {
    el.textContent = message || "";
    el.classList.toggle("is-ok", !!ok);
    el.classList.toggle("is-err", ok === false);
  }

  function formatDistance(m) {
    return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${m} m`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, { ...options, headers, cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      const msg =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d) => d.msg || JSON.stringify(d)).join(", ")
            : "Erreur serveur";
      throw new Error(msg);
    }
    return data;
  }

  async function refreshFooter() {
    const verEl = $("#footer-version");
    const storEl = $("#footer-storage");
    const cliEl = $("#footer-clients");
    try {
      const h = await api("/api/health");
      if (verEl) verEl.textContent = `v${h.version || "?"}`;
      if (storEl) {
        if (h.storage === "supabase") {
          storEl.textContent = "Supabase";
        } else if (h.storage_reason === "missing_env") {
          storEl.textContent = "CSV (env Render manquantes)";
        } else if (h.storage_reason === "init_failed") {
          storEl.textContent = "CSV (init Supabase échouée)";
        } else {
          storEl.textContent = "CSV local";
        }
        storEl.classList.toggle("is-cloud", h.storage === "supabase");
        storEl.classList.toggle("is-local", h.storage !== "supabase");
      }
      if (cliEl) {
        cliEl.textContent =
          typeof h.clients === "number" && h.clients >= 0
            ? `${h.clients} client${h.clients > 1 ? "s" : ""}`
            : "clients ?";
      }
      document.documentElement.dataset.appVersion = h.version || "";
      document.documentElement.dataset.storage = h.storage || "";
    } catch {
      if (storEl) {
        storEl.textContent = "hors ligne";
        storEl.classList.add("is-local");
      }
    }
  }

  refreshFooter();
  setInterval(refreshFooter, 30000);

  function getPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("Géolocalisation indisponible."));
        return;
      }
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true,
        timeout: 20000,
        maximumAge: 0,
      });
    });
  }

  $$("[data-goto]").forEach((btn) => {
    btn.addEventListener("click", () => show(btn.dataset.goto));
  });

  $("#btn-home-brand")?.addEventListener("click", () => show("home"));

  function resetNouveau() {
    $("#form-nouveau").reset();
    $("#edit-client-id").value = "";
    $("#nouveau-eyebrow").textContent = "Enregistrement";
    $("#nouveau-title").textContent = "Nouveau client";
    $("#btn-submit-nouveau").textContent = "Enregistrer";
    $("#btn-supprimer-client").hidden = true;
    setFeedback($("#nouveau-feedback"), "", null);
    hideSuggestions();
    gpsOverride = null;
    $("#input-lat").value = "";
    $("#input-lon").value = "";
    const radio = document.querySelector('input[name="type_entretien"][value="entretien_simple"]');
    if (radio) radio.checked = true;
    document.querySelectorAll('input[name="frequence_passage"]').forEach((r) => {
      r.checked = false;
    });
    $("#input-date-passage").value = "";
    $("#chk-creer-compte").checked = false;
    $("#input-email").value = "";
    $("#input-telephone").value = "";
    $("#input-password").value = "";
    $("#input-password").placeholder = "Auto si vide";
    $("#label-password").textContent = "Mot de passe";
    $("#compte-identifiant-line").hidden = true;
    $("#compte-credentials").hidden = true;
    delete $("#btn-submit-nouveau")?.dataset?.continueVisit;
    clearPendingPhotos();
    hivernageSteps = [];
    hivernagePhotosByInst = {};
    const chkHiv = $("#chk-piscine-hivernee");
    if (chkHiv) chkHiv.checked = false;
    renderHivernageEditor();
    syncBlocPassage();
    syncBlocCompte();
  }

  function syncBlocCompte() {
    const on = !!$("#chk-creer-compte")?.checked;
    const bloc = $("#bloc-compte");
    if (bloc) bloc.hidden = !on;
    const email = $("#input-email");
    const tel = $("#input-telephone");
    if (email) email.required = false;
    if (tel) tel.required = false;
  }

  function genererPasswordLocal(len = 10) {
    const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
    let out = "";
    const arr = new Uint32Array(len);
    crypto.getRandomValues(arr);
    for (let i = 0; i < len; i++) out += chars[arr[i] % chars.length];
    return out;
  }

  function afficherCredentials(client) {
    const box = $("#compte-credentials");
    if (!box) return;
    if (!client?.password_clair && !client?.compte_actif) {
      box.hidden = true;
      return;
    }
    if (!client?.password_clair) {
      box.hidden = true;
      return;
    }
    $("#cred-identifiant").textContent = client.identifiant || "—";
    $("#cred-login").textContent =
      [client.email, client.telephone].filter(Boolean).join(" · ") || "—";
    $("#cred-password").textContent = client.password_clair;
    box.hidden = false;
  }

  function formatDateFr(iso) {
    if (!iso) return "—";
    const [y, m, d] = String(iso).slice(0, 10).split("-");
    if (!y || !m || !d) return iso;
    return `${d}/${m}/${y}`;
  }

  function formatHistDates(item) {
    const saisie = formatDateFr(item.date);
    const ref = (item.date_ref || "").trim();
    if (!ref) {
      return `<span class="meta">${escapeHtml(saisie)}</span>`;
    }
    return (
      `<span class="meta meta--dates">` +
      `<span>${escapeHtml(saisie)}</span>` +
      `<span>${escapeHtml(formatDateFr(ref))}</span>` +
      `</span>`
    );
  }

  function labelFrequence(freq) {
    if (freq === "hebdomadaire") return "chaque semaine";
    if (freq === "bihebdomadaire") return "toutes les 2 semaines";
    return "";
  }

  function syncBlocPassage() {
    const complet = !!document.querySelector(
      'input[name="type_entretien"][value="entretien_complet"]:checked'
    );
    const bloc = $("#bloc-passage");
    if (bloc) bloc.hidden = !complet;
    const preview = $("#passage-preview");
    if (!preview) return;
    if (!complet) {
      preview.textContent = "";
      return;
    }
    const freqEl = document.querySelector('input[name="frequence_passage"]:checked');
    const dateVal = $("#input-date-passage")?.value || "";
    if (!freqEl || !dateVal) {
      preview.textContent = "Choisissez la fréquence et la date du 1er passage.";
      return;
    }
    const step = freqEl.value === "hebdomadaire" ? 7 : 14;
    const start = new Date(`${dateVal}T12:00:00`);
    if (Number.isNaN(start.getTime())) {
      preview.textContent = "Date invalide.";
      return;
    }
    const next = new Date(start);
    next.setDate(next.getDate() + step);
    const next2 = new Date(start);
    next2.setDate(next2.getDate() + step * 2);
    const fmt = (dt) =>
      `${String(dt.getDate()).padStart(2, "0")}/${String(dt.getMonth() + 1).padStart(2, "0")}/${dt.getFullYear()}`;
    preview.textContent = `Puis passages le ${fmt(next)}, ${fmt(next2)}, etc. (${labelFrequence(freqEl.value)}).`;
  }

  function remplirFormClient(client) {
    const form = $("#form-nouveau");
    form.nom.value = client.nom || "";
    form.prenom.value = client.prenom || "";
    form.adresse.value = client.adresse || "";
    form.pompe.value = client.pompe || "";
    form.filtre.value = client.filtre || "";
    form.robot.value = client.robot || "";
    form.skimmer.value = client.skimmer || "";
    form.autres.value = client.autres || "";
    form.vanne_vidange.checked = !!client.vanne_vidange;
    form.injecteurs.checked = !!client.injecteurs;
    $("#input-lat").value = client.latitude != null ? String(client.latitude) : "";
    $("#input-lon").value = client.longitude != null ? String(client.longitude) : "";
    const te = client.type_entretien === "entretien_complet" ? "entretien_complet" : "entretien_simple";
    const radio = document.querySelector(`input[name="type_entretien"][value="${te}"]`);
    if (radio) radio.checked = true;
    document.querySelectorAll('input[name="frequence_passage"]').forEach((r) => {
      r.checked = r.value === (client.frequence_passage || "");
    });
    $("#input-date-passage").value = (client.date_debut_passage || "").slice(0, 10);
    $("#input-email").value = client.email || "";
    $("#input-telephone").value = client.telephone || "";
    $("#chk-creer-compte").checked = !!client.compte_actif;
    const chkHiv = $("#chk-piscine-hivernee");
    if (chkHiv) chkHiv.checked = !!client.piscine_hivernee;
    clearPendingPhotos();
    hivernageSteps = normalizeHivernageSteps(client.checklist_hivernage);
    renderHivernageEditor();
    $("#input-password").value = "";
    $("#input-password").placeholder = client.has_password
      ? "Laisser vide pour conserver"
      : "Auto si vide";
    $("#label-password").textContent = client.has_password
      ? "Nouveau mot de passe (optionnel)"
      : "Mot de passe";
    if (client.identifiant) {
      $("#compte-identifiant").textContent = client.identifiant;
      $("#compte-identifiant-line").hidden = false;
    } else {
      $("#compte-identifiant-line").hidden = true;
    }
    $("#compte-credentials").hidden = true;
    syncBlocPassage();
    syncBlocCompte();
  }

  async function ouvrirEditionClient(client) {
    clientActif = client;
    resetNouveau();
    $("#edit-client-id").value = String(client.id);
    $("#nouveau-eyebrow").textContent = "Fiche client";
    $("#nouveau-title").textContent = "Modifier le client";
    $("#btn-submit-nouveau").textContent = "Enregistrer les modifications";
    $("#btn-supprimer-client").hidden = false;
    await loadClientPhotos(client.id);
    remplirFormClient(client);
    show("nouveau");
  }

  $("#btn-nouveau").addEventListener("click", () => {
    resetNouveau();
    show("nouveau");
  });
  $("#btn-nouveau-depuis-none").addEventListener("click", () => {
    resetNouveau();
    show("nouveau");
  });
  $("#btn-back-nouveau")?.addEventListener("click", () => {
    show($("#edit-client-id").value ? "liste" : "home");
  });
  $("#btn-modifier-confirm")?.addEventListener("click", () => {
    if (clientActif) ouvrirEditionClient(clientActif);
  });
  $("#btn-modifier-visite")?.addEventListener("click", () => {
    if (clientActif) ouvrirEditionClient(clientActif);
  });
  function proposerClient(client, distanceM, { horsRayon = false } = {}) {
    clientActif = client;
    $("#confirm-eyebrow").textContent = horsRayon ? "Client le plus proche" : "Client à proximité";
    $("#confirm-name").textContent = `${client.prenom} ${client.nom}`;
    $("#confirm-adresse").textContent = client.adresse;
    $("#confirm-distance").textContent = `À environ ${formatDistance(distanceM)}`;
    const hint = $("#confirm-hint");
    hint.hidden = !horsRayon;
    hint.textContent = horsRayon
      ? "Hors rayon 1 km (GPS PC souvent imprécis). Validez si c’est le bon."
      : "";
    show("confirm");
  }

  function typeEntretienClient(client) {
    return client?.type_entretien === "entretien_complet"
      ? "entretien_complet"
      : "entretien_simple";
  }

  async function ouvrirVisite(client, typeVisite) {
    if (!client) return;
    clientActif = client;
    show("loading");
    $("#loading-text").textContent = "Ouverture de la visite…";
    try {
      const visite = await api("/api/visites", {
        method: "POST",
        body: JSON.stringify({ client_id: client.id, type: typeVisite }),
      });
      visiteActive = visite;
      await afficherVisite(visite);
      show("visite");
    } catch (err) {
      show("home");
      setFeedback($("#geo-status"), err.message, false);
      alert(err.message || "Impossible d'ouvrir la visite. Rechargez la page (Ctrl+F5).");
    }
  }

  function allerChezClient(client) {
    return ouvrirVisite(client, typeEntretienClient(client));
  }

  $("#btn-valider").addEventListener("click", () => {
    if (!clientActif) return;
    allerChezClient(clientActif);
  });

  $("#btn-back-type").addEventListener("click", () => show("confirm"));

  $$("[data-visite]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!clientActif) return;
      ouvrirVisite(clientActif, btn.dataset.visite);
    });
  });

  $("#btn-depannage-visite")?.addEventListener("click", async () => {
    if (!clientActif) return;
    if (visiteActive?.type === "depannage") {
      setFeedback($("#check-feedback"), "Visite déjà en mode dépannage.", true);
      return;
    }
    const ok = window.confirm("Passer en visite dépannage pour ce client ?");
    if (!ok) return;
    await ouvrirVisite(clientActif, "depannage");
  });

  $("#btn-hivernage-visite")?.addEventListener("click", async () => {
    if (!clientActif) return;
    if (visiteActive?.type === "hivernage") {
      setFeedback($("#check-feedback"), "Visite déjà en mode hivernage.", true);
      return;
    }
    const ok = window.confirm("Passer en visite hivernage pour ce client ?");
    if (!ok) return;
    await ouvrirVisite(clientActif, "hivernage");
  });

  $("#btn-add-hivernage-step")?.addEventListener("click", () => {
    const labeled = collectHivernageFromEditor();
    // conserver les lignes encore vides déjà présentes
    const emptyKept = hivernageSteps.filter(
      (s) => !(s.label || "").trim() && !labeled.some((l) => l.id === s.id)
    );
    hivernageSteps = [...labeled, ...emptyKept, { id: newInstructionId(), label: "", photos: [] }];
    renderHivernageEditor();
    const inputs = $$("#hivernage-editor input[type='text']");
    inputs[inputs.length - 1]?.focus();
  });

  $("#photo-lightbox")?.addEventListener("click", () => closePhotoLightbox());
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closePhotoLightbox();
  });

  $("#btn-client").addEventListener("click", async () => {
    show("loading");
    $("#loading-text").textContent = "Localisation…";
    $("#geo-status").textContent = "";
    plusProcheHorsRayon = null;
    try {
      const pos = await getPosition();
      $("#loading-text").textContent = "Recherche client…";
      const data = await api("/api/clients/proche", {
        method: "POST",
        body: JSON.stringify({
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
        }),
      });
      if (data.trouve) {
        proposerClient(data.client, data.distance_m);
        return;
      }
      if (data.plus_proche || data.client) {
        const c = data.plus_proche || data.client;
        plusProcheHorsRayon = { client: c, distance_m: data.distance_m };
        $("#none-detail").textContent =
          `Personne dans 1 km. Plus proche : ${c.prenom} ${c.nom} à ${formatDistance(data.distance_m)}.`;
        $("#btn-utiliser-proche").hidden = false;
        show("none");
        return;
      }
      $("#none-detail").textContent = data.message || "Aucun client.";
      $("#btn-utiliser-proche").hidden = true;
      show("none");
    } catch (err) {
      show("home");
      $("#geo-status").textContent =
        err.code === 1 ? "Autorisez la localisation." : err.message || "Position impossible.";
    }
  });

  $("#btn-utiliser-proche").addEventListener("click", () => {
    if (!plusProcheHorsRayon) return;
    proposerClient(plusProcheHorsRayon.client, plusProcheHorsRayon.distance_m, { horsRayon: true });
  });

  $("#btn-refuser").addEventListener("click", () => {
    show("liste");
    chargerListe();
  });

  function syncSelectionToolbar() {
    const checks = $$("#liste-clients input[data-select-client]");
    const selected = checks.filter((c) => c.checked);
    const btn = $("#btn-supprimer-selection");
    const all = $("#chk-select-all");
    if (btn) {
      btn.disabled = selected.length === 0;
      btn.textContent =
        selected.length > 0
          ? `Supprimer (${selected.length})`
          : "Supprimer la sélection";
    }
    if (all && checks.length) {
      all.checked = selected.length === checks.length;
      all.indeterminate = selected.length > 0 && selected.length < checks.length;
    } else if (all) {
      all.checked = false;
      all.indeterminate = false;
    }
  }

  async function chargerListe() {
    const ul = $("#liste-clients");
    const all = $("#chk-select-all");
    if (all) {
      all.checked = false;
      all.indeterminate = false;
    }
    ul.innerHTML = '<li class="empty">Chargement…</li>';
    try {
      const clients = await api("/api/clients");
      ul.innerHTML = "";
      refreshFooter();
      if (!clients.length) {
        ul.innerHTML = '<li class="empty">Aucun client</li>';
        syncSelectionToolbar();
        return;
      }
      clients.forEach((c) => {
        const li = document.createElement("li");
        li.className = "pick-item pick-item--row";
        const typeLabel =
          c.type_entretien === "entretien_complet" ? "Complet" : "Classique";
        li.innerHTML = `
          <label class="pick-check">
            <input type="checkbox" data-select-client value="${escapeHtml(String(c.id))}" />
            <span class="visually-hidden">Sélectionner</span>
          </label>
          <button type="button" class="pick-main">
            <strong>${escapeHtml(c.prenom)} ${escapeHtml(c.nom)}</strong>
            <span class="meta">${escapeHtml(c.adresse)}</span>
            <span class="meta">${typeLabel}</span>
          </button>
          <button type="button" class="btn btn--ghost pick-edit" aria-label="Modifier">✎</button>
        `;
        li.querySelector(".pick-main").addEventListener("click", () => allerChezClient(c));
        li.querySelector(".pick-edit").addEventListener("click", (ev) => {
          ev.stopPropagation();
          ouvrirEditionClient(c);
        });
        li.querySelector("input[data-select-client]").addEventListener("change", () => {
          li.classList.toggle("is-selected", li.querySelector("input").checked);
          syncSelectionToolbar();
        });
        ul.appendChild(li);
      });
      syncSelectionToolbar();
    } catch (err) {
      ul.innerHTML = `<li class="empty">${escapeHtml(err.message)}</li>`;
      syncSelectionToolbar();
    }
  }

  $("#chk-select-all")?.addEventListener("change", (e) => {
    const on = e.target.checked;
    $$("#liste-clients input[data-select-client]").forEach((c) => {
      c.checked = on;
      c.closest("li")?.classList.toggle("is-selected", on);
    });
    syncSelectionToolbar();
  });

  $("#btn-supprimer-selection")?.addEventListener("click", async () => {
    const ids = $$("#liste-clients input[data-select-client]:checked").map((c) =>
      Number(c.value)
    );
    if (!ids.length) return;
    const ok = window.confirm(
      `Supprimer ${ids.length} client${ids.length > 1 ? "s" : ""} ? Notes, analyses, fournitures et visites liées seront aussi effacées.`
    );
    if (!ok) return;
    try {
      await api("/api/clients/supprimer", {
        method: "POST",
        body: JSON.stringify({ ids }),
      });
      await chargerListe();
    } catch (err) {
      alert(err.message || "Suppression impossible.");
    }
  });

  $("#btn-liste").addEventListener("click", () => {
    show("liste");
    chargerListe();
  });
  $("#btn-liste-depuis-none").addEventListener("click", () => {
    show("liste");
    chargerListe();
  });

  async function chargerDashboard() {
    const status = $("#dash-status");
    const ul = $("#liste-dashboard");
    const summary = $("#dash-summary");
    if (status) status.textContent = "Chargement…";
    if (ul) ul.innerHTML = "";
    try {
      const data = await api("/api/dashboard");
      if (summary) {
        summary.innerHTML =
          `<div class="dash-stat"><strong>${data.total_clients}</strong><span>Clients</span></div>` +
          `<div class="dash-stat dash-stat--warn"><strong>${data.factures_impayees_total}</strong><span>Factures à payer</span></div>` +
          `<div class="dash-stat dash-stat--ok"><strong>${data.piscines_hivernees}</strong><span>Hivernées</span></div>` +
          `<div class="dash-stat"><strong>${data.total_clients - data.piscines_hivernees}</strong><span>En eau</span></div>`;
      }
      let rows = data.clients || [];
      if (dashFiltre === "impayes") rows = rows.filter((c) => c.a_impaye);
      else if (dashFiltre === "hivernees") rows = rows.filter((c) => c.piscine_hivernee);
      else if (dashFiltre === "actives") rows = rows.filter((c) => !c.piscine_hivernee);

      if (status) {
        status.textContent = rows.length
          ? `${rows.length} client${rows.length > 1 ? "s" : ""}`
          : "Aucun client pour ce filtre.";
      }
      rows.forEach((c) => {
        const li = document.createElement("li");
        li.className = "dash-item";
        const flags = [];
        if (c.a_impaye) {
          flags.push(
            `<span class="dash-flag dash-flag--debt">${c.factures_impayees} impayée${
              c.factures_impayees > 1 ? "s" : ""
            }</span>`
          );
        } else {
          flags.push(`<span class="dash-flag dash-flag--ok">Factures OK</span>`);
        }
        if (c.piscine_hivernee) {
          flags.push(`<span class="dash-flag dash-flag--ice">Hivernée</span>`);
        } else {
          flags.push(`<span class="dash-flag">En eau</span>`);
        }
        li.innerHTML =
          `<button type="button" class="dash-main" data-id="${c.id}">` +
          `<strong>${escapeHtml(c.label)}</strong>` +
          `<span class="muted">${escapeHtml(c.adresse || "")}</span>` +
          `<span class="dash-flags">${flags.join("")}</span>` +
          `</button>` +
          `<label class="dash-toggle" title="Statut hivernage">` +
          `<input type="checkbox" data-hiv-id="${c.id}" ${c.piscine_hivernee ? "checked" : ""} />` +
          `<span>Hivernée</span></label>`;
        $(".dash-main", li)?.addEventListener("click", async () => {
          try {
            const full = await api(`/api/clients/${c.id}`);
            clientActif = full;
            await ouvrirEditionClient(full);
          } catch (err) {
            setFeedback(status, err.message, false);
          }
        });
        $("input[data-hiv-id]", li)?.addEventListener("change", async (ev) => {
          const on = !!ev.target.checked;
          try {
            await api(`/api/clients/${c.id}/hivernage-statut`, {
              method: "PATCH",
              body: JSON.stringify({ piscine_hivernee: on }),
            });
            await chargerDashboard();
          } catch (err) {
            ev.target.checked = !on;
            setFeedback(status, err.message, false);
          }
        });
        ul?.appendChild(li);
      });
    } catch (err) {
      if (status) status.textContent = err.message || "Erreur dashboard";
    }
  }

  $("#btn-dashboard")?.addEventListener("click", () => {
    show("dashboard");
    chargerDashboard();
  });

  $$("#dash-filtres [data-dash-filtre]").forEach((btn) => {
    btn.addEventListener("click", () => {
      dashFiltre = btn.dataset.dashFiltre || "tous";
      $$("#dash-filtres [data-dash-filtre]").forEach((b) =>
        b.classList.toggle("is-on", b === btn)
      );
      chargerDashboard();
    });
  });

  let carteMap = null;
  let carteMarkers = null;

  async function afficherCarteClients() {
    show("carte");
    const status = $("#carte-status");
    const el = $("#carte-clients");
    if (!el) return;
    if (typeof L === "undefined") {
      status.textContent = "Carte indisponible (Leaflet non chargé).";
      return;
    }
    status.textContent = "Chargement des clients…";

    try {
      const clients = await api("/api/clients");
      const withGps = clients.filter(
        (c) => c.latitude != null && c.longitude != null && !Number.isNaN(Number(c.latitude))
      );

      if (!carteMap) {
        carteMap = L.map(el, { zoomControl: true });
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
          attribution: "&copy; OpenStreetMap",
        }).addTo(carteMap);
        carteMarkers = L.layerGroup().addTo(carteMap);
      }

      carteMarkers.clearLayers();
      const bounds = [];

      withGps.forEach((c) => {
        const lat = Number(c.latitude);
        const lon = Number(c.longitude);
        bounds.push([lat, lon]);
        const typeLabel =
          c.type_entretien === "entretien_complet" ? "Complet" : "Classique";
        const marker = L.marker([lat, lon]).bindPopup(
          `<strong>${escapeHtml(c.prenom)} ${escapeHtml(c.nom)}</strong><br>` +
            `${escapeHtml(c.adresse)}<br>` +
            `<em>${typeLabel}</em><br>` +
            `<button type="button" class="carte-go" data-client-id="${c.id}">Ouvrir</button>`
        );
        marker.on("popupopen", () => {
          const btn = document.querySelector(`.carte-go[data-client-id="${c.id}"]`);
          if (!btn) return;
          btn.addEventListener(
            "click",
            () => {
              allerChezClient(c);
            },
            { once: true }
          );
        });
        carteMarkers.addLayer(marker);
      });

      // Position GPS actuelle (si possible)
      try {
        const pos = await getPosition();
        const me = [pos.coords.latitude, pos.coords.longitude];
        bounds.push(me);
        L.circleMarker(me, {
          radius: 9,
          color: "#0f766e",
          fillColor: "#2dd4bf",
          fillOpacity: 0.9,
          weight: 2,
        })
          .bindPopup("Vous êtes ici")
          .addTo(carteMarkers);
      } catch {
        /* GPS optionnel sur la carte */
      }

      requestAnimationFrame(() => {
        carteMap.invalidateSize();
        if (bounds.length === 1) carteMap.setView(bounds[0], 14);
        else if (bounds.length > 1) carteMap.fitBounds(bounds, { padding: [36, 36] });
        else carteMap.setView([46.6, 2.4], 6);
      });

      status.textContent = withGps.length
        ? `${withGps.length} client${withGps.length > 1 ? "s" : ""} sur la carte`
        : "Aucun client avec GPS.";
    } catch (err) {
      status.textContent = err.message || "Impossible de charger la carte.";
    }
  }

  $("#btn-carte")?.addEventListener("click", () => afficherCarteClients());

  async function afficherAgenda() {
    show("agenda");
    const status = $("#agenda-status");
    const ul = $("#liste-agenda");
    status.textContent = "Chargement…";
    ul.innerHTML = "";
    try {
      const data = await api("/api/agenda?jours=60");
      const passages = data.passages || [];
      if (!passages.length) {
        status.textContent = "Aucun passage planifié. Définis fréquence + date sur un entretien complet.";
        ul.innerHTML = '<li class="empty">—</li>';
        return;
      }
      status.textContent = `${passages.length} passage${passages.length > 1 ? "s" : ""} à venir`;
      let lastDate = "";
      passages.forEach((p) => {
        if (p.date !== lastDate) {
          lastDate = p.date;
          const head = document.createElement("li");
          head.className = "agenda-day";
          head.textContent = formatDateFr(p.date);
          ul.appendChild(head);
        }
        const li = document.createElement("li");
        li.className = "pick-item pick-item--row";
        const freq = labelFrequence(p.frequence);
        li.innerHTML = `
          <button type="button" class="pick-main">
            <strong>${escapeHtml(p.client.prenom)} ${escapeHtml(p.client.nom)}</strong>
            <span class="meta">${escapeHtml(p.client.adresse)}</span>
            <span class="meta">${escapeHtml(freq)}</span>
          </button>
        `;
        li.querySelector(".pick-main").addEventListener("click", () => allerChezClient(p.client));
        ul.appendChild(li);
      });
    } catch (err) {
      status.textContent = err.message || "Impossible de charger l’agenda.";
    }
  }

  $("#btn-agenda")?.addEventListener("click", () => afficherAgenda());

  let factureFiltre = "toutes";

  async function afficherFactures(filtre) {
    if (filtre) factureFiltre = filtre;
    show("factures");
    const status = $("#factures-status");
    const ul = $("#liste-factures");
    status.textContent = "Chargement…";
    ul.innerHTML = "";

    $$("#factures-filtres .filtre-chip").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.filtre === factureFiltre);
    });

    try {
      const data = await api(`/api/factures?filtre=${encodeURIComponent(factureFiltre)}`);
      const items = data.factures || [];
      if (!items.length) {
        status.textContent = "Aucune facture pour ce filtre.";
        ul.innerHTML = '<li class="empty">—</li>';
        return;
      }
      status.textContent = `${items.length} facture${items.length > 1 ? "s" : ""}`;
      items.forEach((f) => {
        const li = document.createElement("li");
        li.className = "facture-item";
        const envoyee = f.envoyee === "oui";
        const payee = f.payee === "oui";
        const dates = f.date_ref
          ? `${formatDateFr(f.date)} · ${formatDateFr(f.date_ref)}`
          : formatDateFr(f.date);
        li.innerHTML = `
          <div class="facture-main">
            <strong>${escapeHtml(f.client_label)}</strong>
            <span class="hist-text">${escapeHtml(String(f.quantite))} × ${escapeHtml(f.description)}</span>
            <span class="meta">${escapeHtml(dates)}</span>
          </div>
          <div class="facture-flags">
            <button type="button" class="flag-btn ${envoyee ? "is-on" : ""}" data-flag="envoyee" data-id="${escapeHtml(String(f.id))}" aria-pressed="${envoyee}">
              Envoyée
            </button>
            <button type="button" class="flag-btn ${payee ? "is-on" : ""}" data-flag="payee" data-id="${escapeHtml(String(f.id))}" aria-pressed="${payee}">
              Payée
            </button>
          </div>
        `;
        ul.appendChild(li);
      });

      $$("[data-flag]", ul).forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = btn.dataset.id;
          const flag = btn.dataset.flag;
          const next = btn.getAttribute("aria-pressed") !== "true";
          try {
            await api(`/api/factures/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ [flag]: next }),
            });
            await afficherFactures(factureFiltre);
          } catch (err) {
            alert(err.message || "Mise à jour impossible.");
          }
        });
      });
    } catch (err) {
      status.textContent = err.message || "Impossible de charger les factures.";
    }
  }

  $("#btn-factures")?.addEventListener("click", () => afficherFactures("toutes"));
  $$("#factures-filtres .filtre-chip").forEach((btn) => {
    btn.addEventListener("click", () => afficherFactures(btn.dataset.filtre));
  });

  document.querySelectorAll('input[name="type_entretien"]').forEach((el) => {
    el.addEventListener("change", syncBlocPassage);
  });
  document.querySelectorAll('input[name="frequence_passage"]').forEach((el) => {
    el.addEventListener("change", syncBlocPassage);
  });
  $("#input-date-passage")?.addEventListener("change", syncBlocPassage);
  $("#input-date-passage")?.addEventListener("input", syncBlocPassage);
  $("#chk-creer-compte")?.addEventListener("change", syncBlocCompte);
  $("#btn-gen-password")?.addEventListener("click", () => {
    $("#input-password").value = genererPasswordLocal();
  });
  syncBlocCompte();

  async function refreshHistorique(clientId) {
    const fiche = await api(`/api/clients/${clientId}`);
    clientActif = { ...clientActif, ...fiche };

    const bindItemActions = (ul) => {
      $$("[data-edit-kind]", ul).forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const kind = btn.dataset.editKind;
          const id = btn.dataset.id;
          try {
            if (kind === "note" || kind === "analyse") {
              const current = btn.dataset.texte || "";
              const next = window.prompt(
                kind === "note" ? "Modifier la note :" : "Modifier l’analyse :",
                current
              );
              if (next == null) return;
              const texte = next.trim();
              if (!texte) return;
              const path = kind === "note" ? `/api/notes/${id}` : `/api/analyses/${id}`;
              await api(path, { method: "PUT", body: JSON.stringify({ texte }) });
            } else if (kind === "fourniture") {
              const qty = window.prompt("Quantité :", btn.dataset.quantite || "1");
              if (qty == null) return;
              const desc = window.prompt("Description :", btn.dataset.description || "");
              if (desc == null) return;
              const quantite = parseFloat(String(qty).replace(",", "."));
              const description = desc.trim();
              if (!description || Number.isNaN(quantite)) {
                alert("Quantité ou description invalide.");
                return;
              }
              await api(`/api/fournitures/${id}`, {
                method: "PUT",
                body: JSON.stringify({ quantite, description }),
              });
            }
            await refreshHistorique(clientId);
          } catch (err) {
            alert(err.message || "Modification impossible.");
          }
        });
      });
      $$("[data-del-kind]", ul).forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const kind = btn.dataset.delKind;
          const id = btn.dataset.id;
          const label =
            kind === "note" ? "cette note" : kind === "analyse" ? "cette analyse" : "cette fourniture";
          if (!window.confirm(`Supprimer ${label} ?`)) return;
          try {
            const path =
              kind === "note"
                ? `/api/notes/${id}`
                : kind === "analyse"
                  ? `/api/analyses/${id}`
                  : `/api/fournitures/${id}`;
            await api(path, { method: "DELETE" });
            await refreshHistorique(clientId);
          } catch (err) {
            alert(err.message || "Suppression impossible.");
          }
        });
      });
    };

    const fill = (sel, items, render) => {
      const ul = $(sel);
      ul.innerHTML = "";
      if (!items?.length) {
        ul.innerHTML = '<li class="empty">—</li>';
        return;
      }
      [...items].reverse().forEach((it) => {
        const li = document.createElement("li");
        li.className = "hist-item";
        li.innerHTML = render(it);
        ul.appendChild(li);
      });
      bindItemActions(ul);
    };

    fill(
      "#liste-notes",
      fiche.notes,
      (n) =>
        `<div class="hist-body"><span class="hist-text">${escapeHtml(n.texte)}</span>` +
        `${formatHistDates(n)}</div>` +
        `<div class="hist-actions">` +
        `<button type="button" class="hist-btn" data-edit-kind="note" data-id="${escapeHtml(String(n.id))}" data-texte="${escapeHtml(n.texte)}" title="Modifier">✎</button>` +
        `<button type="button" class="hist-btn hist-btn--del" data-del-kind="note" data-id="${escapeHtml(String(n.id))}" title="Supprimer">✕</button>` +
        `</div>`
    );
    fill(
      "#liste-analyses",
      fiche.analyses,
      (a) =>
        `<div class="hist-body"><span class="hist-text">${escapeHtml(a.texte)}</span>` +
        `${formatHistDates(a)}</div>` +
        `<div class="hist-actions">` +
        `<button type="button" class="hist-btn" data-edit-kind="analyse" data-id="${escapeHtml(String(a.id))}" data-texte="${escapeHtml(a.texte)}" title="Modifier">✎</button>` +
        `<button type="button" class="hist-btn hist-btn--del" data-del-kind="analyse" data-id="${escapeHtml(String(a.id))}" title="Supprimer">✕</button>` +
        `</div>`
    );
    fill(
      "#liste-fournitures",
      fiche.fournitures,
      (f) => {
        const tags = [];
        if ((f.envoyee || "").toLowerCase() === "oui") tags.push("Envoyée");
        if ((f.payee || "").toLowerCase() === "oui") tags.push("Payée");
        const tagHtml = tags.length
          ? `<span class="meta">${escapeHtml(tags.join(" · "))}</span>`
          : "";
        return (
          `<div class="hist-body"><span class="hist-text">${escapeHtml(String(f.quantite))} × ${escapeHtml(f.description)}</span>` +
          `${formatHistDates(f)}${tagHtml}</div>` +
          `<div class="hist-actions">` +
          `<button type="button" class="hist-btn" data-edit-kind="fourniture" data-id="${escapeHtml(String(f.id))}" data-quantite="${escapeHtml(String(f.quantite))}" data-description="${escapeHtml(f.description)}" title="Modifier">✎</button>` +
          `<button type="button" class="hist-btn hist-btn--del" data-del-kind="fourniture" data-id="${escapeHtml(String(f.id))}" title="Supprimer">✕</button>` +
          `</div>`
        );
      }
    );
  }

  function renderEquip(client) {
    const box = $("#visite-equip");
    box.innerHTML = "";
    const tags = [];
    if (client.pompe) tags.push(`Pompe : ${client.pompe}`);
    if (client.filtre) tags.push(`Filtre : ${client.filtre}`);
    if (client.robot) tags.push(`Robot : ${client.robot}`);
    if (client.skimmer) tags.push(`Skimmer : ${client.skimmer}`);
    if (client.vanne_vidange) tags.push("Vanne de vidange");
    if (client.injecteurs) tags.push("Injecteurs");
    if (client.type_entretien === "entretien_complet") tags.push("Entretien complet");
    else tags.push("Entretien classique");
    if (client.frequence_passage && client.prochain_passage) {
      tags.push(`Prochain : ${formatDateFr(client.prochain_passage)} (${labelFrequence(client.frequence_passage)})`);
    }
    if (client.autres) tags.push(client.autres);
    tags.forEach((t) => {
      const span = document.createElement("span");
      span.className = "tag";
      span.textContent = t;
      box.appendChild(span);
    });
  }

  function checklistComplete(checklist) {
    const items = checklist || [];
    // Les étapes optionnelles ne bloquent pas la fin
    const required = items.filter((i) => !i.optional);
    if (!required.length) return items.every((i) => i.done);
    return required.every((i) => i.done);
  }

  function updateFinButton(visite) {
    const btn = $("#btn-fin");
    const hint = $("#fin-hint");
    if (!btn) return;
    const started = !!(visite?.debut && !visite?.fin);
    const finished = !!(visite?.debut && visite?.fin);
    const ok = checklistComplete(visite?.checklist || []);
    btn.disabled = finished || !started || !ok;
    if (hint) {
      if (finished) {
        hint.textContent = "Prestation terminée.";
        hint.hidden = false;
      } else if (!started) {
        hint.textContent = "Appuyez sur Début pour démarrer la prestation.";
        hint.hidden = false;
      } else if (!ok) {
        hint.textContent = "Cochez toutes les étapes obligatoires pour terminer.";
        hint.hidden = false;
      } else {
        hint.textContent = "";
        hint.hidden = true;
      }
    }
  }

  function renderChecklist(checklist) {
    const ul = $("#checklist");
    ul.innerHTML = "";
    checklist.forEach((item) => {
      const li = document.createElement("li");
      li.className = "check-item" + (item.done ? " is-done" : "") + (item.warn ? " is-warn" : "");
      li.innerHTML =
        `<button type="button" class="check-btn" data-item="${escapeHtml(item.id)}">` +
        `<span class="check-mark">${item.done ? "✓" : ""}</span>` +
        `<span class="check-text"><strong>${escapeHtml(item.label)}</strong>` +
        (item.warn ? `<em>${escapeHtml(item.warn)}</em>` : "") +
        (item.optional ? `<em>optionnel</em>` : "") +
        `</span></button>`;
      const photos = Array.isArray(item.photos) ? item.photos : [];
      if (photos.length) {
        const gallery = document.createElement("div");
        gallery.className = "check-photos";
        photos.forEach((ph) => {
          const url = ph.url || `/api/photos/${ph.id}/file`;
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "check-photo-thumb";
          btn.innerHTML = `<img src="${escapeHtml(url)}" alt="" loading="lazy" />`;
          btn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            openPhotoLightbox(url);
          });
          gallery.appendChild(btn);
        });
        li.appendChild(gallery);
      }
      ul.appendChild(li);
    });

    $$(".check-btn", ul).forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!visiteActive) return;
        const item = visiteActive.checklist.find((i) => i.id === btn.dataset.item);
        const next = !(item && item.done);
        setFeedback($("#check-feedback"), "", null);
        try {
          const res = await api(`/api/visites/${visiteActive.id}/checklist`, {
            method: "PATCH",
            body: JSON.stringify({ item_id: btn.dataset.item, done: next }),
          });
          visiteActive = { ...visiteActive, ...res };
          renderChecklist(res.checklist);
          updateFinButton(visiteActive);
        } catch (err) {
          setFeedback($("#check-feedback"), err.message, false);
        }
      });
    });
    updateFinButton(visiteActive);
  }

  function formatDuree(minutes) {
    if (minutes == null || minutes === "") return "—";
    const m = Number(minutes);
    if (Number.isNaN(m)) return "—";
    const h = Math.floor(m / 60);
    const min = Math.round(m % 60);
    if (h <= 0) return `${min} min`;
    return `${h} h ${String(min).padStart(2, "0")} min`;
  }

  function renderChrono(visite) {
    const el = $("#chrono-status");
    const btnDebut = $("#btn-debut");
    if (!visite?.debut) {
      el.textContent = "Pas encore démarré";
      el.classList.remove("is-running", "is-done");
      if (btnDebut) btnDebut.disabled = false;
      updateFinButton(visite);
      return;
    }
    if (visite.debut && !visite.fin) {
      el.textContent = `En cours depuis ${visite.debut}`;
      el.classList.add("is-running");
      el.classList.remove("is-done");
      if (btnDebut) btnDebut.disabled = true;
      updateFinButton(visite);
      return;
    }
    el.textContent = `Durée : ${formatDuree(visite.duree_minutes)} (${visite.debut} → ${visite.fin})`;
    el.classList.add("is-done");
    el.classList.remove("is-running");
    if (btnDebut) btnDebut.disabled = true;
    updateFinButton(visite);
  }

  async function afficherVisite(visite) {
    visiteActive = visite;
    const c = visite.client || clientActif;
    clientActif = c;
    $("#visite-type-label").textContent = TYPE_LABELS[visite.type] || visite.type;
    $("#visite-name").textContent = `${c.prenom} ${c.nom}`;
    $("#visite-adresse").textContent = c.adresse;
    const btnDep = $("#btn-depannage-visite");
    if (btnDep) btnDep.hidden = visite.type === "depannage";
    const btnHiv = $("#btn-hivernage-visite");
    if (btnHiv) btnHiv.hidden = visite.type === "hivernage";
    renderEquip(c);
    renderChrono(visite);
    renderChecklist(visite.checklist || []);
    setFeedback($("#ia-feedback"), "", null);
    setFeedback($("#check-feedback"), "", null);
    $("#ia-texte").value = "";
    setIntent(null);
    await refreshHistorique(c.id);
    // Écoute mots-clés (pas d’enregistrement tant qu’on n’a pas dit note/fourniture/analyse)
    await startWakeListen();
  }

  $("#btn-debut").addEventListener("click", async () => {
    if (!visiteActive) return;
    try {
      const res = await api(`/api/visites/${visiteActive.id}/debut`, { method: "POST", body: "{}" });
      visiteActive = { ...visiteActive, ...res };
      renderChrono(visiteActive);
      setFeedback($("#check-feedback"), "Prestation démarrée.", true);
    } catch (err) {
      setFeedback($("#check-feedback"), err.message, false);
    }
  });

  $("#btn-fin").addEventListener("click", async () => {
    if (!visiteActive) return;
    if (!visiteActive.debut || visiteActive.fin) return;
    if (!checklistComplete(visiteActive.checklist || [])) {
      setFeedback(
        $("#check-feedback"),
        "Impossible de terminer : cochez toutes les étapes obligatoires.",
        false
      );
      updateFinButton(visiteActive);
      return;
    }
    try {
      const res = await api(`/api/visites/${visiteActive.id}/fin`, { method: "POST", body: "{}" });
      visiteActive = { ...visiteActive, ...res };
      renderChrono(visiteActive);
      setFeedback(
        $("#check-feedback"),
        `Prestation terminée — ${formatDuree(res.duree_minutes)}.`,
        true
      );
    } catch (err) {
      setFeedback($("#check-feedback"), err.message, false);
    }
  });

  // ── Intent + vocal (écoute mots-clés uniquement, pas d’enreg. en boucle) ───

  let wantWakeListen = false;
  let armedIntent = null;
  let speechRec = null;
  let speechRestartTimer = null;
  let captureTimer = null;
  let holdStream = null;
  let holdRecorder = null;
  let holdChunks = [];
  let holdRecording = false;
  let processingVocal = false;
  /** idle | wake | capture | busy */
  let voicePhase = "idle";
  const CAPTURE_TIMEOUT_MS = 8000;

  function isMobileVocal() {
    return (
      /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || "") ||
      (navigator.maxTouchPoints > 1 && /Macintosh/i.test(navigator.userAgent || ""))
    );
  }

  function setIntent(name) {
    intentForce = name;
    $$(".chip").forEach((c) => c.classList.toggle("is-on", c.dataset.intent === name));
  }

  function detectWakeIntent(text) {
    const t = String(text || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[.:;!?]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (/\b(note|noter|notes)\b/.test(t)) return "note";
    if (/\b(analyse|analyser|annalyse|analise|analyce|analyzes?)\b/.test(t)) return "analyse";
    if (
      /\b(fourniture|fournitures|forniture|furniture|formiture|facture|facturer|factures|pourriture)\b/.test(
        t
      )
    ) {
      return "fourniture";
    }
    return null;
  }

  function normaliserTexteVocal(text) {
    let out = String(text || "")
      .replace(/[.:;!?]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();

    out = out.replace(
      /\ble\s+(\d{1,2})\s+(\d{1,2})(?:\s+(\d{2,4}))?\b/gi,
      (_, d, m, y) => {
        const day = Number(d);
        const mo = Number(m);
        if (day < 1 || day > 31 || mo < 1 || mo > 12) return _.toString();
        if (y) {
          let yy = Number(y);
          if (yy < 100) yy += 2000;
          return `le ${String(day).padStart(2, "0")}/${String(mo).padStart(2, "0")}/${yy}`;
        }
        return `le ${String(day).padStart(2, "0")}/${String(mo).padStart(2, "0")}`;
      }
    );
    out = out.replace(
      /\b(\d{1,2})\s+(\d{1,2})\s+(\d{2,4})\b/g,
      (_, d, m, y) => {
        const day = Number(d);
        const mo = Number(m);
        if (day < 1 || day > 31 || mo < 1 || mo > 12) return _.toString();
        let yy = Number(y);
        if (yy < 100) yy += 2000;
        return `le ${String(day).padStart(2, "0")}/${String(mo).padStart(2, "0")}/${yy}`;
      }
    );

    const nombres = {
      zero: "0",
      deux: "2",
      trois: "3",
      quatre: "4",
      cinq: "5",
      six: "6",
      sept: "7",
      huit: "8",
      neuf: "9",
      dix: "10",
    };
    Object.entries(nombres).forEach(([mot, ch]) => {
      out = out.replace(new RegExp(`\\b${mot}\\b`, "gi"), ch);
    });
    out = out.replace(/\b(un|une)\s+(bidon|sac|cartouche|seau)\b/gi, "1 $2");
    const intent = detectWakeIntent(out);
    if (intent === "fourniture" && !/\b(fourniture|facture|facturer)\b/i.test(out)) {
      out = `fourniture ${out}`;
    }
    return out;
  }

  function isMostlyWakeWord(text) {
    const t = String(text || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^\w\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return /^(une?\s+)?(note|noter|analyse|analyser|annalyse|analise|fourniture|fournitures|facture|facturer)s?$/.test(
      t
    );
  }

  function isStopCommand(text) {
    const t = String(text || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^\w\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return /^(fin|fins|stop|termine|terminer|fini|finie|cest\s+tout|c\s+est\s+tout)$/.test(t);
  }

  function stripStopSuffix(text) {
    return String(text || "")
      .replace(/\s+(fin|fins|stop|termine|terminer|fini|finie)\s*$/i, "")
      .trim();
  }

  function clearCaptureTimer() {
    if (captureTimer) {
      clearTimeout(captureTimer);
      captureTimer = null;
    }
  }

  function backToWakeOrIdle() {
    clearCaptureTimer();
    armedIntent = null;
    setIntent(null);
    voicePhase = wantWakeListen ? "wake" : "idle";
    setListenStatus(wantWakeListen ? "listening" : "idle");
  }

  function terminerEnregistrementVocal() {
    backToWakeOrIdle();
  }

  function updateEcouteButton() {
    const btn = $("#btn-ecoute");
    if (!btn) return;
    if (wantWakeListen) {
      btn.setAttribute("aria-pressed", "true");
      btn.textContent = "Écoute auto · ON";
    } else {
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = "Écoute auto · OFF";
    }
  }

  function setRecCursor(on) {
    const cursor = $("#rec-cursor");
    if (cursor) cursor.hidden = !on;
  }

  function setListenStatus(state) {
    const panel = $(".ia-panel");
    const dot = $(".ia-dot");
    const sub = $(".ia-sub");
    if (panel) {
      panel.classList.remove("is-listening", "is-busy", "is-capture");
      if (state === "busy") panel.classList.add("is-busy");
      if (state === "listening") panel.classList.add("is-listening");
      if (state === "capture" || state === "speaking") panel.classList.add("is-capture");
    }
    if (dot) {
      dot.classList.toggle("is-listening", state === "listening");
      dot.classList.toggle("is-busy", state === "busy" || state === "capture" || state === "speaking");
    }
    // Curseur rouge UNIQUEMENT pendant une vraie capture (après « note… »)
    setRecCursor(state === "capture" || state === "speaking" || state === "busy");
    if (sub) {
      if (state === "busy") sub.textContent = "Traitement…";
      else if (state === "capture" || state === "speaking") {
        sub.textContent = "Suite… (ou dites « fin »)";
      } else if (state === "listening") {
        sub.textContent = "J’écoute les mots-clés : note / fourniture / analyse";
      } else {
        sub.textContent = "Dites « note… », « fourniture… » ou « analyse… »";
      }
    }
  }

  function stripWakePrefix(texte, intent) {
    let t = String(texte || "").trim();
    if (intent === "note") {
      t = t.replace(/^(une\s+)?(note|noter)[:\s,-]*/i, "").trim();
    } else if (intent === "analyse") {
      t = t.replace(/^(une\s+)?(analyse|analyser|annalyse|analise)[:\s,-]*/i, "").trim();
    } else if (intent === "fourniture") {
      t = t.replace(/^(une\s+)?(facture|facturer|fourniture|fournitures)[:\s,-]*/i, "").trim();
    }
    return t;
  }

  function enterCapture(intent) {
    clearCaptureTimer();
    armedIntent = intent;
    setIntent(intent);
    voicePhase = "capture";
    setListenStatus("capture");
    setFeedback(
      $("#ia-feedback"),
      `Mot-clé « ${intent} » — dictez la suite, puis silence ou « fin ».`,
      null
    );
    captureTimer = setTimeout(() => {
      if (voicePhase === "capture") {
        setFeedback($("#ia-feedback"), "Temps écoulé — redis « note / fourniture / analyse ».", false);
        backToWakeOrIdle();
        scheduleSpeechRestart();
      }
    }, CAPTURE_TIMEOUT_MS);
  }

  async function traiterTexteVocal(texteBrut, { fromHold = false } = {}) {
    const brut = String(texteBrut || "").trim();
    if (!brut || processingVocal) return;

    let texte = normaliserTexteVocal(brut);

    if (isStopCommand(texte) || isStopCommand(stripWakePrefix(texte, armedIntent || detectWakeIntent(texte)))) {
      terminerEnregistrementVocal();
      scheduleSpeechRestart();
      return;
    }

    texte = stripStopSuffix(texte);
    if (!texte) {
      terminerEnregistrementVocal();
      scheduleSpeechRestart();
      return;
    }

    const wake = detectWakeIntent(texte);
    const intent = wake || armedIntent || intentForce;

    // Écoute wake : ignorer tout ce qui n’est PAS un mot-clé (pas d’enreg.)
    if (!fromHold && voicePhase === "wake" && !wake && !armedIntent) {
      return;
    }

    if (!intent) {
      if (fromHold) {
        setFeedback(
          $("#ia-feedback"),
          `Non compris : « ${texte} ». Commencez par note / fourniture / analyse.`,
          false
        );
      }
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return;
    }

    // Juste le mot-clé → ouvrir une fenêtre de capture (une seule fois)
    if (isMostlyWakeWord(texte)) {
      enterCapture(intent);
      scheduleSpeechRestart();
      return;
    }

    // Contenu utile → une seule commande, puis retour écoute mots-clés
    processingVocal = true;
    voicePhase = "busy";
    clearCaptureTimer();
    stopSpeechRec({ keepWanted: true });
    setListenStatus("busy");
    try {
      let full = texte;
      if (armedIntent && !detectWakeIntent(texte)) {
        full = `${armedIntent} ${texte}`;
      }
      full = stripStopSuffix(full);
      const finalIntent = detectWakeIntent(full) || armedIntent || intent;
      const contenu = stripWakePrefix(full, finalIntent);
      if (!contenu || isStopCommand(contenu)) {
        terminerEnregistrementVocal();
        return;
      }
      setIntent(finalIntent);
      $("#ia-texte").value = contenu;
      await envoyerCommande(full);
      armedIntent = null;
    } finally {
      processingVocal = false;
      backToWakeOrIdle();
      scheduleSpeechRestart();
    }
  }

  function stopSpeechRec({ keepWanted = false } = {}) {
    if (speechRestartTimer) {
      clearTimeout(speechRestartTimer);
      speechRestartTimer = null;
    }
    if (speechRec) {
      try {
        speechRec.onresult = null;
        speechRec.onerror = null;
        speechRec.onend = null;
        if (typeof speechRec.abort === "function") speechRec.abort();
        else speechRec.stop();
      } catch {
        try {
          speechRec.stop();
        } catch {
          /* */
        }
      }
      speechRec = null;
    }
    if (!keepWanted) {
      /* caller gère wantWakeListen */
    }
  }

  function scheduleSpeechRestart() {
    if (!wantWakeListen || processingVocal || holdRecording) return;
    if (speechRestartTimer) clearTimeout(speechRestartTimer);
    const delay = isMobileVocal() ? 450 : 220;
    speechRestartTimer = setTimeout(() => {
      speechRestartTimer = null;
      if (!wantWakeListen || processingVocal || holdRecording || speechRec) return;
      startSpeechRec();
    }, delay);
  }

  function startSpeechRec() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return false;
    if (speechRec || processingVocal || holdRecording) return !!speechRec;

    const mobile = isMobileVocal();
    const rec = new SR();
    speechRec = rec;
    rec.lang = "fr-FR";
    // Sur téléphone : sessions courtes (évite la boucle continue)
    rec.continuous = !mobile;
    rec.interimResults = false;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      if (voicePhase === "capture") setListenStatus("capture");
      else if (wantWakeListen) {
        voicePhase = voicePhase === "capture" ? "capture" : "wake";
        if (voicePhase === "wake") setListenStatus("listening");
      }
    };

    rec.onresult = (event) => {
      let finalTxt = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          finalTxt += event.results[i][0].transcript;
        }
      }
      const txt = finalTxt.trim();
      if (!txt) return;

      // Phase wake : uniquement si mot-clé présent
      if (voicePhase === "wake" || voicePhase === "idle") {
        if (!detectWakeIntent(txt) && !armedIntent) return;
        traiterTexteVocal(txt);
        return;
      }
      if (voicePhase === "capture") {
        traiterTexteVocal(txt);
      }
    };

    rec.onerror = (ev) => {
      const err = ev.error || "";
      if (err === "aborted" || err === "no-speech") return;
      if (err === "not-allowed") {
        setFeedback($("#ia-feedback"), "Micro refusé — autorisez-le dans le navigateur.", false);
        wantWakeListen = false;
        updateEcouteButton();
        voicePhase = "idle";
        setListenStatus("idle");
        return;
      }
      // network / etc. : on retentera via onend
    };

    rec.onend = () => {
      speechRec = null;
      if (!wantWakeListen || processingVocal || holdRecording) return;
      // Redémarrer pour réécouter les mots-clés seulement (pas un nouvel « enregistrement »)
      scheduleSpeechRestart();
    };

    try {
      rec.start();
      return true;
    } catch (err) {
      speechRec = null;
      setFeedback($("#ia-feedback"), err.message || "Impossible de démarrer le micro.", false);
      return false;
    }
  }

  function stopWakeListen() {
    wantWakeListen = false;
    armedIntent = null;
    clearCaptureTimer();
    stopSpeechRec();
    voicePhase = "idle";
    updateEcouteButton();
    setListenStatus("idle");
  }

  async function startWakeListen() {
    wantWakeListen = true;
    armedIntent = null;
    clearCaptureTimer();
    voicePhase = "wake";
    updateEcouteButton();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      wantWakeListen = false;
      voicePhase = "idle";
      updateEcouteButton();
      setFeedback($("#ia-feedback"), "Micro refusé dans le navigateur.", false);
      setListenStatus("idle");
      return;
    }
    const ok = startSpeechRec();
    if (!ok) {
      // Pas de Web Speech (souvent iPhone Safari) → proposer maintien
      setListenStatus("idle");
      setFeedback(
        $("#ia-feedback"),
        isMobileVocal()
          ? "Sur ce téléphone, utilisez « Maintenir pour parler »."
          : "Écoute auto indisponible — utilisez Chrome/Edge ou « Maintenir pour parler ».",
        false
      );
    } else {
      setListenStatus("listening");
      setFeedback(
        $("#ia-feedback"),
        "Écoute des mots-clés seule — rien n’est enregistré tant que vous ne dites pas note / fourniture / analyse.",
        true
      );
    }
  }

  function pickRecorderMime() {
    const candidates = isMobileVocal()
      ? ["audio/mp4", "audio/aac", "audio/webm;codecs=opus", "audio/webm", "audio/ogg"]
      : ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
    for (const t of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
    }
    return "";
  }

  async function startHoldRecord() {
    if (holdRecording || processingVocal) return;
    // Couper l’écoute auto pendant le maintien (évite la double capture / boucle)
    stopSpeechRec({ keepWanted: true });
    try {
      holdStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      holdChunks = [];
      const mime = pickRecorderMime();
      holdRecorder = mime
        ? new MediaRecorder(holdStream, { mimeType: mime })
        : new MediaRecorder(holdStream);
      holdRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) holdChunks.push(e.data);
      };
      holdRecorder.start(250);
      holdRecording = true;
      voicePhase = "capture";
      setListenStatus("capture");
      setFeedback($("#ia-feedback"), "Parlez… relâchez pour envoyer.", null);
      const btn = $("#btn-parler");
      if (btn) {
        btn.classList.add("is-recording");
        btn.textContent = "Parlez… (relâcher)";
      }
    } catch (err) {
      setFeedback($("#ia-feedback"), err.message || "Micro indisponible.", false);
      scheduleSpeechRestart();
    }
  }

  async function stopHoldRecord() {
    if (!holdRecording || !holdRecorder) return;
    holdRecording = false;
    const btn = $("#btn-parler");
    if (btn) {
      btn.classList.remove("is-recording");
      btn.textContent = "Maintenir pour parler";
    }
    setListenStatus("busy");
    setFeedback($("#ia-feedback"), "Transcription…", null);

    const blob = await new Promise((resolve) => {
      holdRecorder.onstop = () => {
        resolve(new Blob(holdChunks, { type: holdRecorder.mimeType || "audio/webm" }));
      };
      try {
        holdRecorder.stop();
      } catch {
        resolve(null);
      }
    });
    if (holdStream) {
      holdStream.getTracks().forEach((t) => t.stop());
      holdStream = null;
    }
    holdRecorder = null;
    holdChunks = [];

    if (!blob || blob.size < 800) {
      setFeedback($("#ia-feedback"), "Trop court — maintenez plus longtemps.", false);
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return;
    }
    try {
      const fd = new FormData();
      const type = blob.type || "";
      const ext = type.includes("mp4") || type.includes("aac") ? "mp4" : type.includes("ogg") ? "ogg" : "webm";
      fd.append("audio", blob, `note.${ext}`);
      const res = await fetch("/api/transcribe", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Transcription échouée");
      await traiterTexteVocal(data.texte || "", { fromHold: true });
    } catch (err) {
      setFeedback($("#ia-feedback"), err.message || "Erreur vocal.", false);
      backToWakeOrIdle();
      scheduleSpeechRestart();
    }
  }

  async function demarrerEnregistrementPour(intent) {
    enterCapture(intent);
    if (!wantWakeListen) await startWakeListen();
    else if (!speechRec) startSpeechRec();
  }

  $$(".chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      const name = chip.dataset.intent;
      if (intentForce === name && voicePhase === "capture" && !holdRecording) {
        backToWakeOrIdle();
        scheduleSpeechRestart();
        return;
      }
      await demarrerEnregistrementPour(name);
    });
  });

  $("#btn-ecoute")?.addEventListener("click", async () => {
    if (wantWakeListen) {
      stopWakeListen();
      setFeedback($("#ia-feedback"), "Écoute auto coupée.", null);
      return;
    }
    await startWakeListen();
  });

  async function envoyerCommande(texte) {
    if (!clientActif) {
      setFeedback($("#ia-feedback"), "Aucun client actif.", false);
      return;
    }
    const t = normaliserTexteVocal((texte || "").trim());
    if (!t) {
      setFeedback($("#ia-feedback"), "Texte vide.", false);
      return;
    }
    $("#ia-texte").value = t;
    if (!intentForce) {
      const detected = detectWakeIntent(t);
      if (detected) setIntent(detected);
    }
    if (!intentForce && !detectWakeIntent(t)) {
      setFeedback(
        $("#ia-feedback"),
        `Non compris : « ${t} ». Dites « fourniture 3 bidon de chlore ».`,
        false
      );
      return;
    }
    try {
      const payload = { client_id: clientActif.id, texte: t };
      if (intentForce) payload.intent_force = intentForce;
      const res = await api("/api/ia", { method: "POST", body: JSON.stringify(payload) });
      if (!res.success) {
        setFeedback($("#ia-feedback"), res.message || "Non compris.", false);
        return;
      }
      if (res.intent === "note") {
        const d = res.note.date_ref
          ? `${formatDateFr(res.note.date)} · ${formatDateFr(res.note.date_ref)}`
          : formatDateFr(res.note.date);
        setFeedback($("#ia-feedback"), `Note (${d}) : ${res.note.texte}`, true);
      } else if (res.intent === "analyse") {
        const d = res.analyse.date_ref
          ? `${formatDateFr(res.analyse.date)} · ${formatDateFr(res.analyse.date_ref)}`
          : formatDateFr(res.analyse.date);
        setFeedback($("#ia-feedback"), `Analyse (${d}) : ${res.analyse.texte}`, true);
      } else if (res.intent === "fourniture") {
        const f = res.fourniture;
        const d = f.date_ref
          ? `${formatDateFr(f.date)} · ${formatDateFr(f.date_ref)}`
          : formatDateFr(f.date);
        setFeedback(
          $("#ia-feedback"),
          `Fourniture (${d}) : ${f.quantite} × ${f.description}`,
          true
        );
      }
      $("#ia-texte").value = "";
      setIntent(null);
      await refreshHistorique(clientActif.id);
    } catch (err) {
      setFeedback($("#ia-feedback"), err.message, false);
    }
  }

  $("#btn-envoyer-ia").addEventListener("click", () => envoyerCommande($("#ia-texte").value));

  const btnParler = $("#btn-parler");
  if (btnParler) {
    const down = (e) => {
      e.preventDefault();
      try {
        btnParler.setPointerCapture(e.pointerId);
      } catch {
        /* */
      }
      startHoldRecord();
    };
    const up = (e) => {
      e.preventDefault();
      try {
        if (btnParler.hasPointerCapture?.(e.pointerId)) {
          btnParler.releasePointerCapture(e.pointerId);
        }
      } catch {
        /* */
      }
      stopHoldRecord();
    };
    btnParler.addEventListener("pointerdown", down);
    btnParler.addEventListener("pointerup", up);
    btnParler.addEventListener("pointercancel", up);
    // Ne pas couper sur pointerleave (téléphone : le doigt bouge un peu)
  }

  $("#btn-recaler-gps").addEventListener("click", async () => {
    if (!clientActif) return;
    try {
      const pos = await getPosition();
      const updated = await api(`/api/clients/${clientActif.id}/position`, {
        method: "PATCH",
        body: JSON.stringify({
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
        }),
      });
      clientActif = { ...clientActif, ...updated };
      setFeedback($("#ia-feedback"), "GPS client mis à jour.", true);
    } catch (err) {
      setFeedback($("#ia-feedback"), err.message, false);
    }
  });

  // ── Adresses BAN (rapide) ────────────────────────────────────────────────

  const inputAdresse = $("#input-adresse");
  const listSuggestions = $("#adresse-suggestions");

  function hideSuggestions() {
    listSuggestions.hidden = true;
    listSuggestions.innerHTML = "";
    suggestIndex = -1;
  }

  function renderSuggestions(items) {
    listSuggestions.innerHTML = "";
    suggestIndex = -1;
    if (!items.length) {
      hideSuggestions();
      return;
    }
    items.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item.label;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        inputAdresse.value = item.label;
        $("#input-lat").value = item.latitude;
        $("#input-lon").value = item.longitude;
        gpsOverride = { latitude: item.latitude, longitude: item.longitude };
        hideSuggestions();
      });
      listSuggestions.appendChild(li);
    });
    listSuggestions.hidden = false;
  }

  inputAdresse.addEventListener("input", () => {
    gpsOverride = null;
    const q = inputAdresse.value.trim();
    clearTimeout(suggestTimer);
    if (suggestAbort) suggestAbort.abort();
    if (q.length < 3) {
      hideSuggestions();
      return;
    }
    suggestTimer = setTimeout(async () => {
      const ctrl = new AbortController();
      suggestAbort = ctrl;
      try {
        const res = await fetch(`/api/adresses?q=${encodeURIComponent(q)}`, {
          signal: ctrl.signal,
        });
        const items = await res.json();
        if (inputAdresse.value.trim() !== q) return;
        renderSuggestions(items);
      } catch (e) {
        if (e.name !== "AbortError") hideSuggestions();
      }
    }, 120);
  });

  inputAdresse.addEventListener("keydown", (e) => {
    const items = $$("li", listSuggestions);
    if (listSuggestions.hidden || !items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      suggestIndex = (suggestIndex + 1) % items.length;
      items.forEach((li, i) => li.classList.toggle("is-active", i === suggestIndex));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      suggestIndex = (suggestIndex - 1 + items.length) % items.length;
      items.forEach((li, i) => li.classList.toggle("is-active", i === suggestIndex));
    } else if (e.key === "Enter" && suggestIndex >= 0) {
      e.preventDefault();
      items[suggestIndex].dispatchEvent(new MouseEvent("mousedown"));
    } else if (e.key === "Escape") hideSuggestions();
  });

  inputAdresse.addEventListener("blur", () => setTimeout(hideSuggestions, 150));

  $("#btn-gps-actuel").addEventListener("click", async () => {
    setFeedback($("#nouveau-feedback"), "Lecture GPS…", null);
    try {
      const pos = await getPosition();
      $("#input-lat").value = pos.coords.latitude.toFixed(6);
      $("#input-lon").value = pos.coords.longitude.toFixed(6);
      gpsOverride = {
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
      };
      if (!inputAdresse.value.trim()) {
        inputAdresse.value = `Position GPS ${pos.coords.latitude.toFixed(5)}, ${pos.coords.longitude.toFixed(5)}`;
      }
      setFeedback($("#nouveau-feedback"), "Position GPS prise.", true);
    } catch (err) {
      setFeedback($("#nouveau-feedback"), err.message, false);
    }
  });

  $("#form-nouveau").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideSuggestions();
    const form = e.target;
    const btn = $("#btn-submit-nouveau");
    if (btn?.dataset.continueVisit === "1" && clientActif) {
      delete btn.dataset.continueVisit;
      allerChezClient(clientActif);
      return;
    }
    const editId = $("#edit-client-id").value.trim();
    const typeEl = form.querySelector('input[name="type_entretien"]:checked');
    const typeEntretien = typeEl ? typeEl.value : "entretien_simple";
    const payload = buildClientPayloadFromForm();
    if (
      typeEntretien === "entretien_complet" &&
      payload.frequence_passage &&
      !payload.date_debut_passage
    ) {
      setFeedback($("#nouveau-feedback"), "Indiquez la date du premier passage.", false);
      return;
    }
    if (
      typeEntretien === "entretien_complet" &&
      payload.date_debut_passage &&
      !payload.frequence_passage
    ) {
      setFeedback($("#nouveau-feedback"), "Choisissez la fréquence de passage.", false);
      return;
    }
    if (payload.creer_compte && !payload.email && !payload.telephone) {
      setFeedback(
        $("#nouveau-feedback"),
        "Compte client : indiquez un e-mail ou un téléphone.",
        false
      );
      return;
    }
    if (
      payload.latitude != null &&
      (Number.isNaN(payload.latitude) || Number.isNaN(payload.longitude))
    ) {
      setFeedback($("#nouveau-feedback"), "Latitude / longitude invalides.", false);
      return;
    }

    btn.disabled = true;
    setFeedback($("#nouveau-feedback"), editId ? "Mise à jour…" : "Enregistrement…", null);
    try {
      const client = await api(editId ? `/api/clients/${editId}` : "/api/clients", {
        method: editId ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      const where = client.storage === "supabase" ? "Supabase" : "CSV local";
      setFeedback(
        $("#nouveau-feedback"),
        editId ? `Client mis à jour (${where}).` : `Client enregistré sur ${where}.`,
        true
      );
      clientActif = client;
      refreshFooter();
      if (!editId) {
        $("#edit-client-id").value = String(client.id);
        await flushPendingPhotos(client.id);
      }
      if (editId) {
        await loadClientPhotos(client.id);
        hivernageSteps = normalizeHivernageSteps(client.checklist_hivernage);
        remplirFormClient(client);
        afficherCredentials(client);
        if (!client.password_clair) {
          show("liste");
          chargerListe();
        }
      } else if (client.password_clair) {
        btn.dataset.continueVisit = "1";
        btn.textContent = "Continuer la visite";
        $("#nouveau-title").textContent = "Compte créé";
        await loadClientPhotos(client.id);
        hivernageSteps = normalizeHivernageSteps(client.checklist_hivernage);
        remplirFormClient(client);
        afficherCredentials(client);
      } else {
        clearPendingPhotos();
        form.reset();
        gpsOverride = null;
        allerChezClient(client);
      }
    } catch (err) {
      setFeedback($("#nouveau-feedback"), err.message, false);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-supprimer-client")?.addEventListener("click", async () => {
    const editId = $("#edit-client-id").value.trim();
    if (!editId) return;
    const ok = window.confirm(
      "Supprimer ce client définitivement ? Notes, analyses, fournitures et visites liées seront aussi effacées."
    );
    if (!ok) return;
    try {
      await api(`/api/clients/${editId}`, { method: "DELETE" });
      clientActif = null;
      resetNouveau();
      show("liste");
      chargerListe();
    } catch (err) {
      setFeedback($("#nouveau-feedback"), err.message, false);
    }
  });
})();
