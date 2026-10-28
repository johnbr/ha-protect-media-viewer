/*
 * Card smoke test (jsdom): mounts protect-media-viewer-card with a mocked hass
 * and asserts the grid populates from /events, the camera selector populates
 * from /cameras, type-chip filtering re-queries, and clicking a tile opens the
 * player with the clip URL.
 */
import assert from "node:assert";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
  pretendToBeVisual: true,
});
const { window } = dom;

// Globals the card expects.
global.window = window;
global.document = window.document;
global.HTMLElement = window.HTMLElement;
global.customElements = window.customElements;
global.Node = window.Node;
global.IntersectionObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
// In a browser these are globals; map jsdom's so the card behaves the same here.
global.requestAnimationFrame = window.requestAnimationFrame
  ? window.requestAnimationFrame.bind(window)
  : (cb) => setTimeout(() => cb(Date.now()), 16);
global.cancelAnimationFrame = window.cancelAnimationFrame
  ? window.cancelAnimationFrame.bind(window)
  : (id) => clearTimeout(id);

// jsdom doesn't implement media playback; stub it so the test output is clean.
window.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
window.HTMLMediaElement.prototype.pause = function () {};
window.HTMLMediaElement.prototype.load = function () {};

// Mock hass.callApi.
let eventsCalls = [];
function makeEvent(id, types, cam) {
  return {
    id,
    camera_id: cam,
    camera_name: cam === "roof" ? "Roof" : "Garage",
    start: new Date().toISOString(),
    end: new Date().toISOString(),
    score: 80,
    smart_detect_types: types,
    thumbnail: `/api/protect_media_viewer/thumb/${id}?authSig=x`,
    clip: `/api/protect_media_viewer/clip/${id}?authSig=y`,
  };
}

const hass = {
  async callApi(method, path) {
    if (path.startsWith("protect_media_viewer/cameras")) {
      return { cameras: [{ id: "roof", name: "Roof" }, { id: "garage", name: "Garage" }] };
    }
    if (path.startsWith("protect_media_viewer/events")) {
      eventsCalls.push(path);
      const url = new URL("http://x/" + path);
      const types = url.searchParams.get("types");
      const all = [
        makeEvent("e1", ["vehicle"], "roof"),
        makeEvent("e2", ["person"], "garage"),
        makeEvent("e3", ["animal", "person"], "roof"),
      ];
      const events = types ? all.filter((e) => e.smart_detect_types.some((t) => types.split(",").includes(t))) : all;
      return { events, count: events.length, offset: 0, limit: 60, has_more: false };
    }
    throw new Error("unexpected path " + path);
  },
};

const tick = () => new Promise((r) => setTimeout(r, 0));

async function main() {
  await import("../custom_components/protect_media_viewer/frontend/protect-media-viewer-card.js");
  assert.ok(customElements.get("protect-media-viewer-card"), "element defined");
  assert.ok(window.customCards?.some((c) => c.type === "protect-media-viewer-card"), "registered in customCards");

  const el = document.createElement("protect-media-viewer-card");
  el.setConfig({ title: "Test", default_hours: 24 });
  document.body.appendChild(el); // connectedCallback
  el.hass = hass;
  await tick();
  await tick();

  const grid = el.shadowRoot.querySelector(".grid");
  assert.strictEqual(grid.children.length, 3, `grid has 3 tiles (got ${grid.children.length})`);

  const camOpts = el.shadowRoot.querySelectorAll("select.camera option");
  assert.strictEqual(camOpts.length, 3, "camera selector has All + 2 cameras");

  // Header + status reflect data.
  assert.strictEqual(el.shadowRoot.querySelector(".header").textContent, "Test");
  assert.match(el.shadowRoot.querySelector(".status").textContent, /3 detections/);

  // Tile overlay shows camera + type badge.
  const firstOverlay = grid.children[0].querySelector(".overlay").innerHTML;
  assert.match(firstOverlay, /Roof/, "first tile shows camera name");
  assert.match(firstOverlay, /mdi:car/, "first tile shows vehicle icon");

  // Filter by person -> re-query and fewer tiles.
  eventsCalls = [];
  const personChip = [...el.shadowRoot.querySelectorAll(".chip")].find((c) =>
    c.textContent.includes("Person")
  );
  personChip.click();
  await tick();
  await tick();
  assert.ok(eventsCalls.some((p) => p.includes("types=person")), "re-queried with types=person");
  assert.strictEqual(grid.children.length, 2, "person filter -> 2 tiles (e2, e3)");

  // Click a tile -> player opens with clip URL.
  grid.children[0].click();
  const modal = el.shadowRoot.querySelector(".modal");
  const video = el.shadowRoot.querySelector(".modal video");
  assert.ok(modal.classList.contains("open"), "modal opened");
  assert.match(video.getAttribute("src"), /\/clip\/e2\?authSig=y/, "video src is the signed clip URL");

  // Close clears the src (stops download).
  el.shadowRoot.querySelector(".modal .close").click();
  assert.ok(!modal.classList.contains("open"), "modal closed");
  assert.strictEqual(video.getAttribute("src"), null, "video src cleared on close");

  // --- Pagination scenario: 3 pages (60/60/30) must all load. ---
  // jsdom getBoundingClientRect() returns 0, so the auto-fill path pages through.
  const pagedHass = {
    async callApi(method, path) {
      if (path.startsWith("protect_media_viewer/cameras")) return { cameras: [] };
      const url = new URL("http://x/" + path);
      const offset = Number(url.searchParams.get("offset") || "0");
      const limit = Number(url.searchParams.get("limit") || "60");
      const TOTAL = 150;
      const events = [];
      for (let i = offset; i < Math.min(offset + limit, TOTAL); i++) {
        events.push(makeEvent("p" + i, ["vehicle"], "roof"));
      }
      return { events, count: events.length, offset, limit, has_more: offset + limit < TOTAL };
    },
  };
  const el2 = document.createElement("protect-media-viewer-card");
  el2.setConfig({ title: "Paged" });
  document.body.appendChild(el2);
  el2.hass = pagedHass;

  const grid2 = el2.shadowRoot.querySelector(".grid");
  const deadline = Date.now() + 2000;
  while (grid2.children.length < 150 && Date.now() < deadline) await tick();
  assert.strictEqual(grid2.children.length, 150, `paged to all 150 tiles (got ${grid2.children.length})`);
  assert.match(el2.shadowRoot.querySelector(".status").textContent, /150 detections/, "shows final count");

  // --- Flaky-API scenario: transient failures must self-heal via retry. ---
  // (Mobile network handoffs and HA-still-booting 400s look exactly like this.)
  let failuresLeft = 2;
  const flakyHass = {
    async callApi(method, path) {
      if (path.startsWith("protect_media_viewer/cameras")) return { cameras: [] };
      if (failuresLeft > 0) {
        failuresLeft -= 1;
        throw new Error("transient failure");
      }
      return {
        events: [makeEvent("f1", ["person"], "roof")],
        count: 1, offset: 0, limit: 60, has_more: false,
      };
    },
  };
  const el3 = document.createElement("protect-media-viewer-card");
  el3.setConfig({ title: "Flaky", retry_base_ms: 1 });
  document.body.appendChild(el3);
  const realError = console.error; // the injected failures are expected noise
  console.error = () => {};
  el3.hass = flakyHass;

  const grid3 = el3.shadowRoot.querySelector(".grid");
  const deadline3 = Date.now() + 2000;
  while (grid3.children.length < 1 && Date.now() < deadline3) await tick();
  console.error = realError;
  assert.strictEqual(grid3.children.length, 1, "flaky API self-healed after retries");
  assert.strictEqual(failuresLeft, 0, "both injected failures were consumed");
  assert.match(el3.shadowRoot.querySelector(".status").textContent, /^1 detection$/, "final status is healthy");

  // --- Accordion scenario: a `conditional` fold DETACHES the card. ---
  // hui-conditional-base.setVisibility() removes its child from the DOM when
  // the condition goes false and re-appends it when it comes back, so a fold
  // close/open is a disconnect/connect. The shadow DOM survives, so
  // _ensureBuilt() no-ops on the way back in — the document/window watchers
  // must be torn down and re-established explicitly, or the reopened card
  // keeps its tiles and silently loses infinite scroll (and leaks one keydown
  // listener per cycle).
  const ioLog = [];
  global.IntersectionObserver = class {
    constructor() {
      this.targets = 0;
      this.disconnected = false;
      ioLog.push(this);
    }
    observe() { this.targets += 1; }
    unobserve() { this.targets -= 1; }
    disconnect() { this.targets = 0; this.disconnected = true; }
  };

  // Count only what happens from here on (the cards above keep theirs).
  const live = { scroll: 0, resize: 0, keydown: 0 };
  for (const target of [window, document]) {
    const add = target.addEventListener.bind(target);
    const remove = target.removeEventListener.bind(target);
    target.addEventListener = (type, fn, opts) => {
      if (type in live) live[type] += 1;
      return add(type, fn, opts);
    };
    target.removeEventListener = (type, fn, opts) => {
      if (type in live) live[type] -= 1;
      return remove(type, fn, opts);
    };
  }

  const el4 = document.createElement("protect-media-viewer-card");
  el4.setConfig({ title: "Fold" });
  document.body.appendChild(el4);
  el4.hass = hass;
  await tick();
  await tick();

  assert.strictEqual(
    el4.shadowRoot.querySelector(".grid").children.length, 3, "fold card built"
  );
  assert.deepStrictEqual(live, { scroll: 1, resize: 1, keydown: 1 }, "watchers attached on build");
  assert.strictEqual(ioLog.at(-1).targets, 1, "sentinel observed");

  for (const cycle of [1, 2]) {
    const io = ioLog.at(-1);

    el4.remove(); // fold closes
    assert.ok(io.disconnected, `cycle ${cycle}: observer disconnected on detach`);
    assert.deepStrictEqual(live, { scroll: 0, resize: 0, keydown: 0 },
      `cycle ${cycle}: every watcher released on detach`);

    document.body.appendChild(el4); // fold reopens
    assert.strictEqual(el4.shadowRoot.querySelector(".grid").children.length, 3,
      `cycle ${cycle}: tiles survived the fold`);
    assert.deepStrictEqual(live, { scroll: 1, resize: 1, keydown: 1 },
      `cycle ${cycle}: watchers restored exactly once`);
    assert.notStrictEqual(ioLog.at(-1), io, `cycle ${cycle}: a fresh observer is watching`);
    assert.strictEqual(ioLog.at(-1).targets, 1, `cycle ${cycle}: sentinel observed again`);
  }

  // --- Saved clips: badges, Save from the player, Saved mode, Remove. ---
  const calls = [];
  const savedStore = new Map(); // id -> saved event
  const savedInfo = (note) => ({ saved_at: new Date().toISOString(), saved_by: "Alex", note, size: 1, protect_flagged: true });
  let saving = { available: true, reason: null, count: 0, can_remove: true };
  const savedHass = {
    async callApi(method, path, data) {
      calls.push({ method, path, data });
      if (path.startsWith("protect_media_viewer/cameras")) return { cameras: [] };
      if (method === "GET" && path.startsWith("protect_media_viewer/events")) {
        const flagged = { ...makeEvent("s2", ["person"], "roof"), flagged: true };
        const already = { ...makeEvent("s3", ["vehicle"], "roof"), saved: savedInfo("old"), download: "/dl/s3" };
        return { events: [makeEvent("s1", ["person"], "roof"), flagged, already], count: 3, offset: 0, limit: 60, has_more: false, saving };
      }
      if (method === "GET" && path.startsWith("protect_media_viewer/saved")) {
        const events = [...savedStore.values()];
        return { events, count: events.length, total: events.length, offset: 0, limit: 60, has_more: false, saving };
      }
      const m = path.match(/^protect_media_viewer\/saved\/([^?]+)/);
      if (m && method === "POST") {
        const ev = { ...makeEvent(m[1], ["person"], "roof"), flagged: true, saved: savedInfo(data.note),
          clip: `/api/protect_media_viewer/saved-clip/${m[1]}?t=z`, download: `/api/protect_media_viewer/saved-clip/${m[1]}?t=z&download=1` };
        savedStore.set(m[1], ev);
        return { created: true, event: ev };
      }
      if (m && method === "DELETE") {
        savedStore.delete(m[1]);
        return null;
      }
      throw new Error(`unexpected ${method} ${path}`);
    },
  };
  window.confirm = () => true;

  const el5 = document.createElement("protect-media-viewer-card");
  el5.setConfig({ title: "Saved" });
  document.body.appendChild(el5);
  el5.hass = savedHass;
  await tick();
  await tick();
  const root5 = el5.shadowRoot;
  const grid5 = root5.querySelector(".grid");
  assert.strictEqual(grid5.children.length, 3, "saved test: 3 detections");
  assert.ok(!grid5.children[0].querySelector(".marks").innerHTML.includes("bookmark"), "unsaved tile has no bookmark");
  assert.match(grid5.children[1].querySelector(".marks").innerHTML, /mdi:flag/, "Protect-flagged tile shows the flag");
  assert.match(grid5.children[2].querySelector(".marks").innerHTML, /mdi:bookmark/, "already-saved tile shows the bookmark");
  assert.ok(root5.querySelector("input.search").classList.contains("hidden"), "search box hidden in detections mode");

  // Save from the player, with a note.
  grid5.children[0].click();
  const actions = root5.querySelector(".modal .actions");
  actions.querySelector("input.note-input").value = "package thief";
  actions.querySelector("button.save").click();
  await tick();
  await tick();
  const post = calls.find((c) => c.method === "POST");
  assert.ok(post && post.path.startsWith("protect_media_viewer/saved/s1"), "POSTed the save");
  assert.strictEqual(post.data.note, "package thief", "note sent with the save");
  assert.match(grid5.children[0].querySelector(".marks").innerHTML, /mdi:bookmark/, "tile re-badged after save");
  assert.match(actions.textContent, /Saved by Alex/, "player shows the saved state");
  assert.match(actions.textContent, /package thief/, "player shows the note");
  assert.match(actions.querySelector("a.download").getAttribute("href"), /download=1/, "download link offered");
  assert.ok(actions.querySelector("button.remove"), "admin gets Remove");
  root5.querySelector(".modal .close").click();

  // Already-saved event: no Save button, Download straight away.
  grid5.children[2].click();
  assert.ok(!actions.querySelector("button.save"), "no Save on a saved clip");
  assert.strictEqual(actions.querySelector("a.download").getAttribute("href"), "/dl/s3");
  root5.querySelector(".modal .close").click();

  // Saved mode: different endpoint, ranges and the search box.
  calls.length = 0;
  root5.querySelector('.modes button[data-mode="saved"]').click();
  await tick();
  await tick();
  const listCall = calls.find((c) => c.path.startsWith("protect_media_viewer/saved?"));
  assert.ok(listCall && listCall.path.includes("days=0"), "saved mode lists with days=0 (All)");
  assert.deepStrictEqual(
    [...root5.querySelectorAll("select.time option")].map((o) => o.textContent), ["30d", "1y", "All"], "saved ranges"
  );
  assert.ok(!root5.querySelector("input.search").classList.contains("hidden"), "search box shown in saved mode");
  assert.strictEqual(grid5.children.length, 1, "one saved clip listed");
  assert.match(root5.querySelector(".status").textContent, /^1 saved clip$/, "singular count");

  calls.length = 0;
  const search = root5.querySelector("input.search");
  search.value = "thief";
  search.dispatchEvent(new window.Event("input"));
  await new Promise((r) => setTimeout(r, 350));
  await tick();
  assert.ok(calls.some((c) => c.path.includes("q=thief")), "search re-queries with q=");

  // Remove from Saved mode: DELETE, tile gone, player closed.
  grid5.children[0].click();
  actions.querySelector("button.remove").click();
  await tick();
  await tick();
  assert.ok(calls.some((c) => c.method === "DELETE" && c.path.startsWith("protect_media_viewer/saved/s1")), "DELETE sent");
  assert.strictEqual(grid5.children.length, 0, "removed clip leaves the saved list");
  assert.ok(!root5.querySelector(".modal").classList.contains("open"), "player closed after remove");

  // Non-admin: no Remove. Unavailable store: no Save, the reason instead.
  saving = { available: true, reason: null, count: 1, can_remove: false };
  root5.querySelector('.modes button[data-mode="detections"]').click();
  await tick();
  await tick();
  grid5.children[2].click();
  assert.ok(!actions.querySelector("button.remove"), "non-admin gets no Remove");
  root5.querySelector(".modal .close").click();

  saving = { available: false, reason: "the saved-clips directory has no marker file", count: 0, can_remove: true };
  root5.querySelector('.modes button[data-mode="saved"]').click();
  await tick();
  await tick();
  root5.querySelector('.modes button[data-mode="detections"]').click();
  await tick();
  await tick();
  grid5.children[0].click();
  assert.ok(!actions.querySelector("button.save"), "no Save while the store is unavailable");
  assert.match(actions.textContent, /Saving unavailable: .*marker/, "says why saving is off");
  root5.querySelector(".modal .close").click();

  // show_saved: false hides the toggle; allow_save: false hides Save.
  const el6 = document.createElement("protect-media-viewer-card");
  el6.setConfig({ show_saved: false, allow_save: false, default_mode: "saved" });
  saving = { available: true, reason: null, count: 0, can_remove: true };
  document.body.appendChild(el6);
  el6.hass = savedHass;
  await tick();
  await tick();
  assert.ok(el6.shadowRoot.querySelector(".modes").classList.contains("hidden"), "show_saved:false hides the toggle");
  assert.strictEqual(el6.shadowRoot.querySelector(".grid").children.length, 3, "default_mode ignored without show_saved");
  el6.shadowRoot.querySelector(".grid").children[0].click();
  assert.ok(!el6.shadowRoot.querySelector(".modal .actions button.save"), "allow_save:false hides Save");

  // --- Signed media URLs: never used once lapsed. ---
  // A ttl of 60 s is inside the card's refresh lead, so these URLs count as
  // stale straight away -- every path that would use one must re-sign first.
  const mediaCalls = [];
  let signs = 0;
  const conn = {
    listeners: {},
    addEventListener(type, cb) { this.listeners[type] = cb; },
    removeEventListener(type) { delete this.listeners[type]; },
  };
  const signedHass = {
    connection: conn,
    async callApi(method, path, body) {
      if (path.startsWith("protect_media_viewer/cameras")) return { cameras: [] };
      if (path.startsWith("protect_media_viewer/media")) {
        mediaCalls.push(body);
        signs += 1;
        const media = {};
        for (const id of body.ids) {
          media[id] = {
            thumbnail: `/api/protect_media_viewer/thumb/${id}?authSig=fresh${signs}`,
            clip: `/api/protect_media_viewer/clip/${id}?authSig=fresh${signs}`,
          };
        }
        return { media, media_ttl: 43200 };
      }
      const events = [makeEvent("m1", ["person"], "roof"), makeEvent("m2", ["vehicle"], "garage")];
      return { events, count: 2, offset: 0, limit: 60, has_more: false, media_ttl: 60 };
    },
  };
  const el7 = document.createElement("protect-media-viewer-card");
  el7.setConfig({});
  document.body.appendChild(el7);
  el7.hass = signedHass;
  await tick();
  await tick();
  const root7 = el7.shadowRoot;
  const grid7 = root7.querySelector(".grid");
  assert.strictEqual(grid7.children.length, 2, "signed: two tiles");
  assert.ok(conn.listeners.ready, "signed: watches the connection for reconnects");

  // Opening the player with lapsed URLs re-signs that clip first.
  grid7.children[0].click();
  await tick();
  await tick();
  assert.deepStrictEqual(mediaCalls.at(-1), { ids: ["m1"], saved: false }, "signed: player re-signs its clip");
  assert.match(root7.querySelector(".modal video").getAttribute("src"), /\/clip\/m1\?authSig=fresh/, "signed: video gets the fresh URL");
  root7.querySelector(".modal .close").click();

  // A reconnect (a Home Assistant restart ends every signed URL) re-signs the lot,
  // and a thumbnail not fetched yet takes its fresh URL at once.
  conn.listeners.ready();
  await tick();
  await tick();
  assert.deepStrictEqual(mediaCalls.at(-1), { ids: ["m1", "m2"], saved: false }, "signed: reconnect re-signs every event");
  assert.match(grid7.children[1].querySelector("img").getAttribute("src"), /\/thumb\/m2\?authSig=fresh/, "signed: unloaded thumbnail swapped");

  // Thumbnails failing together are re-signed in one request.
  const before = mediaCalls.length;
  for (const tile of grid7.children) tile.querySelector("img").dispatchEvent(new window.Event("error"));
  await new Promise((r) => setTimeout(r, 150));
  assert.strictEqual(mediaCalls.length, before + 1, "signed: one batched re-sign for a page of errors");
  assert.deepStrictEqual(mediaCalls.at(-1).ids.sort(), ["m1", "m2"], "signed: the batch names every failed tile");
  el7.remove(); // stops its refresh timer

  console.log("Card smoke test: SUCCESS (all assertions passed)");
}

main().catch((err) => {
  console.error("CARD TEST FAILED:", err.message);
  process.exit(1);
});
