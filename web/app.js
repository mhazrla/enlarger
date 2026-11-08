(() => {
  "use strict";

  const MAX_BYTES = 2 * 1024 * 1024 * 1024;
  const MAX_FILES = 50;
  const IMAGE_EXT = ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"];
  const VIDEO_EXT = ["mp4", "mkv", "mov", "avi", "webm"];

  const el = (id) => document.getElementById(id);

  const dom = {
    workspace: document.querySelector(".workspace"),
    tabs: [el("tabImage"), el("tabVideo")],
    panels: { image: el("panelImage"), video: el("panelVideo") },
    drops: { image: el("dropImage"), video: el("dropVideo") },
    lists: { image: el("listImage"), video: el("listVideo") },
    input: el("fileInput"),
    run: el("runButton"),
    runNote: el("runNote"),
    ffmpeg: el("ffmpegState"),
    state: {
      empty: el("stateEmpty"),
      working: el("stateWorking"),
      done: el("stateDone"),
      error: el("stateError"),
    },
    progress: el("progressBar"),
    progressFill: el("progressFill"),
    workingTitle: el("workingTitle"),
    workingDetail: el("workingDetail"),
    workList: el("workList"),
    compareWrap: el("compareWrap"),
    compare: el("compare"),
    compareBefore: el("compareBefore"),
    compareAfter: el("compareAfter"),
    compareRange: el("compareRange"),
    videoSwap: el("videoSwap"),
    resultVideo: el("resultVideo"),
    showBefore: el("showBefore"),
    showAfter: el("showAfter"),
    resultMeta: el("resultMeta"),
    resultList: el("resultList"),
    metaBefore: el("metaBefore"),
    metaAfter: el("metaAfter"),
    metaFrames: el("metaFrames"),
    download: el("downloadLink"),
    downloadAll: el("downloadAll"),
    resultName: el("resultName"),
    errorMessage: el("errorMessage"),
    liveRegion: el("liveRegion"),
    retry: el("retryButton"),
    reset: el("resetResult"),
  };

  const state = {
    mode: "image",
    ffmpeg: null,
    files: { image: [], video: [] },
    size: {
      image: { method: "scale", scale: "2", custom: 2, target: "4k" },
      video: { method: "scale", scale: "2", custom: 2, target: "4k" },
    },
    beforeUrl: null,
    batchId: null,
    poll: null,
    result: null,
    busy: false,
  };

  /* ---------- helpers ---------- */

  const extOf = (name) => {
    const dot = name.lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  };

  const bytes = (n) => {
    if (!Number.isFinite(n)) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  const dims = (pair) =>
    Array.isArray(pair) && pair.length === 2 ? `${pair[0]} × ${pair[1]}` : "Not reported";

  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

  const note = (message, tone) => {
    dom.runNote.textContent = message;
    dom.runNote.style.color = tone === "error" ? "var(--accent)" : "";
  };

  const announce = (message) => {
    if (dom.liveRegion) dom.liveRegion.textContent = message;
  };

  const setBatchLayout = (multi) => {
    dom.workspace.classList.toggle("is-batch", Boolean(multi));
  };

  const clearPoll = () => {
    if (state.poll) {
      clearTimeout(state.poll);
      state.poll = null;
    }
  };

  const showState = (name) => {
    Object.entries(dom.state).forEach(([key, node]) => {
      node.hidden = key !== name;
    });
    dom.reset.hidden = name === "empty" || name === "working";
  };

  const setBusy = (busy) => {
    state.busy = busy;
    Object.values(dom.drops).forEach((drop) => drop.classList.toggle("is-busy", busy));
    Object.values(dom.lists).forEach((list) => list.classList.toggle("is-busy", busy));
    if (busy) {
      dom.run.disabled = true;
      dom.run.textContent = "Upscaling…";
    } else {
      syncRun();
    }
  };

  /* ---------- modes ---------- */

  const setMode = (mode) => {
    if (mode === "video" && state.ffmpeg === false) return;
    state.mode = mode;
    dom.tabs.forEach((tab) => {
      const active = tab.id === `tab${mode[0].toUpperCase()}${mode.slice(1)}`;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    Object.entries(dom.panels).forEach(([key, panel]) => {
      panel.hidden = key !== mode;
    });
    dom.input.accept =
      mode === "image"
        ? IMAGE_EXT.map((e) => `.${e}`).join(",")
        : VIDEO_EXT.map((e) => `.${e}`).join(",");
    syncRun();
  };

  dom.tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => setMode(index === 0 ? "image" : "video"));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = dom.tabs;
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (tabs[next].disabled) return;
      setMode(next === 0 ? "image" : "video");
      tabs[next].focus();
    });
  });

  /* ---------- file list ---------- */

  const renderFileList = (mode) => {
    const list = dom.lists[mode];
    const entries = state.files[mode];
    list.hidden = entries.length === 0;
    list.replaceChildren();

    entries.forEach((entry, index) => {
      const item = document.createElement("li");
      item.className = "file-card";

      const thumb = document.createElement("div");
      thumb.className = "file-thumb";
      if (mode === "image") {
        const image = document.createElement("img");
        image.src = entry.url;
        image.alt = "";
        image.loading = "lazy";
        thumb.append(image);
      } else {
        const label = document.createElement("span");
        label.className = "thumb-fallback";
        label.textContent = extOf(entry.file.name).toUpperCase() || "VID";
        thumb.append(label);
      }

      const meta = document.createElement("div");
      meta.className = "file-meta";
      const name = document.createElement("p");
      name.className = "file-name";
      name.textContent = entry.file.name;
      const detail = document.createElement("p");
      detail.className = "file-detail";
      detail.textContent = entry.dims
        ? `${bytes(entry.file.size)} · ${dims(entry.dims)}`
        : bytes(entry.file.size);
      meta.append(name, detail);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "text-button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${entry.file.name}`);
      remove.addEventListener("click", () => removeFile(mode, index));

      item.append(thumb, meta, remove);
      list.append(item);
    });
  };

  const probeDims = (mode, entry) => {
    if (mode === "image") {
      const probe = new Image();
      probe.onload = () => {
        entry.dims = [probe.naturalWidth, probe.naturalHeight];
        renderFileList(mode);
      };
      probe.src = entry.url;
    } else {
      const probe = document.createElement("video");
      probe.preload = "metadata";
      probe.onloadedmetadata = () => {
        entry.dims = [probe.videoWidth, probe.videoHeight];
        renderFileList(mode);
      };
      probe.src = entry.url;
    }
  };

  const addFiles = (list) => {
    const mode = state.mode;
    const allowed = mode === "image" ? IMAGE_EXT : VIDEO_EXT;
    const label = mode === "image" ? "image" : "video";
    const entries = state.files[mode];
    let count = entries.length;
    let added = 0;
    let rejected = 0;
    let duplicates = 0;

    for (const file of list) {
      if (count >= MAX_FILES) {
        note(`A batch holds ${MAX_FILES} files. Run this set first, then start another.`, "error");
        break;
      }
      if (!allowed.includes(extOf(file.name)) || file.size > MAX_BYTES) {
        rejected += 1;
        continue;
      }
      if (entries.some((entry) => entry.file.name === file.name && entry.file.size === file.size)) {
        duplicates += 1;
        continue;
      }
      const entry = { file, url: URL.createObjectURL(file), dims: null };
      entries.push(entry);
      probeDims(mode, entry);
      count += 1;
      added += 1;
    }

    renderFileList(mode);

    if (rejected > 0) {
      note(
        `${plural(rejected, "file")} skipped. Use ${mode === "image" ? "PNG, JPG, WebP, BMP or TIFF" : "MP4, MKV, MOV, AVI or WebM"} under 2 GB.`,
        "error"
      );
    } else if (added === 0 && duplicates > 0) {
      note("Those files are already in the batch.", "error");
    } else if (added > 0) {
      note(entries.length === 1 ? "Ready when you are." : `${plural(entries.length, label)} ready to run.`);
    }

    syncRun();
  };

  const removeFile = (mode, index) => {
    if (state.busy) return;
    const [entry] = state.files[mode].splice(index, 1);
    if (entry) URL.revokeObjectURL(entry.url);
    renderFileList(mode);
    syncRun();
  };

  dom.input.addEventListener("change", () => {
    const files = Array.from(dom.input.files || []);
    dom.input.value = "";
    if (files.length) addFiles(files);
  });

  Object.entries(dom.drops).forEach(([mode, drop]) => {
    const open = () => {
      if (state.busy) return;
      if (mode !== state.mode) setMode(mode);
      dom.input.click();
    };
    drop.addEventListener("click", open);
    drop.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    drop.addEventListener("dragenter", (event) => {
      event.preventDefault();
      drop.classList.add("is-over");
    });
    drop.addEventListener("dragover", (event) => {
      event.preventDefault();
      drop.classList.add("is-over");
    });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
    drop.addEventListener("drop", (event) => {
      event.preventDefault();
      drop.classList.remove("is-over");
      if (state.busy) return;
      if (mode !== state.mode) setMode(mode);
      const files = Array.from(event.dataTransfer?.files || []);
      if (files.length) addFiles(files);
    });
  });

  /* ---------- size controls ---------- */

  document.querySelectorAll(".choice[data-size-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = button.closest(".mode-panel");
      const mode = panel.id === "panelImage" ? "image" : "video";
      setSizeMethod(mode, button.dataset.sizeMode);
    });
  });

  const setSizeMethod = (mode, method) => {
    state.size[mode].method = method;
    const root = dom.panels[mode];
    root.querySelectorAll("[data-size-mode]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.sizeMode === method));
    });
    el(mode === "image" ? "imageScaleBody" : "videoScaleBody").hidden = method !== "scale";
    el(mode === "image" ? "imageTargetBody" : "videoTargetBody").hidden = method !== "target";

    if (method === "target") {
      const current = state.size[mode].target;
      const active = root.querySelector(`[data-target="${current}"]`);
      if (!active) state.size[mode].target = "4k";
      root.querySelectorAll("[data-target]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.target === state.size[mode].target));
      });
    }
  };

  document.querySelectorAll("[data-scale]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = button.closest(".mode-panel");
      const mode = panel.id === "panelImage" ? "image" : "video";
      state.size[mode].scale = button.dataset.scale;
      panel.querySelectorAll("[data-scale]").forEach((other) => {
        other.setAttribute("aria-pressed", String(other === button));
      });
      const customWrap = el(mode === "image" ? "imageCustomWrap" : "videoCustomWrap");
      customWrap.hidden = button.dataset.scale !== "custom";
      if (button.dataset.scale === "custom") {
        const input = el(mode === "image" ? "imageCustomScale" : "videoCustomScale");
        state.size[mode].custom = Number(input.value) || 2;
        input.focus();
      }
    });
  });

  ["imageCustomScale", "videoCustomScale"].forEach((id) => {
    const input = el(id);
    input.addEventListener("input", () => {
      const mode = id.startsWith("image") ? "image" : "video";
      const value = Number(input.value);
      state.size[mode].custom = Number.isFinite(value) && value >= 1 ? value : 1;
    });
  });

  document.querySelectorAll("[data-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = button.closest(".mode-panel");
      const mode = panel.id === "panelImage" ? "image" : "video";
      state.size[mode].target = button.dataset.target;
      setSizeMethod(mode, "target");
      syncRun();
    });
  });

  /* ---------- options readouts ---------- */

  const bindOutput = (inputId, outputId, digits) => {
    const input = el(inputId);
    const output = el(outputId);
    const update = () => {
      output.textContent = digits ? Number(input.value).toFixed(digits) : input.value;
    };
    input.addEventListener("input", update);
    update();
  };

  bindOutput("imageSharpen", "imageSharpenOut", 1);
  bindOutput("imageQuality", "imageQualityOut", 0);
  bindOutput("videoSharpen", "videoSharpenOut", 1);
  bindOutput("videoCrf", "videoCrfOut", 0);

  const qualityWrap = el("imageQualityWrap");
  el("imageFormat").addEventListener("change", () => {
    const format = el("imageFormat").value;
    qualityWrap.hidden = !["jpg", "webp"].includes(format);
  });

  /* ---------- run ---------- */

  const resolveScale = (mode) => {
    const size = state.size[mode];
    if (size.scale === "custom") {
      return String(Math.min(1000, Math.max(1, Number(size.custom) || 1)));
    }
    return size.scale;
  };

  const syncRun = () => {
    const count = state.files[state.mode].length;
    const ready = count > 0 && !state.busy && state.ffmpeg !== false;
    dom.run.disabled = !ready;
    if (count < 2) {
      dom.run.textContent = state.mode === "image" ? "Upscale image" : "Upscale video";
    } else {
      dom.run.textContent = `Upscale ${plural(count, state.mode === "image" ? "image" : "video")}`;
    }

    if (state.ffmpeg === false) {
      note("The backend is not reachable, so nothing can run yet.", "error");
    } else if (count === 0) {
      note("Choose a file to enable the upscale.");
    } else if (!state.busy) {
      note(
        count === 1
          ? "Ready when you are."
          : `${plural(count, "file")} queued. They run one after another.`
      );
    }
  };

  const payload = (mode) => {
    const form = new FormData();
    form.append("kind", mode);
    state.files[mode].forEach((entry) => form.append("files", entry.file));
    const size = state.size[mode];
    if (size.method === "target") {
      form.append("target", size.target);
    } else {
      form.append("target", "");
      form.append("scale", resolveScale(mode));
    }

    if (mode === "image") {
      form.append("sharpen", el("imageSharpen").value);
      form.append("format", el("imageFormat").value);
      form.append("quality", el("imageQuality").value);
    } else {
      form.append("sharpen", el("videoSharpen").value);
      form.append("crf", el("videoCrf").value);
      form.append("preset", el("videoPreset").value);
      form.append("keep_audio", el("videoAudio").checked ? "true" : "false");
    }
    return form;
  };

  const statusText = (item) => {
    if (item.status === "done") return "Done";
    if (item.status === "error") return "Failed";
    if (item.status === "processing") {
      return typeof item.progress === "number" ? `${Math.round(item.progress * 100)}%` : "Working";
    }
    return "Queued";
  };

  const renderWorkList = (items) => {
    dom.workList.hidden = items.length < 2;
    if (items.length < 2) {
      dom.workList.replaceChildren();
      return;
    }
    const rows = items.map((item) => {
      const row = document.createElement("li");
      row.className = "work-item";
      row.dataset.status = item.status;
      const name = document.createElement("span");
      name.className = "work-name";
      name.textContent = item.original_name;
      const progress = document.createElement("span");
      progress.className = "work-status";
      progress.textContent = statusText(item);
      row.append(name, progress);
      return row;
    });
    dom.workList.replaceChildren(...rows);
  };

  const renderProgress = (data) => {
    const items = data.items || [];
    const percent = Math.round((typeof data.progress === "number" ? data.progress : 0) * 100);
    dom.progress.dataset.indeterminate = "false";
    dom.progress.setAttribute("aria-valuenow", String(percent));
    dom.progressFill.style.width = `${percent}%`;

    const finished = data.completed + data.failed;
    const current = items.find((item) => item.status === "processing") || null;

    if (items.length === 1) {
      const frames = current && current.frames ? ` · ${current.frames} frames` : "";
      dom.workingDetail.textContent = `${percent}%${frames}`;
    } else {
      dom.workingDetail.textContent = current
        ? `${finished} of ${items.length} finished · ${current.original_name}`
        : `${finished} of ${items.length} finished`;
    }
    renderWorkList(items);
  };

  const poll = async (batchId) => {
    if (state.batchId !== batchId) return;
    let data;
    try {
      const response = await fetch(`/api/batch/${batchId}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`The server answered ${response.status}.`);
      data = await response.json();
    } catch (error) {
      finishError(`Lost contact with the upscaler. ${error.message}`);
      return;
    }
    if (state.batchId !== batchId) return;

    if (data.status === "done") {
      finishDone(data);
      return;
    }
    renderProgress(data);
    state.poll = window.setTimeout(() => poll(batchId), 700);
  };

  const startRun = async () => {
    const mode = state.mode;
    const entries = state.files[mode];
    if (!entries.length || state.busy) return;

    clearPoll();
    if (state.beforeUrl) URL.revokeObjectURL(state.beforeUrl);
    state.beforeUrl = URL.createObjectURL(entries[0].file);
    state.result = null;

    setBusy(true);
    showState("working");
    setBatchLayout(entries.length > 1);
    dom.workingTitle.textContent =
      entries.length === 1
        ? `Upscaling ${entries[0].file.name}`
        : `Upscaling ${plural(entries.length, "file")}`;
    dom.workingDetail.textContent =
      entries.length === 1
        ? "Sending the file to the local worker."
        : "Sending the files to the local worker, one after another.";
    dom.workList.replaceChildren();
    dom.progress.dataset.indeterminate = "true";
    dom.progress.removeAttribute("aria-valuenow");
    dom.progressFill.style.width = "";
    announce(`Upscaling ${plural(entries.length, "file")}.`);

    try {
      const response = await fetch("/api/batch", { method: "POST", body: payload(mode) });
      let body = {};
      try {
        body = await response.json();
      } catch {
        body = {};
      }
      if (!response.ok) {
        finishError(body.error || `The upload was rejected (${response.status}).`);
        return;
      }
      state.batchId = body.id;
      renderProgress(body);
      poll(body.id);
    } catch (error) {
      finishError(
        `The upload did not reach the server. ${error.message}. Check that python web.py is still running.`
      );
    }
  };

  const ensureCompareImages = () => {
    if (dom.compareBefore && dom.compareAfter) return;
    dom.compareBefore = document.createElement("img");
    dom.compareBefore.className = "compare-img";
    dom.compareBefore.alt = "The original file before upscaling";
    dom.compareAfter = document.createElement("img");
    dom.compareAfter.className = "compare-img compare-after";
    dom.compareAfter.alt = "The upscaled result";
    dom.compare.append(dom.compareBefore, dom.compareAfter);
  };

  const renderResultList = (items) => {
    const rows = items.map((item) => {
      const row = document.createElement("li");
      row.className = "result-item";
      row.dataset.status = item.status;

      const meta = document.createElement("div");
      meta.className = "result-item-meta";
      const name = document.createElement("p");
      name.className = "file-name";
      name.textContent = item.output_name || item.original_name;
      const detail = document.createElement("p");
      detail.className = "file-detail";
      detail.textContent =
        item.status === "error"
          ? item.error || "This file did not finish."
          : [dims(item.after), item.frames ? `${item.frames} frames` : ""]
              .filter(Boolean)
              .join(" · ");
      meta.append(name, detail);

      let action;
      if (item.status === "done") {
        action = document.createElement("a");
        action.className = "text-button";
        action.href = item.download_url;
        action.setAttribute("download", item.output_name || "");
        action.textContent = "Download";
      } else {
        action = document.createElement("span");
        action.className = "work-status";
        action.dataset.status = "error";
        action.textContent = "Failed";
      }

      row.append(meta, action);
      return row;
    });
    dom.resultList.replaceChildren(...rows);
  };

  const wireSingle = (data, item) => {
    const isImage = data.kind === "image";
    dom.compareWrap.hidden = !isImage;
    dom.videoSwap.hidden = isImage;
    dom.resultMeta.hidden = false;

    dom.metaBefore.textContent = dims(item.before);
    dom.metaAfter.textContent = dims(item.after);
    dom.metaFrames.textContent =
      data.kind === "video" && item.frames ? String(item.frames) : "Not applicable";

    if (isImage) {
      ensureCompareImages();
      dom.compareBefore.src = state.beforeUrl || "";
      dom.compareAfter.src = item.preview_url || "";
      dom.compareRange.value = "50";
      dom.compare.style.setProperty("--pos", "50%");
    } else {
      dom.resultVideo.src = item.preview_url || "";
      setVideoSide("after");
    }

    dom.download.hidden = false;
    dom.download.href = item.download_url;
    dom.download.setAttribute("download", item.output_name || "");
    dom.downloadAll.hidden = true;
    dom.resultName.textContent = item.output_name || "";
  };

  const finishDone = (data) => {
    clearPoll();
    state.batchId = null;
    state.result = data;
    setBusy(false);

    const items = data.items || [];
    if (data.completed === 0) {
      finishError(items[0]?.error || "Every file in this batch failed.");
      return;
    }

    showState("done");
    const multi = items.length > 1;
    setBatchLayout(multi);
    dom.resultList.hidden = !multi;
    dom.resultMeta.hidden = multi;

    if (multi) {
      dom.compareWrap.hidden = true;
      dom.videoSwap.hidden = true;
      renderResultList(items);
      dom.download.hidden = true;
      dom.downloadAll.hidden = false;
      dom.downloadAll.href = data.zip_url;
      dom.downloadAll.setAttribute("download", data.zip_name || "");
      dom.resultName.textContent = data.failed
        ? `${data.completed} of ${data.total} files ready · ${data.failed} failed`
        : `${plural(data.completed, "file")} ready in one ZIP`;
    } else {
      wireSingle(data, items[0]);
    }

    announce(
      data.failed
        ? `Finished ${data.completed} of ${data.total} files. ${data.failed} failed.`
        : `Finished ${plural(data.completed, "file")}. The download is ready.`
    );
  };

  const finishError = (message) => {
    clearPoll();
    state.batchId = null;
    state.result = null;
    setBusy(false);
    showState("error");
    setBatchLayout(false);
    dom.errorMessage.textContent = message;
    announce(`The run did not finish. ${message}`);
    dom.progress.dataset.indeterminate = "false";
    dom.progressFill.style.width = "0";
  };

  const setVideoSide = (side) => {
    const before = side === "before";
    dom.showBefore.setAttribute("aria-pressed", String(before));
    dom.showAfter.setAttribute("aria-pressed", String(!before));
    const item = state.result?.items?.[0];
    const url = before ? state.beforeUrl : item?.preview_url;
    if (url) dom.resultVideo.src = url;
  };

  dom.showBefore.addEventListener("click", () => setVideoSide("before"));
  dom.showAfter.addEventListener("click", () => setVideoSide("after"));

  dom.compareRange.addEventListener("input", () => {
    dom.compare.style.setProperty("--pos", `${dom.compareRange.value}%`);
  });

  dom.run.addEventListener("click", startRun);
  dom.retry.addEventListener("click", startRun);

  dom.reset.addEventListener("click", () => {
    clearPoll();
    state.batchId = null;
    state.result = null;
    if (state.beforeUrl) {
      URL.revokeObjectURL(state.beforeUrl);
      state.beforeUrl = null;
    }
    ["image", "video"].forEach((mode) => {
      state.files[mode].forEach((entry) => URL.revokeObjectURL(entry.url));
      state.files[mode] = [];
      renderFileList(mode);
    });
    dom.resultVideo.removeAttribute("src");
    dom.resultVideo.load();
    if (dom.compareBefore) dom.compareBefore.removeAttribute("src");
    if (dom.compareAfter) dom.compareAfter.removeAttribute("src");
    dom.download.hidden = false;
    dom.downloadAll.hidden = true;
    dom.resultList.hidden = true;
    dom.resultList.replaceChildren();
    dom.resultMeta.hidden = false;
    dom.workList.hidden = true;
    dom.workList.replaceChildren();
    setBatchLayout(false);
    showState("empty");
    syncRun();
    announce("Cleared. Pick new files to start again.");
  });

  /* ---------- health ---------- */

  const health = async () => {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      state.ffmpeg = Boolean(body.ffmpeg);
      dom.ffmpeg.dataset.state = state.ffmpeg ? "ok" : "missing";
      dom.ffmpeg.textContent = state.ffmpeg
        ? "FFmpeg ready"
        : "FFmpeg not found. Video is unavailable";
      if (!state.ffmpeg) {
        dom.tabs[1].disabled = true;
        dom.tabs[1].setAttribute("aria-disabled", "true");
        dom.tabs[1].title = "Install FFmpeg and put it on PATH to upscale video.";
        if (state.mode === "video") setMode("image");
      }
    } catch {
      state.ffmpeg = false;
      dom.ffmpeg.dataset.state = "missing";
      dom.ffmpeg.textContent = "Backend not reachable";
      dom.tabs[1].disabled = true;
      dom.tabs[1].setAttribute("aria-disabled", "true");
      if (state.mode === "video") setMode("image");
    }
    syncRun();
  };

  setMode("image");
  showState("empty");
  health();
})();
