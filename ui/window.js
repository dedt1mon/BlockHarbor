// The Windows app host gives this single tab a registration marker.
// Other browsers and tabs only use the bounded CSS layout.
(() => {
  const launchMarker = new URLSearchParams(location.search).get("window");
  if (launchMarker) sessionStorage.setItem("blockharbor-window", launchMarker);
  const marker = launchMarker || sessionStorage.getItem("blockharbor-window");
  if (!marker || !/^[a-f0-9]{24}$/.test(marker)) return;

  const token =
    location.hash.slice(1) || sessionStorage.getItem("blockharbor-key");
  const originalTitle = document.title;
  let timer,
    attempts = 0,
    attached = false;
  if (launchMarker) document.title = "BlockHarbor [" + marker + "]";

  async function adjust() {
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
    if (!attached && launchMarker && ++attempts < 12) {
      timer = setTimeout(adjust, 180);
    } else {
      document.title = originalTitle;
    }
  }

  window.addEventListener("resize", () => {
    if (!attached) return;
    clearTimeout(timer);
    timer = setTimeout(adjust, 100);
  });
  window.addEventListener("load", adjust, { once: true });
})();
