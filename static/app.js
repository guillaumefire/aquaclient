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

  const OPTION_KEYS = ["ouverture", "local_technique", "hivernage", "entretien"];
  const OPTION_LABELS = {
    ouverture: "Ouverture",
    local_technique: "Local technique",
    hivernage: "Hivernage",
    entretien: "Entretien",
    compte: "Compte client",
  };
  const OPTION_CREATE_LABELS = {
    ouverture: "Créer une ouverture",
    local_technique: "Créer un local technique",
    hivernage: "Créer un hivernage",
    entretien: "Créer un entretien",
    compte: "Créer un compte client",
  };
  const OPTION_HINTS = {
    ouverture: "Ajoute des lignes d’info pour l’ouverture (ex. date, niveau d’eau…).",
    local_technique: "Ajoute des lignes (ex. Pompe Speedeo, Filtre sable…).",
    hivernage: "Choisis actif ou passif, puis complète les étapes + photos / commentaire.",
    entretien: "Ajoute des lignes pour l’entretien de ce client.",
    compte: "Coordonnées et accès client.",
  };

  /** Checklist fixe pour hivernage passif (× = sous-étapes de la vidange hydraulique) */
  const HIVERNAGE_PASSIF_TEMPLATE = [
    { id: "lavage_filtre", label: "Lavage/rinçage du filtre à sable (s)" },
    {
      id: "traitements",
      label:
        "Traitements (mise en circulation de la vanne 6 voies en recirculation pour mélanger)",
    },
    { id: "robot", label: "Nettoyage et rangement du robot" },
    { id: "bassin", label: "Nettoyage bassin" },
    { id: "flotteurs", label: "Mise en place flotteurs" },
    { id: "paniers", label: "Nettoyage et rangement des paniers de skimmer" },
    { id: "guizmo", label: "Mise en place guizmo" },
    { id: "couverture", label: "Mise en place de la couverture d’hivernage" },
    { id: "arret_filtration", label: "Arrêt filtration" },
    {
      id: "vidange_hydraulique",
      label: "Vidange du réseau hydraulique",
      children: [
        { id: "vh_pompe", label: "Pompe de filtration" },
        { id: "vh_filtre", label: "Filtre à sable" },
        { id: "vh_surpresseur", label: "Surpresseur" },
        { id: "vh_pac", label: "Vidange pompe à chaleur" },
        { id: "vh_cellule", label: "Cellule électrolyseur" },
        { id: "vh_chambre", label: "Chambre d’analyse" },
        { id: "vh_reseau", label: "Réseau hydraulique" },
        { id: "vh_vanne6", label: "Vanne 6 voies" },
      ],
    },
    { id: "sondes", label: "Hivernage des sondes" },
    { id: "disjoncter", label: "Disjoncter les coffrets électriques" },
  ];

  /** @type {Record<string, {id:string,label:string,photos:Array,parent_id?:string,selected?:boolean}[]>} */
  let optionsSteps = {
    ouverture: [],
    local_technique: [],
    hivernage: [],
    entretien: [],
  };
  /** @type {{mode:"actif"|"passif", commentaire:string}} */
  let hivernageMeta = { mode: "actif", commentaire: "" };
  let activeOption = null;
  /** @type {Record<string, Array>} photos serveur par instruction_id */
  let hivernagePhotosByInst = {};
  /** @type {Record<string, {localId:string,file:File,url:string}[]>} photos locales avant création client */
  let pendingPhotosByInst = {};
  let dashFiltre = "tous";

  function emptyOptionsSteps() {
    return { ouverture: [], local_technique: [], hivernage: [], entretien: [] };
  }

  function flatPassifTemplate() {
    const out = [];
    HIVERNAGE_PASSIF_TEMPLATE.forEach((item) => {
      out.push({
        id: item.id,
        label: item.label,
        parent_id: null,
        selected: false,
        commentaire: "",
        photos: [],
      });
      (item.children || []).forEach((ch) => {
        out.push({
          id: ch.id,
          label: ch.label,
          parent_id: item.id,
          selected: false,
          commentaire: "",
          photos: [],
        });
      });
    });
    return out;
  }

  function ensurePassifHivernageSteps() {
    const byId = Object.fromEntries(
      (optionsSteps.hivernage || []).map((s) => [s.id, s])
    );
    optionsSteps.hivernage = flatPassifTemplate().map((tpl) => {
      // Alias id historique (typo « pomme »)
      const prev =
        byId[tpl.id] ||
        (tpl.id === "vh_pompe" ? byId.vh_pomme : null);
      return {
        ...tpl,
        selected: prev ? !!prev.selected : false,
        commentaire: prev ? String(prev.commentaire || "").trim() : "",
        photos: prev?.photos || [],
      };
    });
  }

  function optionHasContent(key) {
    if (key === "compte") {
      return !!(
        $("#chk-creer-compte")?.checked ||
        !$("#compte-credentials")?.hidden ||
        !$("#compte-identifiant-line")?.hidden
      );
    }
    if (key === "hivernage") {
      if ((hivernageMeta.commentaire || "").trim()) return true;
      const steps = optionsSteps.hivernage || [];
      if (hivernageMeta.mode === "passif") {
        return steps.some(
          (s) =>
            s.selected ||
            (s.commentaire || "").trim() ||
            (hivernagePhotosByInst[s.id] || []).length ||
            (pendingPhotosByInst[s.id] || []).length
        );
      }
    }
    const steps = optionsSteps[key] || [];
    return steps.some((s) => {
      if ((s.label || "").trim()) return true;
      if ((s.commentaire || "").trim()) return true;
      if ((s.photos || []).length) return true;
      if ((hivernagePhotosByInst[s.id] || []).length) return true;
      if ((pendingPhotosByInst[s.id] || []).length) return true;
      return false;
    });
  }

  function refreshOptionButtonLabels() {
    $$("#option-grid .option-btn").forEach((btn) => {
      const key = btn.dataset.option;
      if (!key) return;
      btn.textContent = optionHasContent(key)
        ? OPTION_LABELS[key] || key
        : OPTION_CREATE_LABELS[key] || OPTION_LABELS[key] || key;
    });
  }

  function mountOptionPanelUnder(key) {
    const panel = $("#option-panel");
    const item = $(`#option-grid [data-option-item="${key}"]`);
    if (!panel || !item) return;
    item.appendChild(panel);
  }

  function unmountOptionPanel() {
    const panel = $("#option-panel");
    const stack = $("#option-grid");
    if (!panel || !stack) return;
    stack.after(panel);
  }

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

  function collectOptionStepsFromEditor() {
    const root = $("#option-steps-editor");
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

  function syncActiveOptionFromEditor() {
    if (!activeOption || !OPTION_KEYS.includes(activeOption)) return;
    const labeled = collectOptionStepsFromEditor();
    const emptyKept = (optionsSteps[activeOption] || []).filter(
      (s) => !(s.label || "").trim() && !labeled.some((l) => l.id === s.id)
    );
    optionsSteps[activeOption] = [...labeled, ...emptyKept];
  }

  function collectHivernageFromEditor() {
    syncActiveOptionFromEditor();
    syncHivernageMetaFromUi();
    const steps = optionsSteps.hivernage || [];
    if (hivernageMeta.mode === "passif") {
      return steps
        .filter((s) => s.selected && (s.label || "").trim())
        .map((s) => ({ id: s.id, label: s.label.trim() }));
    }
    return steps
      .filter((s) => (s.label || "").trim())
      .map((s) => ({ id: s.id, label: s.label.trim() }));
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
    renderOptionStepsEditor();
    refreshOptionButtonLabels();
  }

  function appendStepPhotos(photosEl, step) {
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
  }

  function bindStepCommentaire(row, step) {
    const ta = $("textarea[data-step-com]", row);
    if (!ta) return;
    ta.addEventListener("input", () => {
      step.commentaire = ta.value;
      refreshOptionButtonLabels();
    });
  }

  function renderPassifHivernageEditor(root) {
    ensurePassifHivernageSteps();
    const steps = optionsSteps.hivernage || [];
    root.innerHTML = "";
    const list = document.createElement("div");
    list.className = "hiv-passif-list";
    steps.forEach((step) => {
      const row = document.createElement("div");
      row.className =
        "hiv-step hiv-step--passif" + (step.parent_id ? " hiv-step--child" : "");
      row.dataset.id = step.id;
      row.innerHTML =
        `<label class="hiv-passif-check">` +
        `<input type="checkbox" ${step.selected ? "checked" : ""} />` +
        `<span>${escapeHtml(step.label)}</span>` +
        `</label>` +
        `<textarea class="hiv-step-com" data-step-com rows="2" placeholder="Commentaire pour cette étape…">${escapeHtml(step.commentaire || "")}</textarea>` +
        `<div class="hiv-photos" data-photos></div>`;
      const chk = $("input[type='checkbox']", row);
      chk?.addEventListener("change", () => {
        step.selected = !!chk.checked;
        refreshOptionButtonLabels();
      });
      bindStepCommentaire(row, step);
      appendStepPhotos($("[data-photos]", row), step);
      list.appendChild(row);
    });
    root.appendChild(list);
  }

  function renderOptionStepsEditor() {
    const root = $("#option-steps-editor");
    if (!root) return;
    const key = activeOption;
    if (!key || !OPTION_KEYS.includes(key)) {
      root.innerHTML = "";
      return;
    }

    if (key === "hivernage" && hivernageMeta.mode === "passif") {
      renderPassifHivernageEditor(root);
      return;
    }

    const steps = optionsSteps[key] || [];
    root.innerHTML = "";
    if (!steps.length) {
      const empty = document.createElement("p");
      empty.className = "muted muted--sm";
      empty.textContent = "Section vide — appuie sur « Ajouter une ligne ».";
      root.appendChild(empty);
      return;
    }

    const isHiv = key === "hivernage";
    steps.forEach((step) => {
      const row = document.createElement("div");
      row.className = "hiv-step";
      row.dataset.id = step.id;
      row.innerHTML =
        `<div class="hiv-step-row">` +
        `<input type="text" value="${escapeHtml(step.label)}" placeholder="Info (ex. Pompe Speedeo 1.5 CV)…" />` +
        `<button type="button" class="hiv-step-remove" data-act="remove">Retirer</button>` +
        `</div>` +
        (isHiv
          ? `<textarea class="hiv-step-com" data-step-com rows="2" placeholder="Commentaire pour cette ligne…">${escapeHtml(step.commentaire || "")}</textarea>`
          : "") +
        `<div class="hiv-photos" data-photos></div>`;

      appendStepPhotos($("[data-photos]", row), step);
      if (isHiv) bindStepCommentaire(row, step);

      $(".hiv-step-remove", row)?.addEventListener("click", () => {
        if (!activeOption || !OPTION_KEYS.includes(activeOption)) return;
        syncActiveOptionFromEditor();
        optionsSteps[activeOption] = (optionsSteps[activeOption] || []).filter(
          (s) => s.id !== step.id
        );
        const pend = pendingPhotosByInst[step.id] || [];
        pend.forEach((p) => URL.revokeObjectURL(p.url));
        delete pendingPhotosByInst[step.id];
        renderOptionStepsEditor();
        refreshOptionButtonLabels();
      });

      $("input[type='text']", row)?.addEventListener("input", (ev) => {
        if (!activeOption) return;
        const s = (optionsSteps[activeOption] || []).find((x) => x.id === step.id);
        if (s) s.label = ev.target.value;
        refreshOptionButtonLabels();
      });

      root.appendChild(row);
    });
  }

  function syncHivernageMetaFromUi() {
    const modeEl = document.querySelector('input[name="hivernage_mode"]:checked');
    hivernageMeta.mode = modeEl?.value === "passif" ? "passif" : "actif";
    hivernageMeta.commentaire = ($("#input-hivernage-commentaire")?.value || "").trim();
  }

  function applyHivernageModeUi() {
    const isHiv = activeOption === "hivernage";
    const extra = $("#extra-hivernage");
    const addBloc = $("#bloc-add-option-step");
    const comBloc = $("#bloc-hivernage-commentaire");
    const hint = $("#option-panel-hint");
    const modeHint = $("#hivernage-mode-hint");
    if (extra) {
      extra.hidden = !isHiv;
      extra.style.display = isHiv ? "" : "none";
    }
    if (!isHiv) {
      if (addBloc) addBloc.hidden = false;
      if (comBloc) comBloc.hidden = true;
      return;
    }
    const passif = hivernageMeta.mode === "passif";
    if (addBloc) addBloc.hidden = passif;
    if (comBloc) comBloc.hidden = false;
    const comInput = $("#input-hivernage-commentaire");
    if (comInput) comInput.value = hivernageMeta.commentaire || "";
    document.querySelectorAll('input[name="hivernage_mode"]').forEach((r) => {
      r.checked = r.value === hivernageMeta.mode;
    });
    if (hint) {
      hint.textContent = passif
        ? "Coche les étapes concernées. Chaque ligne a son commentaire + photos. Les lignes indentées sont des sous-réponses de la vidange hydraulique."
        : "Hivernage actif : lignes libres, chacune avec commentaire + photos. Commentaire général en bas si besoin.";
    }
    if (modeHint) {
      modeHint.textContent = passif
        ? "Passif : sélectionne les éléments (commentaire et photos par étape)."
        : "Actif : lignes libres + commentaire et photos par ligne.";
    }
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
    syncActiveOptionFromEditor();
    const steps = activeOption ? optionsSteps[activeOption] || [] : [];
    const fromState = steps.find((s) => s.id === instructionId);
    const label = (fromState?.label || "").trim();
    if (!label) {
      setFeedback(
        $("#nouveau-feedback"),
        "Renseignez le texte de l'étape avant d'ajouter une photo.",
        false
      );
      return;
    }

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
      options_checklists: (() => {
        syncActiveOptionFromEditor();
        syncHivernageMetaFromUi();
        const out = {};
        OPTION_KEYS.forEach((k) => {
          if (k === "hivernage") {
            out.hivernage = {
              mode: hivernageMeta.mode,
              commentaire: hivernageMeta.commentaire || "",
              steps: (optionsSteps.hivernage || []).map((s) => ({
                id: s.id,
                label: (s.label || "").trim(),
                parent_id: s.parent_id || null,
                selected:
                  hivernageMeta.mode === "passif"
                    ? !!s.selected
                    : !!(s.label || "").trim(),
                commentaire: String(s.commentaire || "").trim(),
              })).filter((s) => s.label),
            };
            return;
          }
          out[k] = (optionsSteps[k] || [])
            .map((s) => ({ id: s.id, label: (s.label || "").trim() }))
            .filter((s) => s.label);
        });
        return out;
      })(),
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
        let iid = ph.instruction_id;
        if (iid === "vh_pomme") iid = "vh_pompe";
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
    if (leavingVisite) {
      stopWakeListen();
      if (typeof releaseSharedMic === "function") releaseSharedMic();
    }
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
    optionsSteps = emptyOptionsSteps();
    hivernageMeta = { mode: "actif", commentaire: "" };
    hivernagePhotosByInst = {};
    activeOption = null;
    const chkHiv = $("#chk-piscine-hivernee");
    if (chkHiv) chkHiv.checked = false;
    setEditMode(false);
    closeOptionPanel();
    syncBlocPassage();
    syncBlocCompte();
  }

  function setEditMode(isEdit) {
    const bloc = $("#bloc-edit-options");
    if (bloc) bloc.hidden = !isEdit;
    const del = $("#btn-supprimer-client");
    if (del && !isEdit) del.hidden = true;
    if (isEdit) refreshOptionButtonLabels();
  }

  function pruneEmptyOptionSteps(key) {
    if (!key || !OPTION_KEYS.includes(key)) return;
    optionsSteps[key] = (optionsSteps[key] || []).filter((s) => {
      if ((s.label || "").trim()) return true;
      if ((hivernagePhotosByInst[s.id] || []).length) return true;
      if ((pendingPhotosByInst[s.id] || []).length) return true;
      return false;
    });
  }

  function closeOptionPanel() {
    syncActiveOptionFromEditor();
    const closing = activeOption;
    pruneEmptyOptionSteps(closing);
    activeOption = null;
    const panel = $("#option-panel");
    if (panel) panel.hidden = true;
    unmountOptionPanel();
    $$(".option-btn").forEach((b) => b.classList.remove("is-on"));
    $$(".option-extra").forEach((el) => {
      el.hidden = true;
      el.style.display = "none";
    });
    const stepsBloc = $("#bloc-option-steps");
    if (stepsBloc) stepsBloc.hidden = false;
    const addBloc = $("#bloc-add-option-step");
    if (addBloc) addBloc.hidden = false;
    const comBloc = $("#bloc-hivernage-commentaire");
    if (comBloc) comBloc.hidden = true;
    refreshOptionButtonLabels();
  }

  function openOptionPanel(key) {
    syncActiveOptionFromEditor();
    if (activeOption && activeOption !== key) pruneEmptyOptionSteps(activeOption);
    activeOption = key;
    mountOptionPanelUnder(key);
    const panel = $("#option-panel");
    if (panel) panel.hidden = false;
    $$(".option-btn").forEach((b) =>
      b.classList.toggle("is-on", b.dataset.option === key)
    );
    const extraCompte = $("#extra-compte");
    if (extraCompte) {
      const showCompte = key === "compte";
      extraCompte.hidden = !showCompte;
      extraCompte.style.display = showCompte ? "" : "none";
    }
    const stepsBloc = $("#bloc-option-steps");
    if (stepsBloc) stepsBloc.hidden = key === "compte";
    if (key === "compte") {
      const extraHiv = $("#extra-hivernage");
      if (extraHiv) {
        extraHiv.hidden = true;
        extraHiv.style.display = "none";
      }
      syncBlocCompte();
    } else {
      const blocCompte = $("#bloc-compte");
      if (blocCompte) blocCompte.hidden = true;
      const hint = $("#option-panel-hint");
      if (hint) hint.textContent = OPTION_HINTS[key] || "Ajoute des lignes d’information.";
      if (key === "hivernage") {
        if (hivernageMeta.mode === "passif") {
          ensurePassifHivernageSteps();
        } else if (!(optionsSteps.hivernage || []).length) {
          optionsSteps.hivernage = [
            { id: newInstructionId(), label: "", commentaire: "", photos: [] },
          ];
        }
      } else if (!(optionsSteps[key] || []).length) {
        optionsSteps[key] = [{ id: newInstructionId(), label: "", photos: [] }];
      }
      applyHivernageModeUi();
      renderOptionStepsEditor();
    }
    refreshOptionButtonLabels();
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
    const opts = client.options_checklists || {};
    optionsSteps = emptyOptionsSteps();
    const hivCfg =
      client.hivernage_config ||
      (opts.hivernage && !Array.isArray(opts.hivernage) ? opts.hivernage : null) ||
      {};
    hivernageMeta = {
      mode: hivCfg.mode === "passif" ? "passif" : "actif",
      commentaire: String(hivCfg.commentaire || "").trim(),
    };
    OPTION_KEYS.forEach((k) => {
      if (k === "hivernage") return;
      const raw = opts[k];
      if (Array.isArray(raw)) {
        optionsSteps[k] = raw
          .map((item) => {
            if (item && typeof item === "object") {
              return {
                id: String(item.id || "").trim() || newInstructionId(),
                label: String(item.label || item.texte || "").trim(),
                photos: [],
              };
            }
            const label = String(item || "").trim();
            return label ? { id: newInstructionId(), label, photos: [] } : null;
          })
          .filter(Boolean);
      }
    });
    const hivStepsRaw = Array.isArray(hivCfg.steps)
      ? hivCfg.steps
      : Array.isArray(opts.hivernage)
        ? opts.hivernage
        : client.checklist_hivernage || [];
    if (hivernageMeta.mode === "passif") {
      const hasSelectedFlag = hivStepsRaw.some(
        (s) => s && typeof s === "object" && "selected" in s
      );
      const selectedIds = new Set();
      const selectedLabels = new Set();
      hivStepsRaw.forEach((s) => {
        if (!s) return;
        const lab = String(
          (typeof s === "object" ? s.label || s.texte : s) || ""
        )
          .trim()
          .toLowerCase();
        const iid = typeof s === "object" ? String(s.id || "").trim() : "";
        const isOn = hasSelectedFlag
          ? !!(typeof s === "object" && s.selected)
          : true;
        if (!isOn) return;
        if (iid) selectedIds.add(iid);
        if (lab) selectedLabels.add(lab);
      });
      // Ancien id/libellé « pomme » → « pompe »
      if (selectedIds.has("vh_pomme")) selectedIds.add("vh_pompe");
      if (selectedLabels.has("pomme de filtration")) {
        selectedLabels.add("pompe de filtration");
      }
      const comById = {};
      const comByLabel = {};
      hivStepsRaw.forEach((s) => {
        if (!s || typeof s !== "object") return;
        const c = String(s.commentaire || "").trim();
        if (!c) return;
        const iid = String(s.id || "").trim();
        const lab = String(s.label || s.texte || "")
          .trim()
          .toLowerCase();
        if (iid === "vh_pomme" || iid === "vh_pompe") comById.vh_pompe = c;
        else if (iid) comById[iid] = c;
        if (lab === "pomme de filtration" || lab === "pompe de filtration") {
          comByLabel["pompe de filtration"] = c;
        } else if (lab) comByLabel[lab] = c;
      });
      ensurePassifHivernageSteps();
      optionsSteps.hivernage.forEach((s) => {
        s.selected =
          selectedIds.has(s.id) ||
          selectedLabels.has((s.label || "").toLowerCase());
        s.commentaire =
          comById[s.id] ||
          comByLabel[(s.label || "").toLowerCase()] ||
          "";
      });
    } else {
      optionsSteps.hivernage = hivStepsRaw
        .map((item) => {
          if (item && typeof item === "object") {
            return {
              id: String(item.id || "").trim() || newInstructionId(),
              label: String(item.label || item.texte || "").trim(),
              commentaire: String(item.commentaire || "").trim(),
              photos: [],
              selected: true,
            };
          }
          const label = String(item || "").trim();
          return label
            ? { id: newInstructionId(), label, commentaire: "", photos: [], selected: true }
            : null;
        })
        .filter(Boolean);
    }
    closeOptionPanel();
    refreshOptionButtonLabels();
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
    setEditMode(true);
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

  $("#btn-add-option-step")?.addEventListener("click", () => {
    if (!activeOption || !OPTION_KEYS.includes(activeOption)) return;
    if (activeOption === "hivernage" && hivernageMeta.mode === "passif") return;
    syncActiveOptionFromEditor();
    const labeled = (optionsSteps[activeOption] || []).filter((s) => (s.label || "").trim());
    const emptyKept = (optionsSteps[activeOption] || []).filter(
      (s) => !(s.label || "").trim()
    );
    optionsSteps[activeOption] = [
      ...labeled,
      ...emptyKept,
      {
        id: newInstructionId(),
        label: "",
        commentaire: activeOption === "hivernage" ? "" : undefined,
        photos: [],
      },
    ];
    renderOptionStepsEditor();
    refreshOptionButtonLabels();
    const inputs = $$("#option-steps-editor input[type='text']");
    inputs[inputs.length - 1]?.focus();
  });

  document.querySelectorAll('input[name="hivernage_mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      if (activeOption !== "hivernage") return;
      const prevMode = hivernageMeta.mode;
      syncHivernageMetaFromUi();
      if (hivernageMeta.mode === "passif") {
        ensurePassifHivernageSteps();
      } else if (prevMode === "passif") {
        const selected = (optionsSteps.hivernage || [])
          .filter((s) => s.selected && (s.label || "").trim())
          .map((s) => ({
            id: s.id,
            label: s.label,
            commentaire: String(s.commentaire || "").trim(),
            photos: [],
            selected: true,
          }));
        optionsSteps.hivernage = selected.length
          ? selected
          : [{ id: newInstructionId(), label: "", commentaire: "", photos: [] }];
      }
      applyHivernageModeUi();
      renderOptionStepsEditor();
      refreshOptionButtonLabels();
    });
  });

  $("#input-hivernage-commentaire")?.addEventListener("input", () => {
    hivernageMeta.commentaire = ($("#input-hivernage-commentaire")?.value || "").trim();
    refreshOptionButtonLabels();
  });

  $$("#option-grid .option-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.option;
      if (!key) return;
      if (activeOption === key) {
        closeOptionPanel();
        return;
      }
      openOptionPanel(key);
    });
  });
  $("#btn-close-option")?.addEventListener("click", () => closeOptionPanel());

  $("#photo-lightbox")?.addEventListener("click", () => closePhotoLightbox());
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closePhotoLightbox();
      closeOptionPanel();
    }
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
        const enCours = data.hivernages_en_cours || 0;
        summary.innerHTML =
          `<div class="dash-stat"><strong>${data.total_clients}</strong><span>Clients</span></div>` +
          `<div class="dash-stat dash-stat--warn"><strong>${data.factures_impayees_total}</strong><span>Factures à payer</span></div>` +
          `<div class="dash-stat dash-stat--ok"><strong>${data.piscines_hivernees}</strong><span>Hivernées</span></div>` +
          `<div class="dash-stat"><strong>${enCours}</strong><span>En hivernage</span></div>`;
      }
      let rows = data.clients || [];
      if (dashFiltre === "impayes") rows = rows.filter((c) => c.a_impaye);
      else if (dashFiltre === "hivernees") rows = rows.filter((c) => c.piscine_hivernee);
      else if (dashFiltre === "actives")
        rows = rows.filter((c) => !c.piscine_hivernee && !c.hivernage_en_cours);

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
        } else if (c.hivernage_en_cours) {
          flags.push(`<span class="dash-flag dash-flag--ice">En hivernage</span>`);
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
  $("#chk-creer-compte")?.addEventListener("change", () => {
    syncBlocCompte();
    refreshOptionButtonLabels();
  });
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

  function isHivernageVisite(visite) {
    return (visite?.type || "") === "hivernage";
  }

  function hivernagePausable(visite) {
    return (
      isHivernageVisite(visite) &&
      !!visite?.debut &&
      !!visite?.fin &&
      !checklistComplete(visite?.checklist || [])
    );
  }

  function updateFinButton(visite) {
    const btn = $("#btn-fin");
    const btnDebut = $("#btn-debut");
    const hint = $("#fin-hint");
    if (!btn) return;
    const isHiv = isHivernageVisite(visite);
    const ok = checklistComplete(visite?.checklist || []);
    const running = !!(visite?.debut && !visite?.fin);
    const paused = hivernagePausable(visite);
    const done = !!(visite?.debut && visite?.fin && (!isHiv || ok));

    // Hivernage : on peut terminer même sans tout cocher (pause)
    btn.disabled = !running || (!isHiv && !ok);
    btn.textContent = isHiv && !ok ? "Terminer (pause)" : "Terminer";

    if (btnDebut) {
      btnDebut.disabled = running || done;
      btnDebut.textContent = paused ? "Reprendre" : "Début";
    }

    if (hint) {
      if (done) {
        hint.textContent = isHiv
          ? "Hivernage terminé — 1 facture Hivernage ajoutée."
          : "Prestation terminée.";
        hint.hidden = false;
      } else if (paused) {
        hint.textContent = `Hivernage en cours — temps cumulé ${formatDuree(
          visite.duree_minutes
        )}. Appuyez sur Reprendre.`;
        hint.hidden = false;
      } else if (!visite?.debut) {
        hint.textContent = "Appuyez sur Début pour démarrer la prestation.";
        hint.hidden = false;
      } else if (isHiv && !ok) {
        hint.textContent =
          "Vous pouvez terminer sans tout cocher : la piscine restera « en hivernage ».";
        hint.hidden = false;
      } else if (!ok) {
        hint.textContent = "Cochez toutes les étapes obligatoires pour terminer.";
        hint.hidden = false;
      } else if (isHiv) {
        hint.textContent =
          "Tout est coché — Terminer clôture l’hivernage et ajoute 1 facture.";
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
        (item.commentaire
          ? `<em class="check-com">${escapeHtml(item.commentaire)}</em>`
          : "") +
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
      btn.addEventListener("click", () => {
        if (!visiteActive || btn.dataset.busy === "1") return;
        const item = visiteActive.checklist.find((i) => i.id === btn.dataset.item);
        if (!item) return;
        const next = !item.done;
        const prev = !!item.done;
        // Feedback immédiat (sans attendre Supabase)
        item.done = next;
        const li = btn.closest(".check-item");
        if (li) li.classList.toggle("is-done", next);
        const mark = $(".check-mark", btn);
        if (mark) mark.textContent = next ? "✓" : "";
        updateFinButton(visiteActive);
        setFeedback($("#check-feedback"), "", null);
        btn.dataset.busy = "1";
        api(`/api/visites/${visiteActive.id}/checklist`, {
          method: "PATCH",
          body: JSON.stringify({ item_id: btn.dataset.item, done: next }),
        })
          .then((res) => {
            const photosById = Object.fromEntries(
              (visiteActive.checklist || []).map((i) => [i.id, i.photos])
            );
            const checklist = (res.checklist || []).map((i) => ({
              ...i,
              photos: i.photos || photosById[i.id] || [],
            }));
            visiteActive = {
              ...visiteActive,
              ...res,
              client: visiteActive.client,
              checklist,
            };
            updateFinButton(visiteActive);
          })
          .catch((err) => {
            item.done = prev;
            if (li) li.classList.toggle("is-done", prev);
            if (mark) mark.textContent = prev ? "✓" : "";
            updateFinButton(visiteActive);
            setFeedback($("#check-feedback"), err.message, false);
          })
          .finally(() => {
            delete btn.dataset.busy;
          });
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
    if (!visite?.debut) {
      el.textContent = "Pas encore démarré";
      el.classList.remove("is-running", "is-done");
      updateFinButton(visite);
      return;
    }
    if (visite.debut && !visite.fin) {
      const cumule = visite.duree_minutes
        ? ` · déjà ${formatDuree(visite.duree_minutes)}`
        : "";
      el.textContent = `En cours depuis ${visite.debut}${cumule}`;
      el.classList.add("is-running");
      el.classList.remove("is-done");
      updateFinButton(visite);
      return;
    }
    if (hivernagePausable(visite)) {
      el.textContent = `En pause — temps cumulé ${formatDuree(visite.duree_minutes)}`;
      el.classList.remove("is-running");
      el.classList.add("is-done");
      updateFinButton(visite);
      return;
    }
    el.textContent = `Durée : ${formatDuree(visite.duree_minutes)} (${visite.debut} → ${visite.fin})`;
    el.classList.add("is-done");
    el.classList.remove("is-running");
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
    // Historique en arrière-plan
    refreshHistorique(c.id).catch(() => {});
    releaseSharedMic();
    $("#ia-texte").value = "";
    // Écoute permanente des mots-clés dès l’ouverture de la visite
    startWakeListen()
      .then(() => {
        setFeedback(
          $("#ia-feedback"),
          "Écoute active — dites « note / fourniture / analyse » + le détail. Rien n’est écrit tant qu’il n’y a pas de commande.",
          true
        );
      })
      .catch(() => {
        setFeedback(
          $("#ia-feedback"),
          "Micro indisponible — autorisez-le, ou utilisez « Maintenir pour parler ».",
          false
        );
      });
  }

  function nowLocalStamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  $("#btn-debut").addEventListener("click", () => {
    if (!visiteActive) return;
    const canResume = hivernagePausable(visiteActive);
    if (visiteActive.debut && !visiteActive.fin) return;
    if (visiteActive.debut && visiteActive.fin && !canResume) return;
    const btn = $("#btn-debut");
    if (btn?.dataset.busy === "1") return;
    if (btn) btn.dataset.busy = "1";
    const prevDebut = visiteActive.debut;
    const prevFin = visiteActive.fin;
    const prevDuree = visiteActive.duree_minutes;
    visiteActive.debut = nowLocalStamp();
    visiteActive.fin = null;
    if (!canResume) visiteActive.duree_minutes = null;
    renderChrono(visiteActive);
    setFeedback(
      $("#check-feedback"),
      canResume ? "Hivernage repris." : "Prestation démarrée.",
      true
    );
    api(`/api/visites/${visiteActive.id}/debut`, { method: "POST", body: "{}" })
      .then((res) => {
        visiteActive = {
          ...visiteActive,
          ...res,
          client: visiteActive.client,
          checklist: visiteActive.checklist,
        };
        renderChrono(visiteActive);
      })
      .catch((err) => {
        visiteActive.debut = prevDebut;
        visiteActive.fin = prevFin;
        visiteActive.duree_minutes = prevDuree;
        renderChrono(visiteActive);
        setFeedback($("#check-feedback"), err.message, false);
      })
      .finally(() => {
        if (btn) delete btn.dataset.busy;
      });
  });

  $("#btn-fin").addEventListener("click", () => {
    if (!visiteActive) return;
    if (!visiteActive.debut || visiteActive.fin) return;
    const isHiv = isHivernageVisite(visiteActive);
    const ok = checklistComplete(visiteActive.checklist || []);
    if (!isHiv && !ok) {
      setFeedback(
        $("#check-feedback"),
        "Impossible de terminer : cochez toutes les étapes obligatoires.",
        false
      );
      updateFinButton(visiteActive);
      return;
    }
    const btn = $("#btn-fin");
    if (btn?.dataset.busy === "1") return;
    if (btn) btn.dataset.busy = "1";
    const prevFin = visiteActive.fin;
    const prevDuree = visiteActive.duree_minutes;
    visiteActive.fin = nowLocalStamp();
    renderChrono(visiteActive);
    setFeedback(
      $("#check-feedback"),
      isHiv && !ok ? "Hivernage mis en pause…" : "Prestation terminée.",
      true
    );
    api(`/api/visites/${visiteActive.id}/fin`, { method: "POST", body: "{}" })
      .then((res) => {
        visiteActive = {
          ...visiteActive,
          ...res,
          client: visiteActive.client,
          checklist: visiteActive.checklist,
        };
        if (visiteActive.client) {
          if (res.hivernage_complete) {
            visiteActive.client.piscine_hivernee = true;
            visiteActive.client.hivernage_en_cours = false;
          } else if (res.hivernage_en_cours) {
            visiteActive.client.piscine_hivernee = false;
            visiteActive.client.hivernage_en_cours = true;
          }
        }
        if (clientActif && visiteActive.client_id === clientActif.id) {
          clientActif = { ...clientActif, ...visiteActive.client };
        }
        renderChrono(visiteActive);
        let msg = `Temps cumulé : ${formatDuree(res.duree_minutes)}.`;
        if (isHiv && res.hivernage_complete) {
          msg = `Hivernage terminé — ${msg}`;
          if (res.facture_hivernage) msg += " Facture 1× Hivernage ajoutée.";
        } else if (isHiv && res.hivernage_en_cours) {
          msg = `Piscine en hivernage (pause) — ${msg} Reprenez quand vous voulez.`;
        } else {
          msg = `Prestation terminée — ${msg}`;
        }
        setFeedback($("#check-feedback"), msg, true);
      })
      .catch((err) => {
        visiteActive.fin = prevFin;
        visiteActive.duree_minutes = prevDuree;
        renderChrono(visiteActive);
        setFeedback($("#check-feedback"), err.message, false);
      })
      .finally(() => {
        if (btn) delete btn.dataset.busy;
      });
  });

  // ── Intent + vocal (écoute mots-clés uniquement, pas d’enreg. en boucle) ───

  let wantWakeListen = false;
  let armedIntent = null;
  let speechRec = null;
  let speechRestartTimer = null;
  let speechPhraseBuffer = "";
  let speechPhraseTimer = null;
  let captureTimer = null;
  let holdStream = null;
  let holdRecorder = null;
  let holdChunks = [];
  let holdRecording = false;
  let holdStartedAt = 0;
  let sharedMicStream = null;
  let processingVocal = false;
  /** Buffer phrase vocale (survit aux redémarrages SpeechRecognition) */
  let voicePhraseBuffer = "";
  let voicePhraseTimer = null;
  /** idle | wake | capture | busy */
  let voicePhase = "idle";
  const CAPTURE_TIMEOUT_MS = isMobileVocalSafe() ? 14000 : 8000;

  function isMobileVocalSafe() {
    return (
      /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || "") ||
      (navigator.maxTouchPoints > 1 && /Macintosh/i.test(navigator.userAgent || ""))
    );
  }

  function isMobileVocal() {
    return isMobileVocalSafe();
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

    out = normaliserNombresOraux(out);
    return out.replace(/\s+/g, " ").trim();
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

  function clearVoicePhraseBuffer() {
    voicePhraseBuffer = "";
    if (voicePhraseTimer) {
      clearTimeout(voicePhraseTimer);
      voicePhraseTimer = null;
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
      btn.textContent = "Écoute mots-clés · ON";
    } else {
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = "Écoute mots-clés · OFF";
    }
  }

  function updateRecordButton() {
    const btn = $("#btn-parler");
    if (!btn) return;
    const mobile = isMobileVocal();
    if (holdRecording) {
      btn.classList.add("is-recording");
      btn.textContent = mobile ? "Parlez… (relâcher)" : "Arrêter l’enregistrement";
      btn.setAttribute("aria-pressed", "true");
    } else {
      btn.classList.remove("is-recording");
      btn.textContent = mobile ? "Maintenir pour parler" : "Enregistrer";
      btn.setAttribute("aria-pressed", "false");
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
        sub.textContent = "Continuez : donnez le détail…";
      } else if (state === "listening") {
        sub.textContent = "J’écoute : note / fourniture / analyse + le détail";
      } else {
        sub.textContent = "Écoute des mots-clés";
      }
    }
  }

  function stripWakePrefix(texte, intent) {
    let t = String(texte || "").trim();
    // Enlever le mot-clé partout en tête (ne doit jamais s’écrire dans la note / fourniture)
    if (intent === "note") {
      t = t.replace(/^(une?\s+)?(notes?|noter)\b[:\s,-]*/i, "").trim();
    } else if (intent === "analyse") {
      t = t.replace(/^(une?\s+)?(analyses?|analyser|annalyse|analise|analyce)\b[:\s,-]*/i, "").trim();
    } else if (intent === "fourniture") {
      t = t
        .replace(
          /^(une?\s+)?(fournitures?|forniture|furniture|formiture|pourriture|factures?|facturer)\b[:\s,-]*/i,
          ""
        )
        .trim();
    }
    return t.replace(/^[.,;:\-\s]+/, "").trim();
  }

  function normaliserNombresOraux(text) {
    let out = String(text || "");
    const nombres = {
      zero: "0",
      un: "1",
      une: "1",
      deux: "2",
      trois: "3",
      quatre: "4",
      cinq: "5",
      six: "6",
      sept: "7",
      huit: "8",
      neuf: "9",
      dix: "10",
      onze: "11",
      douze: "12",
      treize: "13",
      quatorze: "14",
      quinze: "15",
      seize: "16",
      vingt: "20",
    };
    // Ne pas transformer « un/une » article hors contexte quantité (fait après pour produits)
    Object.entries(nombres).forEach(([mot, ch]) => {
      if (mot === "un" || mot === "une") return;
      out = out.replace(new RegExp(`\\b${mot}\\b`, "gi"), ch);
    });
    // Erreurs Whisper / Chrome : « deux » → « de », « to », « ceux »…
    out = out.replace(
      /\b(to|too|two|ceux|d['’]?eux|deuxe?|deu)\s+(bidon|sac|seau|cartouche|litre|paquet|bo[iî]te|kg|chlore|brome)\b/gi,
      "2 $2"
    );
    out = out.replace(
      /\b(fourniture|facture|facturer)\s+(de|des|du)\s+(bidon|sac|seau|cartouche|litre|paquet|bo[iî]te)\b/gi,
      "$1 2 $3"
    );
    out = out.replace(
      /^(de|des|du)\s+(bidon|sac|seau|cartouche|litre|paquet|bo[iî]te)\b/i,
      "2 $2"
    );
    out = out.replace(
      /\b(un|une)\s+(bidon|sac|seau|cartouche|litre|paquet|bo[iî]te|kg)\b/gi,
      "1 $2"
    );
    // « 2bidons » / « x2 » / « 2 x »
    out = out.replace(/\b(\d+)\s*x\s*(?=\w)/gi, "$1 ");
    out = out.replace(/\bx\s*(\d+)\b/gi, "$1");
    out = out.replace(/\b(\d+)(bidon|sac|seau|cartouche|litre|paquet)/gi, "$1 $2");
    return out;
  }

  function enterCapture(intent) {
    // Mot-clé seul → on continue d’écouter la suite (sans écrire, sans MediaRecorder)
    clearCaptureTimer();
    armedIntent = intent;
    setIntent(intent);
    voicePhase = "capture";
    setListenStatus("capture");
    setFeedback(
      $("#ia-feedback"),
      `Mot-clé « ${intent} » — dites maintenant le détail (rien n’est enregistré tant que ce n’est pas complet).`,
      null
    );
    if (!speechRec && wantWakeListen) scheduleSpeechRestart();
    captureTimer = setTimeout(() => {
      if (voicePhase === "capture" && !processingVocal && !holdRecording) {
        setFeedback(
          $("#ia-feedback"),
          "Temps écoulé — redites « note / fourniture / analyse » + le détail.",
          false
        );
        clearVoicePhraseBuffer();
        backToWakeOrIdle();
        scheduleSpeechRestart();
      }
    }, Math.max(CAPTURE_TIMEOUT_MS, 14000));
  }

  async function traiterTexteVocal(
    texteBrut,
    { fromHold = false, keywordAlreadySaid = false } = {}
  ) {
    const brut = String(texteBrut || "").trim();
    if (!brut || processingVocal) return false;

    let texte = normaliserTexteVocal(brut);

    if (isStopCommand(texte) || isStopCommand(stripWakePrefix(texte, armedIntent || detectWakeIntent(texte)))) {
      clearVoicePhraseBuffer();
      terminerEnregistrementVocal();
      scheduleSpeechRestart();
      return false;
    }

    texte = stripStopSuffix(texte);
    if (!texte) {
      clearVoicePhraseBuffer();
      terminerEnregistrementVocal();
      scheduleSpeechRestart();
      return false;
    }

    const wake = detectWakeIntent(texte);

    // Vocal : on n’enregistre QUE si un mot-clé a été dit (dans cette prise ou juste avant)
    if (fromHold && !wake && !keywordAlreadySaid) {
      setFeedback(
        $("#ia-feedback"),
        `Aucun mot-clé dit — rien n’a été enregistré. Dites « note… », « fourniture… » ou « analyse… ».`,
        false
      );
      clearVoicePhraseBuffer();
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return false;
    }

    const intent = wake || (keywordAlreadySaid ? armedIntent : null) || (!fromHold ? intentForce : null);

    // Écoute wake : ignorer tout ce qui n’est PAS un mot-clé (pas d’enreg.)
    if (!fromHold && voicePhase === "wake" && !wake && !armedIntent) {
      return false;
    }

    if (!intent) {
      if (fromHold) {
        setFeedback(
          $("#ia-feedback"),
          `Aucun mot-clé dit — rien n’a été enregistré. Dites « note… », « fourniture… » ou « analyse… ».`,
          false
        );
      }
      clearVoicePhraseBuffer();
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return false;
    }

    // Juste le mot-clé → attendre le détail (sans rien écrire)
    if (isMostlyWakeWord(texte)) {
      if (fromHold) {
        setFeedback(
          $("#ia-feedback"),
          `Seulement « ${intent} » entendu. Dites toute la phrase (ex. fourniture 2 bidons de chlore).`,
          false
        );
        clearVoicePhraseBuffer();
        backToWakeOrIdle();
        scheduleSpeechRestart();
        return false;
      }
      enterCapture(intent);
      return false;
    }

    // Contenu utile → enregistrer, puis retour écoute permanente
    let full = texte;
    if (armedIntent && !detectWakeIntent(texte)) {
      full = `${armedIntent} ${texte}`;
    }
    full = stripStopSuffix(full);
    const finalIntent = detectWakeIntent(full) || armedIntent || intent;
    const contenu = stripWakePrefix(full, finalIntent);
    if (!contenu || isStopCommand(contenu) || /^\d+([.,]\d+)?$/.test(contenu.trim())) {
      // Pas encore de détail (ou juste un chiffre) → garder le buffer, continuer d’écouter
      if (!fromHold && finalIntent) {
        enterCapture(finalIntent);
        return false;
      }
      setFeedback(
        $("#ia-feedback"),
        `Il manque le détail après « ${finalIntent || "note"} ».`,
        false
      );
      clearVoicePhraseBuffer();
      terminerEnregistrementVocal();
      scheduleSpeechRestart();
      return false;
    }

    processingVocal = true;
    voicePhase = "busy";
    clearCaptureTimer();
    stopSpeechRec({ keepWanted: true });
    setListenStatus("busy");
    try {
      setIntent(finalIntent);
      // Écrire seulement maintenant (contenu sans mot-clé), puis envoyer
      $("#ia-texte").value = contenu;
      await envoyerCommande(contenu, finalIntent);
      armedIntent = null;
      $("#ia-texte").value = "";
      clearVoicePhraseBuffer();
      return true;
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
    // Écoute continue pour attraper « fourniture 2 bidons… » d’un coup
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 3;

    const flushPhraseBuffer = () => {
      voicePhraseTimer = null;
      const buffered = voicePhraseBuffer.trim();
      if (!buffered || holdRecording || processingVocal) return;

      const intent = detectWakeIntent(buffered) || armedIntent;
      if (!intent) {
        voicePhraseBuffer = "";
        return;
      }

      const normalized = normaliserTexteVocal(buffered);
      const contenu = stripWakePrefix(normalized, intent).trim();

      // Mot-clé seul, ou juste un chiffre (ex. « fourniture 2 ») → encore attendre la suite
      // SANS vider le buffer (sinon on perd le « 2 »)
      if (!contenu || isMostlyWakeWord(normalized) || /^\d+([.,]\d+)?$/.test(contenu)) {
        enterCapture(intent);
        voicePhraseTimer = setTimeout(flushPhraseBuffer, mobile ? 1600 : 1100);
        return;
      }

      // Il manque souvent l’unité juste après le chiffre → encore un peu de marge
      if (/^\d+([.,]\d+)?\s+(de|des|du)?$/i.test(contenu)) {
        enterCapture(intent);
        voicePhraseTimer = setTimeout(flushPhraseBuffer, mobile ? 1400 : 900);
        return;
      }

      // Ne pas vider avant succès : si le parsing attend encore, on garde la phrase
      traiterTexteVocal(normalized).then((ok) => {
        if (ok) clearVoicePhraseBuffer();
      });
    };

    rec.onstart = () => {
      if (voicePhase === "capture") setListenStatus("capture");
      else if (wantWakeListen) {
        voicePhase = voicePhase === "capture" ? "capture" : "wake";
        if (voicePhase === "wake") setListenStatus("listening");
      }
    };

    rec.onresult = (event) => {
      if (holdRecording || processingVocal) return;

      let finalTxt = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const piece = event.results[i][0]?.transcript || "";
        if (event.results[i].isFinal) finalTxt += piece;
      }

      const txt = finalTxt.trim();
      if (!txt) return;

      // Accumuler toute la phrase (ne pas écrire tant que ce n’est pas complet)
      voicePhraseBuffer = `${voicePhraseBuffer} ${txt}`.trim();
      if (voicePhraseTimer) clearTimeout(voicePhraseTimer);

      const hasKeyword = !!(detectWakeIntent(voicePhraseBuffer) || armedIntent);
      if (!hasKeyword) {
        voicePhraseBuffer = "";
        return;
      }

      const waitMs = isMostlyWakeWord(voicePhraseBuffer)
        ? mobile
          ? 2000
          : 1400
        : mobile
          ? 1100
          : 700;
      voicePhraseTimer = setTimeout(flushPhraseBuffer, waitMs);
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
      if (err === "network" && mobile) {
        setFeedback(
          $("#ia-feedback"),
          "Écoute instable sur ce téléphone — utilisez « Maintenir pour parler » en secours.",
          false
        );
      }
    };

    rec.onend = () => {
      speechRec = null;
      if (!wantWakeListen || processingVocal || holdRecording) return;
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
    clearVoicePhraseBuffer();
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
      throw new Error("Micro refusé");
    }
    const ok = startSpeechRec();
    if (!ok) {
      setListenStatus("idle");
      setFeedback(
        $("#ia-feedback"),
        isMobileVocal()
          ? "Écoute auto indisponible — utilisez « Maintenir pour parler »."
          : "Écoute auto indisponible — utilisez Chrome/Edge ou « Maintenir pour parler ».",
        false
      );
      throw new Error("SpeechRecognition indisponible");
    }
    setListenStatus("listening");
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

  async function ensureSharedMic() {
    if (sharedMicStream && sharedMicStream.active) return sharedMicStream;
    sharedMicStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    return sharedMicStream;
  }

  function releaseSharedMic() {
    if (sharedMicStream) {
      sharedMicStream.getTracks().forEach((t) => t.stop());
      sharedMicStream = null;
    }
  }

  /** true si l’enregistrement a été lancé après un mot-clé déjà dit */
  let holdAfterKeyword = false;

  async function startHoldRecord({ fromWake = false, intent = null } = {}) {
    if (holdRecording || processingVocal) return;
    // Couper l’écoute mots-clés pendant l’enregistrement (évite la double capture)
    stopSpeechRec({ keepWanted: true });
    try {
      holdStream = await ensureSharedMic();
      holdChunks = [];
      const mime = pickRecorderMime();
      holdRecorder = mime
        ? new MediaRecorder(holdStream, { mimeType: mime })
        : new MediaRecorder(holdStream);
      holdRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) holdChunks.push(e.data);
      };
      holdRecorder.start(isMobileVocal() ? 120 : 250);
      holdRecording = true;
      holdStartedAt = Date.now();
      // Mot-clé déjà dit (écoute) uniquement — les puces ne forcent pas l’enregistrement
      const forced = intent || (fromWake ? armedIntent : null);
      holdAfterKeyword = !!(fromWake && forced);
      if (forced) {
        armedIntent = forced;
        setIntent(forced);
      }
      voicePhase = "capture";
      setListenStatus("capture");
      updateRecordButton();
      const mobile = isMobileVocal();
      const hint = holdAfterKeyword
        ? mobile
          ? `Mot-clé « ${forced} » — parlez la suite, relâchez pour enregistrer.`
          : `Mot-clé « ${forced} » — parlez la suite puis Arrêter.`
        : mobile
          ? "Maintenez et dites un mot-clé + le détail (ex. fourniture 2 bidons de chlore)."
          : "Dites un mot-clé + le détail (note / fourniture / analyse), puis Arrêter.";
      setFeedback($("#ia-feedback"), hint, null);
    } catch (err) {
      holdRecording = false;
      holdAfterKeyword = false;
      updateRecordButton();
      setFeedback($("#ia-feedback"), err.message || "Micro indisponible.", false);
      scheduleSpeechRestart();
    }
  }

  async function stopHoldRecord() {
    if (!holdRecording || !holdRecorder) return;
    clearCaptureTimer();
    const elapsed = Date.now() - (holdStartedAt || 0);
    if (elapsed < 400) {
      holdRecording = false;
      try {
        holdRecorder.onstop = null;
        holdRecorder.stop();
      } catch {
        /* */
      }
      holdRecorder = null;
      holdChunks = [];
      releaseSharedMic();
      updateRecordButton();
      setFeedback($("#ia-feedback"), "Trop court — parlez 1–2 secondes puis Arrêter.", false);
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return;
    }

    holdRecording = false;
    updateRecordButton();
    setListenStatus("busy");
    setFeedback($("#ia-feedback"), "Transcription…", null);

    const recorder = holdRecorder;
    const chunks = holdChunks;
    const mimeType = recorder.mimeType || "";
    const blob = await new Promise((resolve) => {
      recorder.onstop = () => {
        resolve(new Blob(chunks, { type: mimeType || "audio/webm" }));
      };
      try {
        if (recorder.state !== "inactive") recorder.requestData?.();
        recorder.stop();
      } catch {
        resolve(chunks.length ? new Blob(chunks, { type: mimeType || "audio/webm" }) : null);
      }
    });
    holdRecorder = null;
    holdChunks = [];
    holdStream = null;
    releaseSharedMic();

    if (!blob || blob.size < 350) {
      setFeedback($("#ia-feedback"), "Trop court — recommencez l’enregistrement.", false);
      backToWakeOrIdle();
      scheduleSpeechRestart();
      return;
    }
    try {
      const fd = new FormData();
      const type = blob.type || mimeType || "";
      const ext =
        type.includes("mp4") || type.includes("aac") || type.includes("m4a")
          ? "mp4"
          : type.includes("ogg")
            ? "ogg"
            : type.includes("mpeg") || type.includes("mp3")
              ? "mp3"
              : "webm";
      fd.append("audio", blob, `note.${ext}`);
      const res = await fetch("/api/transcribe", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail;
        throw new Error(
          typeof detail === "string" ? detail : "Transcription échouée"
        );
      }
      let texte = data.texte || "";
      const keywordAlreadySaid = holdAfterKeyword && !!armedIntent;
      if (keywordAlreadySaid && texte && !detectWakeIntent(texte)) {
        texte = `${armedIntent} ${texte}`;
      }
      holdAfterKeyword = false;
      await traiterTexteVocal(texte, { fromHold: true, keywordAlreadySaid });
    } catch (err) {
      holdAfterKeyword = false;
      setFeedback($("#ia-feedback"), err.message || "Erreur vocal.", false);
      backToWakeOrIdle();
      scheduleSpeechRestart();
    }
  }

  async function toggleRecord() {
    if (processingVocal) return;
    if (holdRecording) {
      await stopHoldRecord();
    } else {
      await startHoldRecord();
    }
  }

  $$(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const name = chip.dataset.intent;
      if (intentForce === name) {
        setIntent(null);
        setFeedback($("#ia-feedback"), "Type annulé.", null);
        return;
      }
      setIntent(name);
      const ta = $("#ia-texte");
      if (ta) {
        ta.focus({ preventScroll: false });
        // iOS / Android : focus synchrone dans le geste utilisateur ouvre le clavier
        try {
          ta.click();
        } catch (_) {
          /* ignore */
        }
      }
      const labels = { note: "Note", fourniture: "Fourniture", analyse: "Analyse" };
      setFeedback(
        $("#ia-feedback"),
        `${labels[name] || name} : écrivez puis Envoyer, ou maintenez pour parler.`,
        null
      );
    });
  });

  $("#ia-texte")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      envoyerCommande($("#ia-texte").value);
    }
  });

  $("#btn-ecoute")?.addEventListener("click", async () => {
    if (wantWakeListen) {
      stopWakeListen();
      setFeedback($("#ia-feedback"), "Écoute mots-clés coupée — seul le bouton Enregistrer capture l’audio.", null);
      return;
    }
    await startWakeListen();
  });

  async function envoyerCommande(texte, intentOverride = null) {
    if (!clientActif) {
      setFeedback($("#ia-feedback"), "Aucun client actif.", false);
      return;
    }
    let t = normaliserTexteVocal((texte || "").trim());
    if (!t) {
      setFeedback($("#ia-feedback"), "Texte vide.", false);
      return;
    }
    const detected =
      intentOverride || intentForce || detectWakeIntent(t) || null;
    if (!detected) {
      setFeedback(
        $("#ia-feedback"),
        `Non compris : « ${t} ». Dites « fourniture 3 bidons de chlore ».`,
        false
      );
      return;
    }
    setIntent(detected);
    // Le mot-clé sert seulement à router — on ne l’écrit jamais
    const contenu = stripWakePrefix(t, detected) || t;
    $("#ia-texte").value = contenu;
    try {
      const payload = {
        client_id: clientActif.id,
        texte: contenu,
        intent_force: detected,
      };
      const res = await api("/api/ia", { method: "POST", body: JSON.stringify(payload) });
      if (!res.success) {
        setFeedback($("#ia-feedback"), res.message || "Non compris.", false);
        return;
      }
      if (res.intent === "note") {
        const d = res.note.date_ref
          ? `${formatDateFr(res.note.date)} · ${formatDateFr(res.note.date_ref)}`
          : formatDateFr(res.note.date);
        $("#ia-texte").value = res.note.texte || "";
        setFeedback($("#ia-feedback"), `Note (${d}) : ${res.note.texte}`, true);
      } else if (res.intent === "analyse") {
        const d = res.analyse.date_ref
          ? `${formatDateFr(res.analyse.date)} · ${formatDateFr(res.analyse.date_ref)}`
          : formatDateFr(res.analyse.date);
        $("#ia-texte").value = res.analyse.texte || "";
        setFeedback($("#ia-feedback"), `Analyse (${d}) : ${res.analyse.texte}`, true);
      } else if (res.intent === "fourniture") {
        const f = res.fourniture;
        const d = f.date_ref
          ? `${formatDateFr(f.date)} · ${formatDateFr(f.date_ref)}`
          : formatDateFr(f.date);
        $("#ia-texte").value = `${f.quantite} × ${f.description}`;
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
    // Téléphone : maintenir pendant toute la phrase (évite de ne capter que « fourniture »)
    // Desktop : clic pour démarrer / arrêter
    if (isMobileVocal()) {
      let holdPointerId = null;
      const down = (e) => {
        if (e.button != null && e.button !== 0) return;
        if (holdRecording || holdPointerId != null || processingVocal) return;
        e.preventDefault();
        holdPointerId = e.pointerId ?? "touch";
        try {
          if (e.pointerId != null) btnParler.setPointerCapture(e.pointerId);
        } catch {
          /* */
        }
        startHoldRecord();
      };
      const up = (e) => {
        if (holdPointerId == null && !holdRecording) return;
        if (
          e.pointerId != null &&
          holdPointerId != null &&
          holdPointerId !== "touch" &&
          e.pointerId !== holdPointerId
        ) {
          return;
        }
        e.preventDefault();
        holdPointerId = null;
        try {
          if (e.pointerId != null && btnParler.hasPointerCapture?.(e.pointerId)) {
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
      btnParler.addEventListener("lostpointercapture", () => {
        if (holdRecording) {
          holdPointerId = null;
          stopHoldRecord();
        }
      });
      if (!window.PointerEvent) {
        btnParler.addEventListener(
          "touchstart",
          (e) => {
            if (e.touches.length > 1) return;
            e.preventDefault();
            down(e);
          },
          { passive: false }
        );
        btnParler.addEventListener(
          "touchend",
          (e) => {
            e.preventDefault();
            up(e);
          },
          { passive: false }
        );
        btnParler.addEventListener(
          "touchcancel",
          (e) => {
            e.preventDefault();
            up(e);
          },
          { passive: false }
        );
      }
    } else {
      btnParler.addEventListener("click", (e) => {
        e.preventDefault();
        toggleRecord();
      });
    }
    btnParler.addEventListener("contextmenu", (e) => e.preventDefault());
    updateRecordButton();
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
        setEditMode(true);
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
        setEditMode(true);
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
