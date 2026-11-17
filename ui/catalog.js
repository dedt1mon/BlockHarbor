"use strict";

function openModrinth(profile, category) {
  const dialog = $("#dialog");
  const [categoryLabel, categoryIcon] = kinds[category];
  const itemLabel = {
    mods: "Мод",
    shaderpacks: "Шейдер",
    resourcepacks: "Ресурс-пак",
  }[category];
  modal(
    "Каталог Modrinth",
    `
    <section class="catalog" aria-label="Каталог дополнений">
      <p class="catalog-context"></p>
      <div class="catalog-output">
        <div class="catalog-browse">
          <form class="catalog-search" role="search">
            <input type="search" class="field" maxlength="120" aria-label="Поиск на Modrinth" autofocus>
            <button class="btn" type="submit">${ic("search")}Найти</button>
          </form>
          <div class="catalog-toolbar">
            <button type="button" class="btn" data-catalog-filters aria-expanded="false" aria-controls="catalogFilterPanel">Фильтры</button>
            <select class="field" aria-label="Сортировка результатов"><option value="relevance">По релевантности</option></select>
          </div>
          <div class="catalog-pages"></div>
          <div class="catalog-pager" hidden>
            <button type="button" class="btn" data-page="previous">Назад</button>
            <span aria-live="polite"></span>
            <button type="button" class="btn" data-page="next">Далее</button>
          </div>
        </div>
        <section id="catalogFilterPanel" class="catalog-filter-panel" aria-label="Фильтры" hidden></section>
        <section class="catalog-detail" aria-label="Версии проекта" hidden></section>
      </div>
      <div class="catalog-feedback"><span role="status"></span>
        <button type="button" class="linkbtn" data-catalog-retry hidden>Повторить</button>
      </div>
    </section>`,
  );
  const catalog = dialog.querySelector(".catalog");
  const find = (selector) => catalog.querySelector(selector);
  const browse = find(".catalog-browse");
  const detail = find(".catalog-detail");
  const compatibility = releaseCompatibility(dialog, detail, profile, category);
  const input = find("input");
  const pages = find(".catalog-pages");
  const pager = find(".catalog-pager");
  const output = find(".catalog-output");
  const status = find("[role=status]");
  const retry = find("[data-catalog-retry]");
  find(".catalog-context").textContent = [
    profile.name,
    loaders[profile.loader],
    profile.version,
    categoryLabel,
  ].join("  ·  ");
  input.placeholder = {
    mods: "Найти мод",
    shaderpacks: "Найти шейдер",
    resourcepacks: "Найти ресурс-пак",
  }[category];
  input.focus();
  const number = new Intl.NumberFormat("ru-RU");
  const history = new Map();
  const selection = new Map();
  let page = null,
    targetPage = null,
    project = null;
  let releases = [],
    controller = null,
    retryAction = null;
  let debounce, busyTimer, openedFrom;
  const filters = setupCatalogFilters(
    catalog,
    profile,
    category,
    () => {
      cancel();
      clearStatus();
      updatePager();
    },
    () => search(),
  );

  function clearStatus() {
    status.classList.remove("catalog-error");
    status.textContent = "";
    retry.hidden = true;
    retryAction = null;
  }
  function cancel() {
    clearTimeout(debounce);
    clearTimeout(busyTimer);
    controller?.abort();
    controller = null;
    targetPage = null;
    output.removeAttribute("aria-busy");
  }
  dialog.addEventListener("close", cancel, { once: true });

  async function request(endpoint, params, message, repeat) {
    const current = new AbortController();
    controller = current;
    output.setAttribute("aria-busy", "true");
    clearStatus();
    // Cached/fast responses should never flash a temporary loading message.
    if (!page) status.textContent = message;
    else
      busyTimer = setTimeout(() => {
        status.textContent = message;
      }, 250);
    const active = () =>
      controller === current && dialog.open && catalog.isConnected;
    try {
      const query = new URLSearchParams({
        id: profile.id,
        kind: category,
        ...params,
      });
      const data = await api("/api/modrinth/" + endpoint + "?" + query, {
        signal: current.signal,
      });
      if (active()) {
        clearStatus();
        return data;
      }
    } catch (error) {
      if (active()) {
        status.classList.add("catalog-error");
        status.textContent = error.message;
        retryAction = repeat;
        retry.hidden = false;
      }
    } finally {
      if (controller === current) {
        clearTimeout(busyTimer);
        controller = null;
        targetPage = null;
        output.removeAttribute("aria-busy");
        updatePager();
      }
    }
  }

  function icon(item) {
    return `<span class="catalog-icon" role="img" aria-label="${itemLabel}" data-image="${esc(item.icon || "")}">${ic(categoryIcon)}</span>`;
  }
  function loadIcons(root) {
    for (const slot of root.querySelectorAll(".catalog-icon")) {
      const src = slot.dataset.image;
      if (!src.startsWith("https://cdn.modrinth.com/")) continue;
      const img = new Image();
      img.alt = "";
      img.width = 48;
      img.height = 48;
      img.decoding = "async";
      img.crossOrigin = "anonymous";
      img.referrerPolicy = "no-referrer";
      img.addEventListener(
        "load",
        () => {
          slot.dataset.loaded = "";
        },
        { once: true },
      );
      img.addEventListener(
        "error",
        () => {
          img.remove();
        },
        { once: true },
      );
      slot.append(img);
      img.src = src;
    }
  }
  function makePage(query, data, preset) {
    const list = document.createElement("ul");
    list.className = "catalog-results";
    list.setAttribute("aria-label", categoryLabel);
    list.innerHTML =
      data.hits
        .map(
          (item) => `
      <li><button type="button" class="catalog-result" data-project="${esc(item.id)}">
        ${icon(item)}
        <span class="catalog-copy"><strong>${esc(item.title)}</strong>
          <span class="catalog-description">${esc(item.description)}</span>
          <small>${[esc(item.author), "Скачиваний " + number.format(item.downloads)].filter(Boolean).join(" · ")}</small>
        </span><span class="catalog-next" aria-hidden="true">${ic("arrow")}</span>
      </button></li>`,
        )
        .join("") ||
      '<li class="empty">Совместимых дополнений не найдено. Измени запрос или фильтры.</li>';
    loadIcons(list);
    return { query, data, preset, list, scroll: 0 };
  }
  function updatePager() {
    if (!page) return;
    const { offset, limit, hits, total } = page.data;
    const cursor =
      targetPage?.query === page.query && targetPage.preset === page.preset
        ? targetPage.offset
        : offset;
    pager.hidden = total <= limit && !offset;
    pager
      .querySelector("[data-page=previous]")
      .setAttribute("aria-disabled", String(cursor === 0));
    pager
      .querySelector("[data-page=next]")
      .setAttribute("aria-disabled", String(cursor + limit >= total));
    const range = hits.length
      ? `${number.format(offset + 1)}–${number.format(offset + hits.length)} из ${number.format(total)}`
      : "";
    const count = pager.querySelector("span");
    if (count.textContent !== range) count.textContent = range;
  }
  function showPage(entry, navigationButton) {
    const focusWasInList =
      page !== entry && page?.list.contains(document.activeElement);
    if (page !== entry) {
      if (page) page.scroll = page.list.scrollTop;
      page = entry;
      pages.replaceChildren(page.list);
      page.list.scrollTop = page.scroll;
    }
    updatePager();
    if (focusWasInList) {
      (navigationButton && !pager.hidden ? navigationButton : input).focus({
        preventScroll: true,
      });
    }
  }
  async function search(
    query = input.value.trim(),
    offset = 0,
    navigationButton = null,
    preset = filters.read(),
  ) {
    cancel();
    clearStatus();
    const key = JSON.stringify([query, offset, preset.sort, preset.filters]);
    if (history.has(key)) {
      showPage(history.get(key), navigationButton);
      return;
    }
    targetPage = { query, offset, preset };
    updatePager();
    const data = await request(
      "search",
      { q: query, offset, ...preset },
      "Ищем совместимые дополнения…",
      () => search(query, offset, navigationButton, preset),
    );
    if (!data) return;
    const entry = makePage(query, data, preset);
    // Keep previously visited lists (including decoded icons and scroll position) in this dialog.
    if (history.size >= 20) history.delete(history.keys().next().value);
    history.set(key, entry);
    showPage(entry, navigationButton);
  }

  function updateVersion() {
    const chosen = releases.find(
      (v) => v.id === detail.querySelector("select")?.value,
    );
    if (!chosen) return;
    selection.set(project.id, chosen.id);
    const date = new Date(chosen.published);
    const type = { release: "Релиз", beta: "Бета", alpha: "Альфа" }[
      chosen.type
    ];
    find(".catalog-file").textContent = [
      chosen.filename,
      fmt(chosen.size),
      type,
      Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString("ru-RU"),
    ]
      .filter(Boolean)
      .join("  ·  ");
    compatibility.check(project, chosen);
  }
  async function versions(item, button) {
    cancel();
    updatePager();
    input.value = page.query;
    const data = await request(
      "versions",
      { project: item.id },
      "Загружаем совместимые версии…",
      () => versions(item, button),
    );
    if (!data) return;
    project = item;
    releases = data.versions;
    openedFrom = button;
    page.list
      .querySelector('[aria-current="true"]')
      ?.removeAttribute("aria-current");
    openedFrom.setAttribute("aria-current", "true");
    page.scroll = page.list.scrollTop;
    detail.innerHTML =
      `
      <button type="button" class="linkbtn" data-catalog-back>${ic("back")} К результатам</button>
      <div class="catalog-project">${icon(item)}
        <div><h3 class="catalog-title">${esc(item.title)}</h3>
          <p class="catalog-description">${esc(item.description)}</p></div>
      </div>` +
      (releases.length
        ? `
      <label>Совместимая версия<select class="field" id="catalogVersion">${releases
        .map(
          (v) =>
            `<option value="${esc(v.id)}">${esc(v.name || v.number)}${v.installed ? " · установлено" : ""}</option>`,
        )
        .join("")}</select></label>
      <p class="catalog-file"></p><div class="compatibility-report" aria-live="polite"></div>
      <div class="dialog-actions"><button type="button" class="btn main" data-install-project>Установить в сборку</button></div>`
        : '<p class="empty">У проекта нет доступных файлов для этой версии игры и загрузчика.</p>');
    loadIcons(detail);
    const select = detail.querySelector("select");
    if (select && releases.some((v) => v.id === selection.get(item.id)))
      select.value = selection.get(item.id);
    updateVersion();
    browse.hidden = true;
    detail.hidden = false;
    detail.scrollTop = 0;
    (select || find("[data-catalog-back]")).focus({ preventScroll: true });
  }
  find("form").addEventListener("submit", (e) => {
    e.preventDefault();
    search();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    closeDialog();
  });
  input.addEventListener("input", () => {
    cancel();
    clearStatus();
    updatePager();
    debounce = setTimeout(() => search(), 280);
  });
  detail.addEventListener("change", (e) => {
    if (e.target.id === "catalogVersion") updateVersion();
  });
  catalog.addEventListener("click", async (e) => {
    const el = e.target.closest("button");
    if (!el || el.disabled || el.getAttribute("aria-disabled") === "true")
      return;
    if (el.hasAttribute("data-catalog-back")) {
      compatibility.cancel();
      detail.hidden = true;
      browse.hidden = false;
      page.list.scrollTop = page.scroll;
      openedFrom.focus({ preventScroll: true });
    } else if (el.dataset.project) {
      filters.restore(page.preset);
      const item = page.data.hits.find((hit) => hit.id === el.dataset.project);
      if (item) await versions(item, el);
    } else if (el.dataset.page) {
      filters.restore(page.preset);
      const cursor =
        targetPage?.query === page.query && targetPage.preset === page.preset
          ? targetPage.offset
          : page.data.offset;
      input.value = page.query;
      const step =
        el.dataset.page === "next" ? page.data.limit : -page.data.limit;
      await search(page.query, Math.max(0, cursor + step), el);
    } else if (el.hasAttribute("data-catalog-retry") && retryAction) {
      const repeat = retryAction;
      input.focus({ preventScroll: true });
      await repeat();
    } else if (el.hasAttribute("data-install-project")) {
      el.disabled = true;
      try {
        await runJob("installModrinth", {
          id: profile.id,
          kind: category,
          project: project.id,
          version: detail.querySelector("#catalogVersion").value,
          acknowledgement: el.dataset.acknowledgement || "",
        });
      } catch (error) {
        notice(error.message, true);
        updateVersion();
      }
    }
  });
  if (category === "mods" && profile.loader === "vanilla") {
    find(".catalog-toolbar").hidden = true;
    find("form").hidden = true;
    pages.textContent =
      "Для установки модов выбери сборку с загрузчиком, например Fabric или Forge. Ресурс-паки можно установить и в Vanilla.";
    $("#dialogClose").focus();
  } else search();
}
