// In-browser BIOBUZZ hive-cell detector (YOLO-pose ONNX model, onnxruntime-web, lazy loaded).
// Returns, per detected cell: colour, confidence and the 10 corners in the order of
// hive_model.json cornerOrder (F.BL, F.BR, F.ER, F.P, F.EL, B.BL, B.BR, B.ER, B.P, B.EL).

const ORT_VERSION = "1.20.1";
const ORT_URL = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/ort.min.js`;
const MODEL_URL = "./models/biobuzz_hive.onnx";
const META_URL = "./models/biobuzz_hive.json";

let sessionPromise = null;
let meta = { imgsz: 512, conf: 0.35 };

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (window.ort) { resolve(); return; }
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("onnxruntime-web failed to load"));
    document.head.appendChild(s);
  });
}

export async function detectorAvailable() {
  try {
    const r = await fetch(META_URL, { cache: "no-cache" });
    if (!r.ok) return false;
    meta = { ...meta, ...(await r.json()) };
    return true;
  } catch {
    return false;
  }
}

async function session() {
  if (!sessionPromise) {
    sessionPromise = (async () => {
      await loadScript(ORT_URL);
      window.ort.env.wasm.wasmPaths = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`;
      window.ort.env.wasm.numThreads = Math.min(4, navigator.hardwareConcurrency || 2);
      return window.ort.InferenceSession.create(MODEL_URL, { executionProviders: ["wasm"] });
    })();
    sessionPromise.catch(() => { sessionPromise = null; });
  }
  return sessionPromise;
}

function iou(a, b) {
  const x0 = Math.max(a[0], b[0]), y0 = Math.max(a[1], b[1]);
  const x1 = Math.min(a[2], b[2]), y1 = Math.min(a[3], b[3]);
  const inter = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
  const ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter;
  return ua > 0 ? inter / ua : 0;
}

/** image: a loaded HTMLImageElement (natural size is used). */
export async function detectCells(image, { conf = meta.conf, iouThr = 0.5 } = {}) {
  const sess = await session();
  const S = meta.imgsz;
  const W = image.naturalWidth, H = image.naturalHeight;
  const scale = S / Math.max(W, H);
  const nw = Math.round(W * scale), nh = Math.round(H * scale);
  const left = Math.floor((S - nw) / 2), top = Math.floor((S - nh) / 2);
  const canvas = document.createElement("canvas");
  canvas.width = S;
  canvas.height = S;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "rgb(114,114,114)";
  ctx.fillRect(0, 0, S, S);
  ctx.drawImage(image, left, top, nw, nh);
  const px = ctx.getImageData(0, 0, S, S).data;
  const input = new Float32Array(3 * S * S);
  for (let i = 0, n = S * S; i < n; i++) {
    input[i] = px[i * 4] / 255;
    input[n + i] = px[i * 4 + 1] / 255;
    input[2 * n + i] = px[i * 4 + 2] / 255;
  }
  const feeds = { [sess.inputNames[0]]: new window.ort.Tensor("float32", input, [1, 3, S, S]) };
  const out = (await sess.run(feeds))[sess.outputNames[0]];
  const [, C, N] = out.dims;
  const d = out.data;
  const cand = [];
  for (let j = 0; j < N; j++) {
    const s0 = d[4 * N + j], s1 = d[5 * N + j];
    const score = Math.max(s0, s1);
    if (score < conf) continue;
    const cx = d[j], cy = d[N + j], w = d[2 * N + j], h = d[3 * N + j];
    const kp = [];
    for (let k = 0; k < 10; k++) {
      kp.push([(d[(6 + k * 3) * N + j] - left) / scale, (d[(7 + k * 3) * N + j] - top) / scale, d[(8 + k * 3) * N + j]]);
    }
    cand.push({
      color: s1 > s0 ? "blue" : "red", confidence: score, kpts: kp,
      box: [(cx - w / 2 - left) / scale, (cy - h / 2 - top) / scale, (cx + w / 2 - left) / scale, (cy + h / 2 - top) / scale],
    });
  }
  cand.sort((a, b) => b.confidence - a.confidence);
  const keep = [];
  for (const c of cand) if (keep.every((k) => iou(k.box, c.box) < iouThr)) keep.push(c);
  return keep.slice(0, 4);
}
