/* Transcritor da Gabi — frontend (JavaScript puro, sem dependências).
 *
 * O navegador conduz o processamento, um vídeo de cada vez:
 *   enviar → extrair áudio → transcrever em pedaços (várias requisições curtas)
 * O estado fica no servidor; se a página for fechada, basta clicar em
 * "Transcrever vídeos" de novo para continuar de onde parou.
 */
(() => {
  "use strict";

  const config = JSON.parse(document.getElementById("app-config").textContent);

  const COPY_RESET_MS = 2500;
  const PREVIEW_TIMEOUT_MS = 8000;
  const MAX_ATTEMPTS = 4;
  const RETRY_DELAY_MS = 2000;

  const ICONS = {
    check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    error: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7 7 17"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    circle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7.5"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M9 7v10M15 7v10"/></svg>',
    info: '<svg viewBox="0 0 24 24"><path d="M12 11v6M12 7.5h.01"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="M12 7.5v6M12 17h.01"/></svg>',
    film: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9.5 4.5 2.5-4.5 2.5z"/></svg>',
    spinner: '<span class="spinner"></span>',
  };

  const STATUS = {
    LOCAL: { label: "Pronto para transcrição", tone: "idle", icon: "circle" },
    QUEUED: { label: "Aguardando processamento", tone: "waiting", icon: "clock" },
    PAUSED: { label: "Pausado", tone: "waiting", icon: "pause" },
    UPLOADING: { label: "Enviando vídeo...", tone: "active", icon: "spinner" },
    UPLOAD_ERROR: { label: "Falha no envio", tone: "error", icon: "error" },
    PENDING: { label: "Aguardando processamento", tone: "waiting", icon: "clock" },
    EXTRACTING_AUDIO: { label: "Extraindo áudio...", tone: "active", icon: "spinner" },
    TRANSCRIBING: { label: "Transcrevendo...", tone: "active", icon: "spinner" },
    COMPLETED: { label: "Transcrição concluída", tone: "success", icon: "check" },
    ERROR: { label: "Erro no processamento", tone: "error", icon: "error" },
  };
  // Estados em que ainda há trabalho a fazer (local ou interrompido no servidor).
  const WORKABLE = new Set(["LOCAL", "UPLOAD_ERROR", "PENDING", "EXTRACTING_AUDIO", "TRANSCRIBING"]);
  const SERVER_UNFINISHED = new Set(["PENDING", "EXTRACTING_AUDIO", "TRANSCRIBING"]);

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
    toasts: $("toasts"),
    dialog: $("confirm-dialog"),
    template: $("card-template"),
  };

  const state = {
    items: [],
    running: false,
    current: null,
    attempted: new Set(), // vídeos já tentados na execução atual
    clearing: false,
    generation: 0, // incrementa a cada limpeza; respostas antigas são descartadas
  };

  class VideoRemovedError extends Error {}

  // ---------------------------------------------------------------- utilidades

  const plural = (n, singular, pluralForm) => `${n} ${n === 1 ? singular : pluralForm}`;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const extensionOf = (name) => {
    const dot = name.lastIndexOf(".");
    return dot >= 0 ? name.slice(dot).toLowerCase() : "";
  };
  const csrfToken = () => document.querySelector('meta[name="csrf-token"]').content;
  const urlFor = (name, id) => config.urls[name].replace("/0/", `/${id}/`);

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
      file: null,
      serverId: null,
      name: "",
      size: 0,
      status: "LOCAL",
      progress: null,
      uploadedBytes: 0,
      error: null,
      thumbnailUrl: null,
      audioUrl: null,
      previewUrl: null,
      transcription: "",
      xhr: null,
      el: null,
      copyTimer: null,
      ...props,
    };
    state.items.push(item);
    return item;
  }

  function removeItem(item) {
    if (item.xhr) item.xhr.abort();
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
    item.transcription = video.transcription || "";
    if (video.thumbnail_url) item.thumbnailUrl = video.thumbnail_url;
    item.audioUrl = video.audio_url || null;
    item.file = null; // já está no servidor; libera a referência ao arquivo local
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
      queuePreview(createItem({ file, name: file.name, size: file.size }));
      added++;
    }

    render();
    if (added) {
      const hint = state.running ? "Eles entram na fila automaticamente." : "Clique em “Transcrever vídeos” para começar.";
      toast(`${plural(added, "vídeo adicionado", "vídeos adicionados")}. ${hint}`, "success");
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

  // ---------------------------------------------------------- comunicação

  function uploadFile(item) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", item.file, item.file.name);

      const xhr = new XMLHttpRequest();
      item.xhr = xhr;
      xhr.open("POST", config.urls.upload);
      xhr.setRequestHeader("X-CSRFToken", csrfToken());
      xhr.setRequestHeader("Accept", "application/json");

      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        item.progress = Math.min(100, Math.round((event.loaded / event.total) * 100));
        item.uploadedBytes = Math.min(item.size, event.loaded);
        renderItem(item);
        renderSummary();
      });
      xhr.addEventListener("load", () => {
        item.xhr = null;
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          /* resposta não-JSON (ex.: página de erro do servidor) */
        }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.video) resolve(data.video);
        else reject(new Error((data && data.error) || uploadErrorMessage(xhr.status)));
      });
      const interrupted = () => {
        item.xhr = null;
        reject(new Error("O envio foi interrompido. Verifique sua conexão e tente novamente."));
      };
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

  /** Pede uma etapa ao servidor, com novas tentativas em falhas temporárias. */
  async function postStep(url) {
    const resumeHint = "Clique em “Transcrever vídeos” para continuar de onde parou.";
    for (let attempt = 1; ; attempt++) {
      let response;
      let data;
      try {
        response = await fetch(url, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken(), Accept: "application/json" },
        });
        data = await readJson(response);
      } catch {
        if (attempt < MAX_ATTEMPTS) {
          await sleep(RETRY_DELAY_MS * attempt);
          continue;
        }
        throw new Error(`Sem conexão com o servidor. ${resumeHint}`);
      }
      if ((response.ok || response.status === 409) && data && data.video) return data.video;
      if (response.status === 404) throw new VideoRemovedError();
      if ([500, 502, 503, 504].includes(response.status) && attempt < MAX_ATTEMPTS) {
        await sleep(RETRY_DELAY_MS * attempt);
        continue;
      }
      if (response.status === 403) throw new Error("Sua sessão expirou. Recarregue a página para continuar.");
      throw new Error(`${(data && data.error) || "O servidor não respondeu como esperado."} ${resumeHint}`);
    }
  }

  // ------------------------------------------------------------ processamento

  function nextWorkItem() {
    return state.items.find((i) => WORKABLE.has(i.status) && !state.attempted.has(i));
  }

  async function processItem(item, generation) {
    if (!item.serverId) {
      Object.assign(item, { status: "UPLOADING", progress: 0, uploadedBytes: 0, error: null });
      render();
      try {
        applyServerData(item, await uploadFile(item));
      } catch (err) {
        if (generation !== state.generation) return;
        Object.assign(item, { status: "UPLOAD_ERROR", progress: null, error: friendlyMessage(err, uploadErrorMessage(0)) });
        return;
      }
    }

    if (item.status === "PENDING" || item.status === "EXTRACTING_AUDIO") {
      item.status = "EXTRACTING_AUDIO";
      render();
      applyServerData(item, await postStep(urlFor("extract", item.serverId)));
    }

    while (item.status === "TRANSCRIBING" && generation === state.generation) {
      render();
      applyServerData(item, await postStep(urlFor("transcribe", item.serverId)));
    }
  }

  async function runQueue() {
    if (state.running || !nextWorkItem()) return;
    const generation = state.generation;
    state.running = true;
    state.attempted = new Set();
    render();

    let processed = 0;
    let stopped = false;
    while (generation === state.generation) {
      const item = nextWorkItem();
      if (!item) break;
      state.attempted.add(item);
      state.current = item;
      render();
      try {
        await processItem(item, generation);
      } catch (err) {
        if (generation !== state.generation) break;
        if (err instanceof VideoRemovedError) {
          removeItem(item);
          continue;
        }
        stopped = true;
        toast(friendlyMessage(err, "O processamento foi interrompido."), "error");
        break;
      }
      if (generation !== state.generation) break;
      processed++;
      if (item.status === "COMPLETED") toast(`Transcrição concluída: ${item.name}`, "success");
      if (item.status === "ERROR") toast(`Não foi possível processar “${item.name}”.`, "error");
      if (item.status === "UPLOAD_ERROR") toast(`Não foi possível enviar “${item.name}”.`, "error");
    }

    const sameGeneration = generation === state.generation;
    state.running = false;
    state.current = null;
    render();
    if (sameGeneration && !stopped && processed > 0) announceFinished();
  }

  function announceFinished() {
    const errors = state.items.filter((i) => i.status === "ERROR" || i.status === "UPLOAD_ERROR").length;
    const done = state.items.filter((i) => i.status === "COMPLETED").length;
    if (!errors) toast("Todos os vídeos foram transcritos", "success");
    else toast(`Processamento finalizado: ${done} concluído(s), ${errors} com erro.`, "error");
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
    if (!state.items.length || state.clearing) return;
    if (!(await confirmClear())) return;

    state.clearing = true;
    state.generation++; // interrompe o processamento em andamento
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
      previewQueue.length = 0;
      for (const item of [...state.items]) removeItem(item);
      toast("Todos os vídeos foram removidos", "success");
    } catch (err) {
      toast(friendlyMessage(err, "Sem conexão com o servidor. Tente novamente."), "error");
    } finally {
      state.clearing = false;
      render();
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
    if (!item.transcription) return;
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
      downloadLink: q(".btn--download"),
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

  /** Status exibido: considera se o vídeo está na fila, em andamento ou pausado. */
  function displayStatus(item) {
    if (item === state.current || !WORKABLE.has(item.status)) return item.status;
    if (state.running && !state.attempted.has(item)) return "QUEUED";
    if (SERVER_UNFINISHED.has(item.status)) return "PAUSED";
    return item.status;
  }

  function queuePosition(item) {
    const queue = state.items.filter((i) => i === state.current || (WORKABLE.has(i.status) && !state.attempted.has(i)));
    return Math.max(0, queue.indexOf(item));
  }

  function detailFor(item, status) {
    switch (status) {
      case "LOCAL":
        return `${formatBytes(item.size)} · clique em “Transcrever vídeos” para começar`;
      case "QUEUED": {
        const ahead = queuePosition(item);
        return ahead === 1 ? "Próximo da fila." : `Na fila: ${plural(ahead, "vídeo", "vídeos")} antes deste.`;
      }
      case "PAUSED":
        return item.progress > 0
          ? `Interrompido em ${item.progress}%. Clique em “Transcrever vídeos” para continuar.`
          : "Interrompido. Clique em “Transcrever vídeos” para continuar.";
      case "UPLOADING":
        return item.progress >= 100
          ? "Finalizando o envio e gerando a miniatura..."
          : `${formatBytes(item.uploadedBytes)} de ${formatBytes(item.size)}`;
      case "UPLOAD_ERROR":
        return "Clique em “Transcrever vídeos” para tentar novamente.";
      case "EXTRACTING_AUDIO":
        return "Separando o áudio do vídeo. Em seguida vem a transcrição.";
      case "TRANSCRIBING":
        return "Convertendo a fala em texto. O texto vai aparecendo abaixo.";
      default:
        return "";
    }
  }

  function renderItem(item, position) {
    if (!item.el) item.el = buildCard(item);
    const el = item.el;
    const status = displayStatus(item);
    const meta = STATUS[status] || STATUS.ERROR;

    el.root.classList.toggle("is-active", item === state.current);
    el.root.classList.toggle("is-success", status === "COMPLETED");
    el.root.classList.toggle("is-error", status === "ERROR" || status === "UPLOAD_ERROR");
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
    // Percentual só quando há métrica real: bytes enviados ou áudio já transcrito.
    const determinate = (status === "UPLOADING" || status === "TRANSCRIBING") && item.progress !== null;
    setText(el.statusPercent, determinate ? `${item.progress}%` : "");
    setText(el.detail, detailFor(item, status));

    const indeterminate = status === "EXTRACTING_AUDIO";
    el.progress.hidden = !(determinate || indeterminate);
    el.progress.classList.toggle("is-indeterminate", indeterminate);
    el.progressFill.style.width = determinate ? `${item.progress}%` : "";

    // Erro
    const hasError = status === "ERROR" || status === "UPLOAD_ERROR";
    el.error.hidden = !hasError;
    if (hasError) setText(el.error, item.error || "Não foi possível processar o vídeo.");

    // Transcrição: aparece enquanto é gerada; o botão de copiar só no final.
    const completed = status === "COMPLETED";
    const showText = completed || (item.transcription && SERVER_UNFINISHED.has(item.status));
    el.transcript.hidden = !showText;
    if (showText) {
      const empty = !item.transcription;
      const text = empty ? "Nenhuma fala foi reconhecida neste vídeo." : item.transcription;
      if (el.transcriptText.textContent !== text) {
        const atBottom = el.transcriptText.scrollHeight - el.transcriptText.scrollTop - el.transcriptText.clientHeight < 8;
        el.transcriptText.textContent = text;
        if (!completed && atBottom) el.transcriptText.scrollTop = el.transcriptText.scrollHeight;
      }
      el.transcriptText.classList.toggle("is-empty", empty);
      el.copyButton.hidden = !completed || empty;
    }

    // Áudio para baixar (ex.: para transcrever em outro serviço), assim que foi extraído.
    el.downloadLink.hidden = !item.audioUrl;
    if (item.audioUrl && el.downloadLink.getAttribute("href") !== item.audioUrl) {
      el.downloadLink.href = item.audioUrl;
    }
  }

  function renderSummary() {
    const items = state.items;
    if (!items.length) {
      els.summary.hidden = true;
      return;
    }

    const workable = items.filter((i) => WORKABLE.has(i.status));
    const paused = workable.filter((i) => SERVER_UNFINISHED.has(i.status)).length;
    const done = items.filter((i) => i.status === "COMPLETED").length;
    const errors = items.filter((i) => i.status === "ERROR" || i.status === "UPLOAD_ERROR").length;

    let tone = "active";
    let icon = ICONS.spinner;
    let title;
    let detail;

    if (state.running && state.current) {
      const current = state.current;
      title = `Processando vídeo ${items.indexOf(current) + 1} de ${items.length}`;
      const name = `“${current.name}”`;
      const stage = {
        UPLOADING: `Enviando ${name} (${current.progress || 0}%).`,
        EXTRACTING_AUDIO: `Extraindo o áudio de ${name}.`,
        TRANSCRIBING: `Transcrevendo ${name} (${current.progress || 0}%).`,
      }[current.status] || `Processando ${name}.`;
      detail = `${stage} Mantenha esta página aberta até terminar.`;
    } else if (workable.length) {
      tone = "idle";
      icon = paused ? ICONS.pause : ICONS.film;
      title = `${plural(workable.length, "vídeo aguardando", "vídeos aguardando")} transcrição`;
      detail = paused
        ? "Clique em “Transcrever vídeos”: os interrompidos continuam de onde pararam."
        : "Clique em “Transcrever vídeos” para começar.";
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
    const hasWork = state.items.some((i) => WORKABLE.has(i.status));
    els.transcribeButton.disabled = state.running || state.clearing || !hasWork;
    els.transcribeButton.classList.toggle("is-busy", state.running);
    setText(els.transcribeLabel, state.running ? "Transcrevendo..." : "Transcrever vídeos");
    els.clearButton.disabled = !state.items.length || state.clearing;
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
  els.transcribeButton.addEventListener("click", runQueue);
  els.clearButton.addEventListener("click", clearAll);
  window.addEventListener("beforeunload", (event) => {
    if (state.running) event.preventDefault();
  });

  // Estado inicial vindo do servidor.
  for (const video of config.videos) applyServerData(createItem({}), video);
  render();
})();
