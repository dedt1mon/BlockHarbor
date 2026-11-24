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
    "Каталог дополнений",
    `
    <section class="catalog" data-catalog-view="browse" aria-label="Каталог дополнений">
      <p class="catalog-context"></p>
      <div class="catalog-output">
        <div class="catalog-browse">
          <form class="catalog-search" role="search">
            <input type="search" class="field" maxlength="120" aria-label="Поиск дополнений" autofocus>
            <button class="btn" type="submit">${ic("search")}Найти</button>
            <button type="button" class="btn" data-catalog-filters aria-expanded="false" aria-controls="catalogFilterPanel">Фильтры</button>
          </form>
          <div class="catalog-external-sources" aria-label="Поиск на других сайтах" hidden></div>
          <div class="catalog-pages"></div>
          <div class="catalog-pager" hidden>
            <button type="button" class="btn" data-page="previous">Назад</button>
            <span aria-live="polite"></span>
            <button type="button" class="btn" data-page="next">Далее</button>
          </div>
        </div>
        <section id="catalogFilterPanel" class="catalog-filter-panel" aria-label="Фильтры" hidden></section>
        <section class="catalog-detail" aria-label="Версии проекта" hidden></section>
        <section class="catalog-cart catalog-detail" aria-label="Выбранные дополнения" hidden></section>
      </div>
      <div class="catalog-cart-footer" hidden><button type="button" class="btn main" data-cart-open></button></div>
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
  let sourcesRequest = 0, sourcesTimer = 0, sourcesController = null;
  function renderSources(data) {
    const host = find(".catalog-external-sources");
    const sources = (data.externalSources || []).filter(source => {
      try { const url = new URL(source.url); return url.protocol === "https:" && ["www.curseforge.com", "www.planetminecraft.com"].includes(url.hostname); }
      catch { return false; }
    });
    host.hidden = !sources.length;
    host.innerHTML = '<span>Поиск на сайтах:</span>' + sources.map(source =>
      `<a class="catalog-source-link" data-catalog-source="${esc(source.id)}" href="${esc(source.url)}" target="_blank" rel="noopener noreferrer" title="Открыть поиск на ${esc(source.title)} в браузере">${esc(source.title)}${ic("link")}</a>`).join("");
  }
  async function loadSources(query = input.value.trim()) {
    const current = ++sourcesRequest;
    sourcesController?.abort();
    const controller = new AbortController(); sourcesController = controller;
    try {
      const params = new URLSearchParams({id: profile.id, kind: category, q: query});
      const data = await api("/api/catalog/sources?" + params, {signal: controller.signal});
      if (catalog.isConnected && sourcesRequest === current) renderSources(data);
    } catch { /* External links never block the built-in catalog. */ }
  }
  input.addEventListener("input", () => {
    clearTimeout(sourcesTimer);
    sourcesTimer = setTimeout(() => loadSources(), 200);
  });
  whenDialogClosed(dialog, catalog, () => { clearTimeout(sourcesTimer); sourcesController?.abort(); });
  loadSources();
  const pages = find(".catalog-pages");
  const pager = find(".catalog-pager");
  const output = find(".catalog-output");
  const status = find("[role=status]");
  const retry = find("[data-catalog-retry]");
  const headerActions = dialog.querySelector("#dialogHeaderActions");
  const cart = new Map();
  const cartPanel = find(".catalog-cart");
  const cartFooter = find(".catalog-cart-footer");
  let cartRequest = null, checkedItems = null, checkedToken = "";
  find(".catalog-context").textContent = [
    profile.name,
    loaders[profile.loader],
    profile.version,
    categoryLabel,
  ].join("  ·  ");
  find(".catalog-context").title = find(".catalog-context").textContent;
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

  function setCatalogView(view, actions = "") {
    if (!catalog.isConnected) return;
    catalog.dataset.catalogView = view;
    dialog.classList.toggle("dialog-content-sized", view === "detail");
    headerActions.innerHTML = actions;
    headerActions.hidden = !actions;
  }

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
  whenDialogClosed(dialog, catalog, () => {
    cancel();
    cartRequest?.abort();
    dialog.removeEventListener("click", onCatalogClick);
  });

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
      const data = await api("/api/catalog/" + endpoint + "?" + query, {
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
    // Several sources may report the same canonical project. Preserve the first
    // trusted result and its source; a card must never appear twice on one page.
    const projectIds = new Set();
    data = {...data, hits: data.hits.filter(item => !projectIds.has(item.id) && projectIds.add(item.id))};
    const list = document.createElement("ul");
    list.className = "catalog-results";
    list.setAttribute("aria-label", categoryLabel);
    list.innerHTML =
      data.hits
        .map(
          (item) => `
      <li class="catalog-choice" data-installed="${Boolean(item.installed)}"><button type="button" class="btn catalog-mark" data-mark="${esc(item.id)}" aria-label="${item.installed ? "Уже установлено: " : "Выбрать "}${esc(item.title)}" aria-pressed="false" ${item.installed ? 'disabled title="Уже установлено в этой сборке"' : ""}>${ic(item.installed ? "check" : "plus")}</button><button type="button" class="catalog-result" data-project="${esc(item.id)}">
        ${icon(item)}
        <span class="catalog-copy"><strong>${esc(item.title)}</strong>
          <span class="catalog-description">${esc(item.description)}</span>
          <span class="catalog-installed" ${item.installed ? "" : "hidden"}>${ic("check")}Установлено</span>
          <small>${[esc(item.author), esc(item.sourceTitle || ""), item.downloads ? "Скачиваний " + number.format(item.downloads) : ""].filter(Boolean).join(" · ")}</small>
        </span><span class="catalog-next" aria-hidden="true">${ic("arrow")}</span>
      </button></li>`,
        )
        .join("") ||
      (data.totalIsEstimate && data.offset + data.limit < data.total
        ? '<li class="empty">На этой странице нет дополнений с подтверждённой совместимостью. Проверь следующую страницу или измени фильтры.</li>'
        : '<li class="empty">Дополнений с подтверждённой совместимостью не найдено. Измени запрос или фильтры.</li>');
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
    pager.hidden = false;
    pager.querySelector("[data-page=previous]").disabled = cursor === 0;
    pager.querySelector("[data-page=next]").disabled = cursor + limit >= total;
    pager
      .querySelector("[data-page=previous]")
      .setAttribute("aria-disabled", String(cursor === 0));
    pager
      .querySelector("[data-page=next]")
      .setAttribute("aria-disabled", String(cursor + limit >= total));
    const estimated = page.data.totalIsEstimate && total > 0;
    const range = `Страница ${number.format(Math.floor(offset / limit) + 1)} из ${number.format(Math.max(1, Math.ceil(total / limit)))} · Найдено: ${estimated ? "≈" : ""}${number.format(total)}`;
    const count = pager.querySelector("span");
    count.title = estimated ? `Предварительное количество результатов. На этой странице подтверждено: ${number.format(hits.length)}.` : "";
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
    renderCartControls();
    if (entry.data.externalSources) renderSources(entry.data);
    if (entry.data.partial) status.textContent = entry.data.message;
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
    const key = JSON.stringify([query, offset, preset.filters]);
    if (history.has(key)) {
      showPage(history.get(key), navigationButton);
      return;
    }
    targetPage = { query, offset, preset };
    updatePager();
    const data = await request(
      "search",
      { q: query, offset, ...preset, source: "all" },
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
      chosen.renderers?.join(" / "),
      fmt(chosen.size),
      type,
      Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString("ru-RU"),
    ]
      .filter(Boolean)
      .join("  ·  ");
    const add = headerActions.querySelector("[data-cart-add]");
    add.disabled = Boolean(project.installed);
    add.innerHTML = project.installed ? ic("check") + "Установлено"
      : cart.get(project.id)?.version === chosen.id ? "Убрать из выбранного" : "Добавить в выбранное";
    compatibility.check(project, chosen);
  }
  async function versions(item, button) {
    cancel();
    updatePager();
    input.value = page.query;
    const data = await request(
      "versions",
      { project: item.id, source: item.source || "all" },
      "Загружаем совместимые версии…",
      () => versions(item, button),
    );
    if (!data) return;
    project = item;
    releases = data.versions;
    project.installed = Boolean(project.installed || releases.some(version => version.installed));
    project.installedVersion ||= releases.find(version => version.installed)?.id || "";
    if (project.installed) cart.delete(project.id);
    for (const entry of history.values()) {
      const hit = entry.data.hits.find(row => row.id === project.id);
      if (hit) Object.assign(hit, {installed: project.installed, installedVersion: project.installedVersion});
    }
    openedFrom = button;
    page.list
      .querySelector('[aria-current="true"]')
      ?.removeAttribute("aria-current");
    openedFrom.setAttribute("aria-current", "true");
    page.scroll = page.list.scrollTop;
    detail.innerHTML =
      `
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
      <p class="catalog-file"></p><div class="compatibility-report" aria-live="polite"></div>`
        : '<p class="empty">У проекта нет доступных файлов для этой версии игры и загрузчика.</p>');
    setCatalogView("detail", `<button type="button" class="btn" data-catalog-back>${ic("back")}К каталогу</button>` +
      (releases.length ? `<button type="button" class="btn" data-cart-add>Добавить в выбранное</button><button type="button" class="btn main" data-install-project disabled>Установить с зависимостями</button>` : ""));
    loadIcons(detail);
    const select = detail.querySelector("select");
    const preferred = selection.get(item.id) || item.installedVersion || item.recommendedVersion;
    if (select && releases.some((v) => v.id === preferred)) select.value = preferred;
    updateVersion();
    browse.hidden = true;
    detail.hidden = false;
    detail.scrollTop = 0;
    renderCartControls();
    (select || headerActions.querySelector("[data-catalog-back]")).focus({ preventScroll: true });
  }
  function renderCartControls() {
    cartFooter.hidden = !cart.size || !cartPanel.hidden;
    find("[data-cart-open]").textContent = "К установке (" + cart.size + ")";
    for (const button of pages.querySelectorAll("[data-mark]")) {
      const item = page?.data.hits.find(row => row.id === button.dataset.mark);
      const installed = Boolean(item?.installed);
      const selected = !installed && cart.has(button.dataset.mark);
      button.disabled = installed;
      button.title = installed ? "Уже установлено в этой сборке" : "";
      button.setAttribute("aria-label", (installed ? "Уже установлено: " : selected ? "Убрать из выбранного: " : "Выбрать ") + (item?.title || "дополнение"));
      button.setAttribute("aria-pressed", String(selected));
      button.innerHTML = ic(installed || selected ? "check" : "plus");
      const row = button.closest(".catalog-choice");
      row.dataset.installed = String(installed);
      row.querySelector(".catalog-installed").hidden = !installed;
    }
  }
  function selectItem(item, version = null) {
    if (item.installed) return;
    if (!cart.has(item.id) && cart.size >= 32) {
      notice("За один раз можно выбрать до 32 дополнений.", true);
      return;
    }
    const chosenSource = item.source || "all";
    cart.set(item.id, {project: item.id, kind: category, version: version || item.recommendedVersion || null, source: chosenSource, title: item.title});
  }
  function leaveCart() {
    cartRequest?.abort();
    cartRequest = null;
    cartPanel.hidden = true;
    browse.hidden = false;
    setCatalogView("browse");
    renderCartControls();
    (cart.size ? find("[data-cart-open]") : input).focus({preventScroll: true});
  }
  async function previewCart() {
    cancel();
    clearStatus();
    compatibility.cancel();
    cartRequest?.abort();
    if (!cart.size) { leaveCart(); return; }
    const current = new AbortController();
    cartRequest = current;
    checkedItems = null;
    checkedToken = "";
    browse.hidden = true;
    detail.hidden = true;
    find("#catalogFilterPanel").hidden = true;
    find("[data-catalog-filters]").setAttribute("aria-expanded", "false");
    cartPanel.hidden = false;
    setCatalogView("cart");
    renderCartControls();
    const items = [...cart.values()].map(({title, ...item}) => item);
    cartPanel.innerHTML = `<button type="button" class="linkbtn" data-cart-back>${ic("back")} К результатам</button>
      <h3>К установке (${items.length})</h3>
      <ul class="catalog-cart-items">${[...cart.values()].map(item => `<li><span>${esc(item.title)}</span><button type="button" class="linkbtn" data-cart-remove="${esc(item.project)}" aria-label="Убрать ${esc(item.title)}">Убрать</button></li>`).join("")}</ul>
      <div class="compatibility-report" role="status">Проверяем выбранные выпуски и общие зависимости…</div>
      <div class="dialog-actions"><button type="button" class="btn main" data-cart-install disabled>Установить выбранное</button></div>`;
    const area = cartPanel.querySelector(".compatibility-report");
    const install = cartPanel.querySelector("[data-cart-install]");
    cartPanel.querySelector("[data-cart-back]").focus({preventScroll:true});
    const active = () => current === cartRequest && dialog.open && area.isConnected;
    try {
      const result = await api("/api/catalog/check", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:profile.id, items}), signal:current.signal});
      if (!active()) return;
      for (const row of result.selected || []) {
        const item = [...cartPanel.querySelectorAll("[data-cart-remove]")].find(el => el.dataset.cartRemove === row.project);
        if (item) {
          const label = item.previousElementSibling;
          label.textContent = [cart.get(row.project).title, row.version, row.installed ? "уже в сборке" : ""].filter(Boolean).join(" · ");
          if (row.error) {
            const error = document.createElement("span");
            error.className = "catalog-selection-error";
            error.textContent = row.error;
            label.append(error);
          }
        }
      }
      area.innerHTML = compatibilitySummary(result);
      const available = Array.isArray(result.items) ? result.items : [];
      const normalized = new Map(available.map(item => [item.project, item]));
      // Keep every selected object observable in the job, including a failed
      // preview. The installer rechecks each independent group safely.
      checkedItems = items.map(item => ({...item, ...(normalized.get(item.project) || {})}));
      const partial = available.length < items.length;
      checkedToken = partial ? "" : result.reviewToken || "";
      install.disabled = result.blocked || !available.length;
      install.textContent = result.blocked ? "Установка недоступна" : checkedToken ? "Установить с предупреждением" : partial ? "Установить доступные (" + available.length + ")" : "Установить выбранное";
    } catch (error) {
      if (!active()) return;
      area.innerHTML = `<p class="compat-error">${esc(error.message)}</p><button class="linkbtn" type="button" data-cart-retry>Повторить проверку</button>`;
    }
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
  async function onCatalogClick(e) {
    if (!catalog.isConnected) return;
    const el = e.target.closest("button");
    if (!el || (!catalog.contains(el) && !headerActions.contains(el)) || el.disabled || el.getAttribute("aria-disabled") === "true")
      return;
    if (el.dataset.mark) {
      const item = page?.data.hits.find(hit => hit.id === el.dataset.mark);
      if (!item) return;
      if (cart.has(item.id)) cart.delete(item.id);
      else selectItem(item);
      renderCartControls();
      return;
    }
    if (el.hasAttribute("data-cart-add")) {
      if (project.installed) return;
      const version = detail.querySelector("select").value;
      if (cart.get(project.id)?.version === version) cart.delete(project.id);
      else selectItem(project, version);
      el.textContent = cart.has(project.id) ? "Убрать из выбранного" : "Добавить в выбранное";
      renderCartControls();
      return;
    }
    if (el.hasAttribute("data-cart-open") || el.hasAttribute("data-cart-retry") || el.dataset.cartRemove) {
      if (el.dataset.cartRemove) cart.delete(el.dataset.cartRemove);
      await previewCart();
      return;
    }
    if (el.hasAttribute("data-cart-back")) { leaveCart(); return; }
    if (el.hasAttribute("data-cart-install") && checkedItems) {
      el.disabled = true;
      try {
        window.BatchUI?.rememberTitles([...cart.values()]);
        await runJob("installModrinth", {id:profile.id, items:checkedItems, acknowledgement:checkedToken});
      } catch (error) { el.disabled = false; notice(error.message, true); }
      return;
    }
    if (el.hasAttribute("data-catalog-back")) {
      compatibility.cancel();
      detail.hidden = true;
      browse.hidden = false;
      setCatalogView("browse");
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
      if (project.installed) return;
      el.disabled = true;
      try {
        window.BatchUI?.rememberTitles([{project: project.id, title: project.title}]);
        await runJob("installModrinth", {
          id: profile.id,
          kind: category,
          project: project.id,
          version: detail.querySelector("#catalogVersion").value,
          source: project.source || "all",
          acknowledgement: el.dataset.acknowledgement || "",
        });
      } catch (error) {
        notice(error.message, true);
        updateVersion();
      }
    }
  }
  dialog.addEventListener("click", onCatalogClick);
  if (category === "mods" && profile.loader === "vanilla") {
    find("form").hidden = true;
    pages.textContent =
      "Для установки модов выбери сборку с загрузчиком, например Fabric или Forge. Ресурс-паки можно установить и в Vanilla.";
    $("#dialogClose").focus();
  } else search();
}
