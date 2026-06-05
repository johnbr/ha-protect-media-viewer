/*
 * Protect Media Viewer card
 *
 * A no-build custom card: browse UniFi Protect smart-detection events as a fast,
 * infinite-scrolling thumbnail grid with detection-type filter chips, and play
 * clips inline. Talks to the protect_media_viewer integration's HTTP API.
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

const ICON_BY_TYPE = Object.fromEntries(TYPES.map((t) => [t.key, t.icon]));

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
    this._cameras = [];
  }

  setConfig(config) {
    this._config = config || {};
    this._hours = this._config.default_hours || 24;
    this._selectedTypes = new Set(this._config.default_types || []);
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
    this._ensureBuilt();
  }

  disconnectedCallback() {
    if (this._io) this._io.disconnect();
    if (this._onScroll) {
      window.removeEventListener("scroll", this._onScroll, true);
      window.removeEventListener("resize", this._onScroll, true);
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
    this._events = [];
    this._offset = 0;
    this._hasMore = true;
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
    this._loading = true;
    this._setStatus(this._events.length ? "Loading more…" : "Loading…");

    const params = [];
    const ep = this._entryParam();
    if (ep) params.push(ep);
    params.push(`hours=${this._hours}`);
    params.push(`limit=${this._pageSize()}`);
    params.push(`offset=${this._offset}`);
    if (this._selectedTypes.size) params.push(`types=${[...this._selectedTypes].join(",")}`);
    if (this._camera) params.push(`camera=${encodeURIComponent(this._camera)}`);

    try {
      const res = await this._api(`protect_media_viewer/events?${params.join("&")}`);
      const events = res.events || [];
      this._offset += res.limit || this._pageSize();
      this._hasMore = !!res.has_more;
      for (const ev of events) this._appendTile(ev);
      this._events.push(...events);

      if (!this._events.length && !this._hasMore) {
        this._setStatus("No detections found for these filters.");
      } else if (!this._hasMore) {
        this._setStatus(`${this._events.length} detections`);
      } else {
        // More available — show a running count and keep paging if needed.
        this._setStatus(`${this._events.length} detections — scroll for more…`);
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
      console.error("protect-media-viewer: events query failed", err);
      this._setStatus("Failed to load events.");
      this._hasMore = false;
    } finally {
      this._loading = false;
    }
  }

  // ---- rendering ----------------------------------------------------------

  _render() {
    const minCol = this._config.columns || 180;
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
        .tile .badges ha-icon { --mdc-icon-size: 15px; }
        .tile .when { white-space: nowrap; }
        .tile .cam { opacity: 0.85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
        .modal .close {
          position: absolute; top: -14px; right: -14px; width: 34px; height: 34px;
          border-radius: 50%; background: var(--primary-color); color: #fff;
          border: none; cursor: pointer; font-size: 18px; line-height: 34px;
        }
      </style>
      <ha-card>
        <div class="header"></div>
        <div class="toolbar">
          <div class="chips"></div>
          <div class="spacer"></div>
          <select class="camera"><option value="">All cameras</option></select>
          <select class="time"></select>
        </div>
        <div class="grid"></div>
        <div class="status"></div>
        <div class="sentinel"></div>
      </ha-card>
      <div class="modal">
        <div class="box">
          <button class="close" title="Close">×</button>
          <video controls autoplay playsinline></video>
          <div class="caption"></div>
        </div>
      </div>
    `;

    this._grid = this.shadowRoot.querySelector(".grid");
    this._statusEl = this.shadowRoot.querySelector(".status");
    this._cameraSel = this.shadowRoot.querySelector("select.camera");
    this._timeSel = this.shadowRoot.querySelector("select.time");
    this._modal = this.shadowRoot.querySelector(".modal");
    this._video = this.shadowRoot.querySelector(".modal video");
    this._caption = this.shadowRoot.querySelector(".modal .caption");

    this.shadowRoot.querySelector(".header").textContent =
      this._config.title || "Protect Smart Detections";

    this._renderChips();
    this._renderTimeOptions();

    this._cameraSel.addEventListener("change", () => {
      this._camera = this._cameraSel.value;
      this._reset();
    });
    this._timeSel.addEventListener("change", () => {
      this._hours = Number(this._timeSel.value);
      this._reset();
    });

    // Player modal close handlers.
    const close = () => this._closePlayer();
    this.shadowRoot.querySelector(".modal .close").addEventListener("click", close);
    this._modal.addEventListener("click", (e) => {
      if (e.target === this._modal) close();
    });
    document.addEventListener("keydown", this._onKey = (e) => {
      if (e.key === "Escape") close();
    });

    // Infinite scroll. We use several triggers because an IntersectionObserver
    // alone is unreliable inside Home Assistant's nested scroll containers:
    //  - the observer (works when it fires),
    //  - a capturing scroll listener on window (catches scrolling in any
    //    ancestor scroll container, since scroll events don't bubble),
    //  - a resize listener,
    //  - an auto-fill after each page so the first screenful always fills.
    this._sentinel = this.shadowRoot.querySelector(".sentinel");
    this._io = new IntersectionObserver(() => this._maybeLoadMore(), {
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
  }

  _maybeLoadMore() {
    if (this._loading || !this._hasMore || !this._sentinel) return;
    const rect = this._sentinel.getBoundingClientRect();
    const vh = window.innerHeight || document.documentElement.clientHeight;
    // Load the next page once the sentinel is within ~800px of the viewport.
    if (rect.top <= vh + 800) this._loadMore();
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

  _renderTimeOptions() {
    this._timeSel.innerHTML = "";
    for (const r of TIME_RANGES) {
      const opt = document.createElement("option");
      opt.value = String(r.hours);
      opt.textContent = r.label;
      if (r.hours === this._hours) opt.selected = true;
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
      `<span class="when">${this._fmtTime(ev.start)}</span>` +
      `<span class="badges">${badges}</span>`;

    tile.appendChild(img);
    tile.appendChild(overlay);
    tile.addEventListener("click", () => this._openPlayer(ev));
    this._grid.appendChild(tile);
  }

  _onThumbError(tile, img, ev) {
    const attempt = Number(img.dataset.attempt || "0") + 1;
    if (attempt > THUMB_MAX_RETRIES) {
      tile.classList.add("failed"); // give up: show a broken-image marker
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
    const types = (ev.smart_detect_types || []).join(", ");
    this._caption.textContent =
      `${ev.camera_name || ""} · ${types} · ${this._fmtDateTime(ev.start)}` +
      (ev.score != null ? ` · ${ev.score}%` : "");
    this._video.src = ev.clip;
    this._modal.classList.add("open");
    const playing = this._video.play();
    if (playing && typeof playing.catch === "function") playing.catch(() => {});
  }

  _closePlayer() {
    if (!this._modal.classList.contains("open")) return;
    this._modal.classList.remove("open");
    this._video.pause();
    this._video.removeAttribute("src");
    this._video.load();
  }

  // ---- small utils --------------------------------------------------------

  _setStatus(text) {
    if (this._statusEl) this._statusEl.textContent = text;
  }

  _escape(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  _fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return sameDay ? t : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}`;
  }

  _fmtDateTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }
}

customElements.define("protect-media-viewer-card", ProtectMediaViewerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "protect-media-viewer-card",
  name: "Protect Media Viewer",
  description: "Browse UniFi Protect smart-detection events with fast thumbnails and inline playback.",
  preview: false,
});

console.info("%c PROTECT-MEDIA-VIEWER-CARD ", "color: #fff; background: #03a9f4; font-weight: 700;");
