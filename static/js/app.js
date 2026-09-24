/* Transcritor da Gabi — frontend (JavaScript puro, sem dependências). */
(() => {
  "use strict";

  const config = JSON.parse(document.getElementById("app-config").textContent);

  const POLL_MS = 3000;
  const POLL_HIDDEN_MS = 10000;
  const COPY_RESET_MS = 2500;
  const PREVIEW_TIMEOUT_MS = 8000;

  const ICONS = {
    check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    error: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7 7 17"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    circle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7.5"/></svg>',
    info: '<svg viewBox="0 0 24 24"><path d="M12 11v6M12 7.5h.01"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="M12 7.5v6M12 17h.01"/></svg>',
    film: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9.5 4.5 2.5-4.5 2.5z"/></svg>',
    spinner: '<span class="spinner"></span>',
  };

  const STATUS = {
    LOCAL: { label: "Pronto para envio", tone: "idle", icon: "circle" },
    UPLOADING: { label: "Enviando vídeo...", tone: "active", icon: "spinner" },
    UPLOAD_ERROR: { label: "Falha no envio", tone: "error", icon: "error" },
    PENDING: { label: "Aguardando processamento", tone: "waiting", icon: "clock" },
    EXTRACTING_AUDIO: { label: "Extraindo áudio...", tone: "active", icon: "spinner" },
    TRANSCRIBING: { label: "Transcrevendo...", tone: "active", icon: "spinner" },
    COMPLETED: { label: "Transcrição concluída", tone: "success", icon: "check" },
    ERROR: { label: "Erro no processamento", tone: "error", icon: "error" },
  };
  const SERVER_ACTIVE = new Set(["PENDING", "EXTRACTING_AUDIO", "TRANSCRIBING"]);
  const IN_PROGRESS = new Set(["EXTRACTING_AUDIO", "TRANSCRIBING"]);
  const SENDABLE = new Set(["LOCAL", "UPLOAD_ERROR"]);

  const $ = (id) => document.getElementById(id);
  const els = {
    pickButton: $("pick-button"),
    transcribeButton: $("transcribe-button"),
    transcribeLabel: $("transcribe-label"),
    clearButton: $("clear-button"),
    fileInput: $("file-input"),
    grid: $("video-grid"),
    empty: $("empty-state"),
    summary: $("summary"),
    summaryIcon: $("summary-icon"),
    summaryTitle: $("summary-title"),
    summaryDetail: $("summary-detail"),
    workerAlert: $("worker-alert"),
    toasts: $("toasts"),
    dialog: $("confirm-dialog"),
    template: $("card-template"),
  };

  const state = {
    items: [],
    uploading: false,
    uploadIndex: 0,
    uploadTotal: 0,
    clearing: false,
    workerOnline: true,
    pollTimer: null,
    fetching: false,
    connectionLost: false,
    hadActiveWork: false,
    generation: 0, // incrementa a cada limpeza; respostas antigas são descartadas
  };
  let nextKey = 1;

  // ---------------------------------------------------------------- utilidades

  const plural = (n, singular, pluralForm) => `${n} ${n === 1 ? singular : pluralForm}`;
  const extensionOf = (name) => {
    const dot = name.lastIndexOf(".");
    return dot >= 0 ? name.slice(dot).toLowerCase() : "";
  };
  const csrfToken = () => document.querySelector('meta[name="csrf-token"]').content;

  function formatBytes(bytes) {
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1).replace(".", ",")} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2).replace(".", ",")} GB`;
  }

  function friendlyMessage(err, fallback) {
    // TypeError = falha de rede do fetch (mensagem técnica, em inglês).
    if (!err || err instanceof TypeError || !err.message) return fallback;
    return err.message;
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  // ------------------------------------------------------------------ toasts

  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast toast--${type}`;
    const icon = document.createElement("span");
    icon.className = "toast__icon";
    icon.innerHTML = type === "success" ? ICONS.check : type === "error" ? ICONS.error : ICONS.info;
    const text = document.createElement("span");
    text.textContent = message;
    el.append(icon, text);
    els.toasts.appendChild(el);
    while (els.toasts.children.length > 4) els.toasts.firstElementChild.remove();

    setTimeout(() => {
      el.classList.add("is-leaving");
      setTimeout(() => el.remove(), 250);
    }, type === "error" ? 6500 : 4000);
  }

  // ------------------------------------------------------------------- itens

  function createItem(props) {
    const item = {
      key: nextKey++,
      file: null,
      serverId: null,
      adoptedAt: 0,
      name: "",
      size: 0,
      status: "LOCAL",
      progress: null,
      uploadedBytes: 0,
      error: null,
      thumbnailUrl: null,
      previewUrl: null,
      queuePosition: null,
      hasTranscription: false,
      transcription: null,
      loadingTranscription: false,
      el: null,
      copyTimer: null,
      ...props,
    };
    state.items.push(item);
    return item;
  }

  function removeItem(item) {
    if (item.el) item.el.root.remove();
    clearTimeout(item.copyTimer);
    state.items = state.items.filter((i) => i !== item);
  }

  function applyServerData(item, video) {
    item.serverId = video.id;
    item.name = video.name;
    item.status = video.status;
    item.progress = video.progress;
    item.error = video.error;
    item.queuePosition = video.queue_position;
    item.hasTranscription = video.has_transcription;
    if (video.thumbnail_url) item.thumbnailUrl = video.thumbnail_url;
    if (video.status !== "COMPLETED") item.transcription = null;
    item.file = null; // já está no servidor; libera a referência ao arquivo local
  }

  function adoptUploaded(item, video) {
    // Um polling pode ter trazido este vídeo antes da resposta do upload.
    const duplicate = state.items.find((i) => i !== item && i.serverId === video.id);
    if (duplicate) removeItem(duplicate);
    applyServerData(item, video);
    item.adoptedAt = performance.now();
  }

  function mergeStatus(data, { notify, requestStartedAt }) {
    state.workerOnline = Boolean(data.worker_online);
    const serverIds = new Set(data.videos.map((v) => v.id));

    for (const item of [...state.items]) {
      if (item.serverId && !serverIds.has(item.serverId) && item.adoptedAt < requestStartedAt) {
        removeItem(item);
      }
    }

    for (const video of data.videos) {
      let item = state.items.find((i) => i.serverId === video.id);
      const previous = item ? item.status : null;
      if (!item) item = createItem({ adoptedAt: performance.now() });
      applyServerData(item, video);

      if (notify && previous && previous !== video.status) {
        if (video.status === "COMPLETED") toast(`Transcrição concluída: ${video.name}`, "success");
        if (video.status === "ERROR") toast(`Não foi possível processar “${video.name}”.`, "error");
      }
      if (video.status === "COMPLETED" && video.has_transcription && item.transcription === null) {
        loadTranscription(item);
      }
    }

    const serverItems = state.items.filter((i) => i.serverId);
    if (serverItems.some((i) => SERVER_ACTIVE.has(i.status))) {
      state.hadActiveWork = true;
    } else if (state.hadActiveWork && !state.uploading) {
      state.hadActiveWork = false;
      if (notify) announceFinished(serverItems);
    }
    render();
  }

  function announceFinished(serverItems) {
    const errors = serverItems.filter((i) => i.status === "ERROR").length;
    const done = serverItems.filter((i) => i.status === "COMPLETED").length;
    if (!serverItems.length) return;
    if (!errors) toast("Todos os vídeos foram transcritos", "success");
    else toast(`Processamento finalizado: ${done} concluído(s), ${errors} com erro.`, "error");
  }

  async function loadTranscription(item) {
    if (item.loadingTranscription || !item.serverId) return;
    item.loadingTranscription = true;
    const generation = state.generation;
    try {
      const url = config.urls.transcription.replace("/0/", `/${item.serverId}/`);
      const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
      const data = await readJson(response);
      if (!response.ok || !data) throw new Error();
      if (generation === state.generation) item.transcription = data.transcription || "";
    } catch {
      item.transcriptionFailed = true;
    } finally {
      item.loadingTranscription = false;
      if (state.items.includes(item)) renderItem(item);
    }
  }

  // ------------------------------------------------------ seleção de arquivos

  function addFiles(files) {
    if (!files.length) return;
    let added = 0;
    let duplicates = 0;
    const rejected = [];

    for (const file of files) {
      const extension = extensionOf(file.name);
      if (!config.allowedExtensions.includes(extension)) {
        rejected.push(`“${file.name}” tem um formato não suportado`);
        continue;
      }
      if (file.size === 0) {
        rejected.push(`“${file.name}” está vazio`);
        continue;
      }
      if (file.size > config.maxUploadSizeBytes) {
        rejected.push(`“${file.name}” excede o limite de ${config.maxUploadSizeMb} MB`);
        continue;
      }
      const isDuplicate = state.items.some(
        (i) => i.file && i.file.name === file.name && i.file.size === file.size && i.file.lastModified === file.lastModified,
      );
      if (isDuplicate) {
        duplicates++;
        continue;
      }
      const item = createItem({ file, name: file.name, size: file.size, status: "LOCAL" });
      queuePreview(item);
      added++;
    }

    render();
    if (added) {
      toast(`${plural(added, "vídeo adicionado", "vídeos adicionados")}. Clique em “Transcrever vídeos” para começar.`, "success");
    }
    if (rejected.length === 1) toast(`Arquivo ignorado: ${rejected[0]}.`, "error");
    if (rejected.length > 1) {
      toast(`${rejected.length} arquivos ignorados (formato não suportado ou acima de ${config.maxUploadSizeMb} MB).`, "error");
    }
    if (duplicates) toast(`${plural(duplicates, "vídeo já estava", "vídeos já estavam")} na lista.`, "info");
  }

  // Miniatura local (antes do envio) gerada pelo próprio navegador.
  // Formatos que o navegador não decodifica (ex.: AVI) ficam com o placeholder
  // até o servidor gerar a thumbnail com o FFmpeg.
  const previewQueue = [];
  let previewRunning = false;

  function queuePreview(item) {
    previewQueue.push(item);
    if (!previewRunning) runPreviews();
  }

  async function runPreviews() {
    previewRunning = true;
    while (previewQueue.length) {
      const item = previewQueue.shift();
      if (!item.file || !state.items.includes(item)) continue;
      const dataUrl = await capturePreview(item.file);
      if (dataUrl && state.items.includes(item)) {
        item.previewUrl = dataUrl;
        renderItem(item);
      }
    }
    previewRunning = false;
  }

  function capturePreview(file) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const video = document.createElement("video");
      let finished = false;
      const finish = (result) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        video.removeAttribute("src");
        video.load();
        URL.revokeObjectURL(url);
        resolve(result);
      };
      const timer = setTimeout(() => finish(null), PREVIEW_TIMEOUT_MS);

      video.muted = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.addEventListener("loadedmetadata", () => {
        const duration = video.duration;
        video.currentTime = Number.isFinite(duration) && duration > 2 ? 1 : Number.isFinite(duration) ? duration / 2 : 0;
      });
      video.addEventListener("seeked", () => {
        try {
          const width = 480;
          const height = Math.round((width * video.videoHeight) / video.videoWidth) || 270;
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          canvas.getContext("2d").drawImage(video, 0, 0, width, height);
          finish(canvas.toDataURL("image/jpeg", 0.72));
        } catch {
          finish(null);
        }
      });
      video.addEventListener("error", () => finish(null));
      video.src = url;
    });
  }

  // ------------------------------------------------------------------ upload

  function uploadFile(item) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", item.file, item.file.name);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", config.urls.upload);
      xhr.setRequestHeader("X-CSRFToken", csrfToken());
      xhr.setRequestHeader("Accept", "application/json");

      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        item.progress = Math.min(100, Math.round((event.loaded / event.total) * 100));
        item.uploadedBytes = Math.min(item.size, event.loaded);
        renderItem(item);
      });
      xhr.addEventListener("load", () => {
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          /* resposta não-JSON (ex.: página de erro do servidor) */
        }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.video) resolve(data.video);
        else reject(new Error((data && data.error) || uploadErrorMessage(xhr.status)));
      });
      const interrupted = () => reject(new Error("O envio foi interrompido. Verifique sua conexão e tente novamente."));
      xhr.addEventListener("error", interrupted);
      xhr.addEventListener("abort", interrupted);
      xhr.addEventListener("timeout", interrupted);
      xhr.send(form);
    });
  }

  function uploadErrorMessage(status) {
    if (status === 413) return `O arquivo excede o limite de ${config.maxUploadSizeMb} MB.`;
    if (status === 403) return "Sua sessão expirou. Recarregue a página e tente novamente.";
    if (status === 507) return "Não há espaço em disco suficiente no servidor.";
    return "Não foi possível enviar o vídeo. Tente novamente.";
  }

  async function startTranscription() {
    const queue = state.items.filter((i) => !i.serverId && i.file && SENDABLE.has(i.status));
    if (!queue.length || state.uploading) return;

    const generation = state.generation;
    state.uploading = true;
    state.uploadIndex = 0;
    state.uploadTotal = queue.length;
    state.hadActiveWork = true;
    toast(queue.length === 1 ? "Envio iniciado." : `Envio iniciado: ${queue.length} vídeos.`, "info");
    render();

    let sent = 0;
    let failed = 0;
    for (const item of queue) {
      if (generation !== state.generation) break;
      if (!state.items.includes(item)) continue;
      state.uploadIndex++;
      Object.assign(item, { status: "UPLOADING", progress: 0, uploadedBytes: 0, error: null });
      render();
      try {
        const video = await uploadFile(item);
        if (generation !== state.generation) break;
        adoptUploaded(item, video);
        sent++;
        if (!state.pollTimer && !state.fetching) schedulePoll();
      } catch (err) {
        Object.assign(item, { status: "UPLOAD_ERROR", progress: null, error: friendlyMessage(err, uploadErrorMessage(0)) });
        failed++;
      }
      render();
    }

    state.uploading = false;
    render();
    if (sent) {
      toast(
        sent === 1
          ? "Vídeo enviado. Ele será transcrito assim que chegar a vez dele na fila."
          : `${sent} vídeos enviados. Eles serão transcritos um de cada vez.`,
        "success",
      );
    }
    if (failed) {
      toast(
        failed === 1
          ? "1 vídeo não pôde ser enviado. Veja o motivo no card."
          : `${failed} vídeos não puderam ser enviados. Veja o motivo nos cards.`,
        "error",
      );
    }
    refresh();
  }

  // ----------------------------------------------------------------- polling

  function needsPolling() {
    return state.uploading || state.items.some((i) => i.serverId && SERVER_ACTIVE.has(i.status));
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!needsPolling()) return;
    state.pollTimer = setTimeout(refresh, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
  }

  async function refresh() {
    if (state.fetching) return;
    state.fetching = true;
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    const generation = state.generation;
    const requestStartedAt = performance.now();
    try {
      const response = await fetch(config.urls.status, { headers: { Accept: "application/json" }, cache: "no-store" });
      const data = await readJson(response);
      if (!response.ok || !data) throw new Error();
      if (generation === state.generation) mergeStatus(data, { notify: true, requestStartedAt });
      if (state.connectionLost) {
        state.connectionLost = false;
        toast("Conexão restabelecida.", "success");
      }
    } catch {
      if (!state.connectionLost) {
        state.connectionLost = true;
        toast("Não foi possível atualizar o status. Tentando novamente...", "error");
      }
    } finally {
      state.fetching = false;
      schedulePoll();
    }
  }

  // ------------------------------------------------------------------ limpar

  function confirmClear() {
    return new Promise((resolve) => {
      const dialog = els.dialog;
      if (typeof dialog.showModal !== "function") {
        resolve(window.confirm("Tem certeza que deseja remover todos os vídeos e transcrições?"));
        return;
      }
      dialog.returnValue = "";
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
      dialog.showModal();
    });
  }

  async function clearAll() {
    if (!state.items.length || state.uploading || state.clearing) return;
    if (!(await confirmClear())) return;

    state.clearing = true;
    render();
    try {
      const response = await fetch(config.urls.clear, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken(), Accept: "application/json" },
      });
      const data = await readJson(response);
      if (!response.ok) {
        const fallback = response.status === 403
          ? "Sua sessão expirou. Recarregue a página e tente novamente."
          : "Não foi possível limpar os vídeos. Tente novamente.";
        throw new Error((data && data.error) || fallback);
      }
      state.generation++;
      previewQueue.length = 0;
      for (const item of [...state.items]) removeItem(item);
      state.hadActiveWork = false;
      toast("Todos os vídeos foram removidos", "success");
    } catch (err) {
      toast(friendlyMessage(err, "Sem conexão com o servidor. Tente novamente."), "error");
    } finally {
      state.clearing = false;
      render();
      schedulePoll();
    }
  }

  // ------------------------------------------------------------------ copiar

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        /* tenta o método alternativo abaixo */
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    textarea.remove();
    if (!ok) throw new Error("copy failed");
  }

  async function handleCopy(item) {
    if (item.transcription === null) await loadTranscription(item);
    if (!item.transcription) {
      toast("A transcrição ainda não está disponível. Tente novamente.", "error");
      return;
    }
    try {
      await copyText(item.transcription);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(item.el.transcriptText);
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Não foi possível copiar automaticamente. O texto foi selecionado: use Ctrl+C.", "error");
      return;
    }
    const { copyButton, copyLabel } = item.el;
    copyButton.classList.add("is-copied");
    copyLabel.textContent = "Copiado";
    clearTimeout(item.copyTimer);
    item.copyTimer = setTimeout(() => {
      copyButton.classList.remove("is-copied");
      copyLabel.textContent = "Copiar texto transcrito";
    }, COPY_RESET_MS);
    toast("Transcrição copiada para área de transferência", "success");
  }

  // --------------------------------------------------------------- renderização

  function buildCard(item) {
    const root = els.template.content.firstElementChild.cloneNode(true);
    const q = (selector) => root.querySelector(selector);
    const refs = {
      root,
      thumbImg: q(".card__thumb > img"),
      placeholder: q(".card__placeholder"),
      index: q(".card__index"),
      name: q(".card__name"),
      status: q(".status"),
      statusIcon: q(".status__icon"),
      statusLabel: q(".status__label"),
      statusPercent: q(".status__percent"),
      detail: q(".card__detail"),
      progress: q(".progress"),
      progressFill: q(".progress__fill"),
      error: q(".card__error"),
      transcript: q(".transcript"),
      transcriptText: q(".transcript__text"),
      copyButton: q(".btn--copy"),
      copyLabel: q(".btn--copy__label"),
    };
    refs.thumbImg.addEventListener("error", () => {
      refs.thumbImg.dataset.failed = "1";
      refs.thumbImg.hidden = true;
      refs.placeholder.hidden = false;
    });
    refs.copyButton.addEventListener("click", () => handleCopy(item));
    els.grid.appendChild(root);
    return refs;
  }

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  function detailFor(item) {
    switch (item.status) {
      case "LOCAL":
        return `${formatBytes(item.size)} · clique em “Transcrever vídeos” para enviar`;
      case "UPLOADING":
        return item.progress >= 100
          ? "Finalizando o envio e gerando a miniatura..."
          : `${formatBytes(item.uploadedBytes)} de ${formatBytes(item.size)}`;
      case "UPLOAD_ERROR":
        return "Clique em “Transcrever vídeos” para tentar novamente.";
      case "PENDING":
        if (!state.workerOnline) return "Na fila, aguardando o processador ser iniciado.";
        if (item.queuePosition === null || item.queuePosition === undefined) return "Na fila de processamento.";
        if (item.queuePosition === 0) return "Próximo da fila: o processamento começa em instantes.";
        return `Na fila: ${plural(item.queuePosition, "vídeo", "vídeos")} antes deste.`;
      case "EXTRACTING_AUDIO":
        return "Separando o áudio do vídeo. Em seguida vem a transcrição.";
      case "TRANSCRIBING":
        return "Convertendo a fala em texto. O resultado aparecerá aqui.";
      default:
        return "";
    }
  }

  function renderItem(item, position) {
    if (!item.el) item.el = buildCard(item);
    const el = item.el;
    const meta = STATUS[item.status] || STATUS.ERROR;

    el.root.classList.toggle("is-active", item.status === "UPLOADING" || IN_PROGRESS.has(item.status));
    el.root.classList.toggle("is-success", item.status === "COMPLETED");
    el.root.classList.toggle("is-error", item.status === "ERROR" || item.status === "UPLOAD_ERROR");
    if (position) setText(el.index, `#${position}`);

    setText(el.name, item.name);
    el.name.title = item.name;

    // Miniatura: prévia local do navegador ou thumbnail gerada pelo servidor.
    const src = item.previewUrl || item.thumbnailUrl;
    if (src && el.thumbImg.dataset.src !== src) {
      el.thumbImg.dataset.src = src;
      delete el.thumbImg.dataset.failed;
      el.thumbImg.src = src;
    }
    const showImage = Boolean(src) && !el.thumbImg.dataset.failed;
    el.thumbImg.hidden = !showImage;
    el.placeholder.hidden = showImage;

    // Status
    el.status.className = `status tone-${meta.tone}`;
    if (el.statusIcon.dataset.icon !== meta.icon) {
      el.statusIcon.innerHTML = ICONS[meta.icon];
      el.statusIcon.dataset.icon = meta.icon;
    }
    setText(el.statusLabel, meta.label);
    const showPercent = item.status === "UPLOADING" || (item.status === "TRANSCRIBING" && item.progress > 0);
    setText(el.statusPercent, showPercent ? `${item.progress}%` : "");
    setText(el.detail, detailFor(item));

    // Barra de progresso: percentual só quando existe métrica real.
    const determinate = showPercent;
    const indeterminate = item.status === "EXTRACTING_AUDIO" || (item.status === "TRANSCRIBING" && !(item.progress > 0));
    el.progress.hidden = !(determinate || indeterminate);
    el.progress.classList.toggle("is-indeterminate", indeterminate);
    el.progressFill.style.width = determinate ? `${item.progress}%` : "";

    // Erro
    const hasError = item.status === "ERROR" || item.status === "UPLOAD_ERROR";
    el.error.hidden = !hasError;
    if (hasError) setText(el.error, item.error || "Não foi possível processar o vídeo.");

    // Transcrição
    const completed = item.status === "COMPLETED";
    el.transcript.hidden = !completed;
    if (completed) {
      let text;
      let empty = true;
      if (!item.hasTranscription) text = "Nenhuma fala foi detectada neste vídeo.";
      else if (item.transcription !== null) {
        text = item.transcription;
        empty = false;
      } else if (item.transcriptionFailed && !item.loadingTranscription) {
        text = "Não foi possível carregar a transcrição. Clique em copiar para tentar novamente.";
      } else text = "Carregando transcrição...";
      setText(el.transcriptText, text);
      el.transcriptText.classList.toggle("is-empty", empty);
      el.copyButton.hidden = !item.hasTranscription;
      el.copyButton.disabled = item.loadingTranscription;
    }
  }

  function renderSummary() {
    const items = state.items;
    const serverItems = items.filter((i) => i.serverId);
    const hasActive = serverItems.some((i) => SERVER_ACTIVE.has(i.status));
    els.workerAlert.hidden = state.workerOnline || !hasActive;

    if (!items.length) {
      els.summary.hidden = true;
      return;
    }

    const localItems = items.filter((i) => !i.serverId && SENDABLE.has(i.status));
    const current = serverItems.find((i) => IN_PROGRESS.has(i.status));
    const pending = serverItems.filter((i) => i.status === "PENDING");
    const done = serverItems.filter((i) => i.status === "COMPLETED").length;
    const errors = serverItems.filter((i) => i.status === "ERROR").length;

    let tone = "active";
    let icon = ICONS.spinner;
    let title;
    let detail;

    if (state.uploading) {
      title = `Enviando vídeo ${state.uploadIndex} de ${state.uploadTotal}...`;
      detail = "Mantenha esta página aberta até o envio terminar. A transcrição começa automaticamente.";
    } else if (current) {
      const position = serverItems.indexOf(current) + 1;
      title = `Processando vídeo ${position} de ${serverItems.length}`;
      const stage = current.status === "EXTRACTING_AUDIO" ? "Extraindo áudio de" : "Transcrevendo";
      detail = `${stage} “${current.name}”. Os demais serão processados automaticamente, um por vez.`;
    } else if (pending.length) {
      tone = "idle";
      icon = ICONS.clock;
      title = state.workerOnline ? "Seus vídeos estão na fila" : "Vídeos aguardando o processador";
      detail = `${plural(pending.length, "vídeo aguardando", "vídeos aguardando")}. O processamento começa automaticamente.`;
    } else if (localItems.length) {
      tone = "idle";
      icon = ICONS.film;
      title = `${plural(localItems.length, "vídeo pronto", "vídeos prontos")} para envio`;
      detail = "Clique em “Transcrever vídeos” para começar.";
    } else if (!errors) {
      tone = "success";
      icon = ICONS.check;
      title = "Todos os vídeos foram transcritos";
      detail = `${plural(done, "transcrição pronta", "transcrições prontas")} para copiar.`;
    } else {
      tone = "warning";
      icon = ICONS.alert;
      title = done ? "Processamento finalizado" : "Não foi possível transcrever os vídeos";
      detail = `${done} concluído(s), ${errors} com erro. Veja os detalhes em cada card.`;
    }

    els.summary.hidden = false;
    els.summary.className = `summary summary--${tone}`;
    if (els.summaryIcon.dataset.icon !== icon) {
      els.summaryIcon.innerHTML = icon;
      els.summaryIcon.dataset.icon = icon;
    }
    setText(els.summaryTitle, title);
    setText(els.summaryDetail, detail);
  }

  function renderControls() {
    const hasSendable = state.items.some((i) => !i.serverId && SENDABLE.has(i.status));
    const processing = state.items.some((i) => i.serverId && SERVER_ACTIVE.has(i.status));
    const busy = state.uploading || (!hasSendable && processing);

    els.transcribeButton.disabled = state.uploading || state.clearing || !hasSendable;
    els.transcribeButton.classList.toggle("is-busy", busy);
    setText(
      els.transcribeLabel,
      state.uploading ? "Enviando vídeos..." : busy ? "Transcrevendo..." : "Transcrever vídeos",
    );
    els.clearButton.disabled = !state.items.length || state.uploading || state.clearing;
    els.pickButton.disabled = state.clearing;
  }

  function render() {
    state.items.forEach((item, index) => renderItem(item, index + 1));
    els.empty.hidden = state.items.length > 0;
    renderSummary();
    renderControls();
  }

  // ------------------------------------------------------------------- eventos

  els.pickButton.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => {
    addFiles(Array.from(els.fileInput.files || []));
    els.fileInput.value = ""; // permite selecionar o mesmo arquivo de novo
  });
  els.transcribeButton.addEventListener("click", startTranscription);
  els.clearButton.addEventListener("click", clearAll);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && needsPolling()) refresh();
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.uploading) event.preventDefault();
  });

  // Estado inicial vindo do servidor (evita "piscar" a tela vazia ao recarregar).
  mergeStatus(config.initialStatus, { notify: false, requestStartedAt: 0 });
  schedulePoll();
})();
