// Live presence, per-image locks, live view and draft sync over a public MQTT broker.
// No sign-up: every browser gets a random client id. Locks are enforced by this site's code.

const MQTT_SRC = "https://cdn.jsdelivr.net/npm/mqtt@5.10.1/dist/mqtt.min.js";
const BROKER = "wss://broker.emqx.io:8084/mqtt";
const ROOT = "biobuzz-hive-studio/v2/u5t2c8q1zr";
const HEARTBEAT_MS = 15000;
const PRESENCE_STALE_MS = 50000;
const PRESENCE_MAX_AGE_MS = 5 * 60 * 1000;
const LOCK_SETTLE_MS = 900;
const SYNC_SETTLE_MS = 1100;

export const topicKey = (name) => String(name).replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 140);

function loadMqtt() {
  if (window.mqtt) return Promise.resolve(window.mqtt);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = MQTT_SRC;
    script.async = true;
    const timer = setTimeout(() => reject(new Error("timeout")), 12000);
    script.onload = () => { clearTimeout(timer); window.mqtt ? resolve(window.mqtt) : reject(new Error("missing")); };
    script.onerror = () => { clearTimeout(timer); reject(new Error("load failed")); };
    document.head.appendChild(script);
  });
}

function throttle(fn, ms) {
  let last = 0;
  let timer = null;
  let pending = null;
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) {
      last = Date.now();
      fn(...pending);
      pending = null;
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        last = Date.now();
        if (pending) fn(...pending);
        pending = null;
      }, wait);
    }
  };
}

export function createRealtime(handlers = {}) {
  const id = `bb-${Math.random().toString(36).slice(2, 9)}${Date.now().toString(36).slice(-3)}`;
  const presence = new Map();
  const locks = new Map();
  let client = null;
  let user = { name: "", color: "#2563eb" };
  let status = "offline";
  let synced = false;
  let activity = { image: null, mode: "view" };
  let watched = null;
  let heartbeat = null;
  const held = new Set();

  const topic = (kind, key) => `${ROOT}/${kind}/${key}`;
  const emit = (name, ...args) => { try { handlers[name]?.(...args); } catch (error) { console.error(error); } };
  const setStatus = (next) => { if (status !== next) { status = next; emit("onStatus", next); } };
  const publish = (t, payload, opts = {}) => {
    if (!client || !client.connected) return false;
    client.publish(t, payload === null ? "" : JSON.stringify(payload), { qos: opts.qos ?? 0, retain: Boolean(opts.retain) });
    return true;
  };

  function isOnline(clientId) {
    if (clientId === id) return true;
    const entry = presence.get(clientId);
    if (!entry) return false;
    const now = Date.now();
    return now - entry.seenAt < PRESENCE_STALE_MS && Math.abs(now - (entry.data.ts || 0)) < PRESENCE_MAX_AGE_MS;
  }

  function presencePayload() {
    return { id, name: user.name, color: user.color, image: activity.image, mode: activity.mode, ts: Date.now() };
  }

  function announce() {
    publish(topic("presence", id), presencePayload(), { retain: true, qos: 1 });
  }

  function others() {
    return [...presence.values()]
      .filter((entry) => entry.data.id !== id && isOnline(entry.data.id))
      .map((entry) => entry.data);
  }

  function holderOf(imageName) {
    if (!imageName) return null;
    const entry = locks.get(topicKey(imageName));
    if (!entry || !entry.data || entry.data.owner === id) return null;
    const owner = presence.get(entry.data.owner);
    if (!owner || !isOnline(entry.data.owner) || owner.data.image !== imageName) return null;
    const fresh = Date.now() - entry.seenAt < 5000;
    if (owner.data.mode !== "edit" && !fresh) return null;
    return { id: entry.data.owner, name: owner.data.name || entry.data.name, color: owner.data.color || entry.data.color };
  }

  function watch(imageName) {
    const next = imageName ? topicKey(imageName) : null;
    if (next === watched || !client) return;
    if (watched) client.unsubscribe([topic("live", watched), topic("cursor", watched)]);
    watched = next;
    if (watched && client.connected) client.subscribe([topic("live", watched), topic("cursor", watched)], { qos: 0 });
  }

  function handleMessage(t, buffer) {
    const parts = t.slice(ROOT.length + 1).split("/");
    const [kind, key] = parts;
    const text = buffer && buffer.length ? new TextDecoder().decode(buffer) : "";
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch { return; } }
    if (kind === "presence") {
      if (key === id) return;
      if (data) presence.set(key, { data, seenAt: Date.now() }); else presence.delete(key);
      emit("onPresence", others());
      emit("onLocks");
    } else if (kind === "locks") {
      if (data) locks.set(key, { data, seenAt: Date.now() }); else locks.delete(key);
      if (data && data.owner !== id) {
        for (const name of [...held]) if (topicKey(name) === key) held.delete(name);
      }
      emit("onLocks");
    } else if (kind === "live") {
      if (data && data.id !== id) emit("onLive", key, data);
    } else if (kind === "cursor") {
      if (data && data.id !== id) emit("onCursor", key, data);
    } else if (kind === "drafts") {
      if (data && data.record) emit("onDraft", key, data);
    }
  }

  async function connect(nextUser) {
    if (nextUser) user = { ...user, ...nextUser };
    setStatus("connecting");
    let mqtt;
    try {
      mqtt = await loadMqtt();
    } catch {
      setStatus("offline");
      return;
    }
    client = mqtt.connect(BROKER, {
      clientId: id,
      clean: true,
      keepalive: 20,
      reconnectPeriod: 4000,
      connectTimeout: 10000,
      protocolVersion: 4,
      will: { topic: topic("presence", id), payload: "", qos: 1, retain: true },
    });
    client.on("connect", () => {
      synced = false;
      setStatus("syncing");
      client.subscribe([topic("presence", "+"), topic("locks", "+"), topic("drafts", "+")], { qos: 1 }, () => {
        if (watched) client.subscribe([topic("live", watched), topic("cursor", watched)], { qos: 0 });
        setTimeout(() => {
          synced = true;
          announce();
          for (const name of held) publishLock(name);
          setStatus("online");
          emit("onLocks");
        }, SYNC_SETTLE_MS);
      });
    });
    client.on("message", handleMessage);
    client.on("reconnect", () => setStatus("connecting"));
    client.on("offline", () => setStatus("offline"));
    client.on("close", () => { if (status === "online" || status === "syncing") setStatus("connecting"); });
    client.on("error", () => {});
    clearInterval(heartbeat);
    heartbeat = setInterval(() => {
      if (!client?.connected || !synced) return;
      announce();
      for (const name of held) publishLock(name);
      emit("onPresence", others());
      emit("onLocks");
    }, HEARTBEAT_MS);
  }

  function publishLock(imageName) {
    publish(topic("locks", topicKey(imageName)), {
      owner: id, name: user.name, color: user.color, image: imageName, since: Date.now(),
    }, { retain: true, qos: 1 });
  }

  async function acquire(imageName) {
    if (!client || !client.connected || !synced) return { ok: true, offline: true };
    const holder = holderOf(imageName);
    if (holder) return { ok: false, holder };
    publishLock(imageName);
    await new Promise((resolve) => setTimeout(resolve, LOCK_SETTLE_MS));
    const entry = locks.get(topicKey(imageName));
    if (entry?.data && entry.data.owner !== id) {
      const winner = holderOf(imageName) || { id: entry.data.owner, name: entry.data.name, color: entry.data.color };
      return { ok: false, holder: winner };
    }
    held.add(imageName);
    return { ok: true };
  }

  function release(imageName) {
    if (!imageName || !held.has(imageName)) return;
    held.delete(imageName);
    const key = topicKey(imageName);
    const entry = locks.get(key);
    if (!entry || !entry.data || entry.data.owner === id) {
      publish(topic("locks", key), null, { retain: true, qos: 1 });
      locks.delete(key);
    }
    publish(topic("live", key), null, { retain: true });
  }

  const sendLive = throttle((imageName, payload) => {
    publish(topic("live", topicKey(imageName)), { id, ts: Date.now(), ...payload }, { retain: true });
  }, 140);
  const sendCursor = throttle((imageName, point) => {
    publish(topic("cursor", topicKey(imageName)), { id, name: user.name, color: user.color, x: point?.[0] ?? null, y: point?.[1] ?? null, ts: Date.now() });
  }, 70);

  function leave() {
    for (const name of [...held]) release(name);
    publish(topic("presence", id), null, { retain: true, qos: 1 });
  }
  window.addEventListener("pagehide", leave);

  return {
    id,
    connect,
    get status() { return status; },
    get synced() { return synced; },
    setUser(next) { user = { ...user, ...next }; announce(); },
    setActivity(next) {
      activity = { ...activity, ...next };
      watch(activity.image);
      announce();
    },
    holderOf,
    holds: (imageName) => held.has(imageName),
    acquire,
    release,
    others,
    publishLive: (imageName, payload) => { if (held.has(imageName)) sendLive(imageName, payload); },
    publishCursor: (imageName, point) => { if (held.has(imageName)) sendCursor(imageName, point); },
    publishDraft(imageName, payload) {
      return publish(topic("drafts", topicKey(imageName)), { by: id, name: user.name, savedAt: Date.now(), ...payload }, { retain: true, qos: 1 });
    },
    whoIsOn(imageName) {
      return others().filter((entry) => entry.image === imageName);
    },
    leave,
  };
}
