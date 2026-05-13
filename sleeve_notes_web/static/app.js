// Client-side shims:
// 1. SSE attach for the run-status toast — updates the latest-line text + dispatches
//    runStatus on terminal transitions so HTMX re-fetches the toast template.
// 2. Elapsed-time ticker that updates only the `#run-elapsed` text node, so the
//    indeterminate progress bar's CSS animation runs uninterrupted.
// 3. Density toggle (comfortable / compact), persisted in localStorage.

(function () {
  let es = null;
  let elapsedTimer = null;

  function dispatchStatus() {
    document.body.dispatchEvent(new CustomEvent("runStatus", { bubbles: true }));
  }

  // Switch the bar to determinate mode and set width + percentage text.
  function updateProgress(done, total) {
    if (!total || total <= 0) return;
    const pct = Math.min(100, Math.max(0, Math.round((done / total) * 100)));
    const track = document.getElementById("run-progress-track");
    if (track) {
      track.classList.add("determinate");
      track.style.setProperty("--progress", pct + "%");
    }
    const label = document.getElementById("run-progress-pct");
    if (label) label.textContent = pct + "%";
  }

  // Filter/rewrite CLI output for the toast body. Returns null to skip the line
  // entirely. Mirrors the server-side _filter_line / _prettify_line helpers.
  function prettifyLine(raw) {
    const s = (raw || "").trim();
    if (!s) return null;
    if (s.startsWith("$ ")) return null;
    if (/^\[\d+\/\d+\]\s+progress:/.test(s)) return null;
    if (/^new this run by source:/.test(s)) return null;

    // BPM intro: surface "to-cascade" (real work) not the misleading total.
    let m = s.match(
      /^Looking up BPM\/key for (\d+) tracks? across (\d+) releases?\s*\((\d+) fully cached, (\d+) overridden, (\d+) to cascade with \d+ workers\)\.{0,3}$/
    );
    if (m) {
      const total = +m[1], releases = +m[2], cached = +m[3], overridden = +m[4], toCascade = +m[5];
      if (toCascade === 0) {
        return `All ${total} tracks already have BPM cached — nothing to look up.`;
      }
      if (cached + overridden === 0) {
        return `Looking up BPM and key for ${toCascade} tracks across ${releases} releases…`;
      }
      return `Looking up BPM and key for ${toCascade} of ${total} tracks (${cached + overridden} already cached)…`;
    }

    // Per-track cascade. Marker is anything between the bracket and dash;
    // key= / sources= tails are optional. Drop sources entirely.
    m = s.match(
      /^\[(\d+)\/(\d+)\]\s+\S+\s+(.+?)\s+->\s+(\S+)(?:\s+key=(\S+))?(?:\s+sources=.*)?$/
    );
    if (m) {
      const suffix = m[5] ? ` · key ${m[5]}` : "";
      return `${m[1]} of ${m[2]} · ${m[3]} → ${m[4]} BPM${suffix}`;
    }

    // Final BPM summary line.
    m = s.match(
      /^Cache populated:\s*(\d+)\/(\d+) BPMs\s*\([^)]+\),\s*(\d+)\/\d+ keys\.?$/
    );
    if (m) {
      return `Found BPM for ${m[1]} of ${m[2]} tracks (${m[3]} with key).`;
    }

    return s;
  }

  function attach() {
    if (es) {
      try { es.close(); } catch (_) { /* noop */ }
    }
    es = new EventSource("/run/events");
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (data.kind === "line") {
        const raw = data.text || "";
        // Pick up real progress from `[done/total]` counters before any filtering
        // — even noisy lines (e.g. the per-batch summary) carry valid counts.
        const m = raw.match(/^\s*\[(\d+)\/(\d+)\]/);
        if (m) updateProgress(+m[1], +m[2]);
        const pretty = prettifyLine(raw);
        if (!pretty) return;
        const line = document.getElementById("run-status-line");
        if (line) line.textContent = pretty;
      } else if (data.kind === "status") {
        if (data.status && data.status !== "running") {
          es.close();
          es = null;
          // Force a banner refresh via htmx.ajax — more reliable than relying
          // on the runStatus custom event reaching the right listener.
          // Explicit target/swap form — the 3-arg variant of htmx.ajax overloads
          // the third arg as both source and target which is fragile.
          if (window.htmx) {
            window.htmx.ajax("GET", "/run/banner", { target: "#run-banner", swap: "innerHTML" });
            // Refresh the current page's main content for any terminal status —
            // cancelled and failed jobs can still have written partial data
            // (e.g. some releases imported, some BPMs cached) that the user
            // needs to see reflected without a full reload.
            const url = window.location.pathname + window.location.search;
            window.htmx.ajax("GET", url, { target: "main", select: "main > *", swap: "innerHTML" });
          } else {
            dispatchStatus();
          }
        }
      }
    };
    es.onerror = () => {
      // Let the browser auto-reconnect; if the server is gone the user can refresh.
    };
  }

  // Tick the elapsed-time text node every second while the toast is in the
  // running state. We update one text node only — no full re-render — so the
  // indeterminate progress bar animation isn't interrupted.
  function startElapsedTicker() {
    if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    const toast = document.getElementById("run-toast");
    if (!toast || toast.dataset.status !== "running") return;
    const startedAt = parseFloat(toast.dataset.startedAt || "0");
    if (!startedAt) return;
    const update = () => {
      const el = document.getElementById("run-elapsed");
      const t = document.getElementById("run-toast");
      if (!el || !t || t.dataset.status !== "running") {
        clearInterval(elapsedTimer);
        elapsedTimer = null;
        return;
      }
      const s = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
      el.textContent = formatElapsed(s);
    };
    update();
    elapsedTimer = setInterval(update, 1000);
  }

  function formatElapsed(s) {
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m < 60) return m + "m " + r + "s";
    const h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  }

  window.__sleeveAttachRunStream = function () {
    attach();
    startElapsedTicker();
  };

  // Density toggle — comfortable (default) ↔ compact, persisted in localStorage.
  function applyDensity(d) {
    document.body.classList.remove("density-comfortable", "density-compact");
    document.body.classList.add(d === "compact" ? "density-compact" : "density-comfortable");
    const label = document.getElementById("density-label");
    if (label) label.textContent = d === "compact" ? "compact" : "comfy";
  }
  function getDensity() {
    try { return localStorage.getItem("sleeve.density") || "comfortable"; } catch (_) { return "comfortable"; }
  }
  function setDensity(d) {
    try { localStorage.setItem("sleeve.density", d); } catch (_) { /* noop */ }
    applyDensity(d);
  }
  window.__sleeveToggleDensity = () => setDensity(getDensity() === "compact" ? "comfortable" : "compact");

  // Apply density ASAP to avoid a flash. The body class is the source of truth.
  applyDensity(getDensity());

  // If a fresh page already shows a running job, attach immediately.
  document.addEventListener("DOMContentLoaded", () => {
    applyDensity(getDensity());
    const toast = document.getElementById("run-toast");
    if (toast && toast.dataset.status === "running") {
      attach();
      startElapsedTicker();
    }
  });

  // Also kick the elapsed ticker every time the toast re-renders (htmx swap).
  document.body.addEventListener("htmx:afterSwap", (ev) => {
    if (ev.target && ev.target.id === "run-banner") {
      startElapsedTicker();
    }
  });
})();
