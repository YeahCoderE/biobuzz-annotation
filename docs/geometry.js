// BIOBUZZ hive cell model: real dimensions, corners, the 14 lines, accuracy checks
// and a camera fit (DLT) that estimates hidden corners from the visible ones.

export const POINTS_PER_HIVE = 28;
export const LINES_PER_HIVE = 14;

/** Offset a convex polygon (counter-clockwise) outward by distance d. */
export function offsetPolygon(points, d) {
  const n = points.length;
  const lines = points.map((p, i) => {
    const q = points[(i + 1) % n];
    const dx = q[0] - p[0];
    const dy = q[1] - p[1];
    const len = Math.hypot(dx, dy);
    const nx = dy / len;
    const ny = -dx / len;
    return { p: [p[0] + nx * d, p[1] + ny * d], dir: [dx / len, dy / len] };
  });
  return lines.map((line, i) => {
    const prev = lines[(i - 1 + n) % n];
    return intersect(prev.p, prev.dir, line.p, line.dir);
  });
}

function intersect(p, r, q, s) {
  const cross = r[0] * s[1] - r[1] * s[0];
  if (Math.abs(cross) < 1e-9) return [...q];
  const t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / cross;
  return [p[0] + r[0] * t, p[1] + r[1] * t];
}

/** Turn the JSON description into a geometric model used by the editor and the 3D view. */
export function buildModel(raw) {
  const d = raw.dimensions;
  const half = d.openingWidth / 2;
  // Inner opening, counter-clockwise when viewed from the camera (x right, y up).
  const inner = [[-half, 0], [half, 0], [half, d.eaveHeight], [0, d.peakHeight], [-half, d.eaveHeight]];
  const center = offsetPolygon(inner, d.ribWidth / 2);
  const outer = offsetPolygon(inner, d.ribWidth);
  const zFront = d.cellDepth / 2 - d.ribDepth / 2;
  const keys = ["BL", "BR", "ER", "P", "EL"];
  const corners = {};
  for (const [frame, z] of [["F", zFront], ["B", -zFront]]) {
    keys.forEach((key, i) => { corners[`${frame}.${key}`] = [center[i][0], center[i][1], z]; });
  }
  const churroX = half - d.churroInset;
  const lines = raw.lines.map((line, index) => {
    const world = [line.a, line.b].map((id) => {
      const [x, y, z] = corners[id];
      return line.kind === "iron" ? [Math.sign(x) * churroX, d.eaveHeight, z] : [x, y, z];
    });
    return { ...line, index, world };
  });
  const cornerPoints = {};
  lines.forEach((line, i) => {
    [line.a, line.b].forEach((id, end) => { (cornerPoints[id] ||= []).push(i * 2 + end); });
  });
  const pointCorner = new Array(POINTS_PER_HIVE);
  for (const [id, list] of Object.entries(cornerPoints)) list.forEach((p) => { pointCorner[p] = id; });
  const groups = raw.groups.map((group) => ({ ...group, lineIndexes: group.lines.map((id) => id - 1) }));
  return {
    raw, dims: d, inner, center, outer, zFront, corners, churroX, lines, cornerPoints, pointCorner, groups,
    cornerOrder: raw.cornerOrder, cornerInfo: raw.corners, rules: raw.rules, terms: raw.officialTerms,
  };
}

export function cornerLabel(model, id) {
  const info = model.cornerInfo[id];
  return info ? `${info.name} · מסגרת ${info.frame}` : id;
}

export function endpointLabel(model, lineIndex, end) {
  const line = model.lines[lineIndex];
  const id = end ? line.b : line.a;
  if (line.kind === "iron") return `קצה המוט ב${cornerLabel(model, id).replace("פינת גג", "פינת הגג")}`;
  return cornerLabel(model, id);
}

export function pointWorld(model, pointIndex) {
  return model.lines[pointIndex >> 1].world[pointIndex & 1];
}

const distance = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);

function mean(list) {
  return [list.reduce((s, p) => s + p[0], 0) / list.length, list.reduce((s, p) => s + p[1], 0) / list.length];
}

function hiveDiagonal(points) {
  const filled = points.filter(Boolean);
  if (filled.length < 2) return 0;
  const xs = filled.map((p) => p[0]);
  const ys = filled.map((p) => p[1]);
  return Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
}

function cornerReference(model, points, cornerId, skipLine) {
  const list = model.cornerPoints[cornerId]
    .filter((p) => (p >> 1) !== skipLine && points[p])
    .map((p) => points[p]);
  return list.length ? mean(list) : null;
}

/**
 * Lines whose A/B endpoints were clicked in the wrong order are detected by comparing
 * each endpoint with the other lines that share the same physical corner.
 */
export function fixSwappedEndpoints(model, points) {
  const result = points.map((p) => (p ? [...p] : null));
  const swapped = new Set();
  for (let pass = 0; pass < 3; pass++) {
    let changed = false;
    for (let line = 0; line < LINES_PER_HIVE; line++) {
      const a = result[line * 2];
      const b = result[line * 2 + 1];
      if (!a || !b) continue;
      const refA = cornerReference(model, result, model.pointCorner[line * 2], line);
      const refB = cornerReference(model, result, model.pointCorner[line * 2 + 1], line);
      if (!refA && !refB) continue;
      const keep = (refA ? distance(a, refA) : 0) + (refB ? distance(b, refB) : 0);
      const swap = (refA ? distance(b, refA) : 0) + (refB ? distance(a, refB) : 0);
      if (swap + 3 < keep * 0.45) {
        result[line * 2] = b;
        result[line * 2 + 1] = a;
        if (swapped.has(line)) swapped.delete(line); else swapped.add(line);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return { points: result, swapped: [...swapped].sort((x, y) => x - y) };
}

/** Accuracy report for one hive: corners that disagree, tiny lines and the 3D fit error. */
export function checkHive(model, points) {
  const issues = [];
  const diag = hiveDiagonal(points);
  if (!diag) return { issues, diag, fit: null, filled: points.filter(Boolean).length };
  const tolerance = Math.max(9, diag * 0.035);
  for (const [cornerId, list] of Object.entries(model.cornerPoints)) {
    const frameLines = list.filter((p) => points[p] && model.lines[p >> 1].kind !== "iron");
    if (frameLines.length >= 2) {
      const ref = mean(frameLines.map((p) => points[p]));
      const worst = Math.max(...frameLines.map((p) => distance(points[p], ref)));
      if (worst > tolerance) {
        issues.push({
          type: "corner", cornerId, lines: [...new Set(frameLines.map((p) => p >> 1))], value: Math.round(worst * 2),
          text: `${cornerLabel(model, cornerId)}: הקווים לא נפגשים באותה נקודה (פער של ${Math.round(worst * 2)} פיקסלים).`,
        });
      }
    }
    const iron = list.filter((p) => points[p] && model.lines[p >> 1].kind === "iron");
    if (iron.length && frameLines.length) {
      const ref = mean(frameLines.map((p) => points[p]));
      for (const p of iron) {
        const gap = distance(points[p], ref);
        if (gap > tolerance * 2.2) {
          issues.push({
            type: "iron", cornerId, lines: [p >> 1], value: Math.round(gap),
            text: `קו ${String((p >> 1) + 1).padStart(2, "0")}: קצה המוט רחוק מ${cornerLabel(model, cornerId)} (${Math.round(gap)} פיקסלים). המוט נכנס למסגרת בפינת הגג.`,
          });
        }
      }
    }
  }
  for (let line = 0; line < LINES_PER_HIVE; line++) {
    const a = points[line * 2];
    const b = points[line * 2 + 1];
    if (a && b && distance(a, b) < Math.max(4, diag * 0.01)) {
      issues.push({ type: "short", lines: [line], text: `קו ${String(line + 1).padStart(2, "0")} קצר מדי: שתי הנקודות כמעט באותו מקום.` });
    }
  }
  const fit = fitProjection(model, points);
  if (fit.ok && fit.correspondences >= 8) {
    const limit = Math.max(8, diag * 0.04);
    fit.residuals.forEach((residual) => {
      if (residual.error > limit) {
        issues.push({
          type: "fit", lines: residual.points.map((p) => p >> 1), value: Math.round(residual.error),
          text: `${residual.label}: לא מתאים לצורה התלת־ממדית של הכוורת (סטייה של ${Math.round(residual.error)} פיקסלים).`,
        });
      }
    });
  }
  return { issues, diag, fit: fit.ok ? fit : null, filled: points.filter(Boolean).length };
}

// ---------------------------------------------------------------------------
// Camera fit (Direct Linear Transform) from >= 6 known corners on both frames.
// ---------------------------------------------------------------------------

function jacobiEigen(input) {
  const n = input.length;
  const a = input.map((row) => row.slice());
  const v = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)));
  for (let sweep = 0; sweep < 80; sweep++) {
    let off = 0;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) off += a[i][j] * a[i][j];
    if (off < 1e-22) break;
    for (let p = 0; p < n - 1; p++) {
      for (let q = p + 1; q < n; q++) {
        if (Math.abs(a[p][q]) < 1e-30) continue;
        const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
        const t = (theta >= 0 ? 1 : -1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
        const c = 1 / Math.sqrt(t * t + 1);
        const s = t * c;
        for (let k = 0; k < n; k++) {
          const akp = a[k][p];
          const akq = a[k][q];
          a[k][p] = c * akp - s * akq;
          a[k][q] = s * akp + c * akq;
        }
        for (let k = 0; k < n; k++) {
          const apk = a[p][k];
          const aqk = a[q][k];
          a[p][k] = c * apk - s * aqk;
          a[q][k] = s * apk + c * aqk;
        }
        for (let k = 0; k < n; k++) {
          const vkp = v[k][p];
          const vkq = v[k][q];
          v[k][p] = c * vkp - s * vkq;
          v[k][q] = s * vkp + c * vkq;
        }
      }
    }
  }
  return { values: a.map((row, i) => row[i]), vectors: v };
}

function collectCorrespondences(model, points) {
  const groups = new Map();
  points.forEach((point, index) => {
    if (!point) return;
    const world = pointWorld(model, index);
    const key = world.map((value) => value.toFixed(3)).join(",");
    const entry = groups.get(key) || { world, image: [], points: [] };
    entry.image.push(point);
    entry.points.push(index);
    groups.set(key, entry);
  });
  return [...groups.values()].map((entry) => ({ world: entry.world, image: mean(entry.image), points: entry.points }));
}

export function fitProjection(model, points) {
  const corr = collectCorrespondences(model, points);
  const front = corr.filter((c) => c.world[2] > 0).length;
  const back = corr.length - front;
  if (corr.length < 6) return { ok: false, reason: "few", count: corr.length };
  if (front < 2 || back < 2) return { ok: false, reason: "planar", count: corr.length };
  // The known points must span all three dimensions (e.g. not just one wall).
  const centroid = [0, 1, 2].map((k) => corr.reduce((s, c) => s + c.world[k], 0) / corr.length);
  const cov = [0, 1, 2].map((i) => [0, 1, 2].map((j) =>
    corr.reduce((s, c) => s + (c.world[i] - centroid[i]) * (c.world[j] - centroid[j]), 0) / corr.length));
  const spread = jacobiEigen(cov).values.map(Math.abs).sort((x, y) => x - y);
  if (spread[0] < spread[2] * 0.015) return { ok: false, reason: "planar", count: corr.length };

  const imageMean = mean(corr.map((c) => c.image));
  const imageScale = Math.SQRT2 / (corr.reduce((s, c) => s + distance(c.image, imageMean), 0) / corr.length || 1);
  const worldMean = [0, 1, 2].map((k) => corr.reduce((s, c) => s + c.world[k], 0) / corr.length);
  const worldScale = Math.sqrt(3) / (corr.reduce((s, c) => s + Math.hypot(...c.world.map((v, k) => v - worldMean[k])), 0) / corr.length || 1);

  const ata = Array.from({ length: 12 }, () => new Array(12).fill(0));
  const addRow = (row) => {
    for (let i = 0; i < 12; i++) for (let j = 0; j < 12; j++) ata[i][j] += row[i] * row[j];
  };
  for (const c of corr) {
    const X = (c.world[0] - worldMean[0]) * worldScale;
    const Y = (c.world[1] - worldMean[1]) * worldScale;
    const Z = (c.world[2] - worldMean[2]) * worldScale;
    const u = (c.image[0] - imageMean[0]) * imageScale;
    const v = (c.image[1] - imageMean[1]) * imageScale;
    addRow([X, Y, Z, 1, 0, 0, 0, 0, -u * X, -u * Y, -u * Z, -u]);
    addRow([0, 0, 0, 0, X, Y, Z, 1, -v * X, -v * Y, -v * Z, -v]);
  }
  const { values, vectors } = jacobiEigen(ata);
  let best = 0;
  values.forEach((value, i) => { if (value < values[best]) best = i; });
  const p = vectors.map((row) => row[best]);
  const pn = [p.slice(0, 4), p.slice(4, 8), p.slice(8, 12)];
  // P = T^-1 * Pn * U
  const U = [
    [worldScale, 0, 0, -worldScale * worldMean[0]],
    [0, worldScale, 0, -worldScale * worldMean[1]],
    [0, 0, worldScale, -worldScale * worldMean[2]],
    [0, 0, 0, 1],
  ];
  const Tinv = [[1 / imageScale, 0, imageMean[0]], [0, 1 / imageScale, imageMean[1]], [0, 0, 1]];
  const pu = pn.map((row) => [0, 1, 2, 3].map((j) => row.reduce((s, value, k) => s + value * U[k][j], 0)));
  const P = Tinv.map((row) => [0, 1, 2, 3].map((j) => row.reduce((s, value, k) => s + value * pu[k][j], 0)));
  const project = (w) => {
    const x = P[0][0] * w[0] + P[0][1] * w[1] + P[0][2] * w[2] + P[0][3];
    const y = P[1][0] * w[0] + P[1][1] * w[1] + P[1][2] * w[2] + P[1][3];
    const z = P[2][0] * w[0] + P[2][1] * w[1] + P[2][2] * w[2] + P[2][3];
    return [x / z, y / z];
  };
  const residuals = corr.map((c) => {
    const lineIndex = c.points[0] >> 1;
    const corner = model.pointCorner[c.points[0]];
    const label = model.lines[lineIndex].kind === "iron"
      ? `קצה מוט הברזל (קו ${String(lineIndex + 1).padStart(2, "0")})`
      : cornerLabel(model, corner);
    return { error: distance(project(c.world), c.image), points: c.points, label };
  });
  const rms = Math.sqrt(residuals.reduce((s, r) => s + r.error * r.error, 0) / residuals.length);
  return { ok: true, P, project, residuals, rms, correspondences: corr.length };
}

/** Fill every empty endpoint from the fitted camera. Returns the filled indexes. */
export function autoComplete(model, points, bounds) {
  const fit = fitProjection(model, points);
  if (!fit.ok) return { ok: false, reason: fit.reason, count: fit.count };
  const filled = [];
  const next = points.map((p) => (p ? [...p] : null));
  next.forEach((point, index) => {
    if (point) return;
    const [x, y] = fit.project(pointWorld(model, index));
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    next[index] = [
      Math.round(Math.min(bounds.width - 1, Math.max(0, x))),
      Math.round(Math.min(bounds.height - 1, Math.max(0, y))),
    ];
    filled.push(index);
  });
  return { ok: true, points: next, filled, rms: fit.rms };
}
