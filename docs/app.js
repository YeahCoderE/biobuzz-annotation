const state = {
  images: [],
  lines: [],
  currentName: null,
  activeFilter: "all",
  search: "",
  currentIndex: -1,
  selectedHive: 0,
  selectedLine: 0,
  placementMode: false,
  zoom: 1,
  undo: [],
  redo: [],
  dirty: false,
  drag: null,
  staticMode: !["localhost", "127.0.0.1"].includes(location.hostname)
    || new URLSearchParams(location.search).get("static") === "1",
  uploadedUrls: new Map(),
  uploadDb: null,
};

const $ = (id) => document.getElementById(id);
const elements = {
  list: $("image-list"),
  image: $("source-image"),
  svg: $("annotation-svg"),
  wrap: $("image-wrap"),
  empty: $("empty-state"),
  canvas: $("canvas-scroll"),
  frameLines: $("frame-lines"),
  ironLines: $("iron-lines"),
  tabs: $("hive-tabs"),
  meta: $("metadata-card"),
  toast: $("toast"),
  guide: $("guide-dialog"),
};

const MODEL_LINE_ENDPOINTS = [
  [[66, 54], [66, 157]],
  [[123, 28], [123, 131]],
  [[193, 54], [193, 157]],
  [[250, 28], [250, 131]],
  [[66, 157], [193, 157]],
  [[123, 131], [250, 131]],
  [[66, 54], [193, 54]],
  [[123, 28], [250, 28]],
  [[66, 54], [123, 28]],
  [[193, 54], [250, 28]],
];

function currentImage() {
  return state.images.find((image) => image.name === state.currentName) || null;
}

function freshHive(metadata = [0, 1, 1]) {
  return { metadata: [...metadata], points: Array.from({ length: 28 }, () => null) };
}

function pointCount(hive) {
  return hive.points.filter((point) => point !== null).length;
}

function completeLines(hive) {
  return Array.from({ length: 14 }, (_, index) =>
    hive.points[index * 2] && hive.points[index * 2 + 1],
  ).filter(Boolean).length;
}

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => elements.toast.classList.remove("show"), 2800);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const result = type.includes("application/json") ? await response.json() : response;
  if (!response.ok) throw new Error(result.error || `שגיאת שרת (${response.status})`);
  return result;
}

async function boot() {
  try {
    const data = state.staticMode ? await loadStaticState() : await api("/api/state");
    state.images = data.images;
    state.lines = data.lines;
    if (state.staticMode) {
      state.uploadDb = await openUploadDatabase();
      await restoreUploadedFiles();
      mergeBrowserDrafts();
    }
    renderImageList();
    updateImageCount();
    if (state.images.length) {
      const firstPending = state.images.find((image) => image.status === "todo" || image.status === "invalid") || state.images[0];
      await selectImage(firstPending.name, true);
    }
    updateSummary();
  } catch (error) {
    showToast(`לא הצלחתי לטעון את התמונות: ${error.message}`, true);
  }
}

function updateImageCount() {
  $("image-count").textContent = `${state.images.length} תמונות`;
}

async function loadStaticState() {
  const [manifestResponse, trustedResponse, draftResponse] = await Promise.all([
    fetch("./images.json", { cache: "no-store" }),
    fetch("./data/annotations.json", { cache: "no-store" }),
    fetch("./data/annotations_review.json", { cache: "no-store" }),
  ]);
  if (!manifestResponse.ok) throw new Error("לא ניתן לטעון את רשימת התמונות מ-GitHub.");
  const manifest = await manifestResponse.json();
  const readOptional = async (response) => response.ok ? response.json() : [];
  const trusted = await readOptional(trustedResponse);
  const drafts = await readOptional(draftResponse);
  state.remoteDrafts = drafts;
  const recordMap = new Map([...trusted, ...drafts].map((record) => [String(record.image_path || "").split(/[\\/]/).pop(), record]));
  return {
    images: manifest.images.map((item) => ({
      ...item,
      src: `./images/${encodeURIComponent(item.name)}`,
      ...staticAnnotation(recordMap.get(item.name), item.width, item.height, drafts.includes(recordMap.get(item.name))),
    })),
    lines: manifest.lines,
    total: manifest.images.length,
    drafts: drafts.length,
    trusted: trusted.length,
  };
}

function openUploadDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("biobuzz-local-files", 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("uploads")) {
        request.result.createObjectStore("uploads", { keyPath: "name" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("לא ניתן לפתוח אחסון מקומי לתמונות."));
  });
}

function uploadTransaction(mode, callback) {
  return new Promise((resolve, reject) => {
    const transaction = state.uploadDb.transaction("uploads", mode);
    const store = transaction.objectStore("uploads");
    const request = callback(store);
    transaction.oncomplete = () => resolve(request?.result);
    transaction.onerror = () => reject(transaction.error || new Error("שגיאת אחסון מקומית."));
    transaction.onabort = () => reject(transaction.error || new Error("שמירה מקומית בוטלה."));
  });
}

async function restoreUploadedFiles() {
  const request = await uploadTransaction("readonly", (store) => store.getAll());
  for (const saved of request || []) {
    const src = URL.createObjectURL(saved.file);
    state.uploadedUrls.set(saved.name, src);
    const restored = {
      name: saved.name,
      width: saved.width,
      height: saved.height,
      status: "todo",
      hives: [],
      src,
      localUpload: true,
    };
    const existingIndex = state.images.findIndex((image) => image.name === saved.name);
    if (existingIndex >= 0) state.images[existingIndex] = restored;
    else state.images.push(restored);
  }
  state.images.sort((a, b) => a.name.localeCompare(b.name));
}

function mergeBrowserDrafts() {
  const drafts = JSON.parse(localStorage.getItem(draftStorageKey()) || "{}");
  for (const record of state.remoteDrafts || []) {
    const name = String(record.image_path || "").split(/[\\/]/).pop();
    if (name && !drafts[name]) drafts[name] = record;
  }
  localStorage.setItem(draftStorageKey(), JSON.stringify(drafts));
  for (const image of state.images) {
    const record = drafts[image.name];
    if (record) {
      image.status = "review";
      image.hives = stateFromRecord(record, image.width, image.height);
    }
  }
}

function staticAnnotation(record, width, height, review) {
  if (!record || !Array.isArray(record.label) || record.label.length !== 240) return {};
  const hiveCount = Math.max(0, Math.min(4, Number(record.hive_count) || 0));
  const hives = [];
  for (let hiveIndex = 0; hiveIndex < hiveCount; hiveIndex++) {
    const offset = hiveIndex * 60;
    const metadata = record.label.slice(offset + 1, offset + 4);
    const coordinates = record.label.slice(offset + 4, offset + 60);
    const normalized = coordinates.every((value) => Number.isFinite(value) && value >= 0 && value <= 1);
    const points = [];
    for (let pointIndex = 0; pointIndex < coordinates.length; pointIndex += 2) {
      const x = coordinates[pointIndex];
      const y = coordinates[pointIndex + 1];
      points.push(normalized ? [Math.round(x * width), Math.round(y * height)] : [Math.round(x), Math.round(y)]);
    }
    if (Math.max(...points.map((point) => point[0])) - Math.min(...points.map((point) => point[0])) < width * 0.03
      || Math.max(...points.map((point) => point[1])) - Math.min(...points.map((point) => point[1])) < height * 0.03) continue;
    hives.push({ metadata, points });
  }
  return { status: review ? (hives.length ? "review" : "invalid") : (hives.length ? "trusted" : "todo"), hives };
}

function updateSummary() {
  const done = state.images.filter((image) => image.status === "review" || image.status === "trusted").length;
  $("progress-text").textContent = state.currentName
    ? `תמונה ${state.currentIndex + 1} מתוך ${state.images.length}`
    : "בחרו תמונה כדי להתחיל";
  document.title = `BioBuzz | ${done}/${state.images.length} תויגו`;
}

function filteredImages() {
  return state.images.filter((image) => {
    const matchesFilter = state.activeFilter === "all"
      || (state.activeFilter === "review" && ["review", "invalid"].includes(image.status))
      || image.status === state.activeFilter;
    return matchesFilter && image.name.toLowerCase().includes(state.search.toLowerCase());
  });
}

function renderImageList() {
  const images = filteredImages();
  if (!images.length) {
    elements.list.innerHTML = '<div class="empty-list">לא נמצאו תמונות בסינון הזה.</div>';
    return;
  }
  elements.list.innerHTML = images.map((image) => {
    const statusText = image.status === "review" ? "טיוטה שמורה" : image.status === "invalid" ? "טיוטה דורשת תיקון" : image.status === "trusted" ? "תויגה" : "ממתינה";
    return `<button class="image-row ${image.name === state.currentName ? "active" : ""}" data-name="${escapeHtml(image.name)}">
      <img class="thumb" src="${image.src || `/api/image?name=${encodeURIComponent(image.name)}`}" alt="">
      <span class="image-row-copy"><strong>${escapeHtml(image.name)}</strong><small>${image.width} × ${image.height} · ${statusText}</small></span>
      <i class="status-dot ${image.status}"></i>
    </button>`;
  }).join("");
  elements.list.querySelectorAll(".image-row").forEach((button) => {
    button.addEventListener("click", () => selectImage(button.dataset.name));
  });
}

function normalizedHives(image) {
  const validHives = (image.hives || []).filter((sourceHive) =>
    Array.isArray(sourceHive.points) && sourceHive.points.length === 28,
  );
  if (validHives.length) {
    return validHives.map((sourceHive) => ({
      metadata: sourceHive.metadata || [0, 1, 1],
      points: sourceHive.points.map((point) => point ? [...point] : null),
    }));
  }
  return [freshHive()];
}

async function selectImage(name, force = false) {
  if (state.dirty && !force && !confirm("יש שינויים שלא נשמרו. לעבור לתמונה אחרת ולוותר עליהם?")) return;
  const image = state.images.find((item) => item.name === name);
  if (!image) return;
  state.currentName = name;
  state.currentIndex = state.images.indexOf(image);
  state.hives = normalizedHives(image);
  state.selectedHive = 0;
  state.selectedLine = firstIncompleteLine(state.hives[0]);
  state.placementMode = false;
  state.zoom = 1;
  state.undo = [];
  state.redo = [];
  state.dirty = false;
  elements.image.hidden = true;
  elements.svg.hidden = true;
  elements.empty.hidden = false;
  $("current-name").textContent = name;
  $("image-meta").textContent = `${image.width} × ${image.height} פיקסלים · המקור לא משתנה`;
  $("stage-status").textContent = image.status === "review" ? "טיוטה שמורה לבדיקה" : image.status === "invalid" ? "טיוטה קיימת עם נקודות לא תקינות — סמנו מחדש" : image.status === "trusted" ? "יש תיוג קיים — עריכות יישמרו כטיוטה" : "מוכנה לתיוג";
  $("preview-link").hidden = !image.preview_url;
  if (image.preview_url) $("preview-link").href = image.preview_url;
  $("stage-status").parentElement.classList.add("active");
  elements.image.onload = () => {
    elements.empty.hidden = true;
    elements.image.hidden = false;
    elements.svg.hidden = false;
    elements.svg.setAttribute("viewBox", `0 0 ${image.width} ${image.height}`);
    fitImage();
    renderEditor();
  };
  elements.image.onerror = () => showToast("לא הצלחתי לפתוח את קובץ התמונה.", true);
  elements.image.src = image.src || `/api/image?name=${encodeURIComponent(name)}`;
  if (image.status === "invalid") showToast("הטיוטה הקיימת מכילה נקודות לא תקינות. התיוג לא נמחק; סמנו מחדש ושמרו כדי להחליף אותה.", true);
  renderImageList();
  renderEditor();
  updateSummary();
}

function firstIncompleteLine(hive) {
  const index = hive.points.findIndex((point, i) => point === null && i < 28);
  return index < 0 ? 0 : Math.floor(index / 2);
}

function remember() {
  state.undo.push(JSON.stringify(state.hives));
  if (state.undo.length > 50) state.undo.shift();
  state.redo = [];
  updateUndoButtons();
}

function restoreSnapshot(snapshot) {
  state.hives = JSON.parse(snapshot);
  state.dirty = true;
  renderEditor();
}

function updateUndoButtons() {
  $("undo-button").disabled = state.undo.length === 0;
  $("redo-button").disabled = state.redo.length === 0;
}

function renderEditor() {
  const image = currentImage();
  if (!image || !state.hives) return;
  renderHiveTabs();
  renderMetadata();
  renderLineList(0, 10, elements.frameLines);
  renderLineList(10, 14, elements.ironLines);
  renderSvg();
  renderProgress();
  $("save-button").disabled = !state.hives.every((hive) => hive.points.every(Boolean));
  $("add-hive").disabled = state.hives.length >= 4;
  $("remove-hive").disabled = state.hives.length <= 1;
  renderSelectedLineHelp();
  $("clear-line").disabled = !state.hives[state.selectedHive].points
    .slice(state.selectedLine * 2, state.selectedLine * 2 + 2).some(Boolean);
  updateUndoButtons();
}

function renderSelectedLineHelp() {
  const line = state.lines[state.selectedLine];
  if (!line) return;
  const number = String(state.selectedLine + 1).padStart(2, "0");
  $("selected-line-title").textContent = `קו ${number} · ${line.name}`;
  $("selected-line-help").textContent = line.explanation;
  $("line-start-help").textContent = line.start || "קצה אחד של הקו";
  $("line-end-help").textContent = line.end || "הקצה השני של הקו";
  const model = $("frame-model");
  model.querySelectorAll(".model-edge").forEach((edge) => {
    const selected = Number(edge.dataset.modelLine) === state.selectedLine;
    edge.classList.toggle("selected", selected);
    edge.setAttribute("aria-pressed", String(selected));
  });
  const endpoints = $("model-endpoints");
  endpoints.replaceChildren();
  const positions = MODEL_LINE_ENDPOINTS[state.selectedLine];
  if (positions) {
    positions.forEach(([x, y], index) => {
      const marker = document.createElementNS("http://www.w3.org/2000/svg", "g");
      marker.setAttribute("class", "model-endpoint");
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", x);
      circle.setAttribute("cy", y);
      circle.setAttribute("r", "7");
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", x);
      label.setAttribute("y", y + 3);
      label.textContent = index === 0 ? "A" : "B";
      marker.append(circle, label);
      endpoints.appendChild(marker);
    });
  }
}

function selectLine(index) {
  state.selectedLine = index;
  state.placementMode = true;
  renderEditor();
  showToast(`קו ${index + 1} נבחר — סמנו נקודה בכל קצה של הקו.`);
}

function bindFrameModel() {
  $("frame-model").querySelectorAll(".model-edge").forEach((edge) => {
    const select = () => selectLine(Number(edge.dataset.modelLine));
    edge.addEventListener("click", select);
    edge.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
  });
}

function renderHiveTabs() {
  elements.tabs.innerHTML = state.hives.map((_, index) =>
    `<button class="hive-tab ${index === state.selectedHive ? "active" : ""}" data-hive="${index}" role="tab" aria-selected="${index === state.selectedHive}">
      מסגרת ${index + 1} · ${completeLines(state.hives[index])}/14
    </button>`,
  ).join("");
  elements.tabs.querySelectorAll(".hive-tab").forEach((button) => button.addEventListener("click", () => {
    state.selectedHive = Number(button.dataset.hive);
    state.selectedLine = firstIncompleteLine(state.hives[state.selectedHive]);
    state.placementMode = false;
    renderEditor();
  }));
}

function renderMetadata() {
  if (!state.hives[state.selectedHive]) {
    state.hives = [freshHive()];
    state.selectedHive = 0;
  }
  elements.meta.hidden = false;
  const metadata = state.hives[state.selectedHive].metadata;
  $("hive-color").value = metadata[0] ? "blue" : "red";
  $("hive-side").value = metadata[1] ? "right" : "left";
  $("hive-level").value = metadata[2] ? "top" : "bottom";
}

function renderLineList(from, to, target) {
  target.innerHTML = state.lines.slice(from, to).map((line, localIndex) => {
    const index = from + localIndex;
    const hive = state.hives[state.selectedHive];
    const first = Boolean(hive.points[index * 2]);
    const second = Boolean(hive.points[index * 2 + 1]);
    const isIron = index >= 10;
    return `<div class="line-row ${isIron ? "iron-row" : ""} ${state.selectedLine === index ? "selected" : ""}" data-line="${index}" tabindex="0" role="button" aria-label="קו ${index + 1}: ${escapeHtml(line.name)}">
      <span class="line-number">${String(index + 1).padStart(2, "0")}</span>
      <span class="line-copy"><strong>${escapeHtml(line.name)}</strong><small>${first && second ? "שתי נקודות סומנו" : first || second ? "נקודה אחת — נדרשת עוד נקודה" : "לחצו כדי למקם נקודות"}</small></span>
      <span class="line-check"><i class="point-state ${first ? "filled" : ""}"></i><i class="point-state ${second ? "filled" : ""}"></i></span>
    </div>`;
  }).join("");
  target.querySelectorAll(".line-row").forEach((row) => {
    const select = () => {
      selectLine(Number(row.dataset.line));
    };
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
    });
  });
}

function renderSvg() {
  const image = currentImage();
  if (!image) return;
  elements.svg.replaceChildren();
  state.hives.forEach((hive, hiveIndex) => {
    for (let lineIndex = 0; lineIndex < 14; lineIndex++) {
      const start = hive.points[lineIndex * 2];
      const end = hive.points[lineIndex * 2 + 1];
      if (start && end) {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", start[0]); line.setAttribute("y1", start[1]);
        line.setAttribute("x2", end[0]); line.setAttribute("y2", end[1]);
        line.setAttribute("class", `annotation-line ${lineIndex >= 10 ? "iron" : "frame"} ${hiveIndex === state.selectedHive && lineIndex === state.selectedLine ? "selected" : ""}`);
        line.dataset.hive = hiveIndex;
        line.dataset.line = lineIndex;
        line.addEventListener("pointerdown", (event) => {
          if (hiveIndex !== state.selectedHive) return;
          if (state.placementMode) return;
          event.preventDefault();
          remember();
          state.selectedLine = lineIndex;
          const rect = elements.svg.getBoundingClientRect();
          const scaleX = image.width / rect.width;
          const scaleY = image.height / rect.height;
          const dx = (event.clientX - rect.left) * scaleX - (start[0] + end[0]) / 2;
          const dy = (event.clientY - rect.top) * scaleY - (start[1] + end[1]) / 2;
          state.drag = { type: "line", hiveIndex, lineIndex, dx, dy };
          elements.svg.setPointerCapture(event.pointerId);
        });
        elements.svg.appendChild(line);
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", (start[0] + end[0]) / 2);
        label.setAttribute("y", (start[1] + end[1]) / 2 - 7);
        label.setAttribute("class", "line-label");
        label.setAttribute("text-anchor", "middle");
        label.textContent = hiveIndex ? `${hiveIndex + 1}.${lineIndex + 1}` : String(lineIndex + 1);
        elements.svg.appendChild(label);
      }
      [start, end].forEach((point, endpoint) => {
        if (!point) return;
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("cx", point[0]); circle.setAttribute("cy", point[1]);
        circle.setAttribute("r", hiveIndex === state.selectedHive && lineIndex === state.selectedLine ? "8" : "6");
        circle.setAttribute("class", `endpoint ${state.placementMode && hiveIndex === state.selectedHive && lineIndex === state.selectedLine ? "active" : ""}`);
        circle.setAttribute("aria-label", `קו ${lineIndex + 1}, נקודה ${endpoint + 1}`);
        circle.addEventListener("pointerdown", (event) => {
          event.preventDefault(); event.stopPropagation();
          if (hiveIndex !== state.selectedHive) return;
          remember();
          state.selectedLine = lineIndex;
          state.placementMode = false;
          state.drag = { type: "point", hiveIndex, pointIndex: lineIndex * 2 + endpoint };
          elements.svg.setPointerCapture(event.pointerId);
        });
        elements.svg.appendChild(circle);
      });
    }
  });
}

function pointFromEvent(event) {
  const image = currentImage();
  const rect = elements.svg.getBoundingClientRect();
  return [
    Math.min(image.width - 1, Math.max(0, Math.round((event.clientX - rect.left) * image.width / rect.width))),
    Math.min(image.height - 1, Math.max(0, Math.round((event.clientY - rect.top) * image.height / rect.height))),
  ];
}

elements.svg.addEventListener("pointerdown", (event) => {
  if (event.target !== elements.svg || !state.placementMode || !currentImage()) return;
  const hive = state.hives[state.selectedHive];
  const start = state.selectedLine * 2;
  const nextEndpoint = hive.points[start] ? (hive.points[start + 1] ? null : start + 1) : start;
  if (nextEndpoint === null) {
    showToast("לקו הזה כבר יש שתי נקודות. גררו נקודה כחולה לתיקון.");
    state.placementMode = false;
    renderEditor();
    return;
  }
  remember();
  hive.points[nextEndpoint] = pointFromEvent(event);
  state.dirty = true;
  if (nextEndpoint === start) {
    showToast("הנקודה הראשונה סומנה — לחצו עכשיו על הקצה השני של הקו.");
  } else {
    state.placementMode = false;
    showToast(`קו ${state.selectedLine + 1} הושלם.`);
    state.selectedLine = firstIncompleteLine(hive);
  }
  renderEditor();
});

elements.svg.addEventListener("pointermove", (event) => {
  if (!state.drag) return;
  const drag = state.drag;
  const image = currentImage();
  const position = pointFromEvent(event);
  if (drag.type === "point") {
    state.hives[drag.hiveIndex].points[drag.pointIndex] = position;
  } else {
    const hive = state.hives[drag.hiveIndex];
    const lineStart = drag.lineIndex * 2;
    const a = hive.points[lineStart];
    const b = hive.points[lineStart + 1];
    const rect = elements.svg.getBoundingClientRect();
    const dx = (event.clientX - rect.left) * image.width / rect.width - drag.dx - (a[0] + b[0]) / 2;
    const dy = (event.clientY - rect.top) * image.height / rect.height - drag.dy - (a[1] + b[1]) / 2;
    hive.points[lineStart] = [Math.round(a[0] + dx), Math.round(a[1] + dy)];
    hive.points[lineStart + 1] = [Math.round(b[0] + dx), Math.round(b[1] + dy)];
  }
  state.dirty = true;
  renderSvg();
});

elements.svg.addEventListener("pointerup", () => {
  if (state.drag) {
    state.drag = null;
    renderEditor();
  }
});

function renderProgress() {
  const hive = state.hives[state.selectedHive];
  const done = completeLines(hive);
  const endpointCount = pointCount(hive);
  $("progress-count").textContent = `${done}/14 קווים`;
  $("progress-text").textContent = `${endpointCount}/28 נקודות · מסגרת ${state.selectedHive + 1} מתוך ${state.hives.length}`;
  $("progress-bar").style.width = `${endpointCount / 28 * 100}%`;
}

function fitImage() {
  const image = currentImage();
  if (!image || !elements.image.complete) return;
  const maxWidth = Math.max(200, elements.canvas.clientWidth - 28);
  const maxHeight = Math.max(200, elements.canvas.clientHeight - 22);
  const fitScale = Math.min(maxWidth / image.width, maxHeight / image.height, 1);
  const scale = fitScale * state.zoom;
  elements.wrap.style.width = `${Math.max(1, image.width * scale)}px`;
  $("zoom-level").textContent = state.zoom === 1 ? "התאם" : `${Math.round(state.zoom * 100)}%`;
}

function addHive() {
  if (state.hives.length >= 4) return;
  remember();
  state.hives.push(freshHive());
  state.selectedHive = state.hives.length - 1;
  state.selectedLine = 0;
  state.dirty = true;
  renderEditor();
  showToast("נוספה מסגרת. התחילו לסמן את 14 הקווים שלה.");
}

function removeHive() {
  if (state.hives.length <= 1) return;
  remember();
  state.hives.splice(state.selectedHive, 1);
  state.selectedHive = Math.min(state.selectedHive, state.hives.length - 1);
  state.selectedLine = firstIncompleteLine(state.hives[state.selectedHive]);
  state.placementMode = false;
  state.dirty = true;
  renderEditor();
  showToast("המסגרת הנבחרת הוסרה מהטיוטה.");
}

async function saveDraft() {
  const image = currentImage();
  if (!image) return;
  if (!state.hives.every((hive) => hive.points.every(Boolean))) {
    showToast("צריך לסמן את כל 28 נקודות הקצה בכל מסגרת לפני שמירה.", true);
    return;
  }
  $("save-button").disabled = true;
  try {
    const label = makeBrowserLabel(state.hives, image.width, image.height);
    const record = {
      image_path: image.repoPath || image.name,
      label,
      hive_count: state.hives.length,
      calibration: null,
      line_names: state.lines.map((line) => line.name),
      review_status: "awaiting_user_approval",
      annotation_method: "manual_user_review_ui_not_model_prediction",
      review_note: "Saved in browser storage. Export drafts to share or upload to GitHub.",
    };
    const result = (state.staticMode || image.localUpload)
      ? await saveStaticDraft(record, image)
      : await api("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: image.name, hives: state.hives }),
      });
    image.status = "review";
    image.preview_url = result.preview;
    image.hives = state.hives.map((hive) => ({
      metadata: hive.metadata,
      points: hive.points,
    }));
    state.dirty = false;
    renderImageList();
    renderEditor();
    updateSummary();
    showToast(state.staticMode
      ? "הטיוטה נשמרה בדפדפן. ייצאו אותה כדי להעביר למחשב אחר או ל-GitHub."
      : "הטיוטה נשמרה בנפרד מהתיוגים לאימון.");
  } catch (error) {
    showToast(`השמירה נכשלה: ${error.message}`, true);
    $("save-button").disabled = false;
  }
}

function makeBrowserLabel(hives, width, height) {
  const label = new Array(240).fill(0);
  hives.forEach((hive, hiveIndex) => {
    const offset = hiveIndex * 60;
    label[offset] = 1;
    label.splice(offset + 1, 3, ...hive.metadata.map((value) => Number(Boolean(value))));
    hive.points.forEach((point, pointIndex) => {
      label[offset + 4 + pointIndex * 2] = point[0] / width;
      label[offset + 5 + pointIndex * 2] = point[1] / height;
    });
  });
  return label;
}

function draftStorageKey() {
  return `biobuzz-drafts:${location.pathname}`;
}

async function saveStaticDraft(record, image) {
  const drafts = JSON.parse(localStorage.getItem(draftStorageKey()) || "{}");
  drafts[image.name] = record;
  localStorage.setItem(draftStorageKey(), JSON.stringify(drafts));
  const preview = await makePreviewDataUrl(image, state.hives);
  image.preview_url = preview;
  return { ok: true, preview };
}

async function makePreviewDataUrl(image, hives) {
  const source = elements.image;
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const context = canvas.getContext("2d");
  context.drawImage(source, 0, 0, image.width, image.height);
  hives.forEach((hive) => {
    for (let lineIndex = 0; lineIndex < 14; lineIndex++) {
      const start = hive.points[lineIndex * 2];
      const end = hive.points[lineIndex * 2 + 1];
      context.strokeStyle = lineIndex >= 10 ? "#ff9f0a" : "#15e34a";
      context.lineWidth = Math.max(3, image.width / 450);
      context.beginPath();
      context.moveTo(...start);
      context.lineTo(...end);
      context.stroke();
      context.fillStyle = "#075bff";
      for (const [x, y] of [start, end]) {
        context.beginPath();
        context.arc(x, y, Math.max(5, image.width / 250), 0, Math.PI * 2);
        context.fill();
      }
    }
  });
  return canvas.toDataURL("image/jpeg", 0.92);
}

function refreshStaticDrafts() {
  if (!state.staticMode) return;
  const drafts = JSON.parse(localStorage.getItem(draftStorageKey()) || "{}");
  for (const image of state.images) {
    if (drafts[image.name]) {
      image.status = "review";
      image.hives = stateFromRecord(drafts[image.name], image.width, image.height);
    }
  }
  renderImageList();
}

function stateFromRecord(record, width, height) {
  const result = [];
  for (let hiveIndex = 0; hiveIndex < record.hive_count; hiveIndex++) {
    const offset = hiveIndex * 60;
    const points = [];
    for (let pointIndex = 0; pointIndex < 28; pointIndex++) {
      points.push([
        Math.round(record.label[offset + 4 + pointIndex * 2] * width),
        Math.round(record.label[offset + 5 + pointIndex * 2] * height),
      ]);
    }
    result.push({ metadata: record.label.slice(offset + 1, offset + 4), points });
  }
  return result;
}

function addUploadedFiles(files) {
  const accepted = Array.from(files).filter((file) => file.type.startsWith("image/"));
  if (!accepted.length) {
    showToast("בחרו קובצי תמונה נתמכים.", true);
    return;
  }
  for (const file of accepted) {
    const name = file.name;
    const existing = state.images.find((item) => item.name === name);
    if (existing) {
      if (!confirm(`${name} כבר קיים ברשימה. להחליף אותו בקובץ החדש?`)) continue;
      if (state.uploadedUrls.has(name)) URL.revokeObjectURL(state.uploadedUrls.get(name));
    }
    const src = URL.createObjectURL(file);
    state.uploadedUrls.set(name, src);
    const probe = new Image();
    probe.onload = async () => {
      const image = { name, width: probe.naturalWidth, height: probe.naturalHeight, status: "todo", hives: [], src, localUpload: true };
      const index = state.images.findIndex((item) => item.name === name);
      if (index >= 0) state.images[index] = image;
      else state.images.push(image);
      state.images.sort((a, b) => a.name.localeCompare(b.name));
      try {
        if (state.staticMode) {
          await uploadTransaction("readwrite", (store) =>
            store.put({ name, width: image.width, height: image.height, file }),
          );
        }
        renderImageList();
        updateImageCount();
        await selectImage(name, true);
        showToast(state.staticMode
          ? `${name} נשמרה בדפדפן הזה. לכולם תהיה זמינה רק לאחר העלאה לריפו הציבורי.`
          : `${name} נוספה לרשימת התיוג במחשב הזה.`);
      } catch (error) {
        showToast(`לא הצלחתי לשמור את ${name}: ${error.message}`, true);
      }
    };
    probe.onerror = () => showToast(`לא הצלחתי לקרוא את התמונה ${name}.`, true);
    probe.src = src;
  }
}

function exportDrafts() {
  const drafts = state.staticMode
    ? JSON.parse(localStorage.getItem(draftStorageKey()) || "{}")
    : null;
  if (!state.staticMode) {
    window.location.href = "/api/export";
    return;
  }
  const records = Object.values(drafts || {});
  if (!records.length) {
    showToast("אין עדיין טיוטות לייצוא.", true);
    return;
  }
  downloadJson(records, "annotations_review.json");
}

function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

async function importDraftFile(file) {
  try {
    const records = JSON.parse(await file.text());
    if (!Array.isArray(records)) throw new Error("הקובץ צריך להכיל רשימת טיוטות.");
    const drafts = state.staticMode ? JSON.parse(localStorage.getItem(draftStorageKey()) || "{}") : {};
    for (const record of records) {
      const name = String(record.image_path || "").split(/[\\/]/).pop();
      const image = state.images.find((item) => item.name === name);
      if (!image || !Array.isArray(record.label) || record.label.length !== 240) continue;
      drafts[name] = record;
      image.status = "review";
      image.hives = stateFromRecord(record, image.width, image.height);
    }
    if (state.staticMode) {
      localStorage.setItem(draftStorageKey(), JSON.stringify(drafts));
    } else {
      await api("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(records),
      });
    }
    renderImageList();
    showToast("הטיוטות יובאו לדפדפן הזה.");
  } catch (error) {
    showToast(`לא הצלחתי לייבא: ${error.message}`, true);
  }
}

function changeMetadata(index, value) {
  remember();
  state.hives[state.selectedHive].metadata[index] = value;
  state.dirty = true;
  renderHiveTabs();
}

$("image-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderImageList();
});
document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  state.activeFilter = button.dataset.filter;
  renderImageList();
}));
$("undo-button").addEventListener("click", () => {
  if (!state.undo.length) return;
  state.redo.push(JSON.stringify(state.hives));
  restoreSnapshot(state.undo.pop());
});
$("redo-button").addEventListener("click", () => {
  if (!state.redo.length) return;
  state.undo.push(JSON.stringify(state.hives));
  restoreSnapshot(state.redo.pop());
});
$("save-button").addEventListener("click", saveDraft);
$("upload-images").addEventListener("click", () => $("image-upload").click());
$("image-upload").addEventListener("change", (event) => {
  addUploadedFiles(event.target.files);
  event.target.value = "";
});
$("export-drafts").addEventListener("click", exportDrafts);
$("import-drafts").addEventListener("click", () => $("draft-upload").click());
$("draft-upload").addEventListener("change", (event) => {
  if (event.target.files?.[0]) importDraftFile(event.target.files[0]);
  event.target.value = "";
});
$("add-hive").addEventListener("click", addHive);
$("remove-hive").addEventListener("click", removeHive);
$("clear-line").addEventListener("click", () => {
  const hive = state.hives[state.selectedHive];
  const start = state.selectedLine * 2;
  if (!hive.points[start] && !hive.points[start + 1]) return;
  remember();
  hive.points[start] = null;
  hive.points[start + 1] = null;
  state.placementMode = false;
  state.dirty = true;
  renderEditor();
  showToast(`נקודות קו ${state.selectedLine + 1} נמחקו. אפשר לסמן אותו מחדש.`);
});
$("hive-color").addEventListener("change", (event) => changeMetadata(0, event.target.value === "blue" ? 1 : 0));
$("hive-side").addEventListener("change", (event) => changeMetadata(1, event.target.value === "right" ? 1 : 0));
$("hive-level").addEventListener("change", (event) => changeMetadata(2, event.target.value === "top" ? 1 : 0));
$("zoom-in").addEventListener("click", () => { state.zoom = Math.min(3, state.zoom + 0.2); fitImage(); });
$("zoom-out").addEventListener("click", () => { state.zoom = Math.max(0.4, state.zoom - 0.2); fitImage(); });
$("fit-button").addEventListener("click", () => { state.zoom = 1; fitImage(); });
window.addEventListener("resize", fitImage);
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

function openGuide() {
  if (typeof elements.guide.showModal === "function") elements.guide.showModal();
  else elements.guide.setAttribute("open", "");
}
function closeGuide() {
  if (typeof elements.guide.close === "function") elements.guide.close();
  else elements.guide.removeAttribute("open");
}
["open-guide", "guide-button", "bottom-guide"].forEach((id) => $(id).addEventListener("click", openGuide));
["close-guide", "guide-done"].forEach((id) => $(id).addEventListener("click", closeGuide));

document.addEventListener("keydown", (event) => {
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "SELECT" || elements.guide.open) return;
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    const offset = event.key === "ArrowRight" ? -1 : 1;
    const next = state.images[state.currentIndex + offset];
    if (next) selectImage(next.name);
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
    event.preventDefault();
    $("undo-button").click();
  }
});

boot();
bindFrameModel();
