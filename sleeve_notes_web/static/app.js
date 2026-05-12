// Small client-side shims: SSE attach for the Run panel, plus a once-per-second
// status-pill refresh while a job is running so the elapsed seconds tick.

(function () {
  let es = null;

  function dispatchStatus() {
    document.body.dispatchEvent(new CustomEvent("runStatus", { bubbles: true }));
  }

  function attach() {
    if (es) {
      try { es.close(); } catch (_) { /* noop */ }
    }
    const log = document.getElementById("run-lines");
    const statusEl = document.getElementById("run-status-text");
    if (!log) return;

    es = new EventSource("/run/events");
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (data.kind === "line") {
        const div = document.createElement("div");
        div.textContent = data.text;
        log.appendChild(div);
        const pane = document.getElementById("run-log");
        if (pane) pane.scrollTop = pane.scrollHeight;
      } else if (data.kind === "status") {
        if (statusEl) {
          statusEl.textContent = data.status;
          statusEl.dataset.status = data.status;
        }
        if (data.status && data.status !== "running") {
          es.close();
          es = null;
          dispatchStatus(); // re-render pill + panel form to reflect done state
        }
      }
    };
    es.onerror = () => {
      // Let the browser auto-reconnect; if the server is gone the user can refresh.
    };
  }

  // Status-pill ticker — re-fetch /run/status every 1s while we have an open SSE.
  setInterval(() => {
    if (es && es.readyState === EventSource.OPEN) dispatchStatus();
  }, 1000);

  window.__sleeveAttachRunStream = attach;

  // If a fresh page already shows a running job, attach immediately.
  document.addEventListener("DOMContentLoaded", () => {
    const statusEl = document.getElementById("run-status-text");
    if (statusEl && statusEl.dataset.status === "running") attach();
  });
})();
