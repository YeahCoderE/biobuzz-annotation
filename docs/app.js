import {
  buildModel, cornerLabel, endpointLabel, fixSwappedEndpoints, checkHive, autoComplete, fitProjection, POINTS_PER_HIVE,
} from "./geometry.js";
import { createRealtime, topicKey } from "./realtime.js";
import { detectorAvailable, detectCells } from "./detector.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const USER_KEY = "biobuzz-user";
const DRAFTS_KEY = `biobuzz-drafts:${location.pathname}`;
const PREFS_KEY = "biobuzz-prefs";
const IDLE_LIMIT_MS = 10 * 60 * 1000;
const PALETTE = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2", "#ca8a04", "#dc2626"];
const LINE_ORDER = [0, 2, 4, 8, 6, 1, 3, 5, 9, 7, 10, 11, 12, 13];

const store = {
  get(key, fallback) {
    try { const value = localStorage.getItem(key); return value ? JSON.parse(value) : fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
  },
};

const state = {
  model: null,
  images: [],
  sources: {},
  currentName: null,
  currentIndex: -1,
  hives: null,
  selectedHive: 0,
  selectedLine: 0,
  mode: "corners",
  skipped: new Set(),
  markHidden: false,
  activeFilter: "all",
  search: "",
  zoom: 1,
  undo: [],
  redo: [],
  dirty: false,
  gesture: null,
  hover: null,
  readOnly: false,
  lockState: "none",
  holder: null,
  remote: null,
  user: store.get(USER_KEY, null),
  rt: null,
  viewer: null,
  tour: null,
  lastActivity: Date.now(),
  uploadDb: null,
  uploadedUrls: new Map(),
  prefs: store.get(PREFS_KEY, { dims: false }),
};

const $ = (id) => document.getElementById(id);
const el = {
  list: $("image-list"),
  image: $("source-image"),
  svg: $("annotation-svg"),
  wrap: $("image-wrap"),
  empty: $("empty-state"),
  canvas: $("canvas-scroll"),
  loupe: $("loupe"),
  cursor: $("remote-cursor"),
  toast: $("toast"),
};

const pad2 = (n) => String(n).padStart(2, "0");
const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[c]);
const initials = (name) => (name || "?").trim().split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();

function toast(message, kind = "info") {
  el.toast.textContent = message;
  el.toast.className = `toast show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.toast.classList.remove("show"), kind === "error" ? 5200 : 3200);
}

async function fetchJson(url, fallback) {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    return await response.json();
  } catch (error) {
    if (fallback !== undefined) return fallback;
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Data: records <-> hives
// ---------------------------------------------------------------------------

const freshHive = (metadata = [0, 1, 1]) => ({
  metadata: [...metadata],
  points: Array.from({ length: POINTS_PER_HIVE }, () => null),
  est: Array.from({ length: POINTS_PER_HIVE }, () => false),
});

const recordName = (record) => String(record?.image_path || "").split(/[\\/]/).pop();

function recordToHives(record, width, height) {
  if (!record || !Array.isArray(record.label) || record.label.length !== 240) return null;
  const count = Math.max(0, Math.min(4, Number(record.hive_count) || 0));
  const hives = [];
  for (let h = 0; h < count; h++) {
    const offset = h * 60;
    const coords = record.label.slice(offset + 4, offset + 60);
    const normalized = coords.every((v) => Number.isFinite(v) && v >= 0 && v <= 1.0001);
    const points = [];
    for (let i = 0; i < 56; i += 2) {
      const x = coords[i];
      const y = coords[i + 1];
      points.push(normalized ? [Math.round(x * width), Math.round(y * height)] : [Math.round(x), Math.round(y)]);
    }
    const xs = points.map((p) => p[0]);
    const ys = points.map((p) => p[1]);
    if (Math.max(...xs) - Math.min(...xs) < width * 0.02 || Math.max(...ys) - Math.min(...ys) < height * 0.02) continue;
    const estList = Array.isArray(record.estimated_points?.[h]) ? record.estimated_points[h] : [];
    const est = Array.from({ length: POINTS_PER_HIVE }, (_, i) => estList.includes(i));
    hives.push({ metadata: record.label.slice(offset + 1, offset + 4).map((v) => Number(Boolean(v))), points, est });
  }
  return hives;
}

function hivesToRecord(image, hives) {
  const label = new Array(240).fill(0);
  hives.forEach((hive, h) => {
    const offset = h * 60;
    label[offset] = 1;
    hive.metadata.forEach((value, i) => { label[offset + 1 + i] = Number(Boolean(value)); });
    hive.points.forEach((point, i) => {
      label[offset + 4 + i * 2] = +(point[0] / image.width).toFixed(6);
      label[offset + 5 + i * 2] = +(point[1] / image.height).toFixed(6);
    });
  });
  return {
    image_path: image.name,
    label,
    hive_count: hives.length,
    calibration: null,
    line_names: state.model.lines.map((line) => line.name),
    estimated_points: hives.map((hive) => hive.est.map((v, i) => (v ? i : -1)).filter((i) => i >= 0)),
    review_status: "awaiting_user_approval",
    annotation_method: "manual_user_review_ui_not_model_prediction",
    line_definition: "main.py LINES_INPUT_HE — pentagonal BIOBUZZ cell, A/B order fixed",
    model_version: 2,
    annotator: state.user?.name || "",
    saved_at: new Date().toISOString(),
  };
}

const localDrafts = () => store.get(DRAFTS_KEY, {});

function applyRecordToImage(image, record, status) {
  const hives = recordToHives(record, image.width, image.height);
  if (!hives) return;
  image.status = hives.length ? status : "invalid";
  image.hives = hives;
  image.savedAt = Date.parse(record.saved_at || "") || 0;
  image.annotator = record.annotator || "";
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  try {
    const [modelRaw, manifest, trusted, repoDrafts, sources] = await Promise.all([
      fetchJson("./data/hive_model.json"),
      fetchJson("./images.json"),
      fetchJson("./data/annotations.json", []),
      fetchJson("./data/annotations_review.json", []),
      fetchJson("./data/image_sources.json", {}),
    ]);
    state.model = buildModel(modelRaw);
    state.sources = sources || {};
    state.images = manifest.images.map((item) => ({
      ...item,
      src: `./images/${encodeURIComponent(item.name)}`,
      thumbSrc: item.thumb ? `./thumbs/${encodeURIComponent(item.thumb)}` : `./images/${encodeURIComponent(item.name)}`,
      status: "todo",
      hives: [],
      savedAt: 0,
    }));
    const byName = new Map(state.images.map((image) => [image.name, image]));
    for (const record of Array.isArray(trusted) ? trusted : []) {
      const image = byName.get(recordName(record));
      if (image) applyRecordToImage(image, record, "trusted");
    }
    for (const record of Array.isArray(repoDrafts) ? repoDrafts : []) {
      const image = byName.get(recordName(record));
      if (image) applyRecordToImage(image, record, "review");
    }
    const drafts = localDrafts();
    for (const [name, record] of Object.entries(drafts)) {
      const image = byName.get(name);
      if (!image) continue;
      if (Number(record.model_version) !== 2) continue;
      applyRecordToImage(image, record, "review");
    }
    try {
      state.uploadDb = await openUploadDatabase();
      await restoreUploadedFiles();
    } catch { /* private window: uploads stay disabled */ }
    renderStaticPanel();
    renderImageList();
    $("image-count").textContent = `${state.images.length}`;
    initViewer();
    initRealtime();
    const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
    const wantTour = hash === "tour";
    const fromHash = state.images.find((image) => image.name === hash);
    const first = fromHash || state.images.find((image) => image.status === "todo") || state.images[0];
    if (first) await selectImage(first.name, true);
    if (wantTour) openTour();
  } catch (error) {
    console.error(error);
    toast(`לא הצלחתי לטעון את האתר: ${error.message}`, "error");
  }
}

// ---------------------------------------------------------------------------
// Realtime
// ---------------------------------------------------------------------------

function initRealtime() {
  state.rt = createRealtime({
    onStatus: renderConnection,
    onPresence: () => { renderPresence(); renderImageList(); },
    onLocks: () => { renderImageList(); updateLockFromNetwork(); },
    onLive: (key, data) => {
      if (!state.currentName || key !== topicKey(state.currentName) || !state.readOnly) return;
      if (!Array.isArray(data.hives)) return;
      state.remote = data;
      state.hives = data.hives.map((hive) => ({
        metadata: hive.metadata || [0, 1, 1],
        points: (hive.points || []).map((p) => (p ? [p[0], p[1]] : null)),
        est: hive.est || Array.from({ length: POINTS_PER_HIVE }, () => false),
      }));
      state.selectedHive = Math.min(data.selectedHive || 0, state.hives.length - 1);
      state.selectedLine = data.selectedLine ?? state.selectedLine;
      renderEditor();
    },
    onCursor: (key, data) => {
      if (!state.currentName || key !== topicKey(state.currentName)) return;
      showRemoteCursor(data);
    },
    onDraft: (key, data) => {
      const image = state.images.find((item) => topicKey(item.name) === key);
      if (!image || !data.record) return;
      const savedAt = Number(data.savedAt) || Date.parse(data.record.saved_at || "") || 0;
      if (savedAt <= (image.savedAt || 0)) return;
      if (Number(data.record.model_version) !== 2) return;
      const drafts = localDrafts();
      drafts[image.name] = data.record;
      store.set(DRAFTS_KEY, drafts);
      applyRecordToImage(image, data.record, "review");
      image.savedAt = savedAt;
      renderImageList();
      if (image.name === state.currentName && !state.dirty && state.lockState !== "mine") {
        loadHivesFromImage(image);
        renderEditor();
      }
    },
  });
  if (!state.user) openNameDialog(true);
  else state.rt.connect(state.user);
}

function renderConnection(status) {
  const pill = $("connection");
  const text = { online: "מחובר בזמן אמת", syncing: "מסנכרן…", connecting: "מתחבר…", offline: "לא מחובר" }[status] || status;
  pill.dataset.status = status;
  pill.querySelector("span").textContent = text;
  if (status === "online" && state.currentName && (state.lockState === "offline" || state.lockState === "none")) {
    attemptEdit(state.currentName);
  }
  renderBanner();
}

function renderPresence() {
  const others = state.rt ? state.rt.others() : [];
  const bar = $("presence");
  const me = state.user ? `<button class="avatar me" style="--c:${state.user.color}" data-me title="${escapeHtml(state.user.name)} (אני) — לחצו לשינוי שם">${escapeHtml(initials(state.user.name))}</button>` : "";
  bar.innerHTML = me + others.slice(0, 7).map((person) => {
    const doing = person.image ? `${person.mode === "edit" ? "עובד/ת על" : "צופה ב"} ${person.image}` : "בדף הראשי";
    return `<button class="avatar" style="--c:${escapeHtml(person.color || "#64748b")}" data-image="${escapeHtml(person.image || "")}" title="${escapeHtml(person.name || "אורח")} · ${escapeHtml(doing)}">${escapeHtml(initials(person.name))}${person.mode === "edit" ? '<i class="pen">✎</i>' : ""}</button>`;
  }).join("") + (others.length > 7 ? `<span class="avatar more">+${others.length - 7}</span>` : "");
  $("online-count").textContent = others.length ? `${others.length + 1} מחוברים` : "רק את/ה כאן";
  bar.querySelectorAll("[data-image]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.image) selectImage(button.dataset.image);
  }));
  bar.querySelector("[data-me]")?.addEventListener("click", () => openNameDialog(false));
}

async function attemptEdit(name) {
  if (!state.rt) return;
  state.lockState = "acquiring";
  state.readOnly = true;
  renderBanner();
  renderEditor();
  const result = await state.rt.acquire(name);
  if (state.currentName !== name) {
    if (result.ok) state.rt.release(name);
    return;
  }
  if (result.ok) {
    state.lockState = result.offline ? "offline" : "mine";
    state.readOnly = false;
    state.holder = null;
    state.remote = null;
    state.lastActivity = Date.now();
    state.rt.setActivity({ image: name, mode: result.offline ? "view" : "edit" });
    publishLive();
  } else {
    state.lockState = "other";
    state.readOnly = true;
    state.holder = result.holder;
    state.rt.setActivity({ image: name, mode: "view" });
  }
  renderBanner();
  renderEditor();
}

function updateLockFromNetwork() {
  if (!state.currentName || !state.rt) return;
  if (state.lockState === "other") {
    const holder = state.rt.holderOf(state.currentName);
    if (!holder) {
      state.holder = null;
      state.lockState = "free";
      el.cursor.hidden = true;
      renderBanner();
      toast("התמונה התפנתה — אפשר להתחיל לעבוד עליה.");
    } else if (holder.id !== state.holder?.id) {
      state.holder = holder;
      renderBanner();
    }
  } else if (state.lockState === "free" || state.lockState === "idle") {
    const holder = state.rt.holderOf(state.currentName);
    if (holder) {
      state.holder = holder;
      state.lockState = "other";
      renderBanner();
      renderEditor();
    }
  } else if (state.lockState === "mine" && !state.rt.holds(state.currentName)) {
    state.lockState = "other";
    state.readOnly = true;
    state.holder = state.rt.holderOf(state.currentName);
    renderBanner();
    renderEditor();
  }
}

function renderBanner() {
  const banner = $("lock-banner");
  const name = state.holder?.name || "מישהו";
  const views = {
    acquiring: ["checking", "⏳", "בודק אם מישהו כבר עובד על התמונה…", ""],
    mine: ["mine", "✎", "את/ה עובד/ת על התמונה הזאת. אחרים רואים אותה בצפייה בלבד.", ""],
    offline: ["offline", "⚠", "אין חיבור לשרת זמן אמת. אפשר לעבוד, אבל הנעילה לא פעילה.", ""],
    other: ["other", "🔒", `${name} עובד/ת על התמונה הזאת עכשיו. את/ה בצפייה בלבד, והשינויים מופיעים כאן בזמן אמת.`, ""],
    free: ["free", "✓", "התמונה התפנתה.", '<button class="banner-action" data-action="edit">התחילו לעבוד עליה</button>'],
    idle: ["idle", "⏸", "הנעילה שוחררה אחרי 10 דקות בלי פעילות.", '<button class="banner-action" data-action="edit">המשיכו לעבוד</button>'],
  };
  const view = views[state.lockState];
  if (!view || (state.lockState === "mine" && (state.rt?.others().length || 0) === 0)) {
    banner.hidden = true;
    return;
  }
  const [kind, icon, text, action] = view;
  banner.hidden = false;
  banner.dataset.kind = kind;
  banner.innerHTML = `<span class="banner-icon" ${state.holder ? `style="--c:${escapeHtml(state.holder.color || "#64748b")}"` : ""}>${icon}</span><span class="banner-text">${escapeHtml(text)}</span>${action}`;
  banner.querySelector('[data-action="edit"]')?.addEventListener("click", () => attemptEdit(state.currentName));
}

function publishLive() {
  if (!state.rt || state.lockState !== "mine" || !state.hives) return;
  state.rt.publishLive(state.currentName, {
    hives: state.hives, selectedHive: state.selectedHive, selectedLine: state.selectedLine,
  });
}

function showRemoteCursor(data) {
  const image = currentImage();
  if (!image || data.x === null || data.x === undefined) { el.cursor.hidden = true; return; }
  el.cursor.hidden = false;
  el.cursor.style.left = `${(data.x / image.width) * 100}%`;
  el.cursor.style.top = `${(data.y / image.height) * 100}%`;
  el.cursor.style.setProperty("--c", data.color || "#2563eb");
  el.cursor.querySelector("span").textContent = data.name || "אורח";
  clearTimeout(showRemoteCursor.timer);
  showRemoteCursor.timer = setTimeout(() => { el.cursor.hidden = true; }, 4000);
}

setInterval(() => {
  if (state.lockState === "mine" && Date.now() - state.lastActivity > IDLE_LIMIT_MS) {
    state.rt.release(state.currentName);
    state.rt.setActivity({ mode: "view" });
    state.lockState = "idle";
    state.readOnly = true;
    renderBanner();
    renderEditor();
  }
}, 20000);

function openNameDialog(firstTime) {
  const dialog = $("name-dialog");
  $("name-input").value = state.user?.name || "";
  $("name-skip").hidden = !firstTime;
  dialog.showModal();
  setTimeout(() => $("name-input").focus(), 50);
}

$("name-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("name-input").value.trim().slice(0, 32) || "אורח";
  const color = state.user?.color || PALETTE[Math.floor(Math.random() * PALETTE.length)];
  const firstTime = !state.user;
  state.user = { name, color };
  store.set(USER_KEY, state.user);
  $("name-dialog").close();
  if (firstTime || state.rt.status === "offline") state.rt.connect(state.user);
  else state.rt.setUser(state.user);
  renderPresence();
});
$("name-skip").addEventListener("click", () => {
  $("name-input").value = "אורח";
  $("name-form").requestSubmit();
});

// ---------------------------------------------------------------------------
// Image list
// ---------------------------------------------------------------------------

function filteredImages() {
  const query = state.search.trim().toLowerCase();
  return state.images.filter((image) => {
    const matches = state.activeFilter === "all"
      || (state.activeFilter === "review" && ["review", "invalid", "trusted"].includes(image.status))
      || (state.activeFilter === "todo" && image.status === "todo")
      || (state.activeFilter === "web" && image.name.startsWith("web_"));
    return matches && image.name.toLowerCase().includes(query);
  });
}

function renderImageList() {
  const images = filteredImages();
  const done = state.images.filter((image) => ["review", "trusted"].includes(image.status)).length;
  $("overall-progress").style.width = `${state.images.length ? (done / state.images.length) * 100 : 0}%`;
  $("overall-text").textContent = `${done} מתוך ${state.images.length} תויגו`;
  document.title = `BioBuzz Hive Studio · ${done}/${state.images.length}`;
  if (!images.length) {
    el.list.innerHTML = '<div class="empty-list">אין תמונות בסינון הזה.</div>';
    return;
  }
  el.list.innerHTML = images.map((image) => {
    const statusText = { review: "טיוטה שמורה", invalid: "צריך לתקן", trusted: "מתויגת", todo: "ממתינה" }[image.status] || "ממתינה";
    const holder = state.rt?.holderOf(image.name);
    const viewers = state.rt ? state.rt.whoIsOn(image.name).filter((p) => p.id !== holder?.id) : [];
    const lock = holder ? `<span class="row-lock" style="--c:${escapeHtml(holder.color || "#64748b")}" title="${escapeHtml(holder.name)} עובד/ת עליה">${escapeHtml(initials(holder.name))}</span>` : "";
    const eyes = viewers.length ? `<span class="row-eyes" title="${viewers.map((v) => escapeHtml(v.name)).join(", ")}">👁 ${viewers.length}</span>` : "";
    const source = state.sources[image.name] ? '<span class="row-web" title="תמונה מהאינטרנט">🌐</span>' : "";
    const hiveCount = image.hives?.length ? ` · ${image.hives.length} כוורות` : "";
    return `<button class="image-row ${image.name === state.currentName ? "active" : ""}" data-name="${escapeHtml(image.name)}">
      <span class="thumb-wrap"><img class="thumb" loading="lazy" src="${image.localUpload ? image.src : image.thumbSrc}" alt="">${lock}</span>
      <span class="image-row-copy"><strong>${source}${escapeHtml(image.name)}</strong><small>${statusText}${hiveCount}</small></span>
      ${eyes}<i class="status-dot ${image.status}"></i>
    </button>`;
  }).join("");
  el.list.querySelectorAll(".image-row").forEach((button) => button.addEventListener("click", () => selectImage(button.dataset.name)));
}

$("image-search").addEventListener("input", (event) => { state.search = event.target.value; renderImageList(); });
document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
  state.activeFilter = button.dataset.filter;
  renderImageList();
}));

// ---------------------------------------------------------------------------
// Selecting an image
// ---------------------------------------------------------------------------

const currentImage = () => state.images.find((image) => image.name === state.currentName) || null;

function loadHivesFromImage(image) {
  const valid = (image.hives || []).filter((hive) => Array.isArray(hive.points) && hive.points.length === POINTS_PER_HIVE);
  state.hives = valid.length
    ? valid.map((hive) => ({
      metadata: [...(hive.metadata || [0, 1, 1])],
      points: hive.points.map((p) => (p ? [...p] : null)),
      est: hive.est ? [...hive.est] : Array.from({ length: POINTS_PER_HIVE }, () => false),
    }))
    : [freshHive()];
  state.selectedHive = 0;
  state.mode = state.hives[0].points.every(Boolean) ? "lines" : "corners";
  state.selectedLine = firstIncompleteLine(state.hives[0]) ?? 0;
  state.skipped = new Set();
}

async function selectImage(name, force = false) {
  if (name === state.currentName && !force) return;
  if (state.dirty && !force && !confirm("יש שינויים שלא נשמרו. לעבור תמונה ולוותר עליהם?")) return;
  const image = state.images.find((item) => item.name === name);
  if (!image) return;
  const previous = state.currentName;
  if (previous && state.rt) state.rt.release(previous);
  state.currentName = name;
  state.currentIndex = state.images.indexOf(image);
  loadHivesFromImage(image);
  state.zoom = 1;
  state.undo = [];
  state.redo = [];
  state.dirty = false;
  state.remote = null;
  state.holder = null;
  state.lockState = "none";
  el.cursor.hidden = true;
  history.replaceState(null, "", `#${encodeURIComponent(name)}`);
  el.image.hidden = true;
  el.svg.setAttribute("hidden", "");
  el.empty.hidden = false;
  $("current-name").textContent = name;
  const source = state.sources[name];
  const credit = source ? (source.url ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">${escapeHtml(source.owner)}</a>` : escapeHtml(source.owner)) : "";
  $("image-meta").innerHTML = `${image.width} × ${image.height} פיקסלים${source ? ` · מקור: ${credit}${source.note ? ` — ${escapeHtml(source.note)}` : ""}` : ""}`;
  el.image.onload = () => {
    el.empty.hidden = true;
    el.image.hidden = false;
    el.svg.removeAttribute("hidden");
    el.svg.setAttribute("viewBox", `0 0 ${image.width} ${image.height}`);
    fitImage();
    renderEditor();
  };
  el.image.onerror = () => toast("לא הצלחתי לפתוח את קובץ התמונה.", "error");
  el.image.src = image.src;
  renderImageList();
  renderEditor();
  if (state.rt) {
    state.rt.setActivity({ image: name, mode: "view" });
    attemptEdit(name);
  }
}

// ---------------------------------------------------------------------------
// Editor helpers
// ---------------------------------------------------------------------------

const hive = () => state.hives?.[state.selectedHive];
const lineDone = (h, line) => Boolean(h.points[line * 2] && h.points[line * 2 + 1]);
const completeLines = (h) => state.model.lines.filter((_, i) => lineDone(h, i)).length;

function firstIncompleteLine(h, after = -1) {
  const start = after >= 0 ? LINE_ORDER.indexOf(after) + 1 : 0;
  for (let k = 0; k < LINE_ORDER.length; k++) {
    const line = LINE_ORDER[(start + k) % LINE_ORDER.length];
    if (!lineDone(h, line)) return line;
  }
  return null;
}

function cornerState(h, id) {
  const list = state.model.cornerPoints[id];
  const filled = list.filter((p) => h.points[p]).length;
  return filled === list.length ? "done" : filled ? "partial" : "empty";
}

function activeCorner(h = hive()) {
  if (!h) return null;
  return state.model.cornerOrder.find((id) => cornerState(h, id) !== "done" && !state.skipped.has(id)) || null;
}

function remember() {
  state.undo.push(JSON.stringify(state.hives));
  if (state.undo.length > 80) state.undo.shift();
  state.redo = [];
}

function changed() {
  state.dirty = true;
  state.lastActivity = Date.now();
  renderEditor();
  publishLive();
}

function canEdit() {
  if (!state.readOnly) return true;
  if (state.lockState === "other") toast(`${state.holder?.name || "מישהו"} עובד/ת על התמונה הזאת — צפייה בלבד.`, "error");
  else if (state.lockState === "acquiring") toast("רגע, בודק נעילה…");
  else toast("התמונה בצפייה בלבד. לחצו ״התחילו לעבוד״ בפס העליון.");
  return false;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderStaticPanel() {
  const rules = state.model.rules.map((rule) => `<li>${escapeHtml(rule)}</li>`).join("");
  $("rules-list").innerHTML = rules;
  $("guide-rules").innerHTML = rules;
}

function renderEditor() {
  const image = currentImage();
  const h = hive();
  if (!image || !h) return;
  document.body.classList.toggle("read-only", state.readOnly);
  renderHiveTabs();
  renderMetadata();
  renderTask();
  renderLineGroups();
  renderCanvas();
  renderProgress();
  renderChecks();
  sync3D();
  $("save-button").disabled = state.readOnly || !state.hives.every((item) => item.points.every(Boolean));
  $("add-hive").disabled = state.readOnly || state.hives.length >= 4;
  $("remove-hive").disabled = state.readOnly || state.hives.length <= 1;
  $("undo-button").disabled = state.readOnly || !state.undo.length;
  $("redo-button").disabled = state.readOnly || !state.redo.length;
  $("autocomplete-button").disabled = state.readOnly || h.points.every(Boolean);
  if ($("detect-button")) $("detect-button").disabled = state.readOnly;
  $("prev-image").disabled = state.currentIndex <= 0;
  $("next-image").disabled = state.currentIndex >= state.images.length - 1;
  document.querySelectorAll(".mode-switch button").forEach((button) => button.classList.toggle("active", button.dataset.mode === state.mode));
}

function renderHiveTabs() {
  $("hive-tabs").innerHTML = state.hives.map((item, index) => {
    const color = item.metadata[0] ? "blue" : "red";
    return `<button class="hive-tab ${index === state.selectedHive ? "active" : ""} ${color}" data-hive="${index}" role="tab" aria-selected="${index === state.selectedHive}">
      <i></i>כוורת ${index + 1}<small>${completeLines(item)}/14</small></button>`;
  }).join("");
  $("hive-tabs").querySelectorAll(".hive-tab").forEach((button) => button.addEventListener("click", () => {
    state.selectedHive = Number(button.dataset.hive);
    const h = hive();
    state.mode = h.points.every(Boolean) ? "lines" : state.mode;
    state.selectedLine = firstIncompleteLine(h) ?? 0;
    state.skipped = new Set();
    renderEditor();
    publishLive();
  }));
}

function renderMetadata() {
  const metadata = hive().metadata;
  $("hive-color").value = metadata[0] ? "blue" : "red";
  $("hive-side").value = metadata[1] ? "right" : "left";
  $("hive-level").value = metadata[2] ? "top" : "bottom";
  ["hive-color", "hive-side", "hive-level"].forEach((id) => { $(id).disabled = state.readOnly; });
}

function renderTask() {
  const h = hive();
  const card = $("task-card");
  const model = state.model;
  if (state.mode === "corners") {
    const id = activeCorner(h);
    const doneCorners = model.cornerOrder.filter((cid) => cornerState(h, cid) === "done").length;
    if (!id) {
      const remaining = model.cornerOrder.filter((cid) => cornerState(h, cid) !== "done");
      card.innerHTML = remaining.length
        ? `<div class="task-kicker">דילגת על ${remaining.length} פינות</div>
           <h4>הפינות המוסתרות עדיין ריקות</h4>
           <p>לחצו על <b>השלמה חכמה</b>: האתר מחשב אותן לפי הצורה התלת־ממדית של הכוורת. אפשר גם ללחוץ על פינה ברשימה כדי לסמן אותה ידנית.</p>
           <div class="corner-chips">${remaining.map((cid) => `<button class="chip" data-corner="${cid}">${escapeHtml(cornerLabel(model, cid))}</button>`).join("")}</div>`
        : `<div class="task-kicker done">✓ כל 10 הפינות סומנו</div>
           <h4>כל 28 הנקודות מולאו</h4>
           <p>עברו למצב <b>קווים</b> כדי לבדוק כל קו, או שמרו טיוטה.</p>`;
    } else {
      const info = model.cornerInfo[id];
      const lines = [...new Set(model.cornerPoints[id].map((p) => (p >> 1) + 1))].map(pad2).join(" · ");
      card.innerHTML = `<div class="task-kicker">פינה ${doneCorners + 1} מתוך 10 · מסגרת ${escapeHtml(info.frame)}</div>
        <h4><span class="corner-dot"></span>לחצו על: ${escapeHtml(info.name)}</h4>
        <p>${escapeHtml(info.hint || "")}${info.hint ? ". " : ""}נקודה אחת כאן ממלאת את הקצוות של קווים ${lines}.</p>
        <div class="task-actions">
          <label class="toggle"><input type="checkbox" id="mark-hidden" ${state.markHidden ? "checked" : ""}> הפינה מוסתרת — אני מעריך/ה</label>
          <button class="ghost-button" id="skip-corner" title="קיצור: S">דלג ←</button>
        </div>`;
    }
    card.querySelectorAll("[data-corner]").forEach((button) => button.addEventListener("click", () => {
      state.skipped.delete(button.dataset.corner);
      renderEditor();
    }));
    $("skip-corner")?.addEventListener("click", skipCorner);
    $("mark-hidden")?.addEventListener("change", (event) => { state.markHidden = event.target.checked; });
    return;
  }
  const lineIndex = state.selectedLine;
  const line = model.lines[lineIndex];
  const a = h.points[lineIndex * 2];
  const b = h.points[lineIndex * 2 + 1];
  const estimated = h.est[lineIndex * 2] || h.est[lineIndex * 2 + 1];
  const next = !a ? "A" : !b ? "B" : null;
  card.innerHTML = `<div class="task-kicker kind-${line.kind}">קו ${pad2(line.id)} · ${escapeHtml(model.groups.find((g) => g.key === line.group).name)}</div>
    <h4>${escapeHtml(line.name)}</h4>
    <p>${escapeHtml(line.explanation)}</p>
    <div class="endpoints">
      <div class="endpoint ${a ? "set" : ""} ${next === "A" ? "next" : ""}"><b>A</b><span>${escapeHtml(endpointLabel(model, lineIndex, 0))}</span></div>
      <div class="endpoint ${b ? "set" : ""} ${next === "B" ? "next" : ""}"><b>B</b><span>${escapeHtml(endpointLabel(model, lineIndex, 1))}</span></div>
    </div>
    <div class="task-actions">
      <label class="toggle"><input type="checkbox" id="line-hidden" ${estimated ? "checked" : ""} ${state.readOnly ? "disabled" : ""}> מוסתר — הערכה</label>
      <button class="ghost-button" id="swap-line" ${a && b && !state.readOnly ? "" : "disabled"} title="קיצור: X">⇄ החלף A/B</button>
      <button class="ghost-button danger" id="clear-line" ${(a || b) && !state.readOnly ? "" : "disabled"} title="קיצור: Delete">נקה קו</button>
    </div>
    ${next ? `<div class="task-next">עכשיו לחצו על התמונה בנקודה <b>${next}</b>${state.readOnly ? "" : " · החזיקו Alt כדי לבטל הצמדה"}</div>` : ""}`;
  $("swap-line").addEventListener("click", swapSelectedLine);
  $("clear-line").addEventListener("click", clearSelectedLine);
  $("line-hidden").addEventListener("change", (event) => {
    if (!canEdit()) return;
    remember();
    h.est[lineIndex * 2] = event.target.checked;
    h.est[lineIndex * 2 + 1] = event.target.checked;
    changed();
  });
}

function renderLineGroups() {
  const h = hive();
  const model = state.model;
  $("line-groups").innerHTML = model.groups.map((group) => `
    <div class="line-group">
      <div class="line-group-title"><span class="group-mark ${group.key}"></span><strong>${escapeHtml(group.name)}</strong><small>${escapeHtml(group.note)}</small></div>
      ${group.lineIndexes.map((index) => {
        const line = model.lines[index];
        const a = Boolean(h.points[index * 2]);
        const b = Boolean(h.points[index * 2 + 1]);
        const est = h.est[index * 2] || h.est[index * 2 + 1];
        const selected = state.mode === "lines" && index === state.selectedLine;
        return `<button class="line-row kind-${line.kind} ${selected ? "selected" : ""} ${a && b ? "done" : ""}" data-line="${index}">
          <span class="line-number">${pad2(line.id)}</span>
          <span class="line-copy"><strong>${escapeHtml(line.name)}</strong><small>${a && b ? (est ? "סומן · הערכה" : "שתי הנקודות סומנו") : a || b ? "חסרה נקודה אחת" : "ממתין"}</small></span>
          <span class="line-check"><i class="${a ? "filled" : ""}">A</i><i class="${b ? "filled" : ""}">B</i></span>
        </button>`;
      }).join("")}
    </div>`).join("");
  $("line-groups").querySelectorAll(".line-row").forEach((row) => row.addEventListener("click", () => selectLine(Number(row.dataset.line))));
}

function renderProgress() {
  const h = hive();
  const filled = h.points.filter(Boolean).length;
  $("progress-count").textContent = `${completeLines(h)}/14 קווים · ${filled}/28 נקודות`;
  $("progress-text").textContent = `כוורת ${state.selectedHive + 1} מתוך ${state.hives.length} · תמונה ${state.currentIndex + 1} מתוך ${state.images.length}`;
  $("progress-bar").style.width = `${(filled / 28) * 100}%`;
}

let checkTimer = null;
function renderChecks() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(() => {
    const h = hive();
    if (!h) return;
    const report = checkHive(state.model, h.points);
    const card = $("check-card");
    const filled = report.filled;
    if (filled < 4) {
      card.innerHTML = '<div class="check-head neutral">בדיקת דיוק<small>תופיע אחרי שתסמנו כמה פינות</small></div>';
      return;
    }
    const fitText = report.fit ? `התאמה למודל התלת־ממדי: ±${report.fit.rms.toFixed(1)} פיקסלים` : "התאמה למודל: צריך לפחות 6 פינות משתי המסגרות";
    if (!report.issues.length) {
      card.innerHTML = `<div class="check-head ok">✓ הסימון עקבי<small>${fitText}</small></div>`;
      return;
    }
    card.innerHTML = `<div class="check-head warn">⚠ ${report.issues.length} דברים לבדוק<small>${fitText}</small></div>
      <ul class="check-list">${report.issues.slice(0, 6).map((issue) => `<li><button data-line="${issue.lines?.[0] ?? ""}">${escapeHtml(issue.text)}</button></li>`).join("")}</ul>`;
    card.querySelectorAll("[data-line]").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.line !== "") selectLine(Number(button.dataset.line));
    }));
  }, 120);
}

function screenScale() {
  const image = currentImage();
  const rect = el.svg.getBoundingClientRect();
  return image && rect.width ? image.width / rect.width : 1;
}

function svgNode(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
}

function renderCanvas() {
  const image = currentImage();
  if (!image || !state.hives) return;
  const s = screenScale();
  const model = state.model;
  el.svg.replaceChildren();
  const layerLines = svgNode("g", {}, el.svg);
  const layerHandles = svgNode("g", {}, el.svg);
  const corner = state.mode === "corners" ? activeCorner() : null;
  const activePoints = new Set(corner ? model.cornerPoints[corner] : []);
  state.hives.forEach((h, hiveIndex) => {
    const isCurrent = hiveIndex === state.selectedHive;
    for (let line = 0; line < 14; line++) {
      const a = h.points[line * 2];
      const b = h.points[line * 2 + 1];
      const kind = model.lines[line].kind;
      const selected = isCurrent && state.mode === "lines" && line === state.selectedLine;
      if (a && b) {
        const est = h.est[line * 2] || h.est[line * 2 + 1];
        svgNode("line", {
          x1: a[0], y1: a[1], x2: b[0], y2: b[1],
          class: `ann-line kind-${kind} ${selected ? "selected" : ""} ${isCurrent ? "" : "dim"} ${est ? "est" : ""}`,
          "stroke-width": (selected ? 4.2 : 2.6) * s,
          "stroke-dasharray": est ? `${8 * s} ${6 * s}` : "",
          "data-line": line, "data-hive": hiveIndex,
        }, layerLines);
        if (isCurrent) {
          const label = svgNode("text", {
            x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2 - 8 * s, class: `ann-label ${selected ? "selected" : ""}`,
            "font-size": 13 * s, "stroke-width": 3.5 * s, "text-anchor": "middle",
          }, layerLines);
          label.textContent = hiveIndex ? `${hiveIndex + 1}.${line + 1}` : pad2(line + 1);
        }
      }
    }
    if (!isCurrent) return;
    const drawn = new Map();
    h.points.forEach((point, index) => {
      if (!point) return;
      const key = `${point[0]},${point[1]}`;
      const line = index >> 1;
      const selectedEnd = state.mode === "lines" && line === state.selectedLine;
      if (drawn.has(key) && !selectedEnd) return;
      drawn.set(key, index);
      const r = (selectedEnd ? 7 : 5.5) * s;
      svgNode("circle", {
        cx: point[0], cy: point[1], r, class: `handle ${selectedEnd ? "selected" : ""} ${activePoints.has(index) ? "active" : ""} ${h.est[index] ? "est" : ""}`,
        "stroke-width": 2 * s, "data-point": index,
      }, layerHandles);
      if (selectedEnd) {
        const tag = svgNode("text", {
          x: point[0] + 9 * s, y: point[1] - 9 * s, class: "handle-tag", "font-size": 12 * s, "stroke-width": 3 * s,
        }, layerHandles);
        tag.textContent = index & 1 ? "B" : "A";
      }
    });
  });
  // Rubber band from A to the cursor while placing B.
  const h = hive();
  if (!state.readOnly && state.mode === "lines" && state.hover && h) {
    const a = h.points[state.selectedLine * 2];
    const b = h.points[state.selectedLine * 2 + 1];
    if (a && !b) {
      svgNode("line", { x1: a[0], y1: a[1], x2: state.hover[0], y2: state.hover[1], class: "rubber", "stroke-width": 2 * s, "stroke-dasharray": `${6 * s} ${5 * s}` }, layerLines);
    }
  }
}

// ---------------------------------------------------------------------------
// Interaction on the image
// ---------------------------------------------------------------------------

function imagePoint(event) {
  const image = currentImage();
  const rect = el.svg.getBoundingClientRect();
  return [
    Math.min(image.width - 1, Math.max(0, Math.round(((event.clientX - rect.left) * image.width) / rect.width))),
    Math.min(image.height - 1, Math.max(0, Math.round(((event.clientY - rect.top) * image.height) / rect.height))),
  ];
}

function linkedPoints(h, index, alone) {
  if (alone) return [index];
  const origin = h.points[index];
  const corner = state.model.pointCorner[index];
  return h.points.map((p, i) => i).filter((i) =>
    h.points[i] && state.model.pointCorner[i] === corner && Math.hypot(h.points[i][0] - origin[0], h.points[i][1] - origin[1]) < 1.01);
}

function nearestHandle(point, radiusPx) {
  const h = hive();
  const s = screenScale();
  let best = null;
  h.points.forEach((p, i) => {
    if (!p) return;
    const d = Math.hypot(p[0] - point[0], p[1] - point[1]) / s;
    if (d <= radiusPx && (!best || d < best.d)) best = { i, d };
  });
  return best ? best.i : null;
}

function nearestLine(point, radiusPx) {
  const h = hive();
  const s = screenScale();
  let best = null;
  for (let line = 0; line < 14; line++) {
    const a = h.points[line * 2];
    const b = h.points[line * 2 + 1];
    if (!a || !b) continue;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
    const d = Math.hypot(a[0] + t * dx - point[0], a[1] + t * dy - point[1]) / s;
    if (d <= radiusPx && (!best || d < best.d)) best = { line, d };
  }
  return best ? best.line : null;
}

el.svg.addEventListener("pointerdown", (event) => {
  if (!currentImage() || event.button > 0) return;
  event.preventDefault();
  const handle = event.target.closest?.("[data-point]");
  state.gesture = {
    id: event.pointerId,
    start: [event.clientX, event.clientY],
    point: imagePoint(event),
    handle: handle ? Number(handle.dataset.point) : null,
    alt: event.altKey,
    dragging: false,
    panning: false,
    scroll: [el.canvas.scrollLeft, el.canvas.scrollTop],
  };
  el.svg.setPointerCapture(event.pointerId);
});

el.svg.addEventListener("pointermove", (event) => {
  const image = currentImage();
  if (!image) return;
  const point = imagePoint(event);
  state.hover = point;
  if (!state.readOnly) state.rt?.publishCursor(state.currentName, point);
  const gesture = state.gesture;
  if (gesture && gesture.id === event.pointerId) {
    const moved = Math.hypot(event.clientX - gesture.start[0], event.clientY - gesture.start[1]);
    if (!gesture.dragging && !gesture.panning && moved > 4) {
      if (gesture.handle !== null && !state.readOnly) {
        remember();
        gesture.dragging = true;
        gesture.indexes = linkedPoints(hive(), gesture.handle, gesture.alt);
        gesture.origin = [...hive().points[gesture.handle]];
      } else if (el.canvas.scrollWidth > el.canvas.clientWidth || el.canvas.scrollHeight > el.canvas.clientHeight) {
        gesture.panning = true;
      }
    }
    if (gesture.dragging) {
      const h = hive();
      const dx = point[0] - gesture.point[0];
      const dy = point[1] - gesture.point[1];
      const target = [
        Math.min(image.width - 1, Math.max(0, gesture.origin[0] + dx)),
        Math.min(image.height - 1, Math.max(0, gesture.origin[1] + dy)),
      ];
      gesture.indexes.forEach((i) => { h.points[i] = [...target]; });
      state.dirty = true;
      renderCanvas();
      drawLoupe(event, target);
      publishLive();
      return;
    }
    if (gesture.panning) {
      el.canvas.scrollLeft = gesture.scroll[0] - (event.clientX - gesture.start[0]);
      el.canvas.scrollTop = gesture.scroll[1] - (event.clientY - gesture.start[1]);
      return;
    }
  }
  const placing = !state.readOnly && ((state.mode === "corners" && activeCorner()) || (state.mode === "lines" && !lineDone(hive(), state.selectedLine)));
  if (placing) drawLoupe(event, point); else el.loupe.hidden = true;
  if (state.mode === "lines") renderCanvas();
  el.svg.style.cursor = state.readOnly ? "default" : event.target.closest?.("[data-point]") ? "grab" : placing ? "crosshair" : "default";
});

el.svg.addEventListener("pointerleave", () => {
  el.loupe.hidden = true;
  state.hover = null;
  state.rt?.publishCursor(state.currentName, null);
  if (state.mode === "lines") renderCanvas();
});

function endGesture(event) {
  const gesture = state.gesture;
  if (!gesture || gesture.id !== event.pointerId) return;
  state.gesture = null;
  if (gesture.dragging) {
    el.loupe.hidden = true;
    changed();
    return;
  }
  if (gesture.panning || event.type === "pointercancel") return;
  handleClick(gesture.point, gesture.handle, gesture.alt);
}
el.svg.addEventListener("pointerup", endGesture);
el.svg.addEventListener("pointercancel", endGesture);

function handleClick(point, handleIndex, alt) {
  const h = hive();
  if (!h) return;
  state.lastActivity = Date.now();
  if (state.mode === "corners") {
    const id = activeCorner(h);
    if (!id) {
      const line = nearestLine(point, 10);
      if (line !== null) selectLine(line);
      return;
    }
    if (!canEdit()) return;
    remember();
    for (const p of state.model.cornerPoints[id]) {
      if (!h.points[p]) { h.points[p] = [...point]; h.est[p] = state.markHidden; }
    }
    state.markHidden = false;
    const next = activeCorner(h);
    if (!next) {
      const missing = h.points.filter((p) => !p).length;
      toast(missing ? "סיימתם את הפינות הגלויות. לחצו ״השלמה חכמה״ למוסתרות." : "כל 10 הפינות סומנו! עברו על הקווים ושמרו.", "success");
      if (!missing) state.mode = "lines";
    }
    changed();
    return;
  }
  const line = state.selectedLine;
  if (lineDone(h, line)) {
    const near = nearestLine(point, 10);
    if (near !== null && near !== line) selectLine(near);
    else if (!state.readOnly) toast("הקו הזה כבר סומן. גררו נקודה כדי לתקן, או בחרו קו אחר.");
    return;
  }
  if (!canEdit()) return;
  let target = point;
  if (!alt) {
    const snap = handleIndex ?? nearestHandle(point, 11);
    if (snap !== null && snap !== line * 2 && snap !== line * 2 + 1) target = [...h.points[snap]];
  }
  remember();
  const slot = h.points[line * 2] ? line * 2 + 1 : line * 2;
  h.points[slot] = [...target];
  if (slot === line * 2 + 1) {
    const fixed = fixSwappedEndpoints(state.model, h.points);
    if (fixed.swapped.includes(line)) {
      h.points = fixed.points;
      toast(`הפכתי את A ו־B בקו ${pad2(line + 1)} כדי שיתאימו לפינות.`, "success");
    }
    const next = firstIncompleteLine(h, line);
    if (next !== null) state.selectedLine = next;
    else toast("כל 14 הקווים סומנו! בדקו את בדיקת הדיוק ושמרו.", "success");
  }
  changed();
}

function drawLoupe(event, point) {
  const image = currentImage();
  if (!image || !el.image.complete) return;
  const size = 168;
  const stage = $("image-stage").getBoundingClientRect();
  const s = screenScale();
  const magnify = 3.4;
  const span = (size * s) / magnify;
  const ctx = el.loupe.getContext("2d");
  el.loupe.width = size * (window.devicePixelRatio || 1);
  el.loupe.height = size * (window.devicePixelRatio || 1);
  ctx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#0f172a";
  ctx.fillRect(0, 0, size, size);
  ctx.drawImage(el.image, point[0] - span / 2, point[1] - span / 2, span, span, 0, 0, size, size);
  const h = hive();
  ctx.lineWidth = 2;
  h.points.forEach((p) => {
    if (!p) return;
    const x = ((p[0] - point[0]) / span) * size + size / 2;
    const y = ((p[1] - point[1]) / span) * size + size / 2;
    if (x < -10 || y < -10 || x > size + 10 || y > size + 10) return;
    ctx.strokeStyle = "#fff";
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.stroke();
  });
  ctx.strokeStyle = "rgba(250,204,21,0.95)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(size / 2, 0); ctx.lineTo(size / 2, size / 2 - 7);
  ctx.moveTo(size / 2, size / 2 + 7); ctx.lineTo(size / 2, size);
  ctx.moveTo(0, size / 2); ctx.lineTo(size / 2 - 7, size / 2);
  ctx.moveTo(size / 2 + 7, size / 2); ctx.lineTo(size, size / 2);
  ctx.stroke();
  let left = event.clientX - stage.left + 26;
  let top = event.clientY - stage.top - size - 26;
  if (left + size > stage.width - 8) left = event.clientX - stage.left - size - 26;
  if (top < 8) top = event.clientY - stage.top + 26;
  el.loupe.style.left = `${left}px`;
  el.loupe.style.top = `${top}px`;
  el.loupe.hidden = false;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

function selectLine(index) {
  state.mode = "lines";
  state.selectedLine = index;
  renderEditor();
  publishLive();
}

function skipCorner() {
  const id = activeCorner();
  if (!id) return;
  state.skipped.add(id);
  toast(`דילגתי על ${cornerLabel(state.model, id)}. בסוף לחצו ״השלמה חכמה״.`);
  renderEditor();
}

function swapSelectedLine() {
  if (!canEdit()) return;
  const h = hive();
  const line = state.selectedLine;
  if (!lineDone(h, line)) return;
  remember();
  [h.points[line * 2], h.points[line * 2 + 1]] = [h.points[line * 2 + 1], h.points[line * 2]];
  [h.est[line * 2], h.est[line * 2 + 1]] = [h.est[line * 2 + 1], h.est[line * 2]];
  changed();
}

function clearSelectedLine() {
  if (!canEdit()) return;
  const h = hive();
  const line = state.selectedLine;
  remember();
  h.points[line * 2] = null;
  h.points[line * 2 + 1] = null;
  h.est[line * 2] = false;
  h.est[line * 2 + 1] = false;
  changed();
  toast(`קו ${pad2(line + 1)} נוקה — סמנו A ואז B.`);
}

function runAutoComplete() {
  if (!canEdit()) return;
  const image = currentImage();
  const h = hive();
  const result = autoComplete(state.model, h.points, image);
  if (!result.ok) {
    toast(result.reason === "planar"
      ? "צריך פינות משתי המסגרות (לפחות 2 מהקדמית ו־2 מהאחורית), ולא רק מקיר אחד."
      : `צריך לפחות 6 פינות שונות כדי לחשב. סימנתם ${result.count}.`, "error");
    return;
  }
  if (!result.filled.length) { toast("אין נקודות חסרות להשלמה."); return; }
  remember();
  h.points = result.points;
  result.filled.forEach((i) => { h.est[i] = true; });
  state.skipped = new Set();
  toast(`השלמתי ${result.filled.length} נקודות לפי המודל התלת־ממדי (סטייה ממוצעת ±${result.rms.toFixed(1)} פיקסלים). הקווים המשוערים מקווקווים — בדקו אותם.`, "success");
  if (h.points.every(Boolean)) state.mode = "lines";
  changed();
}

function addHive() {
  if (!canEdit() || state.hives.length >= 4) return;
  remember();
  state.hives.push(freshHive(hive().metadata));
  state.selectedHive = state.hives.length - 1;
  state.mode = "corners";
  state.skipped = new Set();
  changed();
  toast("נוספה כוורת. סמנו את 10 הפינות שלה.");
}

function removeHive() {
  if (!canEdit() || state.hives.length <= 1) return;
  if (!confirm(`להסיר את כוורת ${state.selectedHive + 1} מהתמונה?`)) return;
  remember();
  state.hives.splice(state.selectedHive, 1);
  state.selectedHive = Math.min(state.selectedHive, state.hives.length - 1);
  changed();
}

function undo() {
  if (state.readOnly || !state.undo.length) return;
  state.redo.push(JSON.stringify(state.hives));
  state.hives = JSON.parse(state.undo.pop());
  state.selectedHive = Math.min(state.selectedHive, state.hives.length - 1);
  changed();
}

function redo() {
  if (state.readOnly || !state.redo.length) return;
  state.undo.push(JSON.stringify(state.hives));
  state.hives = JSON.parse(state.redo.pop());
  changed();
}

async function saveDraft() {
  const image = currentImage();
  if (!image || !canEdit()) return;
  if (!state.hives.every((h) => h.points.every(Boolean))) {
    toast("צריך את כל 28 הנקודות בכל כוורת לפני שמירה. השתמשו ב״השלמה חכמה״ לנקודות מוסתרות.", "error");
    return;
  }
  const swappedText = [];
  state.hives.forEach((h, index) => {
    const fixed = fixSwappedEndpoints(state.model, h.points);
    if (fixed.swapped.length) {
      h.points = fixed.points;
      swappedText.push(`כוורת ${index + 1}: ${fixed.swapped.map((l) => pad2(l + 1)).join(", ")}`);
    }
  });
  const issues = state.hives.flatMap((h) => checkHive(state.model, h.points).issues);
  if (issues.length && !confirm(`בדיקת הדיוק מצאה ${issues.length} דברים לבדוק:\n\n${issues.slice(0, 5).map((i) => `• ${i.text}`).join("\n")}\n\nלשמור בכל זאת?`)) {
    renderEditor();
    return;
  }
  const record = hivesToRecord(image, state.hives);
  const drafts = localDrafts();
  drafts[image.name] = record;
  if (!store.set(DRAFTS_KEY, drafts)) {
    toast("לא הצלחתי לשמור בדפדפן (האחסון מלא או חסום). ייצאו את הטיוטות לקובץ.", "error");
  }
  image.status = "review";
  image.hives = JSON.parse(JSON.stringify(state.hives));
  image.savedAt = Date.parse(record.saved_at);
  state.dirty = false;
  const synced = state.rt?.publishDraft(image.name, { record });
  renderImageList();
  renderEditor();
  toast(`${swappedText.length ? `תיקנתי סדר A/B (${swappedText.join(" · ")}). ` : ""}הטיוטה נשמרה${synced ? " וסונכרנה לכל המכשירים" : " בדפדפן"}.`, "success");
}

// ---------------------------------------------------------------------------
// Zoom
// ---------------------------------------------------------------------------

function fitImage(anchor) {
  const image = currentImage();
  if (!image) return;
  const maxWidth = Math.max(200, el.canvas.clientWidth - 24);
  const maxHeight = Math.max(200, el.canvas.clientHeight - 20);
  const fit = Math.min(maxWidth / image.width, maxHeight / image.height, 1);
  const before = el.wrap.getBoundingClientRect();
  el.wrap.style.width = `${Math.max(1, image.width * fit * state.zoom)}px`;
  $("zoom-level").textContent = state.zoom === 1 ? "התאם" : `${Math.round(state.zoom * 100)}%`;
  if (anchor && before.width) {
    const after = el.wrap.getBoundingClientRect();
    const ratio = after.width / before.width;
    el.canvas.scrollLeft += (anchor[0] - before.left) * (ratio - 1);
    el.canvas.scrollTop += (anchor[1] - before.top) * (ratio - 1);
  }
  renderCanvas();
}

function setZoom(value, anchor) {
  state.zoom = Math.min(6, Math.max(0.5, Math.round(value * 100) / 100));
  fitImage(anchor);
}

el.canvas.addEventListener("wheel", (event) => {
  if (!event.ctrlKey && !event.metaKey) return;
  event.preventDefault();
  setZoom(state.zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15), [event.clientX, event.clientY]);
}, { passive: false });
$("zoom-in").addEventListener("click", () => setZoom(state.zoom * 1.25));
$("zoom-out").addEventListener("click", () => setZoom(state.zoom / 1.25));
$("fit-button").addEventListener("click", () => setZoom(1));
window.addEventListener("resize", () => fitImage());

// ---------------------------------------------------------------------------
// 3D viewer
// ---------------------------------------------------------------------------

async function initViewer() {
  try {
    const { createHiveViewer } = await import("./hive3d.js");
    state.viewer = createHiveViewer($("viewer3d"), state.model, {
      compact: true,
      onSelect: (index) => selectLine(index),
    });
    state.viewer.showDimensions(Boolean(state.prefs.dims));
    $("dims-toggle").classList.toggle("active", Boolean(state.prefs.dims));
    sync3D();
  } catch (error) {
    console.error(error);
    $("viewer3d").innerHTML = '<div class="viewer-error">המודל התלת־ממדי לא נטען (אין חיבור ל־CDN?). העורך עובד כרגיל.</div>';
  }
}

function sync3D() {
  const viewer = state.viewer;
  const h = hive();
  if (!viewer || !h) return;
  viewer.setFrameColor(h.metadata[0] ? "blue" : "red");
  viewer.setLineStates(state.model.lines.map((_, i) => (lineDone(h, i) ? "done" : h.points[i * 2] || h.points[i * 2 + 1] ? "partial" : "empty")));
  viewer.setMode(state.mode);
  if (state.mode === "corners") {
    const states = Object.fromEntries(state.model.cornerOrder.map((id) => [id, cornerState(h, id)]));
    viewer.setSelectedLine(-1, { fly: false });
    viewer.setCorners(states, activeCorner(h));
  } else {
    viewer.setCorners({}, null, { fly: false });
    viewer.setSelectedLine(state.selectedLine);
  }
}

$("view-reset").addEventListener("click", () => state.viewer?.resetView());
$("dims-toggle").addEventListener("click", () => {
  state.prefs.dims = !state.prefs.dims;
  store.set(PREFS_KEY, state.prefs);
  state.viewer?.showDimensions(state.prefs.dims);
  $("dims-toggle").classList.toggle("active", state.prefs.dims);
});

// ---------------------------------------------------------------------------
// Tour: full-screen 3D demonstration of every part and every line
// ---------------------------------------------------------------------------

const TOUR_PARTS = [
  { title: "כוורת אחת — CELL", text: "הכוורת היא תא שקוף בצורת בית: מנסרה מחומשת. הפתח ברוחב 20″ (50.8 ס״מ) ובגובה 14″, והעומק 12.04″. גררו כדי לסובב וגללו כדי להתקרב.", view: [28, 20], dims: true, overlay: false, rotate: true },
  { title: "המסגרת הקדמית", text: "מסגרת אדומה או כחולה בצורת בית (Goal Rib). בתיוג, ״קדמית״ היא המסגרת הקרובה יותר למצלמה בתמונה.", parts: ["frontRib"], view: [14, 10] },
  { title: "המסגרת האחורית", text: "אותה צורה בדיוק, בצד הרחוק מהמצלמה. רואים אותה דרך הפלסטיק השקוף, ולכן קל לבלבל בינה לבין הקדמית.", parts: ["backRib"], view: [48, 16] },
  { title: "הגג", text: "שני שיפועים שנפגשים בשיא. הקירות בגובה 7.61″, והשיא ב־14″ מעל הרצפה.", parts: ["roof"], view: [20, 46], dims: true },
  { title: "קירות ורצפה", text: "היריעה התחתונה מתכופפת לצורת U: קיר שמאלי, רצפה וקיר ימני.", parts: ["walls", "floor"], view: [62, 8] },
  { title: "מוטות הברזל (Churro)", text: "שני מוטות מתכת משושים בפינות הגג, לאורך הכוורת. עליהם אזיקונים בצבע הכוורת. אלה קווים 13 ו־14.", parts: ["churros"], view: [70, 24] },
  { title: "צינור הבסיס", text: "צינור מרובע בתחתית שמחבר את התא לזרוע ולציר. הוא בולט מהצד של הדופן האחורית.", parts: ["baseTube"], view: [128, -8] },
  { title: "תגי AprilTag", text: "מדבקה עם 4 תגים מתחת לרצפה, שפונה למטה אל המגרש. הרובוט משתמש בה כדי לזהות את התא.", parts: ["tags"], view: [18, -48] },
  { title: "הדופן האחורית", text: "פלטה שקופה שסוגרת את הצד האחורי של התא. בצד הפתוח (הפתח) נכנסים הכדורים.", parts: ["backSkin"], view: [150, 14] },
  { title: "פירוק לחלקים", text: "כל החלקים יחד, במבט מפורק. מכאן עוברים ל־14 הקווים שמתייגים.", explode: 1, view: [36, 22] },
];

function tourSteps() {
  const lineSteps = state.model.lines.map((line, index) => ({
    title: `קו ${pad2(line.id)} · ${line.name}`,
    text: line.explanation,
    line: index,
    a: endpointLabel(state.model, index, 0),
    b: endpointLabel(state.model, index, 1),
  }));
  return [...TOUR_PARTS, { title: "14 הקווים", text: "10 קווים על שתי המסגרות (5 לכל מסגרת) ועוד 4 לאורך הכוורת. לכל קו יש קצה A וקצה B קבועים. לחצו על קו כדי לראות אותו.", overlay: true, view: [30, 22] }, ...lineSteps];
}

async function openTour(startAt = 0) {
  const overlay = $("tour");
  overlay.hidden = false;
  document.body.classList.add("tour-open");
  if (!state.tour) {
    try {
      const { createHiveViewer } = await import("./hive3d.js");
      const viewer = createHiveViewer($("tour-viewer"), state.model, {
        onSelect: (index) => goTourStep(tourSteps().findIndex((step) => step.line === index)),
      });
      state.tour = { viewer, index: 0, playing: false, timer: null };
    } catch (error) {
      $("tour-viewer").innerHTML = '<div class="viewer-error">המודל התלת־ממדי לא נטען.</div>';
      return;
    }
  }
  const h = hive();
  state.tour.viewer.setFrameColor(h?.metadata[0] ? "blue" : "red");
  renderTourSteps();
  goTourStep(startAt);
}

function closeTour() {
  $("tour").hidden = true;
  document.body.classList.remove("tour-open");
  if (state.tour) { state.tour.playing = false; clearTimeout(state.tour.timer); }
  if (location.hash === "#tour") history.replaceState(null, "", state.currentName ? `#${encodeURIComponent(state.currentName)}` : "#");
}

function renderTourSteps() {
  const steps = tourSteps();
  $("tour-steps").innerHTML = steps.map((step, index) => `<button class="tour-step ${step.line !== undefined ? "line-step" : ""}" data-step="${index}">${step.line !== undefined ? `<b>${pad2(step.line + 1)}</b>` : `<i>${index + 1}</i>`}<span>${escapeHtml(step.line !== undefined ? state.model.lines[step.line].name : step.title)}</span></button>`).join("");
  $("tour-steps").querySelectorAll("[data-step]").forEach((button) => button.addEventListener("click", () => {
    stopTourPlay();
    goTourStep(Number(button.dataset.step));
  }));
}

function goTourStep(index) {
  const steps = tourSteps();
  if (!state.tour || index < 0 || index >= steps.length) return;
  const step = steps[index];
  const viewer = state.tour.viewer;
  state.tour.index = index;
  viewer.setExplode(step.explode || 0);
  viewer.highlightParts(step.parts || null);
  viewer.showDimensions(Boolean(step.dims));
  viewer.setAutoRotate(Boolean(step.rotate) && state.tour.playing);
  const isLine = step.line !== undefined;
  viewer.showOverlay(isLine || Boolean(step.overlay));
  viewer.setMode("lines");
  if (isLine) {
    viewer.setSelectedLine(step.line, { fly: false });
    viewer.flyToLine(step.line, 900);
  } else {
    viewer.setSelectedLine(-1, { fly: false });
    if (step.view) viewer.focus([0, 6.6, 0], step.view[0], step.view[1], step.explode ? 74 : 62);
  }
  $("tour-kicker").textContent = isLine ? `שלב ${index + 1} מתוך ${steps.length} · קו ${pad2(step.line + 1)} מתוך 14` : `שלב ${index + 1} מתוך ${steps.length} · חלקי הכוורת`;
  $("tour-title").textContent = step.title;
  $("tour-text").textContent = step.text;
  $("tour-ends").hidden = !isLine;
  if (isLine) {
    $("tour-a").textContent = step.a;
    $("tour-b").textContent = step.b;
  }
  $("tour-steps").querySelectorAll("[data-step]").forEach((button) => {
    const active = Number(button.dataset.step) === index;
    button.classList.toggle("active", active);
    if (active) button.scrollIntoView({ block: "nearest" });
  });
  $("tour-prev").disabled = index === 0;
  $("tour-next").disabled = index === steps.length - 1;
  $("tour-progress").style.width = `${((index + 1) / steps.length) * 100}%`;
  if (state.tour.playing) {
    clearTimeout(state.tour.timer);
    state.tour.timer = setTimeout(() => {
      if (state.tour.index < steps.length - 1) goTourStep(state.tour.index + 1);
      else stopTourPlay();
    }, isLine ? 4200 : 5200);
  }
}

function stopTourPlay() {
  if (!state.tour) return;
  state.tour.playing = false;
  clearTimeout(state.tour.timer);
  state.tour.viewer.setAutoRotate(false);
  $("tour-play").textContent = "▶ הפעלה";
}

$("tour-play").addEventListener("click", () => {
  if (!state.tour) return;
  if (state.tour.playing) { stopTourPlay(); return; }
  state.tour.playing = true;
  $("tour-play").textContent = "⏸ השהיה";
  const steps = tourSteps();
  goTourStep(state.tour.index >= steps.length - 1 ? 0 : state.tour.index);
});
$("tour-prev").addEventListener("click", () => { stopTourPlay(); goTourStep(state.tour.index - 1); });
$("tour-next").addEventListener("click", () => { stopTourPlay(); goTourStep(state.tour.index + 1); });
$("tour-close").addEventListener("click", closeTour);
$("tour-practice").addEventListener("click", () => {
  const step = tourSteps()[state.tour?.index ?? 0];
  closeTour();
  if (step?.line !== undefined) selectLine(step.line);
});
["open-tour", "open-tour-2", "viewer-expand"].forEach((id) => $(id)?.addEventListener("click", () => {
  const line = state.mode === "lines" && id === "viewer-expand" ? tourSteps().findIndex((step) => step.line === state.selectedLine) : 0;
  openTour(Math.max(0, line));
}));

// ---------------------------------------------------------------------------
// Uploads (this browser only), export & import
// ---------------------------------------------------------------------------

function openUploadDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("biobuzz-local-files", 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("uploads")) request.result.createObjectStore("uploads", { keyPath: "name" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function uploadTransaction(mode, callback) {
  return new Promise((resolve, reject) => {
    const transaction = state.uploadDb.transaction("uploads", mode);
    const request = callback(transaction.objectStore("uploads"));
    transaction.oncomplete = () => resolve(request?.result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}

async function restoreUploadedFiles() {
  const saved = await uploadTransaction("readonly", (store) => store.getAll());
  for (const item of saved || []) {
    const src = URL.createObjectURL(item.file);
    state.uploadedUrls.set(item.name, src);
    const image = { name: item.name, width: item.width, height: item.height, status: "todo", hives: [], src, thumbSrc: src, localUpload: true, savedAt: 0 };
    const drafts = localDrafts();
    if (drafts[item.name] && Number(drafts[item.name].model_version) === 2) applyRecordToImage(image, drafts[item.name], "review");
    const index = state.images.findIndex((other) => other.name === item.name);
    if (index >= 0) state.images[index] = image; else state.images.push(image);
  }
  state.images.sort((a, b) => a.name.localeCompare(b.name));
}

function addUploadedFiles(files) {
  const accepted = Array.from(files).filter((file) => file.type.startsWith("image/"));
  if (!accepted.length) { toast("בחרו קובצי תמונה.", "error"); return; }
  for (const file of accepted) {
    const src = URL.createObjectURL(file);
    const probe = new Image();
    probe.onload = async () => {
      const image = { name: file.name, width: probe.naturalWidth, height: probe.naturalHeight, status: "todo", hives: [], src, thumbSrc: src, localUpload: true, savedAt: 0 };
      const index = state.images.findIndex((other) => other.name === file.name);
      if (index >= 0) state.images[index] = image; else state.images.push(image);
      state.images.sort((a, b) => a.name.localeCompare(b.name));
      try {
        if (state.uploadDb) await uploadTransaction("readwrite", (store) => store.put({ name: file.name, width: image.width, height: image.height, file }));
      } catch { /* keep in memory only */ }
      $("image-count").textContent = `${state.images.length}`;
      renderImageList();
      await selectImage(file.name, true);
      toast(`${file.name} נוספה לדפדפן הזה בלבד. כדי שתופיע לכולם, העלו אותה לתיקייה docs/images בריפו.`);
    };
    probe.onerror = () => toast(`לא הצלחתי לקרוא את ${file.name}.`, "error");
    probe.src = src;
  }
}

function exportDrafts() {
  const records = Object.values(localDrafts()).filter((record) => Number(record.model_version) === 2);
  if (!records.length) { toast("אין עדיין טיוטות לייצוא.", "error"); return; }
  const blob = new Blob([JSON.stringify(records, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "annotations_review.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`יוצאו ${records.length} טיוטות. העלו את הקובץ ל־docs/data/annotations_review.json כדי לשתף.`, "success");
}

async function importDrafts(file) {
  try {
    const records = JSON.parse(await file.text());
    if (!Array.isArray(records)) throw new Error("הקובץ צריך להכיל רשימה של טיוטות.");
    const drafts = localDrafts();
    let count = 0;
    for (const record of records) {
      const image = state.images.find((item) => item.name === recordName(record));
      if (!image || !Array.isArray(record.label) || record.label.length !== 240) continue;
      drafts[image.name] = { ...record, model_version: record.model_version || 2 };
      applyRecordToImage(image, record, "review");
      count += 1;
    }
    store.set(DRAFTS_KEY, drafts);
    renderImageList();
    if (currentImage() && !state.dirty) { loadHivesFromImage(currentImage()); renderEditor(); }
    toast(`יובאו ${count} טיוטות.`, "success");
  } catch (error) {
    toast(`הייבוא נכשל: ${error.message}`, "error");
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

document.querySelectorAll(".mode-switch button").forEach((button) => button.addEventListener("click", () => {
  state.mode = button.dataset.mode;
  if (state.mode === "lines" && lineDone(hive(), state.selectedLine)) {
    state.selectedLine = firstIncompleteLine(hive()) ?? state.selectedLine;
  }
  renderEditor();
  publishLive();
}));
$("undo-button").addEventListener("click", undo);
$("redo-button").addEventListener("click", redo);
$("save-button").addEventListener("click", saveDraft);
$("autocomplete-button").addEventListener("click", runAutoComplete);
$("add-hive").addEventListener("click", addHive);
$("remove-hive").addEventListener("click", removeHive);
$("prev-image").addEventListener("click", () => { const image = state.images[state.currentIndex - 1]; if (image) selectImage(image.name); });
$("next-image").addEventListener("click", () => { const image = state.images[state.currentIndex + 1]; if (image) selectImage(image.name); });
$("upload-images").addEventListener("click", () => $("image-upload").click());
$("image-upload").addEventListener("change", (event) => { addUploadedFiles(event.target.files); event.target.value = ""; });
$("export-drafts").addEventListener("click", exportDrafts);
$("import-drafts").addEventListener("click", () => $("draft-upload").click());
$("draft-upload").addEventListener("change", (event) => { if (event.target.files?.[0]) importDrafts(event.target.files[0]); event.target.value = ""; });
for (const [id, index, map] of [["hive-color", 0, { blue: 1, red: 0 }], ["hive-side", 1, { right: 1, left: 0 }], ["hive-level", 2, { top: 1, bottom: 0 }]]) {
  $(id).addEventListener("change", (event) => {
    if (!canEdit()) { renderMetadata(); return; }
    remember();
    hive().metadata[index] = map[event.target.value];
    changed();
  });
}
["open-guide", "guide-button"].forEach((id) => $(id).addEventListener("click", () => $("guide-dialog").showModal()));
$("close-guide").addEventListener("click", () => $("guide-dialog").close());
$("guide-done").addEventListener("click", () => $("guide-dialog").close());
$("guide-tour").addEventListener("click", () => { $("guide-dialog").close(); openTour(0); });

window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

document.addEventListener("keydown", (event) => {
  const tag = document.activeElement?.tagName;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(tag) || document.querySelector("dialog[open]")) return;
  if (!$("tour").hidden) {
    if (event.key === "Escape") closeTour();
    if (event.key === "ArrowLeft") { stopTourPlay(); goTourStep(state.tour.index + 1); }
    if (event.key === "ArrowRight") { stopTourPlay(); goTourStep(state.tour.index - 1); }
    if (event.key === " ") { event.preventDefault(); $("tour-play").click(); }
    return;
  }
  state.lastActivity = Date.now();
  const key = event.key.toLowerCase();
  const mod = event.ctrlKey || event.metaKey;
  if (mod && key === "z" && !event.shiftKey) { event.preventDefault(); undo(); return; }
  if (mod && (key === "y" || (key === "z" && event.shiftKey))) { event.preventDefault(); redo(); return; }
  if (mod && key === "s") { event.preventDefault(); saveDraft(); return; }
  if (mod) return;
  if (event.key === "ArrowLeft") $("next-image").click();
  else if (event.key === "ArrowRight") $("prev-image").click();
  else if (key === "s") skipCorner();
  else if (key === "x") swapSelectedLine();
  else if (key === "a") runAutoComplete();
  else if (key === "c") { state.mode = "corners"; renderEditor(); }
  else if (key === "l") { state.mode = "lines"; renderEditor(); }
  else if (event.key === "Delete" || event.key === "Backspace") { if (state.mode === "lines") clearSelectedLine(); }
  else if (key === "n" || key === "p") {
    const pos = LINE_ORDER.indexOf(state.selectedLine);
    selectLine(LINE_ORDER[(pos + (key === "n" ? 1 : LINE_ORDER.length - 1)) % LINE_ORDER.length]);
  } else if (event.key === "+" || event.key === "=") setZoom(state.zoom * 1.25);
  else if (event.key === "-") setZoom(state.zoom / 1.25);
  else if (event.key === "0") setZoom(1);
  else if (event.key === "Escape") { el.loupe.hidden = true; }
});

["pointerdown", "keydown"].forEach((type) => document.addEventListener(type, () => { state.lastActivity = Date.now(); }, { passive: true }));

// ---------------------------------------------------------------------------
// Automatic detection (trained model, runs in the browser)
// ---------------------------------------------------------------------------

async function runDetect() {
  if (!canEdit()) return;
  const image = currentImage();
  if (!image || !el.image.complete || !el.image.naturalWidth) return;
  const button = $("detect-button");
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "🤖 מזהה…";
  const startedOn = state.currentName;
  try {
    const found = await detectCells(el.image);
    if (state.currentName !== startedOn) return;  // the user moved to another image meanwhile
    if (!found.length) { toast("המודל לא מצא כוורת בתמונה הזאת.", "error"); return; }
    // slot order like the Python tools: left to right
    const meanX = (c) => c.kpts.reduce((s, k) => s + k[0], 0) / c.kpts.length;
    const cells = [...found].sort((a, b) => meanX(a) - meanX(b));
    const sx = image.width / el.image.naturalWidth, sy = image.height / el.image.naturalHeight;
    const centers = cells.map((c) => {
      const xs = c.kpts.map((k) => k[0]), ys = c.kpts.map((k) => k[1]);
      return [xs.reduce((a, b) => a + b, 0) / xs.length, ys.reduce((a, b) => a + b, 0) / ys.length];
    });
    const ref = cells.length > 1
      ? [centers.reduce((a, c) => a + c[0], 0) / centers.length, centers.reduce((a, c) => a + c[1], 0) / centers.length]
      : [el.image.naturalWidth / 2, el.image.naturalHeight / 2];
    const order = state.model.cornerOrder;
    remember();
    state.hives = cells.map((c, i) => {
      const h = freshHive([c.color === "blue" ? 1 : 0, centers[i][0] > ref[0] ? 1 : 0, centers[i][1] < ref[1] ? 1 : 0]);
      const corner = {};
      order.forEach((id, k) => {
        corner[id] = c.kpts[k][2] >= 0.5 ? [Math.round(c.kpts[k][0] * sx), Math.round(c.kpts[k][1] * sy)] : null;
      });
      // frame endpoints from the confident corners; iron-rod endpoints and unsure corners from the 3D fit
      h.points = h.points.map((_, p) => {
        const line = state.model.lines[p >> 1];
        const xy = corner[state.model.pointCorner[p]];
        return line.kind === "iron" || !xy ? null : [...xy];
      });
      const fit = fitProjection(state.model, h.points);
      if (fit.ok) {
        h.points = h.points.map((xy, p) => {
          if (xy) return xy;
          const [x, y] = fit.project(state.model.lines[p >> 1].world[p & 1]);
          return Number.isFinite(x) && Number.isFinite(y) ? [Math.round(x), Math.round(y)] : null;
        });
      }
      h.points = h.points.map((xy, p) => xy || [...(corner[state.model.pointCorner[p]] ||
        [Math.round(c.kpts[order.indexOf(state.model.pointCorner[p])][0] * sx), Math.round(c.kpts[order.indexOf(state.model.pointCorner[p])][1] * sy)])]);
      h.est = h.points.map(() => true);
      return h;
    });
    state.selectedHive = 0;
    state.mode = "lines";
    state.skipped = new Set();
    changed();
    toast(`המודל זיהה ${cells.length} כוורות (${cells.map((c) => `${c.color === "blue" ? "כחולה" : "אדומה"} ${Math.round(c.confidence * 100)}%`).join(", ")}). כל הקווים מסומנים כמשוערים — בדקו, תקנו ושמרו.`, "success");
  } catch (error) {
    console.error(error);
    toast("הזיהוי האוטומטי נכשל: " + (error.message || error), "error");
  } finally {
    button.textContent = label;
    button.disabled = state.readOnly;
  }
}

detectorAvailable().then((ok) => {
  const button = $("detect-button");
  if (!button || !ok) return;
  button.hidden = false;
  button.addEventListener("click", runDetect);
});

boot();

// Exposed for debugging in the browser console.
window.biobuzz = { state, fitProjection };
