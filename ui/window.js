// Page lifetime is separate from visibility: hiding for a game keeps the
// launcher alive, while closing the page ends its own launcher session.
(() => {
  const launchMarker = new URLSearchParams(location.search).get("window");
  if (launchMarker) sessionStorage.setItem("blockharbor-window", launchMarker);
  const candidate = launchMarker || sessionStorage.getItem("blockharbor-window");
  const marker = candidate && /^[a-f0-9]{24}$/.test(candidate) ? candidate : "";

  const token =
    location.hash.slice(1) || sessionStorage.getItem("blockharbor-key");
  if (!token) return;
  const page = Array.from(crypto.getRandomValues(new Uint8Array(12)), value => value.toString(16).padStart(2, "0")).join("");
  const originalTitle = document.title;
  let timer,
    attempts = 0,
    attached = false,
    leaving = false;
  if (launchMarker && marker) document.title = "BlockHarbor [" + marker + "]";

  function pageEvent(event) {
    const body = JSON.stringify({ token, event, page });
    if (event === "closed") {
      try {
        if (navigator.sendBeacon?.("/api/window", new Blob([body], { type: "application/json" }))) return;
      } catch {
        // A keepalive request also works when sendBeacon is unavailable.
      }
    }
    fetch("/api/window", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: event === "closed",
    }).catch(() => {});
  }

  async function adjust() {
    if (!marker || leaving) return;
    try {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: {
          Authorization: "Bearer " + token,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ action: "fitWindow", marker }),
      });
      if (response.ok) attached = (await response.json()).attached;
    } catch {
      // During a shutdown the page may outlive the local server.
    }
    if (!attached && launchMarker && !leaving && ++attempts < 12) {
      timer = setTimeout(adjust, 180);
    } else {
      document.title = originalTitle;
    }
  }

  // Register and fit only when opening the page. Window resizing, maximizing,
  // and snapping are owned by the user and the operating system.
  function opened() {
    leaving = false;
    pageEvent("opened");
    adjust();
  }
  if (document.readyState === "complete") opened();
  else window.addEventListener("load", opened, { once: true });
  window.addEventListener("pageshow", event => {
    if (event.persisted) opened();
  });
  window.addEventListener("pagehide", () => {
    leaving = true;
    clearTimeout(timer);
    pageEvent("closed");
  });
})();
