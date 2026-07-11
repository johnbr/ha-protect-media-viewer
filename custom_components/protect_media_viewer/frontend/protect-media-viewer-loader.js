/*
 * Protect Media Viewer card loader.
 *
 * Why a loader instead of loading the card module directly: the browser makes
 * exactly ONE attempt to fetch a <script type="module">, and a failed module
 * fetch is cached in the page's module map, so nothing ever retries it. On
 * phones/tablets the Companion app routinely (re)loads the page inside a
 * network gap — Wi-Fi still re-associating after wake, a Wi-Fi/cellular
 * handoff, or Home Assistant itself still booting. The app shell comes out of
 * the service-worker cache and renders fine, but the card module fetch fails
 * once, customElements.define() never runs, and every card paints Home
 * Assistant's "Configuration error" until a manual reload.
 *
 * This loader dynamic-imports the card and retries with backoff (and
 * immediately when connectivity returns), and reports failures/recoveries to
 * the integration's /log endpoint so they are visible in the HA log.
 */

(() => {
  // The loader is deliberately delivered twice (extra_module_url AND a
  // Lovelace resource, under different URLs so one failed fetch can't poison
  // the other's module-map entry). Only the first copy to evaluate may run.
  if (window.__protectMediaViewerLoader) return;
  window.__protectMediaViewerLoader = true;

  const CARD_URL = "__CARD_URL__"; // rewritten by frontend.py at serve time
  const TAG = "protect-media-viewer-card";
  const REPORT_URL = "/api/protect_media_viewer/log";
  const MAX_ATTEMPTS = 20; // backoff caps at 60s; ~15 min of trying

  let attempts = 0;
  let timer = null;
  let loaded = false;

  const report = (event, detail) => {
    // Fire-and-forget; while offline this fails silently, which is fine — the
    // recovery beacon after a successful retry is the one that matters.
    try {
      fetch(REPORT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event,
          attempts,
          detail: String(detail || "").slice(0, 300),
        }),
      }).catch(() => {});
    } catch (_err) {
      /* ignore */
    }
  };

  const tryLoad = () => {
    timer = null;
    if (loaded) return;
    attempts += 1;
    // Retries must cache-bust: the module map caches failures, so re-importing
    // the SAME URL rejects instantly without touching the network. A changed
    // query string is a fresh module (the card guards customElements.define,
    // so evaluating a second copy is harmless).
    const url = attempts === 1 ? CARD_URL : `${CARD_URL}&r=${attempts}`;
    import(url)
      .then(() => {
        if (!customElements.get(TAG)) {
          // Evaluated but didn't define the element — corrupt cached copy; the
          // next attempt's cache-buster fetches fresh bytes past it.
          throw new Error(`module evaluated but <${TAG}> was not defined`);
        }
        loaded = true;
        if (attempts > 1) report("card_load_recovered");
      })
      .catch((err) => {
        if (attempts === 3) report("card_load_failed", err);
        if (attempts >= MAX_ATTEMPTS) {
          report("card_load_gave_up", err);
          return; // the `online` listener below can still restart us
        }
        timer = setTimeout(tryLoad, Math.min(60000, 1000 * 2 ** (attempts - 1)));
      });
  };

  // Connectivity is the usual culprit — when it returns, retry immediately
  // instead of waiting out the backoff (and even after giving up).
  window.addEventListener("online", () => {
    if (loaded) return;
    if (timer) clearTimeout(timer);
    if (attempts >= MAX_ATTEMPTS) attempts = MAX_ATTEMPTS - 1;
    tryLoad();
  });

  tryLoad();
})();
