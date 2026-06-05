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

  console.log("Phase 4 card smoke test: SUCCESS (all assertions passed)");
}

main().catch((err) => {
  console.error("CARD TEST FAILED:", err.message);
  process.exit(1);
});
