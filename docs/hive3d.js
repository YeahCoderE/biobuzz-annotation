// Interactive 3D model of one BIOBUZZ hive cell, built from the official dimensions.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";

const FRAME_COLORS = { red: 0xd9262c, blue: 0x2457d6 };
const LINE_COLORS = { frame: 0x22c55e, depth: 0xa855f7, iron: 0xf59e0b };
const SELECTED = 0xfde047;

const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const len2 = (a) => Math.hypot(a[0], a[1]);

function roundedPoints(points, radius, segments = 6) {
  const out = [];
  const n = points.length;
  for (let i = 0; i < n; i++) {
    const prev = points[(i - 1 + n) % n];
    const cur = points[i];
    const next = points[(i + 1) % n];
    const v1 = sub(prev, cur);
    const v2 = sub(next, cur);
    const r = Math.min(radius, len2(v1) / 2.4, len2(v2) / 2.4);
    const a = [cur[0] + (v1[0] / len2(v1)) * r, cur[1] + (v1[1] / len2(v1)) * r];
    const b = [cur[0] + (v2[0] / len2(v2)) * r, cur[1] + (v2[1] / len2(v2)) * r];
    for (let s = 0; s <= segments; s++) {
      const t = s / segments;
      out.push([
        (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * cur[0] + t * t * b[0],
        (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * cur[1] + t * t * b[1],
      ]);
    }
  }
  return out;
}

const toShapePoints = (pts) => pts.map(([x, y]) => new THREE.Vector2(x, y));

function quad(p, q, z0, z1) {
  const geometry = new THREE.BufferGeometry();
  const v = [p[0], p[1], z0, q[0], q[1], z0, q[0], q[1], z1, p[0], p[1], z1];
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(v, 3));
  geometry.setIndex([0, 1, 2, 0, 2, 3]);
  geometry.computeVertexNormals();
  return geometry;
}

function tagTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 1024;
  canvas.height = 260;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, 1024, 260);
  let seed = 7;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  [40, 270, 545, 775].forEach((x0) => {
    ctx.fillStyle = "#000";
    ctx.fillRect(x0, 22, 200, 200);
    const cell = 200 / 8;
    for (let gy = 1; gy < 7; gy++) {
      for (let gx = 1; gx < 7; gx++) {
        ctx.fillStyle = rand() > 0.5 ? "#fff" : "#000";
        ctx.fillRect(x0 + gx * cell, 22 + gy * cell, cell, cell);
      }
    }
  });
  ctx.fillStyle = "#d9262c";
  ctx.fillRect(0, 238, 1024, 14);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function shadowTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 256;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(128, 128, 10, 128, 128, 128);
  gradient.addColorStop(0, "rgba(15,23,42,0.32)");
  gradient.addColorStop(1, "rgba(15,23,42,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(canvas);
}

function label(text, className) {
  const div = document.createElement("div");
  div.className = className;
  div.textContent = text;
  return new CSS2DObject(div);
}

export function createHiveViewer(container, model, options = {}) {
  const compact = Boolean(options.compact);
  const d = model.dims;
  const zF = model.zFront;
  const center = new THREE.Vector3(0, d.peakHeight / 2 - 0.4, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(compact ? 34 : 32, 1, 0.1, 600);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);
  renderer.domElement.classList.add("hive3d-canvas");

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.className = "hive3d-labels";
  container.appendChild(labelRenderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.enablePan = !compact;
  controls.minDistance = 18;
  controls.maxDistance = 120;
  controls.target.copy(center);

  scene.add(new THREE.HemisphereLight(0xffffff, 0xb6c2d4, 1.15));
  const sun = new THREE.DirectionalLight(0xffffff, 1.5);
  sun.position.set(18, 30, 26);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.55);
  fill.position.set(-22, 8, -18);
  scene.add(fill);

  const hive = new THREE.Group();
  scene.add(hive);
  const parts = {};
  const addPart = (key, object, explode) => {
    object.userData.base = object.position.clone();
    object.userData.explode = explode || new THREE.Vector3();
    (parts[key] ||= []).push(object);
    hive.add(object);
    return object;
  };

  // --- Goal Ribs (the red/blue pentagon frames) -----------------------------
  const ribMaterial = new THREE.MeshStandardMaterial({ color: FRAME_COLORS.red, roughness: 0.42, metalness: 0.05 });
  const outerRounded = roundedPoints(model.outer, 0.95);
  const innerRounded = roundedPoints(model.inner, 0.42);
  const ribShape = new THREE.Shape(toShapePoints(outerRounded));
  ribShape.holes.push(new THREE.Path(toShapePoints(innerRounded)));
  const ribGeometry = new THREE.ExtrudeGeometry(ribShape, { depth: d.ribDepth, bevelEnabled: false, curveSegments: 4 });
  ribGeometry.translate(0, 0, -d.ribDepth / 2);
  const yOuterBottom = model.outer[0][1];
  const tabShape = new THREE.Shape(toShapePoints([[-3.8, yOuterBottom + 0.05], [3.8, yOuterBottom + 0.05], [0.8, -1.65], [-0.8, -1.65]]));
  tabShape.holes.push(new THREE.Path(toShapePoints([[-0.5, -1.35], [-0.5, -0.38], [0.5, -0.38], [0.5, -1.35]])));
  const tabGeometry = new THREE.ExtrudeGeometry(tabShape, { depth: d.ribDepth, bevelEnabled: false });
  tabGeometry.translate(0, 0, -d.ribDepth / 2);
  for (const [key, z] of [["frontRib", zF], ["backRib", -zF]]) {
    const group = new THREE.Group();
    group.add(new THREE.Mesh(ribGeometry, ribMaterial), new THREE.Mesh(tabGeometry, ribMaterial));
    group.position.set(0, 0, z);
    addPart(key, group, new THREE.Vector3(0, 0, Math.sign(z) * 5));
  }

  // --- Clear skins ------------------------------------------------------------
  const skinMaterial = new THREE.MeshStandardMaterial({
    color: 0xdbeafe, transparent: true, opacity: 0.17, roughness: 0.08, side: THREE.DoubleSide, depthWrite: false,
  });
  const [BL, BR, ER, P, EL] = model.inner;
  const zs = zF;
  addPart("roof", new THREE.Mesh(quad(EL, P, -zs, zs), skinMaterial), new THREE.Vector3(-1.2, 3, 0));
  addPart("roof", new THREE.Mesh(quad(P, ER, -zs, zs), skinMaterial), new THREE.Vector3(1.2, 3, 0));
  addPart("walls", new THREE.Mesh(quad(BL, EL, -zs, zs), skinMaterial), new THREE.Vector3(-3, 0, 0));
  addPart("walls", new THREE.Mesh(quad(ER, BR, -zs, zs), skinMaterial), new THREE.Vector3(3, 0, 0));
  addPart("floor", new THREE.Mesh(quad(BR, BL, -zs, zs), skinMaterial), new THREE.Vector3(0, -2.6, 0));
  const backSkinGeometry = new THREE.ShapeGeometry(new THREE.Shape(toShapePoints(innerRounded)));
  const backSkin = new THREE.Mesh(backSkinGeometry, skinMaterial.clone());
  backSkin.material.opacity = 0.26;
  backSkin.position.z = -d.cellDepth / 2 + 0.02;
  addPart("backSkin", backSkin, new THREE.Vector3(0, 0, -8));

  // --- Churros (iron rods) with cable ties -------------------------------------
  const metal = new THREE.MeshStandardMaterial({ color: 0xc3cad6, roughness: 0.3, metalness: 0.75 });
  const churroLength = zF * 2 + d.ribDepth * 0.6;
  const churroGeometry = new THREE.CylinderGeometry(0.27, 0.27, churroLength, 6);
  churroGeometry.rotateX(Math.PI / 2);
  const tieMaterial = new THREE.MeshStandardMaterial({ color: FRAME_COLORS.red, roughness: 0.5 });
  const tieGeometry = new THREE.TorusGeometry(0.4, 0.075, 6, 18);
  for (const side of [-1, 1]) {
    const rod = new THREE.Group();
    rod.add(new THREE.Mesh(churroGeometry, metal));
    for (const z of [-3.2, 0, 3.2]) {
      const tie = new THREE.Mesh(tieGeometry, tieMaterial);
      tie.position.z = z;
      rod.add(tie);
    }
    rod.position.set(side * model.churroX, d.eaveHeight, 0);
    addPart("churros", rod, new THREE.Vector3(side * 1.6, 1.6, 0));
  }

  // --- Cell Base Tube ---------------------------------------------------------
  const tubeLength = d.cellDepth + 3.5;
  const baseTube = new THREE.Mesh(new THREE.BoxGeometry(1, 1, tubeLength), metal);
  baseTube.position.set(0, -0.86, d.cellDepth / 2 - tubeLength / 2);
  addPart("baseTube", baseTube, new THREE.Vector3(0, -3.6, 0));

  // --- AprilTag cluster under the floor ----------------------------------------
  const tags = new THREE.Mesh(
    new THREE.PlaneGeometry(16.4, 4.2),
    new THREE.MeshStandardMaterial({ map: tagTexture(), roughness: 0.7, side: THREE.DoubleSide }),
  );
  tags.rotation.x = Math.PI / 2;
  tags.position.set(0, -0.04, -2.2);
  addPart("tags", tags, new THREE.Vector3(0, -5, 0));

  // --- Floor shadow and orientation marks -----------------------------------------
  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(19, 48),
    new THREE.MeshBasicMaterial({ map: shadowTexture(), transparent: true, depthWrite: false }),
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -2.4;
  scene.add(ground);

  const orient = new THREE.Group();
  scene.add(orient);
  const orientLabels = [
    [compact ? "קדימה · מצלמה" : "קדימה · לכיוון המצלמה", new THREE.Vector3(0, -2.4, 16.5), "hive3d-orient front"],
    ["אחורה", new THREE.Vector3(0, -2.4, -15.5), "hive3d-orient"],
    ["שמאל", new THREE.Vector3(-16.5, -2.4, 0), "hive3d-orient"],
    ["ימין", new THREE.Vector3(16.5, -2.4, 0), "hive3d-orient"],
  ];
  for (const [text, position, className] of orientLabels) {
    const tag = label(text, className);
    tag.position.copy(position);
    orient.add(tag);
  }

  // Camera that "took the photo" — always in front of the front frame.
  const cameraRig = new THREE.Group();
  const camBody = new THREE.Mesh(new THREE.BoxGeometry(2.4, 1.6, 1.5), new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.5 }));
  const camLens = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.62, 1, 20), new THREE.MeshStandardMaterial({ color: 0x0f172a, roughness: 0.3, metalness: 0.4 }));
  camLens.rotation.x = Math.PI / 2;
  camLens.position.z = -1.1;
  cameraRig.add(camBody, camLens);
  cameraRig.position.set(0, 1.5, 27);
  cameraRig.lookAt(0, 6.2, 0);
  scene.add(cameraRig);
  const sightGeometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 1.5, 26), new THREE.Vector3(0, 6, zF + 0.6)]);
  const sight = new THREE.Line(sightGeometry, new THREE.LineDashedMaterial({ color: 0x64748b, dashSize: 0.8, gapSize: 0.6 }));
  sight.computeLineDistances();
  scene.add(sight);
  const camTag = label("📷 המצלמה", "hive3d-camtag");
  camTag.position.set(0, 3.4, 27);
  scene.add(camTag);
  if (compact) { cameraRig.scale.setScalar(0.8); }

  // --- 14 line overlays ---------------------------------------------------------
  const unitCylinder = new THREE.CylinderGeometry(1, 1, 1, 12);
  const overlay = new THREE.Group();
  scene.add(overlay);
  const cellCenter = new THREE.Vector3(0, d.peakHeight * 0.45, 0);
  const lineViews = model.lines.map((line, index) => {
    const a = new THREE.Vector3(...line.world[0]);
    const b = new THREE.Vector3(...line.world[1]);
    const direction = new THREE.Vector3().subVectors(b, a);
    const length = direction.length();
    const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(0.5);
    const quaternion = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.clone().normalize());
    const material = new THREE.MeshBasicMaterial({ color: LINE_COLORS[line.kind], transparent: true, opacity: 0.9 });
    const mesh = new THREE.Mesh(unitCylinder, material);
    mesh.position.copy(mid);
    mesh.quaternion.copy(quaternion);
    mesh.scale.set(0.16, length, 0.16);
    mesh.renderOrder = 2;
    const pick = new THREE.Mesh(unitCylinder, new THREE.MeshBasicMaterial({ visible: false }));
    pick.position.copy(mid);
    pick.quaternion.copy(quaternion);
    pick.scale.set(0.95, length, 0.95);
    pick.userData.lineIndex = index;
    overlay.add(mesh, pick);
    const outward = mid.clone().sub(cellCenter);
    outward.z *= 0.35;
    if (outward.lengthSq() < 1e-6) outward.set(0, 1, 0);
    const tagPosition = mid.clone().add(outward.normalize().multiplyScalar(1.5));
    const tag = label(String(line.id).padStart(2, "0"), `hive3d-linetag kind-${line.kind}`);
    tag.position.copy(tagPosition);
    tag.element.dataset.line = index;
    tag.element.title = line.name;
    tag.element.addEventListener("click", (event) => { event.stopPropagation(); options.onSelect?.(index); });
    overlay.add(tag);
    return { line, a, b, mid, length, mesh, pick, tag, material };
  });

  const endpointMaterial = new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false });
  const endpointA = new THREE.Mesh(new THREE.SphereGeometry(0.5, 20, 14), endpointMaterial);
  const endpointB = endpointA.clone();
  endpointA.renderOrder = endpointB.renderOrder = 5;
  const pulse = new THREE.Mesh(new THREE.SphereGeometry(0.42, 16, 12), new THREE.MeshBasicMaterial({ color: SELECTED, depthTest: false }));
  pulse.renderOrder = 6;
  const glow = new THREE.Mesh(unitCylinder, new THREE.MeshBasicMaterial({ color: SELECTED, transparent: true, opacity: 0.28, depthTest: false }));
  glow.renderOrder = 4;
  const tagA = label("A", "hive3d-end end-a");
  const tagB = label("B", "hive3d-end end-b");
  overlay.add(endpointA, endpointB, pulse, glow, tagA, tagB);

  // --- Corner markers (corner mode) ---------------------------------------------
  const cornerGroup = new THREE.Group();
  scene.add(cornerGroup);
  const cornerViews = Object.entries(model.corners).map(([id, position]) => {
    const material = new THREE.MeshBasicMaterial({ color: 0x94a3b8, depthTest: false, transparent: true, opacity: 0.95 });
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.55, 18, 12), material);
    mesh.position.set(...position);
    mesh.renderOrder = 7;
    cornerGroup.add(mesh);
    return { id, mesh, material };
  });
  const cornerTag = label("", "hive3d-cornertag");
  cornerGroup.add(cornerTag);
  cornerGroup.visible = false;

  // --- Dimensions -------------------------------------------------------------
  const dims = new THREE.Group();
  scene.add(dims);
  const dimMaterial = new THREE.LineBasicMaterial({ color: 0x334155 });
  const addDim = (from, to, text, labelOffset = new THREE.Vector3()) => {
    const geometry = new THREE.BufferGeometry().setFromPoints([from, to]);
    dims.add(new THREE.Line(geometry, dimMaterial));
    const tag = label(text, "hive3d-dim");
    tag.position.copy(from.clone().add(to).multiplyScalar(0.5).add(labelOffset));
    dims.add(tag);
  };
  const half = d.openingWidth / 2;
  addDim(new THREE.Vector3(-half, -2.9, zF), new THREE.Vector3(half, -2.9, zF), `${d.openingWidth}″ · 50.8 ס״מ`, new THREE.Vector3(0, -0.9, 0));
  addDim(new THREE.Vector3(half + 2.1, 0, zF), new THREE.Vector3(half + 2.1, d.eaveHeight, zF), `${d.eaveHeight}″`, new THREE.Vector3(1.6, 0, 0));
  addDim(new THREE.Vector3(half + 4.4, 0, zF), new THREE.Vector3(half + 4.4, d.peakHeight, zF), `${d.peakHeight}″`, new THREE.Vector3(1.6, 0, 0));
  addDim(new THREE.Vector3(half + 2.1, -2.9, d.cellDepth / 2), new THREE.Vector3(half + 2.1, -2.9, -d.cellDepth / 2), `${d.cellDepth}″ עומק`, new THREE.Vector3(1.8, -0.6, 0));
  dims.visible = false;

  // --- Part labels (tour) ---------------------------------------------------------
  const partLabels = {
    frontRib: [new THREE.Vector3(-half - 3.2, d.eaveHeight + 3.5, zF), "מסגרת קדמית (Goal Rib)"],
    backRib: [new THREE.Vector3(half + 3, d.peakHeight + 1.2, -zF), "מסגרת אחורית"],
    roof: [new THREE.Vector3(0, d.peakHeight + 2.6, 0), "גג — שני שיפועים"],
    walls: [new THREE.Vector3(-half - 3.5, 3.5, 0), "קירות"],
    floor: [new THREE.Vector3(0, -3.2, 4), "רצפה"],
    churros: [new THREE.Vector3(half + 2.6, d.eaveHeight + 2.2, 0), "מוט ברזל (Churro)"],
    baseTube: [new THREE.Vector3(0, -4.3, -8.5), "צינור הבסיס ← לציר"],
    tags: [new THREE.Vector3(0, -5.2, -2.2), "AprilTag מתחת לרצפה"],
    backSkin: [new THREE.Vector3(0, d.eaveHeight, -d.cellDepth / 2 - 2.5), "דופן אחורית סגורה"],
  };
  const partTags = {};
  for (const [key, [position, text]] of Object.entries(partLabels)) {
    const tag = label(text, "hive3d-part");
    tag.position.copy(position);
    tag.visible = false;
    scene.add(tag);
    partTags[key] = tag;
  }

  // --- State ------------------------------------------------------------------
  let selected = -1;
  let hovered = -1;
  let mode = "lines";
  let activeCorner = null;
  let explode = 0;
  let explodeTarget = 0;
  let highlighted = null;
  let disposed = false;
  let flight = null;
  let overlayWanted = true;
  const lineStates = new Array(model.lines.length).fill("empty");

  function applyLineStyles() {
    lineViews.forEach((view, index) => {
      const isSelected = index === selected;
      const state = lineStates[index];
      view.material.color.setHex(isSelected ? SELECTED : LINE_COLORS[view.line.kind]);
      view.material.opacity = isSelected ? 1 : state === "done" ? 0.95 : state === "partial" ? 0.6 : 0.32;
      const radius = isSelected ? 0.3 : index === hovered ? 0.24 : 0.15;
      view.mesh.scale.set(radius, view.length, radius);
      view.tag.element.classList.toggle("selected", isSelected);
      view.tag.element.classList.toggle("done", state === "done");
      view.tag.element.classList.toggle("hover", index === hovered);
    });
    const view = lineViews[selected];
    const show = Boolean(view) && mode === "lines";
    [endpointA, endpointB, pulse, glow, tagA, tagB].forEach((object) => { object.visible = show; });
    if (show) {
      endpointA.position.copy(view.a);
      endpointB.position.copy(view.b);
      tagA.position.copy(view.a).add(new THREE.Vector3(0, 1.3, 0));
      tagB.position.copy(view.b).add(new THREE.Vector3(0, 1.3, 0));
      glow.position.copy(view.mid);
      glow.quaternion.copy(view.mesh.quaternion);
      glow.scale.set(0.62, view.length, 0.62);
    }
  }

  function applyCornerStyles(states = {}) {
    cornerViews.forEach((view) => {
      const state = states[view.id];
      const active = view.id === activeCorner;
      view.material.color.setHex(active ? SELECTED : state === "done" ? 0x22c55e : state === "partial" ? 0x84cc16 : 0x94a3b8);
      view.mesh.scale.setScalar(active ? 1.35 : 1);
    });
    const active = cornerViews.find((view) => view.id === activeCorner);
    cornerTag.visible = Boolean(active);
    if (active) {
      cornerTag.position.copy(active.mesh.position).add(new THREE.Vector3(0, 1.6, 0));
      cornerTag.element.textContent = `${model.cornerInfo[activeCorner].name} · ${model.cornerInfo[activeCorner].frame}`;
    }
  }
  let cornerStates = {};

  function setFrameColor(color) {
    const hex = FRAME_COLORS[color] || FRAME_COLORS.red;
    ribMaterial.color.setHex(hex);
    tieMaterial.color.setHex(hex);
  }

  function viewPosition(az, el, distance) {
    const a = THREE.MathUtils.degToRad(az);
    const e = THREE.MathUtils.degToRad(el);
    return new THREE.Vector3(Math.sin(a) * Math.cos(e), Math.sin(e), Math.cos(a) * Math.cos(e)).multiplyScalar(distance);
  }

  const baseDistance = compact ? 55 : 60;
  const focusPull = compact ? 0.1 : 0.35;
  // In the full-screen tour a caption covers the lower part, so aim a little lower
  // to lift the hive up in the frame.
  const lift = new THREE.Vector3(0, compact ? 0 : 3.2, 0);
  function flyTo(position, target, duration = 750) {
    position = position.clone().sub(lift);
    target = target.clone().sub(lift);
    flight = {
      fromPos: camera.position.clone(), toPos: position, fromTarget: controls.target.clone(), toTarget: target,
      start: performance.now(), duration,
    };
  }

  function flyToLine(index, duration) {
    const view = lineViews[index];
    if (!view) return;
    const [az, el] = view.line.view || [20, 18];
    const target = center.clone().lerp(view.mid, focusPull);
    flyTo(target.clone().add(viewPosition(az, el, baseDistance * 0.92)), target, duration);
  }

  function flyToCorner(id, duration) {
    const position = model.corners[id];
    if (!position) return;
    const p = new THREE.Vector3(...position);
    const az = THREE.MathUtils.clamp(p.x * 3.2, -55, 55) + (p.z < 0 ? Math.sign(p.x || 1) * 18 : 0);
    const el = p.y > d.eaveHeight + 1 ? 30 : p.y < 1 ? -10 : 14;
    const target = center.clone().lerp(p, focusPull * 0.85);
    flyTo(target.clone().add(viewPosition(az, el, baseDistance * 0.95)), target, duration);
  }

  function resetView(duration = 700) {
    flyTo(center.clone().add(viewPosition(28, 20, baseDistance)), center.clone(), duration);
  }

  camera.position.copy(center.clone().add(viewPosition(28, 20, baseDistance)).sub(lift));
  controls.target.copy(center).sub(lift);
  camera.lookAt(controls.target);

  // --- Picking --------------------------------------------------------------------
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let down = null;
  function pickLine(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(lineViews.map((view) => view.pick), false)[0];
    return hit ? hit.object.userData.lineIndex : -1;
  }
  renderer.domElement.addEventListener("pointerdown", (event) => { down = [event.clientX, event.clientY]; flight = null; });
  renderer.domElement.addEventListener("pointerup", (event) => {
    if (!down || mode !== "lines" || !overlay.visible) return;
    const moved = Math.hypot(event.clientX - down[0], event.clientY - down[1]);
    down = null;
    if (moved > 5) return;
    const index = pickLine(event);
    if (index >= 0) options.onSelect?.(index);
  });
  renderer.domElement.addEventListener("pointermove", (event) => {
    if (event.buttons || mode !== "lines" || !overlay.visible) return;
    const index = pickLine(event);
    if (index !== hovered) {
      hovered = index;
      renderer.domElement.style.cursor = index >= 0 ? "pointer" : "grab";
      applyLineStyles();
    }
  });
  renderer.domElement.addEventListener("pointerleave", () => { if (hovered !== -1) { hovered = -1; applyLineStyles(); } });

  // --- Highlight & explode --------------------------------------------------------
  const allMaterials = () => {
    const set = new Set();
    hive.traverse((object) => { if (object.material) set.add(object.material); });
    return [...set];
  };
  const originalOpacity = new Map(allMaterials().map((m) => [m, { opacity: m.opacity, transparent: m.transparent, emissive: m.emissive?.clone() }]));

  function highlightParts(keys) {
    highlighted = keys && keys.length ? new Set(keys) : null;
    for (const [material, original] of originalOpacity) {
      material.opacity = original.opacity;
      material.transparent = original.transparent;
      if (material.emissive) material.emissive.copy(original.emissive);
    }
    for (const [key, objects] of Object.entries(parts)) {
      const on = !highlighted || highlighted.has(key);
      objects.forEach((object) => object.traverse((child) => {
        if (!child.material) return;
        if (!child.userData.ownMaterial) {
          child.material = child.material.clone();
          child.userData.ownMaterial = true;
          originalOpacity.set(child.material, { opacity: child.material.opacity, transparent: child.material.transparent, emissive: child.material.emissive?.clone() });
        }
        const original = originalOpacity.get(child.material);
        child.material.transparent = highlighted ? true : original.transparent;
        child.material.opacity = on ? Math.max(original.opacity, highlighted && original.opacity < 0.5 ? 0.42 : original.opacity) : original.opacity * 0.18;
        if (child.material.emissive) child.material.emissive.setHex(on && highlighted ? 0x3b2f00 : 0x000000);
      }));
    }
    for (const [key, tag] of Object.entries(partTags)) tag.visible = Boolean(highlighted && highlighted.has(key));
  }

  function setFrameColorOnClones(color) {
    const hex = FRAME_COLORS[color] || FRAME_COLORS.red;
    for (const key of ["frontRib", "backRib"]) parts[key].forEach((object) => object.traverse((child) => child.material?.color?.setHex(hex)));
    parts.churros.forEach((object) => object.children.slice(1).forEach((tie) => tie.material.color.setHex(hex)));
  }

  function updateExplode() {
    for (const objects of Object.values(parts)) {
      objects.forEach((object) => {
        object.position.copy(object.userData.base).addScaledVector(object.userData.explode, explode);
      });
    }
    overlay.visible = explode < 0.05 && overlayWanted;
    cornerGroup.visible = explode < 0.05 && mode === "corners";
  }

  // --- Loop ----------------------------------------------------------------------
  const size = { width: 0, height: 0 };
  function resize() {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height || (width === size.width && height === size.height)) return;
    size.width = width;
    size.height = height;
    renderer.setSize(width, height);
    labelRenderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);

  function frame(now) {
    if (disposed) return;
    requestAnimationFrame(frame);
    if (!container.isConnected || container.offsetParent === null) return;
    resize();
    if (flight) {
      const t = Math.min(1, (now - flight.start) / flight.duration);
      const k = t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
      camera.position.lerpVectors(flight.fromPos, flight.toPos, k);
      controls.target.lerpVectors(flight.fromTarget, flight.toTarget, k);
      if (t >= 1) flight = null;
    }
    if (Math.abs(explode - explodeTarget) > 0.001) {
      explode += (explodeTarget - explode) * 0.12;
      if (Math.abs(explode - explodeTarget) < 0.002) explode = explodeTarget;
      updateExplode();
    }
    const view = lineViews[selected];
    if (view && pulse.visible) {
      const t = ((now % 1700) / 1700);
      const k = t < 0.15 ? 0 : t > 0.85 ? 1 : (t - 0.15) / 0.7;
      pulse.position.lerpVectors(view.a, view.b, k * k * (3 - 2 * k));
      const s = 1 + Math.sin(now / 160) * 0.12;
      endpointA.scale.setScalar(s);
      endpointB.scale.setScalar(s);
    }
    if (activeCorner && cornerGroup.visible) {
      const active = cornerViews.find((c) => c.id === activeCorner);
      if (active) active.mesh.scale.setScalar(1.25 + Math.sin(now / 170) * 0.25);
    }
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
    // CSS2DRenderer ignores hidden parents; hide labels whose group is hidden.
    for (const object of labelObjects) {
      for (let parent = object.parent; parent; parent = parent.parent) {
        if (!parent.visible) { object.element.style.display = "none"; break; }
      }
    }
  }
  const labelObjects = [];
  scene.traverse((object) => { if (object.isCSS2DObject) labelObjects.push(object); });
  requestAnimationFrame(frame);
  applyLineStyles();

  return {
    setFrameColor(color) { setFrameColor(color); setFrameColorOnClones(color); },
    setSelectedLine(index, { fly = true } = {}) {
      const changed = index !== selected;
      selected = index;
      applyLineStyles();
      if (changed && fly && index >= 0) flyToLine(index);
    },
    setLineStates(states) { states.forEach((state, i) => { lineStates[i] = state; }); applyLineStyles(); },
    setMode(next) {
      mode = next;
      cornerGroup.visible = mode === "corners" && explode < 0.05;
      applyLineStyles();
      applyCornerStyles(cornerStates);
    },
    setCorners(states, active, { fly = true } = {}) {
      const changed = active !== activeCorner;
      cornerStates = states || {};
      activeCorner = active;
      applyCornerStyles(cornerStates);
      if (changed && fly && active) flyToCorner(active);
    },
    showOverlay(on) { overlayWanted = on; updateExplode(); },
    showDimensions(on) { dims.visible = on; },
    showCamera(on) { cameraRig.visible = sight.visible = camTag.visible = on; },
    highlightParts,
    setExplode(value) { explodeTarget = value; },
    flyToLine,
    flyToCorner,
    resetView,
    focus(position, az, el, distance) { flyTo(new THREE.Vector3(...position).add(viewPosition(az, el, distance || baseDistance)), new THREE.Vector3(...position)); },
    setAutoRotate(on) { controls.autoRotate = on; controls.autoRotateSpeed = 1.1; },
    dispose() {
      disposed = true;
      observer.disconnect();
      renderer.dispose();
      container.replaceChildren();
    },
  };
}
