/*
 * Protect Media Viewer card
 *
 * A no-build custom card: browse UniFi Protect smart-detection events as a fast,
 * infinite-scrolling thumbnail grid with detection-type filter chips, and play
 * clips inline. Clips can be saved (kept outside Protect, so they outlive the
 * NVR's retention) and browsed in a second "Saved" mode. Talks to the
 * protect_media_viewer integration's HTTP API.
 */

const TYPES = [
  { key: "person", label: "Person", icon: "mdi:walk" },
  { key: "vehicle", label: "Vehicle", icon: "mdi:car" },
  { key: "animal", label: "Animal", icon: "mdi:paw" },
  { key: "face", label: "Face", icon: "mdi:face-recognition" },
  { key: "licensePlate", label: "Plate", icon: "mdi:card-text-outline" },
];

const TIME_RANGES = [
  { label: "1h", hours: 1 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
];

// Saved mode looks back further: saved clips are old by definition.
const SAVED_RANGES = [
  { label: "30d", days: 30 },
  { label: "1y", days: 365 },
  { label: "All", days: 0 },
];

const ICON_BY_TYPE = Object.fromEntries(TYPES.map((t) => [t.key, t.icon]));

// Recognition chips (backend recognition.py): at most this many per tile; the
// tooltip and the player caption always list everything.
const RECOG_MAX_CHIPS = 2;
const RECOG_ICON = {
  "face:known": "mdi:account-check",
  "face:unknown": "mdi:account-question",
  "face:unidentified": "mdi:account-question",
  "plate:known": "mdi:car-search",
  "plate:unknown": "mdi:card-text-outline",
};

// How many times to retry a thumbnail that fails to load before marking it broken.
const THUMB_MAX_RETRIES = 5;

class ProtectMediaViewerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._built = false;
    this._events = [];
    this._offset = 0;
    this._hasMore = true;
    this._loading = false;
    this._selectedTypes = new Set();
    this._camera = "";
    this._hours = 24;
    this._io = null;
    this._onScroll = null;
    this._onKey = null;
    this._watching = false;
    this._cameras = [];
    this._mode = "detections";
    this._days = 0;
    this._query = "";
    this._saving = null; // {available, reason, can_remove, count} from the API
    this._tiles = new Map(); // event id -> tile, so a save/remove can re-badge it
    this._current = null; // the event open in the player
    this._gen = 0; // bumped by _reset(); responses from an older query are dropped
  }

  setConfig(config) {
    this._config = config || {};
    this._hours = this._config.default_hours || 24;
    this._selectedTypes = new Set(this._config.default_types || []);
    this._mode =
      this._config.default_mode === "saved" && this._config.show_saved !== false ? "saved" : "detections";
    if (this._built) {
      // Rebuild controls to reflect new config defaults.
      this._built = false;
      this.shadowRoot.innerHTML = "";
      this._ensureBuilt();
    }
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureBuilt();
  }

  connectedCallback() {
    // Fires MORE THAN ONCE. A `conditional` card removes its child from the
    // DOM when the condition goes false and re-appends it when it comes back
    // (hui-conditional-base.setVisibility), so an accordion fold detaches and
    // reattaches this card on every close/open. `_ensureBuilt()` no-ops after
    // the first build — the shadow DOM survives the detach — so the watchers
    // that disconnectedCallback() tore down have to be re-attached here, or a
    // reopened fold keeps its tiles but has lost infinite scroll.
    this._ensureBuilt();
    this._attachWatchers();
  }

  disconnectedCallback() {
    this._detachWatchers();
  }

  // ---- document/window watchers -------------------------------------------
  // Kept out of _render() so they can be re-established on reattach without
  // rebuilding (and torn down on detach) — see connectedCallback().

  _attachWatchers() {
    if (this._watching || !this._built || !this.isConnected) return;
    if (!this._sentinel) return;
    this._watching = true;

    // Infinite scroll. We use several triggers because an IntersectionObserver
    // alone is unreliable inside Home Assistant's nested scroll containers:
    //  - the observer (works when it fires),
    //  - a capturing scroll listener on window (catches scrolling in any
    //    ancestor scroll container, since scroll events don't bubble),
    //  - a resize listener,
    //  - an auto-fill after each page so the first screenful always fills.
    // Observing an already-visible sentinel delivers an initial callback, so a
    // reattached card that is short of a screenful pages itself back up.
    this._io = new IntersectionObserver(() => this._maybeLoadMore(), {
      root: this._scrollEl,
      rootMargin: "800px",
    });
    this._io.observe(this._sentinel);

    // Throttle to one check per frame (scroll fires very frequently; the check
    // calls getBoundingClientRect which forces layout).
    this._onScroll = () => {
      if (this._scrollScheduled) return;
      this._scrollScheduled = true;
      requestAnimationFrame(() => {
        this._scrollScheduled = false;
        this._maybeLoadMore();
      });
    };
    window.addEventListener("scroll", this._onScroll, true);
    window.addEventListener("resize", this._onScroll, true);

    // Escape closes the player. On `document`, so it must come off again on
    // detach — otherwise every fold close leaves a listener holding this card.
    this._onKey = (e) => {
      if (e.key === "Escape") this._closePlayer();
    };
    document.addEventListener("keydown", this._onKey);
  }

  _detachWatchers() {
    this._watching = false;
    if (this._io) {
      this._io.disconnect();
      this._io = null;
    }
    if (this._onScroll) {
      window.removeEventListener("scroll", this._onScroll, true);
      window.removeEventListener("resize", this._onScroll, true);
      this._onScroll = null;
    }
    if (this._onKey) {
      document.removeEventListener("keydown", this._onKey);
      this._onKey = null;
    }
  }

  getCardSize() {
    return 12;
  }

  // ---- query helpers ------------------------------------------------------

  _entryParam() {
    return this._config.entry ? `entry=${encodeURIComponent(this._config.entry)}` : "";
  }

  async _api(path) {
    return this._hass.callApi("GET", path);
  }

  _savedPath(id) {
    const ep = this._entryParam();
    return `protect_media_viewer/saved/${encodeURIComponent(id)}${ep ? "?" + ep : ""}`;
  }

  _isSavedMode() {
    return this._mode === "saved";
  }

  _pageSize() {
    return this._config.page_size || 60;
  }

  // ---- lifecycle ----------------------------------------------------------

  _ensureBuilt() {
    if (this._built || !this._hass || !this.isConnected) return;
    this._built = true;
    this._render();
    this._loadCameras();
    this._reset();
  }

  _reset() {
    this._gen += 1;
    this._loading = false; // an in-flight page belongs to the old query now
    this._events = [];
    this._tiles.clear();
    this._offset = 0;
    this._hasMore = true;
    this._loadRetries = 0;
    this._grid.innerHTML = "";
    this._setStatus("");
    this._loadMore();
  }

  async _loadCameras() {
    try {
      const ep = this._entryParam();
      const res = await this._api(`protect_media_viewer/cameras${ep ? "?" + ep : ""}`);
      this._cameras = res.cameras || [];
      this._renderCameraOptions();
    } catch (err) {
      // Non-fatal: camera selector just stays as "All cameras".
      console.warn("protect-media-viewer: failed to load cameras", err);
    }
  }

  async _loadMore() {
    if (this._loading || !this._hasMore) return;
    const gen = this._gen;
    const saved = this._isSavedMode();
    this._loading = true;
    this._setStatus(this._events.length ? "Loading more…" : "Loading…");

    const params = [];
    const ep = this._entryParam();
    if (ep) params.push(ep);
    if (saved) {
      params.push(`days=${this._days}`);
      if (this._query) params.push(`q=${encodeURIComponent(this._query)}`);
    } else {
      params.push(`hours=${this._hours}`);
    }
    params.push(`limit=${this._pageSize()}`);
    params.push(`offset=${this._offset}`);
    if (this._selectedTypes.size) params.push(`types=${[...this._selectedTypes].join(",")}`);
    if (this._camera) params.push(`camera=${encodeURIComponent(this._camera)}`);

    try {
      const res = await this._api(`protect_media_viewer/${saved ? "saved" : "events"}?${params.join("&")}`);
      if (gen !== this._gen) return; // filters changed while this page was in flight
      this._loadRetries = 0;
      if (res.saving) this._saving = res.saving;
      const events = res.events || [];
      this._offset += res.limit || this._pageSize();
      this._hasMore = !!res.has_more;
      for (const ev of events) this._appendTile(ev);
      this._events.push(...events);

      if (!this._events.length && !this._hasMore) {
        this._setStatus(
          saved
            ? this._saving && this._saving.available === false
              ? `Saved clips unavailable: ${this._saving.reason}`
              : "No saved clips match these filters."
            : "No detections found for these filters."
        );
      } else if (!this._hasMore) {
        this._setStatus(this._countText(this._events.length, saved));
      } else {
        // More available — show a running count and keep paging if needed.
        this._setStatus(`${this._countText(this._events.length, saved)} — scroll for more…`);
        // If a camera filter dropped the whole page, keep paging automatically.
        if (!events.length) {
          this._loading = false;
          this._loadMore();
          return;
        }
        // Auto-fill: if the page didn't push the sentinel past the viewport,
        // load the next one (also covers the "observer never fires" case).
        this._loading = false;
        requestAnimationFrame(() => this._maybeLoadMore());
        return;
      }
    } catch (err) {
      if (gen !== this._gen) return;
      console.error("protect-media-viewer: events query failed", err);
      // Transient failures are normal on mobile (network handoff, page loaded
      // while HA is still booting so the API 400s until the entry is ready) —
      // retry with backoff before settling into the failed state.
      this._loadRetries = (this._loadRetries || 0) + 1;
      if (this._loadRetries <= 5) {
        this._setStatus("Failed to load events — retrying…");
        const base = this._config.retry_base_ms || 2000;
        const delay = Math.min(30000, base * 2 ** (this._loadRetries - 1));
        setTimeout(() => {
          if (!this.isConnected || gen !== this._gen) return;
          if (!this._cameras.length) this._loadCameras();
          this._loadMore();
        }, delay);
      } else {
        this._setStatus("Failed to load events.");
        this._hasMore = false;
      }
    } finally {
      if (gen === this._gen) this._loading = false;
    }
  }

  // ---- rendering ----------------------------------------------------------

  _render() {
    // Replacing the shadow DOM orphans anything watching the old nodes (the
    // setConfig rebuild path lands here too).
    this._detachWatchers();

    const minCol = this._config.columns || 180;
    // Bounded height for the scrolling grid area; accepts any CSS length.
    const scrollHeight = this._config.height || "70vh";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px; }
        .header { font-size: 1.2em; font-weight: 500; padding: 4px 4px 10px; }
        .toolbar {
          display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
          padding: 0 4px 12px; border-bottom: 1px solid var(--divider-color);
          margin-bottom: 12px;
        }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .chip {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 5px 10px; border-radius: 16px; cursor: pointer;
          background: var(--secondary-background-color);
          color: var(--primary-text-color); font-size: 0.85em; user-select: none;
          border: 1px solid transparent;
        }
        .chip ha-icon { --mdc-icon-size: 16px; }
        .chip[aria-pressed="true"] {
          background: var(--primary-color); color: var(--text-primary-color, #fff);
        }
        select {
          padding: 5px 8px; border-radius: 8px; font-size: 0.85em;
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
        }
        .spacer { flex: 1; }
        .modes { display: inline-flex; border-radius: 16px; overflow: hidden;
          border: 1px solid var(--divider-color); }
        .modes button {
          border: none; padding: 5px 12px; font-size: 0.85em; cursor: pointer;
          background: var(--secondary-background-color); color: var(--primary-text-color);
        }
        .modes button[aria-pressed="true"] {
          background: var(--primary-color); color: var(--text-primary-color, #fff);
        }
        input.search {
          padding: 5px 8px; border-radius: 8px; font-size: 0.85em; min-width: 0; width: 11em;
          background: var(--secondary-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color);
        }
        .hidden { display: none !important; }
        /* The header + toolbar stay put; only this area scrolls. */
        .scroll {
          max-height: ${scrollHeight};
          overflow-y: auto;
          overscroll-behavior: contain;
          padding-right: 2px;
        }
        .grid {
          display: grid; gap: 8px;
          grid-template-columns: repeat(auto-fill, minmax(${minCol}px, 1fr));
        }
        .tile {
          position: relative; aspect-ratio: 16 / 9; border-radius: 8px;
          overflow: hidden; cursor: pointer; background: var(--secondary-background-color);
        }
        .tile img {
          width: 100%; height: 100%; object-fit: cover; display: block;
          transition: transform 0.15s ease;
        }
        .tile:hover img { transform: scale(1.04); }
        .tile.failed::after {
          content: ""; position: absolute; inset: 0;
          background:
            var(--secondary-background-color)
            url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="gray" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="m3 16 5-5 4 4"/><path d="m14 13 2-2 5 5"/><line x1="3" y1="3" x2="21" y2="21"/></svg>')
            center / 36px no-repeat;
        }
        .tile .overlay {
          position: absolute; left: 0; right: 0; bottom: 0;
          padding: 14px 6px 4px;
          background: linear-gradient(transparent, rgba(0,0,0,0.7));
          color: #fff; font-size: 0.72em;
          display: flex; align-items: center; justify-content: space-between; gap: 4px;
        }
        .tile .badges { display: flex; gap: 2px; }
        /* Saved / flagged-in-Protect marks, top-right. */
        .tile .marks {
          position: absolute; top: 4px; right: 4px; display: flex; gap: 3px;
          pointer-events: none;
        }
        .tile .marks ha-icon {
          --mdc-icon-size: 16px; color: #fff; background: rgba(0,0,0,0.6);
          border-radius: 50%; padding: 3px;
        }
        .tile .marks .saved { background: rgba(3,169,244,0.9); }
        .tile .badges ha-icon { --mdc-icon-size: 15px; }
        .tile .when { white-space: nowrap; }
        .tile .cam { opacity: 0.85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        /* Recognition chips: green = named in Protect, amber = recognised but
           unnamed. Unidentified faces get no chip (they are most face events). */
        .tile .recog {
          /* right: clear of the two .marks icons (2 x 22px + gap, top-right) */
          position: absolute; top: 4px; left: 4px; right: 56px;
          display: flex; flex-wrap: wrap; gap: 3px; pointer-events: none;
        }
        .rchip {
          display: inline-flex; align-items: center; gap: 3px; max-width: 100%;
          padding: 1px 6px 1px 4px; border-radius: 10px; font-size: 0.7em; color: #fff;
          background: rgba(0,0,0,0.6); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .rchip ha-icon { --mdc-icon-size: 13px; flex: none; }
        .rchip.known { background: rgba(46,125,50,0.88); }
        .rchip.unknown { background: rgba(200,120,0,0.88); }
        .rchip .pct { opacity: 0.8; }
        .status { text-align: center; padding: 16px; color: var(--secondary-text-color); }
        .sentinel { height: 1px; }
        /* player modal */
        .modal {
          position: fixed; inset: 0; background: rgba(0,0,0,0.85);
          display: none; align-items: center; justify-content: center; z-index: 9999;
        }
        .modal.open { display: flex; }
        .modal .box { position: relative; max-width: 92vw; max-height: 88vh; }
        .modal video { max-width: 92vw; max-height: 80vh; border-radius: 8px; background: #000; }
        .modal .caption { color: #ddd; font-size: 0.85em; padding-top: 8px; text-align: center; }
        .modal .actions {
          display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: center;
          padding-top: 10px; color: #ddd; font-size: 0.85em;
        }
        .modal .actions input {
          padding: 6px 8px; border-radius: 8px; border: 1px solid #666; min-width: 0;
          width: min(22em, 60vw); background: #222; color: #eee; font-size: 0.95em;
        }
        .modal .btn {
          display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px;
          border-radius: 16px; border: none; cursor: pointer; font-size: 0.95em;
          background: var(--primary-color); color: #fff; text-decoration: none;
        }
        .modal .btn ha-icon { --mdc-icon-size: 16px; }
        .modal .btn.secondary { background: #444; }
        .modal .btn.danger { background: var(--error-color, #db4437); }
        .modal .btn[disabled] { opacity: 0.6; cursor: default; }
        .modal .saved-label { color: #8fd3ff; }
        .modal .note { font-style: italic; color: #eee; }
        .modal .msg { width: 100%; text-align: center; color: #ffb4a8; }
        .modal .close {
          position: absolute; top: -14px; right: -14px; width: 34px; height: 34px;
          border-radius: 50%; background: var(--primary-color); color: #fff;
          border: none; cursor: pointer; font-size: 18px; line-height: 34px;
        }
      </style>
      <ha-card>
        <div class="header"></div>
        <div class="toolbar">
          <div class="modes">
            <button data-mode="detections">Detections</button><button data-mode="saved">Saved</button>
          </div>
          <div class="chips"></div>
          <div class="spacer"></div>
          <input class="search" type="search" placeholder="Search notes" aria-label="Search saved clips">
          <select class="camera"><option value="">All cameras</option></select>
          <select class="time"></select>
        </div>
        <div class="scroll">
          <div class="grid"></div>
          <div class="status"></div>
          <div class="sentinel"></div>
        </div>
      </ha-card>
      <div class="modal">
        <div class="box">
          <button class="close" title="Close">×</button>
          <video controls autoplay playsinline></video>
          <div class="caption"></div>
          <div class="actions"></div>
        </div>
      </div>
    `;

    this._grid = this.shadowRoot.querySelector(".grid");
    this._scrollEl = this.shadowRoot.querySelector(".scroll");
    this._statusEl = this.shadowRoot.querySelector(".status");
    this._cameraSel = this.shadowRoot.querySelector("select.camera");
    this._timeSel = this.shadowRoot.querySelector("select.time");
    this._modal = this.shadowRoot.querySelector(".modal");
    this._video = this.shadowRoot.querySelector(".modal video");
    this._caption = this.shadowRoot.querySelector(".modal .caption");
    this._actions = this.shadowRoot.querySelector(".modal .actions");
    this._modesEl = this.shadowRoot.querySelector(".modes");
    this._searchEl = this.shadowRoot.querySelector("input.search");

    this.shadowRoot.querySelector(".header").textContent =
      this._config.title || "Protect Smart Detections";

    this._renderChips();
    this._renderModes();

    this._cameraSel.addEventListener("change", () => {
      this._camera = this._cameraSel.value;
      this._reset();
    });
    this._timeSel.addEventListener("change", () => {
      if (this._isSavedMode()) this._days = Number(this._timeSel.value);
      else this._hours = Number(this._timeSel.value);
      this._reset();
    });
    for (const btn of this._modesEl.querySelectorAll("button")) {
      btn.addEventListener("click", () => {
        if (btn.dataset.mode === this._mode) return;
        this._mode = btn.dataset.mode;
        this._renderModes();
        this._reset();
      });
    }
    // Debounced: every keystroke would otherwise be a request.
    this._searchEl.addEventListener("input", () => {
      clearTimeout(this._searchTimer);
      this._searchTimer = setTimeout(() => {
        const q = this._searchEl.value.trim();
        if (q === this._query) return;
        this._query = q;
        this._reset();
      }, 300);
    });

    // Player modal close handlers. These live on shadow-DOM nodes, so they go
    // away with the nodes themselves — unlike the document/window watchers,
    // which _attachWatchers() owns.
    const close = () => this._closePlayer();
    this.shadowRoot.querySelector(".modal .close").addEventListener("click", close);
    this._modal.addEventListener("click", (e) => {
      if (e.target === this._modal) close();
    });

    this._sentinel = this.shadowRoot.querySelector(".sentinel");
    this._attachWatchers();
  }

  _maybeLoadMore() {
    if (this._loading || !this._hasMore || !this._sentinel) return;
    const sentinelTop = this._sentinel.getBoundingClientRect().top;
    // Measure against the inner scroll container's bottom (the grid scrolls
    // there, not the window), falling back to the viewport.
    const bottom = this._scrollEl
      ? this._scrollEl.getBoundingClientRect().bottom
      : window.innerHeight || document.documentElement.clientHeight;
    if (sentinelTop <= bottom + 800) this._loadMore();
  }

  _renderChips() {
    const wrap = this.shadowRoot.querySelector(".chips");
    wrap.innerHTML = "";
    for (const t of TYPES) {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.setAttribute("aria-pressed", this._selectedTypes.has(t.key) ? "true" : "false");
      chip.innerHTML = `<ha-icon icon="${t.icon}"></ha-icon><span>${t.label}</span>`;
      chip.addEventListener("click", () => {
        if (this._selectedTypes.has(t.key)) this._selectedTypes.delete(t.key);
        else this._selectedTypes.add(t.key);
        chip.setAttribute("aria-pressed", this._selectedTypes.has(t.key) ? "true" : "false");
        this._reset();
      });
      wrap.appendChild(chip);
    }
  }

  // Mode toggle, plus the controls that differ between the two modes.
  _renderModes() {
    const saved = this._isSavedMode();
    this._modesEl.classList.toggle("hidden", this._config.show_saved === false);
    for (const btn of this._modesEl.querySelectorAll("button")) {
      btn.setAttribute("aria-pressed", btn.dataset.mode === this._mode ? "true" : "false");
    }
    this._searchEl.classList.toggle("hidden", !saved);
    this._renderTimeOptions();
  }

  _renderTimeOptions() {
    this._timeSel.innerHTML = "";
    const saved = this._isSavedMode();
    const ranges = saved
      ? SAVED_RANGES.map((r) => ({ label: r.label, value: r.days }))
      : TIME_RANGES.map((r) => ({ label: r.label, value: r.hours }));
    const current = saved ? this._days : this._hours;
    for (const r of ranges) {
      const opt = document.createElement("option");
      opt.value = String(r.value);
      opt.textContent = r.label;
      if (r.value === current) opt.selected = true;
      this._timeSel.appendChild(opt);
    }
  }

  _renderCameraOptions() {
    const current = this._cameraSel.value;
    this._cameraSel.innerHTML = '<option value="">All cameras</option>';
    for (const c of this._cameras) {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name;
      this._cameraSel.appendChild(opt);
    }
    this._cameraSel.value = current;
  }

  _appendTile(ev) {
    const tile = document.createElement("div");
    tile.className = "tile";

    const img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = (ev.smart_detect_types || []).join(", ");
    img.dataset.attempt = "0";
    // Self-heal: a thumbnail can briefly 404 (e.g. the NVR hasn't finished
    // generating it). Retry with backoff before giving up, and cache-bust each
    // retry so the browser doesn't reuse the failed response.
    img.addEventListener("error", () => this._onThumbError(tile, img, ev));
    img.addEventListener("load", () => tile.classList.remove("failed"));
    img.src = ev.thumbnail;

    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const badges = (ev.smart_detect_types || [])
      .map((t) => ICON_BY_TYPE[t])
      .filter(Boolean)
      .map((icon) => `<ha-icon icon="${icon}"></ha-icon>`)
      .join("");
    overlay.innerHTML =
      `<span class="cam">${this._escape(ev.camera_name || "")}</span>` +
      `<span class="when">${this._fmtTime(ev.start, this._isSavedMode())}</span>` +
      `<span class="badges">${badges}</span>`;

    tile.appendChild(img);
    tile.appendChild(overlay);

    const recog = (ev.recognition || []).filter((r) => r.verdict !== "unidentified");
    if (recog.length) {
      const chips = document.createElement("div");
      chips.className = "recog";
      const shown = recog.slice(0, RECOG_MAX_CHIPS);
      chips.innerHTML = shown
        .map((r) => {
          const icon = RECOG_ICON[`${r.kind}:${r.verdict}`] || "mdi:help";
          const pct = r.confidence != null ? ` <span class="pct">${this._escape(String(r.confidence))}%</span>` : "";
          return `<span class="rchip ${r.verdict}"><ha-icon icon="${icon}"></ha-icon>${this._escape(this._recogLabel(r))}${pct}</span>`;
        })
        .join("") + (recog.length > shown.length ? `<span class="rchip">+${recog.length - shown.length}</span>` : "");
      tile.appendChild(chips);
    }
    const marks = document.createElement("div");
    marks.className = "marks";
    tile.appendChild(marks);
    this._renderMarks(tile, ev);
    tile.addEventListener("click", () => this._openPlayer(ev));
    this._tiles.set(ev.id, tile);
    this._grid.appendChild(tile);
  }

  _renderMarks(tile, ev) {
    const marks = tile.querySelector(".marks");
    marks.innerHTML =
      (ev.saved ? '<ha-icon class="saved" icon="mdi:bookmark" title="Saved"></ha-icon>' : "") +
      (ev.flagged ? '<ha-icon icon="mdi:flag" title="Flagged in Protect"></ha-icon>' : "");
    // Saved note first, then who/what Protect recognised.
    tile.title = [ev.saved && ev.saved.note, this._recogSummary(ev)].filter(Boolean).join(" · ");
  }

  _onThumbError(tile, img, ev) {
    const attempt = Number(img.dataset.attempt || "0") + 1;
    if (attempt > THUMB_MAX_RETRIES) {
      tile.classList.add("failed"); // give up: show a broken-image marker
      tile.title = `No thumbnail: event ${ev.id} (${(ev.smart_detect_types || []).join(", ")})`;
      console.warn(
        `protect-media-viewer: thumbnail gave up for event ${ev.id}`,
        { camera: ev.camera_name, types: ev.smart_detect_types, start: ev.start, url: ev.thumbnail }
      );
      return;
    }
    img.dataset.attempt = String(attempt);
    const delay = Math.min(8000, 500 * 2 ** attempt);
    setTimeout(() => {
      if (!img.isConnected) return;
      // Re-request the SAME signed URL. We must NOT add query params: HA signs
      // every param except width/height, so a cache-buster would invalidate the
      // signature (401 -> log spam + IP-ban risk). A failed fetch isn't cached,
      // so toggling src re-fetches; a not-ready thumbnail simply 404s until it
      // exists, then loads.
      img.removeAttribute("src");
      img.src = ev.thumbnail;
    }, delay);
  }

  _openPlayer(ev) {
    this._current = ev;
    const summary = this._recogSummary(ev);
    this._caption.textContent =
      `${ev.camera_name || ""} · ${(ev.smart_detect_types || []).join(", ")} · ${this._fmtDateTime(ev.start)}` +
      (ev.score != null ? ` · ${ev.score}%` : "") +
      (summary ? ` · ${summary}` : "");
    this._renderActions();
    this._video.src = ev.clip;
    this._modal.classList.add("open");
    const playing = this._video.play();
    if (playing && typeof playing.catch === "function") playing.catch(() => {});
  }

  // ---- save / download / remove (player) ----------------------------------

  _renderActions(message = "") {
    const ev = this._current;
    const a = this._actions;
    a.innerHTML = "";
    if (!ev) return;
    const saving = this._saving || {};

    if (ev.saved) {
      const s = ev.saved;
      const by = s.saved_by ? ` by ${this._escape(s.saved_by)}` : "";
      const label = document.createElement("span");
      label.className = "saved-label";
      label.innerHTML = `<ha-icon icon="mdi:bookmark-check"></ha-icon> Saved${by} ${this._escape(this._fmtDate(s.saved_at))}`;
      a.appendChild(label);
      if (s.note) {
        const note = document.createElement("span");
        note.className = "note";
        note.textContent = `“${s.note}”`;
        a.appendChild(note);
      }
      if (ev.download) {
        const dl = document.createElement("a");
        dl.className = "btn secondary download";
        dl.href = ev.download;
        dl.setAttribute("download", "");
        dl.innerHTML = '<ha-icon icon="mdi:download"></ha-icon> Download';
        a.appendChild(dl);
      }
      a.appendChild(this._button("Edit note", "mdi:pencil", "secondary edit", () => this._editNote(ev)));
      if (saving.can_remove) {
        a.appendChild(this._button("Remove", "mdi:delete", "danger remove", () => this._remove(ev)));
      }
    } else if (this._config.allow_save !== false) {
      if (saving.available === false) {
        const off = document.createElement("span");
        off.className = "msg";
        off.textContent = `Saving unavailable: ${saving.reason || "unknown reason"}`;
        a.appendChild(off);
      } else {
        const input = document.createElement("input");
        input.className = "note-input";
        input.placeholder = "Note (optional)";
        input.maxLength = 500;
        const btn = this._button("Save", "mdi:bookmark-plus", "save", () => this._save(ev, input, btn));
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") btn.click();
        });
        a.appendChild(input);
        a.appendChild(btn);
      }
    }
    if (message) {
      const msg = document.createElement("span");
      msg.className = "msg";
      msg.textContent = message;
      a.appendChild(msg);
    }
  }

  _button(text, icon, cls, onClick) {
    const b = document.createElement("button");
    b.className = `btn ${cls}`;
    b.innerHTML = `<ha-icon icon="${icon}"></ha-icon> ${text}`;
    b.addEventListener("click", onClick);
    return b;
  }

  async _save(ev, input, btn) {
    btn.disabled = true;
    btn.innerHTML = '<ha-icon icon="mdi:timer-sand"></ha-icon> Saving…';
    try {
      const res = await this._hass.callApi("POST", this._savedPath(ev.id), { note: input.value || "" });
      this._applySaved(ev, res.event);
      if (this._saving && res.created) this._saving.count = (this._saving.count || 0) + 1;
      if (this._current === ev) this._renderActions();
    } catch (err) {
      if (this._current === ev) this._renderActions(this._errText(err));
    }
  }

  async _editNote(ev) {
    const note = window.prompt("Note for this saved clip", (ev.saved && ev.saved.note) || "");
    if (note === null) return;
    try {
      const res = await this._hass.callApi("PATCH", this._savedPath(ev.id), { note });
      this._applySaved(ev, res.event);
      if (this._current === ev) this._renderActions();
    } catch (err) {
      if (this._current === ev) this._renderActions(this._errText(err));
    }
  }

  async _remove(ev) {
    if (!window.confirm("Remove this saved clip? It moves to the trash and is deleted later.")) return;
    try {
      await this._hass.callApi("DELETE", this._savedPath(ev.id));
    } catch (err) {
      if (this._current === ev) this._renderActions(this._errText(err));
      return;
    }
    ev.saved = null;
    ev.download = undefined;
    if (this._saving && this._saving.count) this._saving.count -= 1;
    const tile = this._tiles.get(ev.id);
    if (this._isSavedMode()) {
      // Gone from this list, and its saved-clip URL no longer plays.
      if (tile) tile.remove();
      this._tiles.delete(ev.id);
      this._events = this._events.filter((e) => e !== ev);
      this._setStatus(this._countText(this._events.length, true));
      if (this._current === ev) this._closePlayer();
      return;
    }
    if (tile) this._renderMarks(tile, ev);
    if (this._current === ev) this._renderActions();
  }

  // Fold a save/edit response into the event object the grid and player share.
  _applySaved(ev, saved) {
    ev.saved = saved.saved;
    ev.download = saved.download;
    ev.flagged = saved.flagged;
    const tile = this._tiles.get(ev.id);
    if (tile) this._renderMarks(tile, ev);
  }

  _errText(err) {
    return (err && err.body && err.body.message) || (err && err.message) || (err && err.error) || "Request failed";
  }

  _closePlayer() {
    if (!this._modal.classList.contains("open")) return;
    this._current = null;
    this._modal.classList.remove("open");
    this._video.pause();
    this._video.removeAttribute("src");
    this._video.load();
  }

  // ---- small utils --------------------------------------------------------

  _countText(n, saved) {
    const noun = saved ? "saved clip" : "detection";
    return `${n} ${noun}${n === 1 ? "" : "s"}`;
  }

  _setStatus(text) {
    if (this._statusEl) this._statusEl.textContent = text;
  }

  // "Johnny", "Unknown face", "Roadrunner · 1FSTSLA", "9UYB036".
  _recogLabel(r) {
    if (r.kind === "plate") {
      if (r.name && r.plate) return `${r.name} · ${r.plate}`;
      return r.name || r.plate || "Plate";
    }
    if (r.name) return r.name;
    return r.verdict === "unknown" ? "Unknown face" : "Face unclear";
  }

  // Every recognition in the event, for the tooltip and player caption.
  _recogSummary(ev) {
    return (ev.recognition || [])
      .map((r) => this._recogLabel(r) + (r.confidence != null ? ` (${r.confidence}%)` : ""))
      .join(", ");
  }

  _escape(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  _fmtTime(iso, withDate = false) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (sameDay && !withDate) return t;
    const date = d.toLocaleDateString(
      [],
      d.getFullYear() === now.getFullYear()
        ? { month: "short", day: "numeric" }
        : { year: "numeric", month: "short", day: "numeric" }
    );
    return `${date} ${t}`;
  }

  _fmtDate(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
  }

  _fmtDateTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }
}

// Define defensively: if this module is ever evaluated twice (e.g. it's both
// auto-registered by the integration and added as a manual Lovelace resource),
// a second customElements.define() would throw and abort the module, which can
// leave the card "undefined" and surface as a Configuration error.
if (!customElements.get("protect-media-viewer-card")) {
  customElements.define("protect-media-viewer-card", ProtectMediaViewerCard);

  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "protect-media-viewer-card",
    name: "UniFi Protect Media Viewer",
    description: "Browse UniFi Protect smart-detection events with fast thumbnails and inline playback.",
    preview: false,
  });

  console.info("%c PROTECT-MEDIA-VIEWER-CARD ", "color: #fff; background: #03a9f4; font-weight: 700;");
}
