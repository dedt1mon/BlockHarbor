"use strict";
const $ = (s) => document.querySelector(s);
const tokens = location.hash.slice(1);
if (tokens) {
  sessionStorage.setItem("blockharbor-key", tokens);
  history.replaceState(null, "", location.pathname);
}
const key = sessionStorage.getItem("blockharbor-key") || "";
const paths = {
  home: "M3 10 12 3l9 7v10H15v-6H9v6H3z",
  layers: "m3 8 9-5 9 5-9 5z M3 12l9 5 9-5 M3 16l9 5 9-5",
  users:
    "M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M14 4a4 4 0 0 1 0 8 M19 15a4 4 0 0 1 3 4v2 M13 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  user: "M20 21v-2a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v2 M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  settings:
    "M4 7h8m4 0h4M4 17h3m4 0h9 M16 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0 M11 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
  cube: "m3 7 9-5 9 5v10l-9 5-9-5z M3 7l9 5 9-5 M12 12v10",
  play: "m7 3 14 9-14 9z",
  plus: "M12 4v16M4 12h16",
  arrow: "M4 12h15m-6-6 6 6-6 6",
  gear: "m9.5 3-.6 2.2-1.4.8-2.2-.6-2.4 4.2 1.6 1.6v1.6l-1.6 1.6 2.4 4.2 2.2-.6 1.4.8.6 2.2h5l.6-2.2 1.4-.8 2.2.6 2.4-4.2-1.6-1.6v-1.6l1.6-1.6-2.4-4.2-2.2.6-1.4-.8-.6-2.2z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  star: "M12 3 14.8 8.7 21.1 9.6 16.5 14.1 17.6 20.4 12 17.4 6.4 20.4 7.5 14.1 2.9 9.6 9.2 8.7Z",
  download: "M12 3v12m-5-5 5 5 5-5 M4 16v4h16v-4",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M15 15l6 6",
  upload: "M12 15V3m-5 5 5-5 5 5 M4 15v5h16v-5",
  trash: "M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7",
  close: "m5 5 14 14M19 5 5 19",
  folder: "M3 6h7l2 3h9v11H3z",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4 M12 2V1",
  link: "M8 12l7-7m-5-2h8v8 M9 5H4v15h15v-5",
  plug: "M8 3v5m8-5v5 M5 8h14v2a7 7 0 0 1-14 0z M12 17v5",
  sun: "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M12 1v3m0 16v3M1 12h3m16 0h3 M4 4l2 2m12 12 2 2M20 4l-2 2M6 18l-2 2",
  texture: "M3 3h18v18H3z M3 9h18M3 15h18M9 3v6m6 0v6m-6 0v6",
  share: "M8 11l8-5M8 13l8 5 M8 12a3 3 0 1 1-6 0 3 3 0 1 1 6 0 M22 5a3 3 0 1 1-6 0 3 3 0 1 1 6 0 M22 19a3 3 0 1 1-6 0 3 3 0 1 1 6 0",
  copy: "M8 8h12v13H8z M16 8V3H3v13h5",
  down: "m6 9 6 6 6-6",
  check: "m3 12 6 6L21 6",
  refresh: "M20 8a8 8 0 1 0 1 7 M20 3v5h-5",
  back: "M20 12H4m6-6-6 6 6 6",
  image: "M3 4h18v16H3z M3 17l6-6 4 4 3-4 5 5",
  monitor: "M3 3h18v13H3z M12 16v4m-5 1h10",
  game: "M6 7h12l3 10q0 4-4 2l-3-3h-4l-3 3q-4 2-4-2z M7 10v4m-2-2h4m7 1h.1m2-3h.1",
};
const ic = (n) =>
  '<i aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="' +
  (paths[n] || paths.cube) +
  '"/></svg></i>';
document
  .querySelectorAll("[data-icon]")
  .forEach((el) => (el.outerHTML = ic(el.dataset.icon)));
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const btn = (id, label, icon, cls = "") =>
  '<button type="button" class="btn ' +
  cls +
  '" data-act="' +
  id +
  '">' +
  (icon ? ic(icon) : "") +
  label +
  "</button>";
const names = {
  home: "Главная",
  builds: "Сборки",
  addons: "Настройки сборки",
  friends: "Друзья",
  profile: "Профиль",
  settings: "Настройки",
  gallery: "Скриншоты",
};
const loaders = {
  vanilla: "Vanilla",
  fabric: "Fabric",
  forge: "Forge",
  neoforge: "NeoForge",
  quilt: "Quilt",
};
const kinds = {
  mods: ["Моды", "plug", ""],
  shaderpacks: ["Шейдеры", "sun", "purple"],
  resourcepacks: ["Ресурс-паки", "texture", "blue"],
};
const pages = {
  steamInstall: "https://store.steampowered.com/about/",
};
let state,
  view = "home",
  settingsSection = "general",
  kind = "mods",
  lastJob = "",
  acceptedJobGeneration = 0,
  lastPlayJob = "",
  shownExit = "",
  activePoll = false,
  pollTimer = 0,
  refreshAfterPlay = false,
  pollError = "",
  selectingBuild = false,
  navigationRequest = 0,
  loadedSkin = 0;
const active = () => state.profiles.find((p) => p.id === state.active);
const favoriteRequests = new Set();
let noticeTimer = 0;
let noticeOwner = "";
const backgroundNotices = new Map();
const deliveredBackgroundNotices = new Set();
function moveNotice() {
  const box = $("#notice");
  const parent = $("#steamCompatibilityDialog[open]") || ($("#dialog").open ? $("#dialog") : document.body);
  if (box.parentElement !== parent) parent.append(box);
}
function clearNotice() {
  clearTimeout(noticeTimer);
  noticeTimer = 0;
  noticeOwner = "";
  const box = $("#notice");
  box.hidden = true;
  box.className = "";
  box.replaceChildren();
}
function notice(msg, bad = false, busy = false, owner = "") {
  const box = $("#notice");
  const style = bad ? "bad" : busy ? "busy" : "success";
  if (busy && noticeOwner === owner && !box.hidden && box.className === style && box.textContent === msg)
    return;
  clearTimeout(noticeTimer);
  if (!msg) {
    clearNotice();
    flushBackgroundNotices();
    return;
  }
  noticeOwner = owner;
  box.hidden = false;
  moveNotice();
  box.className = style;
  const text = document.createElement("span");
  text.textContent = msg;
  box.innerHTML = ic(bad ? "close" : busy ? "refresh" : "check");
  box.append(text);
  if (!busy) noticeTimer = setTimeout(() => {
    clearNotice();
    flushBackgroundNotices();
  }, 5000);
}
function rememberBackgroundNotice(item) {
  deliveredBackgroundNotices.add(item.key);
  if (deliveredBackgroundNotices.size > 100) {
    deliveredBackgroundNotices.delete(deliveredBackgroundNotices.values().next().value);
  }
  if (item.persist) {
    try {
      const previous = JSON.parse(sessionStorage.getItem("blockharbor-seen-notices") || "[]");
      sessionStorage.setItem("blockharbor-seen-notices", JSON.stringify([...previous.filter((key) => key !== item.key).slice(-29), item.key]));
    } catch { /* Notification delivery must also work without session storage. */ }
  }
}
function backgroundNotice(source, event) {
  const box = $("#notice");
  const key = event ? source + ":" + event.id : "";
  let delivered = deliveredBackgroundNotices.has(key);
  if (event?.persist && !delivered) {
    try { delivered = JSON.parse(sessionStorage.getItem("blockharbor-seen-notices") || "[]").includes(key); }
    catch { /* An unavailable history does not prevent showing the result. */ }
  }
  if (!event?.message || (!event.busy && delivered)) {
    backgroundNotices.delete(source);
    if (noticeOwner === source && box.classList.contains("busy")) clearNotice();
  } else {
    const item = {...event, source, key};
    // A manually requested check already reports its result as a normal job.
    // Do not repeat the same result after that foreground notice disappears.
    if (!item.busy && !box.hidden && box.textContent === item.message) {
      rememberBackgroundNotice(item);
      backgroundNotices.delete(source);
    } else backgroundNotices.set(source, item);
  }
  flushBackgroundNotices();
}
function flushBackgroundNotices() {
  const box = $("#notice");
  if (state?.job?.busy || lastJob || state?.gameRunning) {
    if (noticeOwner) clearNotice();
    return;
  }
  // Foreground saves/errors and completed results retain their full five
  // seconds. Only an ongoing background stage can be replaced by its next one.
  if (!box.hidden && (!noticeOwner || !box.classList.contains("busy"))) return;
  const waiting = [...backgroundNotices.values()];
  const item = waiting.find((candidate) => !candidate.busy) || waiting[0];
  if (!item) return;
  if (!item.busy) {
    backgroundNotices.delete(item.source);
    rememberBackgroundNotice(item);
  }
  notice(item.message, Boolean(item.bad), Boolean(item.busy), item.source);
}
function observeProgress(job) {
  // A completed app update already includes preparing its embedded components.
  const startup = job.updater?.startupResult ? null : job.startupNotice;
  backgroundNotice("bootstrap", startup ? {...startup, persist: true} : null);
  window.UpdatesUI?.observe(job.updater);
  window.BatchUI?.observe(job);
  window.ExperienceUI?.observeNotifications(job.notificationUnread);
}
function head(title, actions = "", sub = "") {
  return (
    '<div class="page-head"><div><h1>' +
    esc(title) +
    "</h1>" +
    (sub ? "<p>" + sub + "</p>" : "") +
    '</div><div class="actions">' +
    actions +
    "</div></div>"
  );
}
function titleRow(title, extra = "") {
  return '<div class="section-head"><h2>' + title + "</h2>" + extra + "</div>";
}
function fmt(size) {
  return size >= 1048576
    ? (size / 1048576).toFixed(1) + " МБ"
    : Math.max(1, Math.round(size / 1024)) + " КБ";
}
async function api(path, opt = {}) {
  const headers = { ...opt.headers, Authorization: "Bearer " + key };
  let r;
  try {
    r = await fetch(path, { ...opt, headers });
  } catch {
    throw Error(
      "Связь с приложением прервана. Открой BlockHarbor.exe ещё раз.",
    );
  }
  if (
    [
      "/api/preview",
      "/api/skin-preview",
      "/api/skin.png",
    ].includes(path.split("?")[0]) &&
    r.ok
  )
    return r.blob();
  const data = await r.json();
  if (!r.ok) throw Error(data.error || "Операция не удалась.");
  return data;
}
const act = (action, data = {}) =>
  api("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...data }),
  });
async function refresh() {
  const generation = acceptedJobGeneration;
  const current = await api("/api/state");
  if (generation !== acceptedJobGeneration) return;
  state = current;
  render();
}
function navigate(v) {
  navigationRequest++;
  if (v === "downloads") return showSettingsSection("downloads");
  if (v === "packs") return window.PackHubUI?.openCatalog();
  if (v === "worlds") { kind = "worlds"; v = "addons"; }
  view = v;
  render();
  $("main").scrollTop = 0;
  window.scrollTo(0, 0);
}
async function navigateFresh(v, section = null) {
  const request = ++navigationRequest;
  const current = await api("/api/state");
  if (request !== navigationRequest) return;
  state = current;
  if (section !== null) settingsSection = section === "downloads" ? "downloads" : "general";
  navigate(v);
}
function showSettingsSection(section) {
  settingsSection = section === "downloads" ? "downloads" : "general";
  navigate("settings");
}
function selectInfo(p) {
  return (
    '<span class="tag">' +
    esc(p.version) +
    '</span><span class="tag"><em></em>' +
    esc(loaders[p.loader]) +
    "</span>"
  );
}
function localMode() {
  return state.mode === "local";
}
function playControls() {
  const identity = localMode()
    ? ""
    : '<span class="tag">' +
      ic("user") +
      esc(
        state.mode === "prism"
          ? "Microsoft · Prism"
          : state.account || "Microsoft",
      ) +
      "</span>";
  return (
    '<div class="play-controls">' +
    identity +
    btn("play", "Играть", "play", "main") +
    "</div>"
  );
}
function updatePlayButtons() {
  const running = Array.isArray(state.runningProfiles)
    ? state.runningProfiles.includes(state.active)
    : state.gameRunning;
  const unavailable = Boolean(
    selectingBuild || running || state.job?.busy,
  );
  const enableSteam = document.querySelector('[data-act="enableSteam"]');
  if (enableSteam) enableSteam.disabled = unavailable;
  document.querySelectorAll('[data-act="play"]').forEach((button) => {
    if (button.disabled !== unavailable) button.disabled = unavailable;
    button.title = running
      ? "Эта сборка уже используется. Для ещё одного Minecraft выбери другую сборку или её отдельную копию."
      : "Запустить выбранную сборку";
    const label = running
      ? "Сборка запущена"
      : state.job?.busy
        ? "Подождите…"
        : "Играть";
    if (button.dataset.label !== label) {
      button.innerHTML = ic(running ? "game" : "play") + label;
      button.dataset.label = label;
    }
  });
  document.querySelectorAll('[data-act="saveProfile"], [data-act="saveProfileOnly"], [data-act="saveWithFabric"]').forEach((button) => {
    button.disabled = Boolean(profileJob || profileSubmission);
  });
}
function homeBuildPicker() {
  if (state.profiles.length < 2) return "";
  const options = state.profiles.map(
    (p) =>
      '<option value="' +
      esc(p.id) +
      '"' +
      (p.id === state.active ? " selected" : "") +
      ">" +
      esc(p.name) +
      " · " +
      esc(p.version) +
      " · " +
      esc(loaders[p.loader]) +
      "</option>",
  );
  return (
    '<label class="build-picker"><span>Сборка</span><select id="homeBuild" title="' + esc(active()?.name || "") + '">' +
    options.join("") +
    "</select></label>"
  );
}
function home() {
  const p = active();
  if (!p)
    return emptyBuildView(
      "Игровая панель",
      "Чтобы запустить Minecraft, создай сборку или загрузи готовый ZIP из BlockHarbor.",
    ) + '<section class="panel"><h3>Мой персонаж</h3><p>Настрой ник и скин в профиле лаунчера.</p><button type="button" class="btn" data-view="profile">' + ic("user") + 'Открыть профиль</button></section>';
  return (
    head(
      "Игровая панель",
      homeBuildPicker() + btn("create", "Новая сборка", "plus"),
    ) +
    '<div class="hero"><canvas id="harborScene" aria-hidden="true"></canvas><div class="hero-body"><div class="eyebrow">Активная сборка</div><h2>' +
    esc(p.name) +
    '</h2><div class="tags">' +
    selectInfo(p) +
    '</div><div class="actions">' +
    playControls() +
    btn("addons", "Содержимое", "layers", "secondary") +
    "</div></div></div>" +
    titleRow(
      "Дополнения сборки",
      '<button class="linkbtn" data-act="addons">Управление ' +
        ic("arrow") +
        "</button>",
    ) +
    '<div class="tiles">' +
    Object.entries(kinds)
      .map(
        ([k, [name, icon, col]]) =>
          '<button class="tile" data-kind="' +
          k +
          '"><span class="tile-icon ' +
          col +
          '">' +
          ic(icon) +
          "</span><small>" +
          String(state.contents[k].length).padStart(2, "0") +
          "</small><h3>" +
          name +
          "</h3></button>",
      )
      .join("") +
    "</div>"
  );
}
function buildIdentity(p) {
  if (p.cover && window.ExperienceUI?.media(p.cover)) return `<img class="build-cover" src="${ExperienceUI.media(p.cover)}" alt=""><span class="build-name"><strong>${esc(p.name)}</strong><small>${esc(loaders[p.loader])} · ${esc(p.version)}${p.group_name?" · "+esc(p.group_name):""}</small>${p.description?`<small class="build-description">${esc(p.description)}</small>`:""}</span>`;
  return (
    '<span class="mini-icon">' +
    ic("cube") +
    '</span><span class="build-name"><strong>' +
    esc(p.name) +
    "</strong><small>" +
    esc(loaders[p.loader]) +
    " · " +
    esc(p.version) +
    "</small></span>"
  );
}
function buildRows(profiles) {
  return profiles
    .map((p) => {
      const selected = p.id === state.active;
      const favorite = Boolean(p.favorite);
      const favoriteLabel = favorite ? "Убрать из избранного" : "Добавить в избранное";
      return (
        '<div data-profile="' + esc(p.id) + '" class="build-row build-color-' + esc(p.color || 'green') + ' ' +
        (selected ? " active" : "") +
        '">' +
        '<button type="button" class="build-pick" aria-pressed="' +
        selected +
        '" data-pick="' +
        esc(p.id) +
        '" title="Выбрать сборку «' +
        esc(p.name) +
        '»">' +
        buildIdentity(p) +
        "</button>" +
        '<button type="button" class="btn build-favorite-toggle" data-favorite-build="' +
        esc(p.id) + '" aria-pressed="' + favorite + '" aria-label="' +
        favoriteLabel + ' · ' + esc(p.name) + '" title="' + favoriteLabel + '"' +
        (favoriteRequests.has(p.id) ? ' disabled aria-busy="true"' : "") + '>' +
        ic("star") + "</button>" +
        '<button type="button" class="btn build-settings" data-build-settings="' +
        esc(p.id) +
        '" aria-label="Настройки сборки «' +
        esc(p.name) +
        '»" title="Настройки сборки">' +
        ic("gear") +
        "<span>Настройки</span></button>" +
        '<button type="button" class="btn build-settings" data-export-build="' +
        esc(p.id) +
        '" title="Выгрузить сборку в ZIP" aria-label="Выгрузить сборку «' +
        esc(p.name) +
        '»">' +
        ic("upload") +
        "<span>Выгрузить</span></button>" +
        `<button type="button" class="btn build-settings${window.PackHubUI?.isPublished(p.id) ? " is-published" : ""}" data-pack-share="${esc(p.id)}" title="${window.PackHubUI?.isPublished(p.id) ? "Управление публикацией" : "Поделиться сборкой"}" aria-label="${window.PackHubUI?.isPublished(p.id) ? "Опубликовано" : "Поделиться сборкой"} «${esc(p.name)}»">${ic(window.PackHubUI?.isPublished(p.id) ? "check" : "share")}<span>${window.PackHubUI?.isPublished(p.id) ? "Опубликовано" : "Поделиться"}</span></button>` +
        '<button type="button" class="build-remove" data-remove-build="' +
        esc(p.id) +
        '" aria-label="Удалить сборку «' +
        esc(p.name) +
        '»" title="Удалить сборку">' +
        ic("trash") +
        "<span>Удалить</span></button></div>"
      );
    })
    .join("");
}
function deletedBuilds() {
  const profiles = state.deletedProfiles || [];
  if (!profiles.length) return "";
  return (
    '<details class="deleted-builds"><summary>Удалённые сборки <span>' +
    profiles.length +
    "</span></summary>" +
    profiles
      .map(
        (p) =>
          '<div class="build-row archived-build">' +
          buildIdentity(p) +
          '<div class="archive-actions"><button type="button" class="btn" data-restore-build="' +
          esc(p.id) +
          '"' +
          (p.purging ? ' disabled title="Удаление файлов уже началось"' : "") +
          ' aria-label="Восстановить сборку «' +
          esc(p.name) +
          '»">' +
          ic("refresh") +
          "Восстановить</button>" +
          '<button type="button" class="build-remove" data-purge-build="' +
          esc(p.id) +
          '" aria-label="Удалить сборку «' +
          esc(p.name) +
          '» с диска">' +
          ic("trash") +
          (p.purging ? "Продолжить удаление" : "Удалить с диска") +
          "</button></div></div>",
      )
      .join("") +
    "</details>"
  );
}
function builds() {
  return (
    head(
      "Мои сборки",
      btn("packCatalog", "Каталог сборок", "search") +
      btn("importBuild", "Загрузить сборку", "download") +
        btn("create", "Новая сборка", "plus", "main"),
      "У каждой сборки свои миры, дополнения и настройки.",
    ) +
    (window.ExperienceUI?.filterBar() || "") +
    (state.profiles.length
      ? buildRows(window.ExperienceUI ? ExperienceUI.sortedProfiles(state.profiles) : state.profiles)
      : '<div class="empty">Сборок пока нет.</div>') +
    deletedBuilds()
  );
}
function emptyBuildView(title, message) {
  const restore = state.deletedProfiles?.length
    ? '<button type="button" class="btn" data-view="builds">К списку сборок</button>'
    : "";
  return (
    head(title) +
    '<section class="panel"><p>' +
    message +
    '</p><div class="actions">' +
    btn("create", "Новая сборка", "plus", "main") +
    btn("importBuild", "Загрузить сборку", "download") +
    restore +
    "</div></section>"
  );
}
// Reuse decoded images when tabs/state rebuild the list, without an unbounded cache.
const installedIconImages = new Map();
function installedIcon(file) {
  return (
    '<span class="installed-icon" aria-hidden="true" data-image="' +
    esc(file.icon) +
    '">' +
    ic(kinds[kind][1]) +
    "</span>"
  );
}
function mountInstalledIcons() {
  for (const slot of document.querySelectorAll("#view .installed-icon")) {
    const url = slot.dataset.image;
    if (!/^\/api\/content-icon\/[a-f0-9]{64}\.png$/.test(url)) continue;
    const cached = installedIconImages.get(url);
    if (cached && !cached.isConnected) {
      slot.append(cached);
      slot.dataset.loaded = "";
      installedIconImages.delete(url);
      installedIconImages.set(url, cached);
      continue;
    }
    const image = new Image(36, 36);
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.onload = async () => {
      image.onload = image.onerror = null;
      try {
        await image.decode();
      } catch {
        image.remove();
        return;
      }
      slot.dataset.loaded = "";
      installedIconImages.delete(url);
      installedIconImages.set(url, image);
      if (installedIconImages.size > 128) {
        installedIconImages.delete(installedIconImages.keys().next().value);
      }
    };
    image.onerror = () => {
      image.onload = image.onerror = null;
      image.remove();
    };
    slot.append(image);
    image.src = url;
  }
}
function addons() {
  const p = active();
  if (!p) return builds();
  const desc = {
    mods: "Дополнения для " + esc(loaders[p.loader]) + " и Minecraft " + esc(p.version) + ". Файлы можно перетащить в окно.",
    shaderpacks: "Выбери шейдерпак в настройках Minecraft. Для шейдеров нужен совместимый графический мод.",
    resourcepacks: "Добавленные наборы включаются в Minecraft: «Настройки → Наборы ресурсов».",
  };
  return head(p.name, "", esc(p.description || "")) +
    '<div class="build-settings-bar"><div class="tags build-settings-info">' + selectInfo(p) +
    '</div><div class="build-settings-actions" role="group" aria-label="Действия сборки">' +
    btn("folder", "Папка сборки", "folder") +
    (kind === "errors"
      ? '<button type="button" class="btn" data-experience="diagnostics" data-experience-profile="' + esc(p.id) + '">' + ic("search") + 'Проверить сборку</button>'
      : "") + "</div></div>" +
    '<div class="tabs" aria-label="Содержимое сборки">' + Object.entries(kinds).map(([k,[v]]) => '<button class="' + (k === kind ? "on" : "") + '" data-kind="' + k + '">' + v + ' · ' + state.contents[k].length + '</button>').join("") + '<button class="' + (kind === "worlds" ? "on" : "") + '" data-kind="worlds">Миры и копии</button><button class="' + (kind === "errors" ? "on" : "") + '" data-kind="errors">Ошибки сборки</button></div>' +
    (kind === "errors"
      ? '<section class="panel" id="buildErrorsPanel"><h2>Ошибки сборки</h2><div data-error-list aria-live="polite">Загружаем записи…</div></section>'
      : kind === "worlds"
      ? (window.BuildCareUI?.worlds(p) || '<div class="empty">Подготавливаем список миров…</div>')
      : '<div class="splitbar"><p>' + desc[kind] + '</p><div class="actions">' + btn("add", "Добавить файл", "plus") + btn("openCatalog", "Скачать", "download", "main") + '</div></div>' +
        (window.BuildCareUI?.content(p, kind, state.contents[kind]) || '<div class="empty">Подготавливаем управление содержимым…</div>'));
}
async function showSteamCheck() {
  const profile = active();
  if (!profile) return;
  modal(
    "Проверка игры по сети",
    '<p id="steamCheckWaiting" role="status">Проверяем файлы и последний запуск…</p>',
  );
  const pending = $("#steamCheckWaiting");
  const data = await api(
    "/api/steam/check?" + new URLSearchParams({ id: profile.id }),
  );
  if (!pending.isConnected || !$("#dialog").open || state.active !== profile.id)
    return;
  const row = (title, value) =>
    "<p><strong>" + esc(title) + "</strong><br>" + esc(value) + "</p>";
  const when = data.logTime
    ? new Date(data.logTime * 1000).toLocaleString("ru-RU")
    : "";
  const repairs = [];
  if (data.issues.includes("host"))
    repairs.push("В настройках e4steam отключено открытие мира через Steam.");
  if (data.issues.includes("appid"))
    repairs.push(
      "В файле steam_appid.txt указан другой App ID. Для этой сборки e4steam нужен 480.",
    );
  const body = [
    row(
      data.profile,
      ["Minecraft " + data.version, data.loader, data.loaderVersion]
        .filter(Boolean)
        .join(" · "),
    ),
    row(
      "Компоненты Steam",
      !data.supported
        ? "У этой версии и загрузчика нет встроенного комплекта."
        : (data.ready ? "Установлены" : "Требуется подготовка") +
            " · e4steam " +
            data.coreVersion +
            " · Public Servers " +
            data.addonVersion,
    ),
    repairs.map((text) => row("Настройка сборки", text)).join(""),
    data.configNote ? row("Настройки", data.configNote) : "",
    row(when ? "Последний журнал · " + when : "Журнал Minecraft", data.log),
    data.modCode
      ? '<label for="steamModCode">Код набора модов · ' +
        Number(data.modCount) +
        " JAR</label>" +
        '<div class="actions steam-code"><input class="field" id="steamModCode" readonly value="' +
        esc(data.modCode) +
        '">' +
        btn("copySteamCode", "Копировать", "copy") +
        '</div><p class="note">Сравните код на обоих компьютерах. Он учитывает версию Minecraft, загрузчик и содержимое всех JAR. При расхождении сверьте моды или передайте другу ZIP сборки.</p>'
      : row("Сравнение модов", data.modNote),
    data.downloadProxy
      ? '<p class="note">Для загрузок обнаружен прокси. Игровой трафик e4steam проходит через настольный Steam.</p>'
      : "",
    repairs.length
      ? '<p class="note">Исправление восстановит отмеченные параметры. Копия исходных файлов сохранится в папке сборки.</p>'
      : "",
    '<div class="dialog-actions">' +
      (data.supported && !data.ready
        ? btn("prepareSteam", "Подготовить файлы Steam", "download", "main")
        : "") +
      (data.supported && repairs.length
        ? btn("repairSteamSettings", "Исправить настройки Steam", "gear")
        : "") +
      btn("openSteam", "Открыть Steam", "users") +
      "</div>",
  ].join("");
  modal("Проверка игры по сети", body);
}

function friendsView() {
  const profile = active();
  const steam = state.steam;
  const readiness = !profile
    ? "Для совместной игры создай сборку с поддержкой Steam или загрузи ZIP друга."
    : steam.supported
      ? steam.installed
        ? "e4steam и Public Servers готовы в этой сборке."
        : "Для этой сборки BlockHarbor подготовит сетевые компоненты при следующем запуске с главной страницы."
      : steam.canEnable
        ? "BlockHarbor установит Fabric и e4steam для существующих миров этой сборки. Миры останутся в той же папке, на той же версии Minecraft."
        : (steam.message || steam.reason || "Для этой версии и загрузчика нет встроенной поддержки Steam.") +
          " Доступны одиночная игра, обычные серверы и другие способы подключения.";
  const controls = steam.supported
    ? ""
    : steam.canEnable
      ? btn("enableSteam", "Включить Steam в этой сборке", "users", "main")
      : btn("steamBuild", "Создать сборку для Steam", "plus");
  return (
    head("Игра через Steam", homeBuildPicker()) +
    '<div class="friends-panels"><section class="panel"><div class="line">' +
    ic("cube") +
    "<h3>" +
    esc(profile?.name || "Сборка для игры с друзьями") +
    "</h3>" +
    (profile ? selectInfo(profile) : "") +
    "</div>" +
    "<p>" +
    esc(readiness) +
    "</p>" +
    '<div class="actions steam-actions">' +
    controls +
    (profile ? "" : btn("importBuild", "Загрузить сборку", "download")) +
    btn("openSteam", "Открыть Steam", "users") +
    (profile ? btn("checkSteam", "Проверить подключение", "search") : "") +
    '</div><p class="note">Войдите в разные аккаунты настольного Steam и добавьтесь в друзья. Запускайте Minecraft из BlockHarbor. На каждом компьютере Steam и BlockHarbor должны работать от одного пользователя с одинаковыми правами. ' +
    '<a href="' +
    pages.steamInstall +
    '" target="_blank" rel="noopener noreferrer">Скачать Steam</a></p></section>' +
    '<section class="panel"><h3>Пригласить друга</h3>' +
    '<ol class="steam-steps"><li>В одиночном мире нажми <strong>Esc → Открыть для сети</strong>.</li>' +
    "<li>Выбери <strong>Для друзей Steam</strong> или <strong>Только по приглашению</strong> и открой мир.</li>" +
    "<li>Нажми <strong>Пригласить друзей</strong> и выбери друга в оверлее Steam.</li></ol></section>" +
    '<section class="panel"><h3>Принять приглашение</h3>' +
    '<ol class="steam-steps"><li>Запусти сборку и дождись главного меню Minecraft.</li>' +
    "<li>Прими приглашение друга в Steam. Оверлей обычно открывается сочетанием <strong>Shift + Tab</strong>.</li>" +
    '<li>Подтверди вход в Minecraft. Хозяин должен оставаться в мире.</li></ol><p class="note">Если приглашение не приходит, хозяин может открыть мир в режиме «Для друзей Steam». Вставь его полный зелёный адрес <code>s-…-….steam</code> из чата в «Сетевая игра → Прямое подключение». После повторного открытия мира понадобится новый адрес.</p><p class="note">С включённым VPN Steam выбирает доступное прямое соединение или ретранслятор Valve. Если подключение зависло, проверьте на обоих компьютерах, разрешает ли VPN игровой трафик Steam и UDP. Когда VPN поддерживает раздельную маршрутизацию, Steam и Java можно направить через рабочий маршрут.</p></section></div>'
  );
}
let profileDraft = null;
let profileJob = "";
let profileSubmission = null;
function characterDraft() {
  const saved = {
    nickname: state.nickname || "Player",
    model: state.model || "classic",
    realName: state.account || state.realName || "",
  };
  if (!profileDraft) {
    profileDraft = { ...saved, file: null, dirty: {}, revisions: { nickname: 0, model: 0, realName: 0 } };
  } else {
    // A build selection or a state refresh must preserve edits to the player.
    // Only untouched fields follow newly saved settings.
    for (const name of Object.keys(saved)) {
      if (!profileDraft.dirty[name]) profileDraft[name] = saved[name];
    }
  }
  return profileDraft;
}
function editCharacter(name, value) {
  const draft = characterDraft();
  if (draft[name] !== value) {
    draft[name] = value;
    draft.dirty[name] = true;
    draft.revisions[name]++;
  }
  return draft;
}
function captureCharacterFields() {
  const draft = characterDraft();
  if ($("#nick")) editCharacter("nickname", $("#nick").value.trim());
  if ($("#realName")) editCharacter("realName", $("#realName").value.trim());
  if ($("#skinModel")) editCharacter("model", $("#skinModel").value);
  return draft;
}
function validatePlayerName(value) {
  const name = value.trim();
  if (!/^[A-Za-z0-9_]{3,16}$/.test(name))
    throw Error("Ник должен содержать от 3 до 16 латинских букв, цифр или знаков подчёркивания.");
  return name;
}
function profile() {
  const hasBuild = Boolean(active());
  const draft = characterDraft();
  const pick = btn("pickSkin", "Выбрать скин игрока", "image");
  return (
    head(
      "Профиль",
      hasBuild
        ? ""
        : btn("create", "Новая сборка", "plus", "main") +
            btn("importBuild", "Загрузить сборку", "download"),
      "Ник общий для всех сборок и миров. Изменения профиля применяются при следующем запуске игры.",
    ) +
    '<div class="panel side-panel"><div class="skin-stage" id="skinStage">' +
    (state.skin || draft.file
      ? '<img id="skinPreview" alt="Персонаж с выбранным скином">'
      : ic("user") + "<small>Предпросмотр скина</small>") +
    '</div><div class="stack"><h3>Мой персонаж</h3>' +
    '<label>Ник в игре<input id="nick" class="field" maxlength="16" required pattern="[A-Za-z0-9_]{3,16}" spellcheck="false" autocomplete="off" placeholder="Твой ник" value="' +
    esc(draft.nickname) + '"></label>' +
    (localMode()
      ? ""
      : '<small class="note">При входе через Microsoft игра использует имя твоего Java-профиля. Этот ник используется в режиме «Локальный профиль».</small><label>Имя профиля Minecraft<input id="realName" class="field" maxlength="16" value="' +
        esc(draft.realName) +
        '"></label>') +
    '<label>Модель скина<select id="skinModel" class="field"><option value="classic">Classic · широкие руки</option><option value="slim"' +
    (draft.model === "slim" ? " selected" : "") +
    ">Slim · узкие руки</option></select></label>" +
    '<div class="actions">' +
    pick +
    (draft.file
      ? '<small class="note">' + esc(draft.file.name) + "</small>"
      : "") +
    "</div>" +
    (hasBuild && state.canUseFabricSkin && (state.skin || draft.file)
      ? '<label><span><input type="checkbox" id="useFabricSkin"> Установить Fabric для скина в выбранной сборке</span></label>'
      : "") +
    '<div class="actions">' +
    btn("saveProfile", "Сохранить все параметры", "check", "main") +
    "</div>" +
    (state.skinNetwork
      ? '<small class="note">В общем мире с поддержкой BlockHarbor другие участники увидят сохранённый скин при входе.</small>'
      : "") +
    (state.skin
      ? '<div><button type="button" class="linkbtn" data-act="officialSkin">Установить скин в Java-профиле</button></div>'
      : "") +
    "</div></div>"
  );
}
function generalSettings() {
  const modes = [
    ["local", "Локальный профиль"],
    ["prism", "Microsoft через Prism"],
    ["direct", "Свой Microsoft Client ID"],
  ];
  return (
    '<div class="panel stack"><h3>Запуск Minecraft</h3><label>Профиль запуска<select id="mode" class="field">' +
    modes
      .map(
        ([k, v]) =>
          '<option value="' +
          k +
          '" ' +
          (state.mode === k ? "selected" : "") +
          ">" +
          v +
          "</option>",
      )
      .join("") +
    '</select></label><p id="modeHelp"></p><label>Оперативная память для игры, МБ<input id="ram" class="field" type="number" min="1024" max="32768" step="256" value="' +
    (state.autoMemory ? "" : state.memory) +
    '" placeholder="Автоматически · ' +
    state.recommendedMemory +
    '"></label><small class="note">Оставь поле пустым для автоматического выбора.</small>' +
    '<div id="advancedAuth" class="stack"><label>Application (client) ID<input id="clientId" class="field" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" value="' +
    esc(state.clientId) +
    '"></label>' +
    "<p>Требуется регистрация приложения для Minecraft Services с redirect URI <code>http://localhost:43821/callback</code>.</p></div>" +
    '<div class="actions">' +
    btn("saveSettings", "Сохранить настройки", "check", "main") +
    '<span id="authAction">' +
    (state.account
      ? btn("logout", "Выйти (" + esc(state.account) + ")", "user")
      : btn("signin", "Войти в Microsoft", "user")) +
    "</span></div></div>"
  );
}
function settings() {
  const downloads = settingsSection === "downloads";
  return head("Настройки") +
    '<div class="tabs settings-tabs" role="tablist" aria-label="Разделы настроек">' +
    '<button type="button" role="tab" id="settingsGeneralTab" aria-controls="settingsPanel" aria-selected="' + !downloads + '" class="' + (!downloads ? "on" : "") + '" data-settings-section="general">' + ic("settings") + 'Основные</button>' +
    '<button type="button" role="tab" id="settingsDownloadsTab" aria-controls="settingsPanel" aria-selected="' + downloads + '" class="' + (downloads ? "on" : "") + '" data-settings-section="downloads">' + ic("download") + 'Загрузки и обновления</button></div>' +
    '<section id="settingsPanel" role="tabpanel" aria-labelledby="' + (downloads ? "settingsDownloadsTab" : "settingsGeneralTab") + '">' +
    (downloads ? (window.UpdatesUI?.render({embedded: true}) || "") : generalSettings() + (window.ExperienceUI?.settingsPanel() || "")) + '</section>';
}
function updateModeHelp() {
  const mode = $("#mode").value;
  const help = {
    local:
      "Minecraft запускается под общим ником из профиля лаунчера. Его можно изменить в «Профиле» или на главном экране. Подходит для одиночной игры и серверов, принимающих локальные профили. При первом запуске потребуется интернет для загрузки выбранной версии и Java.",
    prism:
      "Для серверов с проверкой аккаунта нужен Microsoft-аккаунт с Minecraft Java Edition. При первом запуске BlockHarbor подготовит Prism и предложит войти. Дальше запускай ту же сборку кнопкой «Играть».",
    direct:
      "Вход Microsoft через собственное приложение с одобренным Client ID. Этот режим использует имя и права твоего Java-профиля.",
  };
  $("#modeHelp").textContent = help[mode];
  $("#advancedAuth").hidden = mode !== "direct";
  $("#authAction").hidden = mode !== "direct";
}
function render() {
  if (!state) return;
  $("#crumb").textContent = names[view];
  $("#topUser").textContent = localMode()
    ? state.nickname || "Player"
    : state.mode === "prism"
      ? "Microsoft · Prism"
      : state.account || "Мой профиль";
  document.querySelectorAll("aside [data-view]").forEach((el) => {
    el.classList.toggle(
      "sel",
      el.dataset.view === (view === "addons" ? "builds" : view),
    );
    el.title = el.textContent.trim();
    el.setAttribute("aria-label", el.textContent.trim());
  });
  const archiveOpen = $(".deleted-builds")?.open;
  $("#view").innerHTML = {
    home,
    builds,
    addons,
    friends: friendsView,
    profile,
    settings,
    gallery: () => window.ExperienceUI?.gallery() || "",
  }[view]();
  if (view === "profile" && window.ExperienceUI) $("#view").insertAdjacentHTML("beforeend", ExperienceUI.wardrobe());
  window.ExperienceUI?.mount();
  window.BuildCareUI?.mount();
  window.PackHubUI?.mount();
  window.UpdatesUI?.mount();
  if (archiveOpen && $(".deleted-builds")) $(".deleted-builds").open = true;
  updatePlayButtons();
  if (view === "settings" && settingsSection === "general") updateModeHelp();
  if (view === "addons") mountInstalledIcons();
  if (view === "home") drawHarbor($("#harborScene"));
  if (view === "profile" && (state.skin || characterDraft().file)) preview();
}
async function preview() {
  let run = ++loadedSkin;
  try {
    const draft = characterDraft();
    const query = "?model=" + encodeURIComponent(draft.model);
    const blob = draft.file
      ? await api("/api/skin-preview" + query, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: draft.file,
        })
      : await api("/api/preview" + query);
    if (run !== loadedSkin || !$("#skinPreview")) return;
    const data = await new Promise((resolve) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.readAsDataURL(blob);
    });
    if (run === loadedSkin && $("#skinPreview")) $("#skinPreview").src = data;
  } catch (e) {
    notice(e.message, true);
  }
}
function modal(title, body) {
  const d = $("#dialog");
  $("#dialogTitle").textContent = title;
  $("#dialogBody").innerHTML = body;
  $("#dialogBody").scrollTop = 0;
  if (!d.open) d.showModal();
  moveNotice();
}
function closeDialog() {
  $("#dialog").close();
  moveNotice();
}
function whenDialogClosed(dialog, scope, callback) {
  const onClose = () => {
    // Native close events are queued. An earlier modal can close immediately
    // before this scope opens in the shared dialog; keep its requests alive.
    if (dialog.open && scope.isConnected) return;
    dialog.removeEventListener("close", onClose);
    callback();
  };
  dialog.addEventListener("close", onClose);
}
function releaseOptions(versions, current) {
  return versions
    .map((version) => {
      const value = esc(version);
      return (
        '<option value="' +
        value +
        '"' +
        (version === current ? " selected" : "") +
        ">" +
        value +
        "</option>"
      );
    })
    .join("");
}
const newBuildForms = new WeakMap();
function currentBuildForm(form) {
  return Boolean(form?.isConnected && $("#dialog").open && $("#newForm") === form);
}
function renderBuildVersions(form) {
  const data = newBuildForms.get(form);
  if (!data || !currentBuildForm(form)) return;
  const select = form.querySelector("#version"), current = select.value;
  const showAll = form.querySelector("#allBuildVersions").checked;
  if (showAll && data.allVersions.length) {
    const groups = {
      release: "Релизы", snapshot: "Снапшоты", old_beta: "Бета-версии", old_alpha: "Альфа и ранние версии",
    };
    const entries = [...Object.keys(groups), ...new Set(data.allVersions.map(row => row.type).filter(type => !groups[type]))];
    select.innerHTML = entries.map(type => {
      const versions = data.allVersions.filter(row => row.type === type).map(row => row.id);
      return versions.length ? '<optgroup label="' + esc(groups[type] || "Другие версии") + '">' + releaseOptions(versions, current) + "</optgroup>" : "";
    }).join("");
  } else {
    // Hiding snapshots should not silently change a version already selected.
    const retained = current && !data.releases.includes(current)
      ? '<optgroup label="Выбранная версия">' + releaseOptions([current], current) + "</optgroup>"
      : "";
    select.innerHTML = retained + releaseOptions(data.releases, current);
  }
}
function updateBuildLoaderUI(form) {
  const data = newBuildForms.get(form);
  if (!data || !currentBuildForm(form)) return;
  const version = form.querySelector("#version").value;
  const select = form.querySelector("#loader"), selected = select.value;
  const rows = data.version === version ? data.report?.loaders || [] : [];
  for (const option of select.options) {
    const row = rows.find(item => item.id === option.value);
    option.disabled = row?.supported === false;
    option.textContent = loaders[option.value] + (option.value === "vanilla" ? " · без модов" : "") +
      (row?.manual ? " · требуется ручная установка" : option.disabled ? " · недоступен" : "");
  }
  select.value = selected;
  const row = rows.find(item => item.id === selected);
  const unsupported = row?.supported === false;
  const unknown = !row || row.supported === null;
  const status = form.querySelector("#loaderStatus");
  status.dataset.state = data.loading ? "loading" : unsupported ? "unsupported" : unknown ? "unknown" : "supported";
  status.textContent = data.loading
    ? "Проверяем загрузчики для Minecraft " + version + "…"
    : unsupported
      ? row.message || loaders[selected] + " недоступен для Minecraft " + version + ". Выбери другой загрузчик или Vanilla."
      : unknown
        ? data.error ? data.error + " При установке лаунчер проверит совместимость ещё раз."
          : row?.message || "Совместимость загрузчика пока не подтверждена. Проверим её при установке."
        : row.message || (loaders[selected] + " доступен для Minecraft " + version + ".");
  form.querySelector("#buildVanillaChoice").hidden = !unsupported;
  form.querySelector('[type="submit"]').disabled = data.loading || data.submitting || unsupported || pendingBuild?.form === form;
}
function loadBuildLoaders(form, force = false) {
  const data = newBuildForms.get(form);
  if (!data || !currentBuildForm(form)) return Promise.resolve(null);
  const version = form.querySelector("#version").value;
  if (!force && data.version === version && (data.promise || data.report || data.error))
    return data.report ? Promise.resolve(data.report) : data.promise || Promise.resolve(null);
  const ticket = ++data.request;
  if (data.version !== version) data.report = null;
  data.version = version;
  data.error = "";
  data.loading = !data.report;
  updateBuildLoaderUI(form);
  const isCurrent = () => currentBuildForm(form) && data.request === ticket && form.querySelector("#version").value === version;
  data.promise = (async () => {
    try {
      const query = new URLSearchParams({version});
      if (force) query.set("refresh", "1");
      const report = await api("/api/loaders?" + query);
      if (!isCurrent()) return null;
      if (report.version !== version || !Array.isArray(report.loaders)) throw new Error("Некорректный ответ каталога загрузчиков.");
      data.report = report;
      return report;
    } catch (error) {
      if (isCurrent()) data.error = "Не удалось проверить каталог загрузчиков. Твой выбор сохранён.";
      return null;
    } finally {
      if (isCurrent()) {
        data.loading = false;
        data.promise = null;
        updateBuildLoaderUI(form);
      }
    }
  })();
  return data.promise;
}
async function loadReleases(force = false) {
  const select = $("#version"),
    status = $("#catalogStatus");
  if (!select) return;
  const form = select.closest("#newForm"), data = newBuildForms.get(form);
  const ticket = String(Number(select.dataset.request || 0) + 1);
  select.dataset.request = ticket;
  const refreshLoaders = force ? loadBuildLoaders(form, true) : null;
  try {
    const result = await api("/api/releases" + (force ? "?refresh=1" : "?cached=1"));
    if (!currentBuildForm(form) || select.dataset.request !== ticket) return;
    state.versions = result.versions;
    data.releases = result.versions;
    data.allVersions = result.allVersions || [];
    form.querySelector("#allBuildVersions").disabled = !data.allVersions.length;
    renderBuildVersions(form);
    if (!force) loadBuildLoaders(form);
    status.hidden = !result.stale;
    status.textContent = result.stale
      ? result.message || "Не удалось обновить каталог Mojang. Доступны сохранённые версии."
      : "";
    if (force) notice(result.stale ? status.textContent : "Список версий обновлён.", Boolean(result.stale));
  } catch (error) {
    if (!currentBuildForm(form) || select.dataset.request !== ticket) return;
    status.hidden = false;
    status.textContent =
      "Не удалось обновить каталог Mojang. Доступны сохранённые версии.";
    if (force) notice(status.textContent, true);
  } finally {
    if (refreshLoaders) await refreshLoaders;
    if (force && currentBuildForm(form) && select.dataset.request === ticket) await loadBuildLoaders(form);
  }
}
let pendingBuild = null;
function finishBuild(form, destination) {
  if (!form.isConnected || !$("#dialog").open) return;
  closeDialog();
  navigate(destination);
}
function createBox(preferredLoader = "fabric") {
  modal(
    "Новая сборка",
    '<form id="newForm" class="stack"><label>Название<input class="field" id="buildName" placeholder="Название сборки" maxlength="48" required autofocus></label><div class="field-group"><label>Версия Minecraft<select class="field" id="version" required>' +
      releaseOptions(state.versions, "1.20.1") +
      '</select></label><label>Загрузчик<select class="field" id="loader">' +
      Object.entries(loaders)
        .sort(([a], [b]) => (a === "fabric" ? -1 : b === "fabric" ? 1 : 0))
        .map(
          ([k, v]) =>
            '<option value="' +
            k +
            '"' +
            (k === preferredLoader ? " selected" : "") +
            ">" +
            v +
            (k === "vanilla" ? " · без модов" : "") +
            "</option>",
        )
        .join("") +
      '</select></label></div><label class="build-version-toggle"><input type="checkbox" id="allBuildVersions" disabled> Показывать снапшоты и старые версии</label><div class="build-loader-compatibility"><p id="loaderStatus" class="note" aria-live="polite"></p><button type="button" class="linkbtn" id="buildVanillaChoice" data-act="useVanilla" hidden>Выбрать Vanilla</button></div><button type="button" class="linkbtn" data-act="versions">' +
      ic("refresh") +
      ' Обновить список версий</button><p id="catalogStatus" class="note" hidden></p><div class="dialog-actions">' +
      btn("close", "Отмена", "") +
      '<button class="btn main" type="submit">' +
      ic("plus") +
      "Создать сборку</button></div></form>",
  );
  const form = $("#newForm");
  newBuildForms.set(form, {releases: [...state.versions], allVersions: [], version: "", request: 0,
    report: null, promise: null, error: "", loading: false, submitting: false});
  loadBuildLoaders(form);
  loadReleases();
}
async function runJob(action, data = {}) {
  const j = await act(action, data);
  acceptedJobGeneration++;
  lastJob = j.job;
  if (action === "play") lastPlayJob = j.job;
  if (action === "purgeBuild") closeDialog();
  if (action === "installModrinth") window.BatchUI?.start(j.job, data);
  notice("Подготовка…", false, true);
  await tick();
  return j.job;
}
function showGameExit(job) {
  const box = $("#gameExit");
  const failure = job.gameExit;
  if (!failure) {
    box.hidden = true;
    return;
  }
  if (
    !failure.id ||
    failure.id === shownExit ||
    job.busy ||
    (!noticeOwner && $("#notice").classList.contains("busy"))
  )
    return;
  if (
    job.id === lastPlayJob &&
    job.ok &&
    $("#notice").textContent === job.message
  )
    notice("");
  shownExit = failure.id;
  box.hidden = false;
  const diagnosticsButton = box.querySelector('[data-experience="showBuildErrors"]');
  if (diagnosticsButton) diagnosticsButton.dataset.experienceProfile = failure.profile || state.active;
  $("#gameExitMessage").textContent =
    failure.message ||
    "Minecraft завершился с ошибкой (код " +
      failure.code +
      "). Журнал находится в папке сборки.";
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!state) return;
  // Hidden pages still need an occasional heartbeat for the local server's idle timeout.
  const delay = document.hidden
    ? 60000
    : state.job?.busy || lastJob || window.UpdatesUI?.isActive()
      ? 900
      : state.job?.updater?.autoEnabled ? 3000 : 15000;
  pollTimer = setTimeout(tick, delay);
}
async function tick(wake = false) {
  if (activePoll) return;
  clearTimeout(pollTimer);
  activePoll = true;
  const generation = acceptedJobGeneration;
  try {
    const j = await api("/api/job");
    // A job accepted while this request was in flight owns the next snapshot.
    // An older response must not replace its state, result or per-item progress.
    if (generation !== acceptedJobGeneration) return;
    pollError = "";
    if (typeof j.gameRunning === "boolean") state.gameRunning = j.gameRunning;
    if (Array.isArray(j.runningProfiles)) state.runningProfiles = j.runningProfiles;
    if (typeof j.localGameRunning === "boolean") state.localGameRunning = j.localGameRunning;
    if (typeof j.launcherCount === "number") state.launcherCount = j.launcherCount;
    state.job = j;
    window.PackHubUI?.observeJob(j);
    const updateNav = document.querySelector('aside [data-view="settings"]');
    if (updateNav) updateNav.classList.toggle("has-update", Boolean(j.updateAvailable));
    updatePlayButtons();
    if (j.busy) {
      lastJob = j.id;
      notice(j.message, false, true);
    } else if (j.id && lastJob === j.id) {
      lastJob = "";
      if (j.id === profileJob) {
        if (j.ok && profileSubmission) {
          state.nickname = profileSubmission.nickname;
          state.realName = profileSubmission.realName;
          state.model = profileSubmission.model;
          if (profileSubmission.file) state.skin = true;
          if (profileDraft?.file === profileSubmission.file) profileDraft.file = null;
          for (const name of ["nickname", "model", "realName"]) {
            if (profileDraft?.revisions[name] === profileSubmission.revisions[name]) profileDraft.dirty[name] = false;
          }
          characterDraft();
        }
        profileJob = "";
        profileSubmission = null;
      }
      const ended =
        j.ok &&
        j.id === lastPlayJob &&
        !j.gameRunning &&
        j.message.startsWith("Запущен Minecraft");
      notice(ended ? "Игра завершена." : j.message, !j.ok);
      if (j.id === lastPlayJob && j.ok && j.gameRunning)
        refreshAfterPlay = true;
      else await refresh();
      if (generation !== acceptedJobGeneration) return;
      if (pendingBuild && j.id === pendingBuild.id) {
        const { form, destination } = pendingBuild;
        pendingBuild = null;
        if (j.ok) finishBuild(form, destination);
        else updateBuildLoaderUI(form);
      }
    }
    if (refreshAfterPlay && !document.hidden && (!j.gameRunning || wake)) {
      await refresh();
      if (generation !== acceptedJobGeneration) return;
      refreshAfterPlay = false;
    }
    showGameExit(j);
    showStagedReview(j);
    observeProgress(j);
  } catch (e) {
    if (generation !== acceptedJobGeneration) return;
    if (!window.UpdatesUI?.handoffPending() && pollError !== e.message) notice(e.message, true);
    pollError = e.message;
  } finally {
    activePoll = false;
    schedulePoll();
  }
}
async function putFile(file, type, id, check = false, dependencies = "", acknowledgement = "") {
  const q = new URLSearchParams({ kind: type, id, name: file.name });
  if (check) q.set("check", "1");
  if (dependencies) q.set("dependencies", dependencies);
  if (acknowledgement) q.set("acknowledgement", acknowledgement);
  return api("/api/file?" + q, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
}
let uploadTarget = null;
async function downloadPlayerFile(path, filename) {
  const file = await api(path);
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
async function dispatch(action) {
  for (const extension of [window.ExperienceUI, window.BuildCareUI, window.PackHubUI, window.UpdatesUI]) {
    if (extension?.dispatch && await extension.dispatch(action)) return;
  }
  switch (action) {
    case "close":
      return closeDialog();
    case "dismissExit":
      $("#gameExit").hidden = true;
      return;
    case "importBuild":
      $("#buildZipPick").click();
      return;
    case "create":
      return createBox();
    case "addons":
      return navigateFresh("addons");
    case "play": {
      const id = state.active;
      const confirmation = await confirmSteamCompatibility({id}, {isCurrent: () => view === "home" && state.active === id});
      if (!confirmation) return;
      return runJob("play", {id, allowWithoutSteam: confirmation.allowWithoutSteam});
    }
    case "versions":
      return loadReleases(true);
    case "useVanilla": {
      const form = $("#newForm");
      if (!currentBuildForm(form)) return;
      form.querySelector("#loader").value = "vanilla";
      updateBuildLoaderUI(form);
      form.querySelector("#loader").focus();
      return;
    }
    case "folder":
      await act("folder", { id: state.active });
      return;
    case "openCatalog":
      return openModrinth(active(), kind);
    case "add":
      uploadTarget = { id: state.active, kind };
      $("#filePick").accept = kind === "mods" ? ".jar" : ".zip";
      $("#filePick").click();
      return;
    case "enableSteam":
      return runJob("enableSteam", { id: state.active });
    case "checkSteam":
      return showSteamCheck();
    case "prepareSteam":
    case "repairSteamSettings":
      closeDialog();
      return runJob(action, { id: state.active });
    case "copySteamCode": {
      const input = $("#steamModCode");
      input.focus();
      input.select();
      try {
        await navigator.clipboard.writeText(input.value);
        return notice("Код набора модов скопирован.");
      } catch {
        return notice("Код выделен. Нажми Ctrl+C, чтобы скопировать.");
      }
    }
    case "openSteam": {
      const result = await act("openSteam");
      if (result.steamAction === "install")
        notice("Открыта официальная страница установки Steam.");
      return;
    }
    case "steamBuild":
      createBox("forge");
      $("#buildName").value = "Мир Steam";
      return;
    case "officialSkin": {
      const signedIn = Boolean(state.account && state.mode === "direct");
      const body = signedIn
        ? "<p>Отправить сохранённый PNG и модель в профиль Minecraft <strong>" +
          esc(state.account) +
          "</strong>? Этот скин увидят игроки на серверах, где ты входишь под этим Java-профилем.</p>"
        : "<p>Скачай сохранённый PNG и открой свой Java-профиль на Minecraft.net. В разделе смены скина выбери модель " +
          (state.model === "slim" ? "Slim" : "Classic") +
          " и загрузи этот файл. Затем зайди в игру под тем же аккаунтом.</p>";
      return modal(
        "Скин Java-профиля",
        body +
          '<div class="actions">' +
          btn("downloadSkin", "Скачать мой PNG", "down") +
          (signedIn
            ? btn(
                "confirmOfficialSkin",
                "Применить к аккаунту",
                "check",
                "main",
              )
            : '<a class="btn main" target="_blank" rel="noopener noreferrer" href="https://www.minecraft.net/msaprofile/mygames">Открыть Minecraft.net</a>') +
          "</div>",
      );
    }
    case "confirmOfficialSkin":
      closeDialog();
      return runJob("uploadSkin");
    case "downloadSkin":
      return downloadPlayerFile(
        "/api/skin.png",
        (state.nickname || "BlockHarbor") + ".png",
      );
    case "pickSkin":
      $("#skinPick").click();
      return;
    case "saveProfile": {
      const draft = captureCharacterFields();
      draft.nickname = validatePlayerName(draft.nickname);
      if (
        $("#useFabricSkin")?.checked && active() &&
        state.canUseFabricSkin
      ) {
        return modal(
          "Установить Fabric для скина?",
          "<p>Для скина в сборке «" +
            esc(active().name) +
            "» нужен Fabric. Лаунчер установит его на ту же версию Minecraft, в папку твоих миров. Можно сохранить параметры в профиле без изменения сборки.</p>" +
            '<div class="actions">' +
            btn("saveWithFabric", "Установить и сохранить", "check", "main") +
            btn("saveProfileOnly", "Сохранить в профиле", "user") +
            "</div>",
        );
      }
      return saveAppearance();
    }
    case "saveProfileOnly":
      closeDialog();
      return saveAppearance();
    case "saveWithFabric":
      closeDialog();
      return saveAppearance("fabric");
    case "saveSettings":
      try {
        await saveSettings();
      } catch (error) {
        return notice("Не удалось сохранить настройки. " + error.message, true);
      }
      await refresh();
      return notice("Успешно сохранено");
    case "signin":
      await saveSettings();
      return runJob("signin");
    case "logout":
      await act("logout");
      await refresh();
      return notice("Сессия завершена.");
  }
}
async function saveAppearance(destination = "profile") {
  if (profileJob || profileSubmission) return;
  const draft = captureCharacterFields();
  draft.nickname = validatePlayerName(draft.nickname);
  const query = new URLSearchParams({
    nickname: draft.nickname,
    realName: draft.realName,
    model: draft.model,
    fabric: destination === "fabric" ? "1" : "0",
    skinDestination: destination,
  });
  if (destination === "fabric") query.set("id", state.active);
  profileSubmission = { ...draft, revisions: { ...draft.revisions } };
  updatePlayButtons();
  try {
    const j = await api("/api/profile?" + query, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: profileSubmission.file,
    });
    acceptedJobGeneration++;
    profileJob = lastJob = j.job;
    notice("Сохраняем параметры…", false, true);
    await tick();
  } catch (error) {
    profileSubmission = null;
    throw error;
  } finally {
    updatePlayButtons();
  }
}
document.addEventListener("input", (e) => {
  if (e.target.id === "nick") editCharacter("nickname", e.target.value);
  if (e.target.id === "realName") editCharacter("realName", e.target.value);
});
document.addEventListener("change", (e) => {
  const form = e.target.closest("#newForm");
  if (form && ["version", "loader", "allBuildVersions"].includes(e.target.id)) {
    if (e.target.id === "allBuildVersions") renderBuildVersions(form);
    if (e.target.id === "loader") updateBuildLoaderUI(form);
    else loadBuildLoaders(form);
    return;
  }
  if (e.target.id === "skinModel") {
    editCharacter("model", e.target.value);
    if (state.skin || characterDraft().file) preview();
  }
});
const saveSettings = () =>
  act("settings", {
    mode: $("#mode").value,
    clientId: $("#clientId").value,
    memory: $("#ram").value || "auto",
  });
document.addEventListener("change", async (e) => {
  if (e.target.id === "mode") return updateModeHelp();
  if (e.target.id !== "homeBuild" || selectingBuild) return;
  const selection = e.target;
  selectingBuild = true;
  selection.disabled = true;
  updatePlayButtons();
  try {
    await act("select", { id: selection.value });
    await refresh();
    notice("Выбрана сборка «" + active().name + "»");
  } catch (error) {
    selection.value = state.active;
    notice(error.message, true);
  } finally {
    selectingBuild = false;
    selection.disabled = false;
    updatePlayButtons();
  }
});
document.addEventListener("click", async (e) => {
  const el = e.target.closest("button");
  if (!el) return;
  try {
    if (el.dataset.settingsSection) return await navigateFresh("settings", el.dataset.settingsSection);
    if (el.dataset.view) { if ($("#dialog").open) closeDialog(); return await navigateFresh(el.dataset.view); }
    if (el.dataset.kind) {
      kind = el.dataset.kind;
      return await navigateFresh("addons");
    }
    if (el.dataset.favoriteBuild) {
      const id = el.dataset.favoriteBuild;
      const build = state.profiles.find((p) => p.id === id);
      if (!build || favoriteRequests.has(id)) return;
      favoriteRequests.add(id);
      el.disabled = true;
      el.setAttribute("aria-busy", "true");
      try {
        const result = await act("experienceFavorite", { id, favorite: !Boolean(build.favorite) });
        const current = state.profiles.find((p) => p.id === id);
        if (current) current.favorite = result.favorite ? 1 : 0;
        if (view === "builds") render();
        notice(result.message || (result.favorite ? "Сборка добавлена в избранное." : "Сборка убрана из избранного."));
      } finally {
        favoriteRequests.delete(id);
        const button = document.querySelector('[data-favorite-build="' + CSS.escape(id) + '"]');
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
        if (view === "builds" && (document.activeElement === document.body || document.activeElement === el)) {
          if (button && !button.closest("[hidden]")) button.focus();
          else $("#buildSearch")?.focus();
        }
      }
      return;
    }
    if (el.dataset.pick || el.dataset.buildSettings) {
      if (selectingBuild) return;
      selectingBuild = true;
      try {
        const id = el.dataset.pick || el.dataset.buildSettings;
        if (id !== state.active) {
          await act("select", { id });
          await refresh();
        }
        if (el.dataset.buildSettings) {
          kind = "mods";
          return navigate("addons");
        }
        document.querySelector('[data-pick="' + CSS.escape(id) + '"]')?.focus();
        return notice("Выбрана сборка «" + active().name + "»");
      } finally {
        selectingBuild = false;
        updatePlayButtons();
      }
    }
    if (el.dataset.exportBuild) {
      const build = state.profiles.find((p) => p.id === el.dataset.exportBuild);
      if (!build) return;
      return modal(
        "Выгрузить сборку «" + build.name + "»",
        "<p>В ZIP войдут версия Minecraft, загрузчик, моды, шейдеры, ресурс-паки и настройки модов.</p>" +
          '<label><input type="checkbox" id="archiveWorlds"> Включить сохранённые миры</label>' +
          '<div class="dialog-actions">' +
          btn("close", "Отмена", "") +
          '<button type="button" class="btn main" data-save-archive="' +
          esc(build.id) +
          '">' +
          ic("download") +
          "Скачать ZIP</button></div>",
      );
    }
    if (el.dataset.saveArchive) {
      el.disabled = true;
      try {
        notice("Готовим ZIP сборки…", false, true);
        const result = await api("/api/build-export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: el.dataset.saveArchive,
            worlds: $("#archiveWorlds").checked,
          }),
        });
        const link = document.createElement("a");
        link.href = result.download;
        link.download = result.name;
        document.body.append(link);
        link.click();
        link.remove();
        if (el.isConnected && $("#dialog").open) closeDialog();
        notice("ZIP готов. Скачивание началось.");
      } finally {
        el.disabled = false;
      }
      return;
    }
    if (el.dataset.removeBuild) {
      const build = state.profiles.find((p) => p.id === el.dataset.removeBuild);
      if (!build) return;
      modal(
        "Удалить сборку «" + build.name + "»?",
        "<p>Сборка вместе с мирами и дополнениями переместится в папку удалённых сборок. Её можно вернуть через раздел «Удалённые сборки».</p>" +
          '<div class="dialog-actions">' +
          btn("close", "Отмена", "") +
          '<button type="button" class="btn remove-confirm" data-confirm-build="' +
          esc(build.id) +
          '">' +
          ic("trash") +
          "Удалить сборку</button></div>",
      );
      return;
    }
    if (el.dataset.purgeBuild) {
      const build = state.deletedProfiles.find(
        (p) => p.id === el.dataset.purgeBuild,
      );
      if (!build) return;
      modal(
        "Навсегда удалить сборку «" + build.name + "»?",
        "<p>Все миры, моды, шейдеры, ресурс-паки и настройки этой сборки будут удалены с диска. Восстановление через лаунчер станет невозможным.</p>" +
          '<div class="dialog-actions">' +
          btn("close", "Отмена", "") +
          '<button type="button" class="btn remove-confirm" data-confirm-purge="' +
          esc(build.id) +
          '">' +
          ic("trash") +
          "Удалить навсегда</button></div>",
      );
      return;
    }
    if (el.dataset.confirmPurge) {
      el.disabled = true;
      try {
        await runJob("purgeBuild", { id: el.dataset.confirmPurge });
      } finally {
        el.disabled = false;
      }
      return;
    }
    if (el.dataset.confirmBuild || el.dataset.restoreBuild) {
      const restore = Boolean(el.dataset.restoreBuild);
      const id = el.dataset.restoreBuild || el.dataset.confirmBuild;
      const build = (restore ? state.deletedProfiles : state.profiles).find(
        (p) => p.id === id,
      );
      if (!build) return;
      el.disabled = true;
      try {
        await act(restore ? "restoreBuild" : "deleteBuild", { id });
        if (!restore) closeDialog();
        await refresh();
        notice(
          "Сборка «" +
            build.name +
            (restore ? "» восстановлена." : "» удалена."),
        );
        if (restore)
          document
            .querySelector('[data-pick="' + CSS.escape(id) + '"]')
            ?.focus();
        else
          document.querySelector('.build-pick, [data-act="create"]')?.focus();
      } finally {
        el.disabled = false;
      }
      return;
    }
    if (el.dataset.act) {
      el.disabled = true;
      try {
        await dispatch(el.dataset.act);
      } finally {
        el.disabled = false;
        if (el.dataset.act === "play" || el.dataset.act === "enableSteam")
          updatePlayButtons();
      }
    }
  } catch (err) {
    notice(err.message, true);
    if ($("#dialog").open) {
      let node = $("#dialogError");
      if (!node) {
        node = document.createElement("p");
        node.id = "dialogError";
        $("#dialogBody").append(node);
      }
      node.textContent = err.message;
    }
  }
});
$("#dialogClose").addEventListener("click", closeDialog);
$("#dialog").addEventListener("close", moveNotice);
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "newForm") return;
  e.preventDefault();
  const form = e.target,
    submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  const data = newBuildForms.get(form);
  const destination = view === "friends" ? "friends" : "addons";
  const draft = {
    name: form.querySelector("#buildName").value,
    version: form.querySelector("#version").value,
    loader: form.querySelector("#loader").value,
  };
  const isCurrent = () => form.isConnected && $("#dialog").open &&
    form.querySelector("#buildName").value === draft.name &&
    form.querySelector("#version").value === draft.version &&
    form.querySelector("#loader").value === draft.loader;
  data.submitting = true;
  updateBuildLoaderUI(form);
  try {
    const compatibility = await loadBuildLoaders(form);
    if (!isCurrent()) return;
    if (compatibility?.loaders.find(row => row.id === draft.loader)?.supported === false) return;
    const confirmation = await confirmSteamCompatibility(draft, {isCurrent});
    if (!confirmation) return;
    const result = await act("create", {...draft, allowWithoutSteam: confirmation.allowWithoutSteam});
    if (result.job) {
      pendingBuild = { id: result.job, form, destination };
      acceptedJobGeneration++;
      lastJob = result.job;
      notice("Создаём сборку…", false, true);
      await tick();
    } else {
      await refresh();
      finishBuild(form, destination);
      notice(confirmation.allowWithoutSteam ? "Сборка создана. Встроенное подключение через Steam недоступно." : "Сборка создана.");
    }
  } catch (err) {
    notice(err.message, true);
  } finally {
    data.submitting = false;
    updateBuildLoaderUI(form);
  }
});
$("#filePick").addEventListener("change", async (e) => {
  const files = [...e.target.files],
    t = uploadTarget;
  e.target.value = "";
  if (!files.length || !t) return;
  let count = 0,
    skipped = 0;
  const errors = [];
  for (const f of files) {
    try {
      if (t.kind === "mods") {
        notice("Проверяем " + f.name + "…", false, true);
        const decision = await confirmManualMod(f, t.id);
        if (decision === "installed") {
          count++;
          continue;
        }
        if (!decision) {
          skipped++;
          continue;
        }
      }
      notice("Импорт: " + f.name, false, true);
      await putFile(f, t.kind, t.id);
      count++;
    } catch (err) {
      errors.push(f.name + " — " + err.message);
    }
  }
  await refresh();
  notice(
    [
      "Добавлено файлов: " + count + ".",
      skipped ? "Пропущено: " + skipped + "." : "",
      ...errors,
    ]
      .filter(Boolean)
      .join(" "),
    !!errors.length,
  );
});
$("#buildZipPick").addEventListener("change", async (event) => {
  const picker = event.target;
  const file = picker.files[0];
  picker.value = "";
  if (!file || picker.disabled) return;
  if (!file.size || file.size > 8 * 1024 ** 3)
    return notice("Выбери ZIP сборки размером до 8 ГБ.", true);
  picker.disabled = true;
  try {
    notice("Загружаем и распаковываем сборку…", false, true);
    const build = await api("/api/build-import", {
      method: "POST",
      headers: { "Content-Type": "application/zip" },
      body: file,
    });
    await refresh();
    notice("Сборка «" + build.name + "» загружена.");
    document
      .querySelector('[data-pick="' + CSS.escape(build.id) + '"]')
      ?.focus();
  } catch (error) {
    notice(error.message, true);
  } finally {
    picker.disabled = false;
  }
});
$("#skinPick").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 1024 * 1024)
    return notice("Файл скина превышает 1 МБ.", true);
  const draft = captureCharacterFields();
  draft.file = file;
  render();
});
refresh()
  .then(() => {
    if (state.job.busy) {
      lastJob = state.job.id;
      notice(state.job.message, false, true);
    }
    else showGameExit(state.job);
    observeProgress(state.job);
    schedulePoll();
  })
  .catch((error) => {
    $("#view").innerHTML = head(
      "Не удалось открыть лаунчер",
      "",
      esc(error.message),
    );
    const retry = document.createElement("button");
    retry.className = "btn";
    retry.textContent = "Повторить";
    retry.onclick = () => location.reload();
    $("#view").append(retry);
    notice(error.message, true);
  });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) schedulePoll();
  else if (state) tick(true);
});
window.addEventListener("focus", () => {
  if (state && !document.hidden) tick(true);
});

window.addEventListener("load", () => { if (state) render(); });
