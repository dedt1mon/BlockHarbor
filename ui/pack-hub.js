"use strict";

// The catalog uses the existing modal host; filters, results and chosen releases
// stay independent of the build list and background refreshes.
(() => {
  const catalog = {q: "", version: "", loader: "", category: "", sort: "relevance", offset: 0, nextOffset: 0, page: null, error: "", busy: false, needsSearch: true};
  const categories = {adventure: "Приключения", challenging: "Испытания", combat: "Сражения", "kitchen-sink": "Всё вместе", lightweight: "Лёгкие сборки", magic: "Магия", multiplayer: "Для друзей", optimization: "Оптимизация", quests: "Квесты", technology: "Технологии"};
  const community = {page: null, fetchedAt: 0, attemptedAt: 0, busy: false, error: "", needsSearch: true, controller: null};
  const publicationCache = new Map();
  const publicationRequests = new Map();
  const publicationEpochs = new Map();
  const communityDownloads = new Set();
  let publicationRequest = 0;
  let pendingPublication = null, selectedCommunity = null;
  const sharingEnabled = () => typeof state !== "undefined" && state?.capabilities?.communitySharing === true;
  const exactCommunityId = () => sharingEnabled() && (/^BH-[0-9A-F]{12}$/i.test(catalog.q.trim())
    || /^https:\/\/blockharbor-community-catalog\.ded8timon\.chatgpt\.site\/v1\/builds\/BH-[0-9A-F]{12}(?:\/archive)?$/i.test(catalog.q.trim()));
  const choices = new Map();
  const releaseRows = new Map();
  const releaseInstalls = new Map();
  let searchGeneration = 0, selectedProject = null, prepared = null;
  let previewSteamAccepted = new Set();
  let searchController = null, releaseController = null, catalogScroll = 0, returnFocus = null, catalogOpen = false;
  const feature = (action, params = {}, options = {}) => api("/api/feature?" + new URLSearchParams({action, ...params}), options);
  const button = (attr, label, icon = "", cls = "") => `<button type="button" class="btn ${cls}" ${attr}>${icon ? ic(icon) : ""}${label}</button>`;
  const option = (value, label, selected) => `<option value="${esc(value)}"${selected === value ? " selected" : ""}>${esc(label)}</option>`;
  const empty = (message) => `<div class="empty">${esc(message)}</div>`;
  const number = new Intl.NumberFormat("ru-RU");

  function discardPrepared() {
    previewSteamAccepted = new Set();
    if (!prepared) return;
    const token = prepared.token;
    prepared = null;
    act("packDiscard", {token}).catch(() => {});
  }

  function stopSearch() {
    ++searchGeneration;
    searchController?.abort();
    searchController = null;
    catalog.busy = false;
    community.controller?.abort();
    community.controller = null;
    community.busy = false;
  }

  function packModal(title, content, page, options = {}) {
    if (!catalogOpen) {
      returnFocus = document.activeElement !== document.body ? document.activeElement : document.querySelector('[data-act="packCatalog"]');
      catalogOpen = true;
    }
    modal(title, `<div class="pack-dialog" data-pack-view="${page}">${content}</div>`, options);
  }

  function rememberCatalog() {
    const host = document.querySelector(".pack-hub");
    if (!host) return;
    catalog.q = host.querySelector('[name="q"]').value;
    catalogScroll = host.querySelector(".pack-results").scrollTop;
  }

  function openCatalog({focusProject = "", focusCommunity = ""} = {}) {
    discardPrepared();
    releaseController?.abort();
    releaseController = null;
    packModal("Каталог сборок", renderPacks(), "catalog");
    loadArt();
    const host = document.querySelector(".pack-hub");
    host.querySelector(".pack-results").scrollTop = catalogScroll;
    const previous = [...host.querySelectorAll("[data-pack-project], [data-pack-community]")].find(button =>
      button.dataset.packProject === focusProject || button.dataset.packCommunity === focusCommunity);
    (previous || host.querySelector('[name="q"]'))?.focus({preventScroll: true});
    if ((!catalog.page || catalog.needsSearch) && !catalog.busy) search(catalog.nextOffset);
    if (sharingEnabled() && !community.busy && (community.needsSearch || Date.now() - community.fetchedAt >= 15000)) searchCommunity(community.page?.offset || 0);
  }

  function results() {
    if (!catalog.page) return "";
    if (!catalog.page.hits.length) return empty("Модпаки не найдены. Попробуй другие фильтры.");
    return `<div class="pack-grid">${catalog.page.hits.map(item => `<article class="pack-card">
      <div class="pack-card-top"><span class="pack-art" data-pack-icon="${esc(item.icon)}">${ic("layers")}</span><div><h3>${esc(item.title)}</h3><span class="meta">${esc(item.author)}</span></div></div>
      <p>${esc(item.description)}</p><div class="pack-card-footer"><span class="meta">${number.format(item.downloads)} загрузок</span>${button(`data-pack-project="${esc(item.id)}"`, "Выбрать выпуск", "arrow")}</div>
    </article>`).join("")}</div>`;
  }

  function pager() {
    const page = catalog.page;
    if (!page || exactCommunityId()) return "";
    return `<div class="pack-pager" aria-label="Страницы модпаков">${button('data-pack-page="previous"' + (catalog.offset <= 0 || catalog.busy ? " disabled" : ""), "Назад", "back")}<span class="meta">Модпаки · ${page.total ? `${page.offset + 1}–${Math.min(page.total, page.offset + page.limit)} из ${number.format(page.total)}` : "0 результатов"}</span>${button('data-pack-page="next"' + (catalog.offset + page.limit >= page.total || catalog.busy ? " disabled" : ""), "Далее", "arrow")}</div>`;
  }

  function communityResults() {
    if (!sharingEnabled()) return "";
    const page = community.page;
    let status = "";
    if (community.error) status = `<p class="pack-status pack-error" role="status">${esc(community.error)} ${button("data-pack-community-retry", "Повторить")}</p>`;
    else if (community.busy) status = '<p class="pack-status" role="status">Ищем сборки игроков…</p>';
    else if (page?.warning || ["github", "github-legacy"].includes(page?.source)) status = `<p class="pack-status pack-error" role="status">${esc(page.warning || "Основной каталог недоступен. В резервном каталоге нет новых часовых публикаций.")}</p>`;
    else if (page && !page.hits.length) status = '<p class="pack-status" role="status">Сборок игроков по этому запросу пока нет.</p>';
    else if (page?.source === "primary" && page.hits.some(item => !item.expiresAt))
      status = '<p class="pack-status" role="status">Основной сервер ещё не подтверждает часовое автоудаление публикаций.</p>';
    const cards = (page?.hits || []).map(item => `<article class="pack-card"><h4>${esc(item.name)}</h4>
      <p>Minecraft ${esc(item.minecraft)} · ${esc(loaders[item.loader] || item.loader)} · ${esc(categories[item.category] || item.category)}</p>
      <div class="pack-card-footer"><span class="meta">${esc(item.id)} · ${fmt(item.size)}</span>${button(`data-pack-community="${esc(item.id)}"${communityDownloads.has(item.id) ? ' disabled aria-busy="true"' : ""}`, "Скачать", "download")}</div></article>`).join("");
    return `<h3 class="pack-source-title">Сборки игроков BlockHarbor</h3>${status}<div class="pack-grid" aria-busy="${community.busy}">${cards}</div>`;
  }

  function communityPager() {
    if (!sharingEnabled()) return "";
    const page = community.page;
    if (!page || page.total <= page.limit) return "";
    return `<div class="pack-pager" aria-label="Страницы сборок игроков">${button('data-pack-community-page="prev"' + (community.busy || !page.offset ? " disabled" : ""), "Назад", "back")}
      <span class="meta">Игроки · ${page.offset + 1}–${Math.min(page.total, page.offset + page.limit)} из ${number.format(page.total)}</span>
      ${button('data-pack-community-page="next"' + (community.busy || page.offset + page.limit >= page.total ? " disabled" : ""), "Далее", "arrow")}</div>`;
  }

  function updateCommunityResults(host) {
    if (!sharingEnabled() || !host?.isConnected) return;
    host.innerHTML = communityResults();
    const pagination = host.closest(".pack-hub")?.querySelector(".community-page-host");
    if (pagination) pagination.innerHTML = communityPager();
  }

  async function searchCommunity(offset = 0, quiet = false) {
    if (!sharingEnabled()) return;
    community.controller?.abort();
    const controller = new AbortController();
    community.controller = controller;
    community.busy = true;
    community.attemptedAt = Date.now();
    community.needsSearch = true;
    community.error = "";
    const host = document.querySelector(".community-results");
    if (!quiet) updateCommunityResults(host);
    try {
      const page = await feature("communitySearch", {q: catalog.q, category: catalog.category, version: catalog.version, loader: catalog.loader, offset}, {signal: controller.signal});
      if (controller.signal.aborted || !sharingEnabled()) return;
      community.page = page;
      community.fetchedAt = Date.now();
      community.needsSearch = false;
      if (offset > 0 && page.total <= offset) {
        community.controller = null;
        return searchCommunity(0, quiet);
      }
    } catch (error) {
      if (!controller.signal.aborted) community.error = error.message;
    } finally {
      if (community.controller === controller) {
        community.controller = null;
        community.busy = false;
        updateCommunityResults(host);
      }
    }
  }

  function searchCatalog() {
    // Retain the previous cards while both sources refresh. Clearing their
    // heights first makes the second source jump underneath the pointer.
    community.needsSearch = true;
    catalog.needsSearch = true;
    updateResults();
    document.querySelector(".pack-results")?.scrollTo(0, 0);
    if (sharingEnabled()) searchCommunity();
    search();
  }

  function updateShareButtons(id) {
    if (!sharingEnabled()) return;
    const rows = (publicationCache.get(id)?.publications || []).filter(row => !row.expiresAt || row.expiresAt * 1000 > Date.now());
    const published = rows.some(row => !row.statusUnverified), uncertain = rows.length > 0 && !published;
    for (const control of document.querySelectorAll('[data-pack-share]')) {
      if (control.dataset.packShare !== id) continue;
      const name = state.profiles.find(p => p.id === id)?.name || "";
      control.classList.toggle("is-published", published);
      control.title = uncertain ? "Проверить публикацию" : published ? "Управление публикацией" : "Поделиться сборкой";
      const title = uncertain ? "Проверить публикацию" : published ? "Опубликовано" : "Поделиться";
      control.setAttribute("aria-label", title + ": " + name);
      const label = control.querySelector("span");
      if (label) label.textContent = title;
    }
  }

  function savePublications(id, data) {
    const publications = Array.isArray(data.publications) ? data.publications : [];
    publicationCache.set(id, {publications, fetched: Date.now()});
    updateShareButtons(id);
    return publications;
  }

  async function fetchPublications(id, force = false) {
    if (!sharingEnabled()) return [];
    const cached = publicationCache.get(id);
    if (!force && cached && Date.now() - cached.fetched < 15000)
      return cached.publications.filter(row => !row.expiresAt || row.expiresAt * 1000 > Date.now());
    if (publicationRequests.has(id)) return publicationRequests.get(id);
    const epoch = publicationEpochs.get(id) || 0;
    const request = feature("communityPublication", {id}).then(data => {
      if (!sharingEnabled()) return [];
      if ((publicationEpochs.get(id) || 0) !== epoch) return data.publications || [];
      return savePublications(id, data);
    }).finally(() => {
      if (publicationRequests.get(id) === request) publicationRequests.delete(id);
    });
    publicationRequests.set(id, request);
    return request;
  }

  function refreshOpenCommunity() {
    if (sharingEnabled() && !document.hidden && document.querySelector('.pack-dialog[data-pack-view="catalog"]') &&
        !community.busy && Date.now() - community.attemptedAt >= 15000)
      searchCommunity(community.page?.offset || 0, true);
  }

  function refreshShareButtons() {
    if (!sharingEnabled() || !state?.profiles?.length || !document.querySelector("[data-pack-share]")) return;
    const current = ++publicationRequest;
    const ids = state.profiles.map(p => p.id);
    for (const id of ids) updateShareButtons(id);
    // Query only uncached/expired profiles. Do not block the build list on network access.
    for (const id of ids) {
      fetchPublications(id).then(() => {
        if (current === publicationRequest) updateShareButtons(id);
      }).catch(() => {
        // Offline mode retains a previously known status instead of showing a false "Поделиться".
      });
    }
  }


  function publicationRows(list) {
    if (!list.length) return "";
    return `<div class="community-publications">${list.map(row => `<section class="community-receipt">
      ${list.length > 1 ? `<p class="community-receipt-name">${esc(row.name)}</p>` : ""}
      <label class="community-code-label">Код или ссылка на сборку</label>
      <p class="community-expiry">${row.expiresAt
        ? (row.statusUnverified ? "Последний подтверждённый срок: " : "Публикация доступна до ") + esc(new Date(row.expiresAt * 1000).toLocaleString("ru-RU", {dateStyle:"short", timeStyle:"short"})) + ", затем автоматически удалится из каталога."
        : "Сервер не подтвердил срок автоматического удаления. Когда публикация станет не нужна, удали её вручную."} Локальная сборка сохранится.</p>
      <div class="community-id"><input class="field" readonly aria-label="Код сборки ${esc(row.name)}" data-publication-id="${esc(row.build)}" value="${esc(row.build)}">
        ${button("data-pack-copy-id", "Скопировать ID", "copy")}</div>
      <div class="community-published-actions" role="group" aria-label="Действия с публикацией ${esc(row.name)}">
        ${row.shareUrl ? button(`data-pack-copy-link="${esc(row.shareUrl)}"`, "Скопировать ссылку", "copy") : ""}
        ${button(`data-pack-withdraw="${esc(row.build)}"`, "Удалить публикацию", "trash", "danger")}
      </div>
      <p class="note">Друг может вставить ID или ссылку в «Каталог сборок». Ссылка также скачивает ZIP в браузере, пока публикация доступна.</p>
    </section>`).join("")}</div>`;
  }

  async function readPublications(form) {
    if (!sharingEnabled()) return;
    const host = form.querySelector(".community-receipts");
    const revision = String(Number(host.dataset.revision || 0) + 1);
    host.dataset.revision = revision;
    try {
      const publications = await fetchPublications(form.dataset.packPublish, true);
      if (!sharingEnabled() || !form.isConnected || host.dataset.revision !== revision) return;
      host.innerHTML = publicationRows(publications);
      form.classList.remove("is-checking");
      form.classList.toggle("is-published", publications.length > 0);
      form.classList.toggle("is-unverified", publications.some(row => row.statusUnverified));
      syncPublicationSubmit(form);
      form.querySelector(".community-publication-status").textContent = publications.length
        ? publications.some(row => row.statusUnverified)
          ? "Статус: доступность публикации не подтверждена. Код и удаление доступны."
          : "Статус: опубликовано. Автоматическое удаление через 1 час после публикации."
        : "Статус: не опубликовано.";
      if (pendingPublication?.id === form.dataset.packPublish) {
        setPublicationBusy(form, true, pendingPublication.progress);
      }
    } catch (error) {
      if (!form.isConnected || host.dataset.revision !== revision) return;
      form.classList.add("is-checking");
      syncPublicationSubmit(form);
      form.querySelector(".community-publication-status").textContent = "Не удалось проверить статус публикации.";
      host.innerHTML = `<p class="pack-error" role="alert">${esc(error.message)}</p>
        <div class="community-retry-actions" role="group">
          ${button("data-pack-publication-retry", "Повторить проверку")}
        </div>`;
    }
  }

  function syncPublicationSubmit(form) {
    // Keep the action beside status while the publication is checked.
    const submit = form.elements.namedItem("communityPublishSubmit");
    if (!submit) return;
    submit.hidden = form.classList.contains("is-published");
    submit.disabled = submit.hidden || form.classList.contains("is-checking") || form.dataset.policyVerified !== "true" || form.getAttribute("aria-busy") === "true";
  }

  async function readPublicationCapabilities(form) {
    if (!sharingEnabled()) return;
    const status = form.querySelector(".community-policy-status");
    const retry = form.querySelector("[data-pack-policy-retry]");
    form.dataset.policyVerified = "false";
    syncPublicationSubmit(form);
    if (retry) retry.disabled = true;
    try {
      const policy = await feature("communityCapabilities");
      if (!sharingEnabled() || !form.isConnected || !status) return;
      status.textContent = policy?.message || "Не удалось подтвердить правила публикации на сервере.";
      status.dataset.mode = policy?.enabled === true ? "verified" : "unverified";
      form.dataset.policyVerified = String(policy?.enabled === true);
      syncPublicationSubmit(form);
      if (retry) retry.hidden = policy?.enabled === true;
    } catch (error) {
      if (!form.isConnected || !status) return;
      status.textContent = "Невозможно проверить сервер. Часовое удаление и антиспам не подтверждены.";
      status.dataset.mode = "unverified";
      if (retry) retry.hidden = false;
    } finally {
      if (retry?.isConnected) retry.disabled = false;
    }
  }

  function showPublish(id) {
    if (!sharingEnabled()) return;
    const profile = state.profiles.find(p => p.id === id);
    if (!profile) return;
    const previous = (publicationCache.get(id)?.publications || []).filter(row =>
      !row.expiresAt || row.expiresAt * 1000 > Date.now());
    const knownPublished = previous.length > 0;
    modal("Поделиться сборкой", `<form id="communityPublishForm" class="community-publish ${knownPublished ? "is-published" : "is-checking"}" data-pack-publish="${esc(id)}">
      <label>Название в каталоге<input class="field" name="name" value="${esc(profile.name)}" required maxlength="48"></label>
      <div class="field-group"><label>Версия Minecraft<input class="field" readonly value="${esc(profile.version)}"></label>
      <label>Загрузчик<input class="field" readonly value="${esc(loaders[profile.loader] || profile.loader)}"></label></div>
      <label>Категория<select class="field" name="category" aria-label="Категория" required><option value="">Выбери категорию</option>${Object.entries(categories).map(([value, label]) => option(value, label, "")).join("")}</select></label>
      <label class="community-config-toggle"><input type="checkbox" name="includeConfigs" aria-describedby="communityConfigHelp"><span>Включить настройки модов</span></label>
      <p class="note" id="communityConfigHelp" hidden>В архив войдут подходящие файлы из config и defaultconfigs. Перед публикацией проверь их содержимое на личные данные.</p>
      <p class="note community-publish-description">Максимальный размер ZIP — 256 МиБ. Новая публикация доступна 60 минут, одновременно разрешены 3 активные публикации. Для одной сборки разрешены 3 попытки подряд, затем — не более одной новой попытки каждые 5 минут. Публикация станет доступна после подтверждения этих правил сервером. Миры, журналы и данные учётных записей не публикуются.</p>
      <p role="status" class="community-policy-status">Проверяем, действуют ли новые ограничения на сервере…</p>
      <button type="button" class="linkbtn" data-pack-policy-retry hidden>Повторить проверку сервера</button>
      <p role="status" class="community-feedback"></p>
      <div class="community-receipts">${knownPublished ? publicationRows(previous) : ""}</div>
      <div class="community-publish-footer"><button id="communityPublishSubmit" class="btn main" type="submit" data-pack-publish-submit disabled>${ic("share")}Опубликовать</button><p role="status" class="community-publication-status">Проверяем статус публикации…</p></div>
    </form>`, {contentSized: true});
    const form = document.querySelector("[data-pack-publish]");
    if (pendingPublication?.id === id) setPublicationBusy(form, true, pendingPublication.progress);
    readPublicationCapabilities(form);
    readPublications(form);
  }

  function setPublicationBusy(form, busy, message) {
    form.setAttribute("aria-busy", String(busy));
    for (const control of form.querySelectorAll('input[name], select, [data-pack-withdraw]')) control.disabled = busy;
    syncPublicationSubmit(form);
    form.querySelector(".community-feedback").textContent = message;
  }

  function showPublicationBan(details) {
    const seconds = Math.max(0, Number(details?.retryAfterSeconds) || 300);
    const totalMinutes = Math.ceil(seconds / 60);
    const remaining = totalMinutes + " мин";
    const until = Number(details?.blockedUntil);
    const date = Number.isSafeInteger(until) && until > Date.now() / 1000 &&
      until < Date.now() / 1000 + seconds + 60
        ? new Date(until * 1000).toLocaleString("ru-RU", {dateStyle:"short", timeStyle:"short"})
        : "";
    modal("Повторная публикация", `
      <section class="community-ban-dialog" role="alert">
        <h3>Подожди перед следующей попыткой</h3>
        <p>Для этой сборки использованы 3 быстрые попытки. Следующая попытка станет доступна после паузы; далее можно пробовать не чаще одного раза в 5 минут.</p>
        <p><strong>Осталось: ${esc(remaining)}</strong>${date ? " · До " + esc(date) : ""}</p>
        <p class="note">Остальные возможности BlockHarbor работают как обычно: ты можешь запускать Minecraft, устанавливать моды и скачивать сборки игроков.</p>
        <div class="dialog-actions community-ban-actions">
          ${button('data-act="close"', "Понятно")}
        </div>
      </section>`);
  }

  function observeJob(job) {
    if (!sharingEnabled()) return;
    refreshOpenCommunity();
    const request = pendingPublication;
    if (!request || !request.job || job.id !== request.job || job.busy) return;
    pendingPublication = null;
    if (job.ok) {
      publicationCache.delete(request.id);
      publicationEpochs.set(request.id, (publicationEpochs.get(request.id) || 0) + 1);
      publicationRequests.delete(request.id);
      // Refresh the listing button even if the management dialog has been closed.
      fetchPublications(request.id, true).catch(() => {});
      community.needsSearch = true;
      if (document.querySelector(".community-results")) searchCommunity();
    }
    const form = document.querySelector("[data-pack-publish]");
    if (job.publicationBan) {
      if (form?.dataset.packPublish === request.id) setPublicationBusy(form, false, job.message);
      showPublicationBan(job.publicationBan);
      return;
    }
    if (form?.dataset.packPublish !== request.id) return;
    setPublicationBusy(form, false, job.ok ? request.done : job.message);
    if (job.ok) readPublications(form);
  }

  async function publicationAction(form, action, params) {
    if (!sharingEnabled()) return;
    if (pendingPublication) return notice("Дождись завершения текущей публикации.", true);
    const publishing = action === "communityPublish";
    const request = {id: form.dataset.packPublish, job: null,
      progress: publishing ? "Публикуем сборку…" : "Снимаем сборку с публикации…",
      done: publishing ? "Сборка опубликована. Её ID можно скопировать ниже." : "Сборка снята с публикации."};
    pendingPublication = request;
    setPublicationBusy(form, true, request.progress);
    try {
      request.job = await runJob(action, {id: request.id, ...params});
      if (state.job) observeJob(state.job);
    } catch (error) {
      pendingPublication = null;
      if (form.isConnected) setPublicationBusy(form, false, error.message);
      else notice(error.message, true);
    }
  }

  function renderPacks() {
    const sharing = sharingEnabled();
    const searchLabel = sharing ? "Название сборки, ID или ссылка" : "Название модпака";
    return `<section class="pack-hub"><div class="pack-intro"><p>Готовые сборки устанавливаются отдельно, со своей версией Minecraft и настройками.</p></div><form class="pack-search" data-pack-search role="search"><input type="search" class="field" name="q" maxlength="180" placeholder="${searchLabel}" aria-label="${searchLabel}" value="${esc(catalog.q)}"><button class="btn main" type="submit">${ic("search")}Найти</button></form>
      <div class="pack-filters"><label>Minecraft<select class="field" data-pack-filter="version">${option("", "Все версии", catalog.version)}${(state.versions || []).map(v => option(v, v, catalog.version)).join("")}</select></label>
      <label>Загрузчик<select class="field" data-pack-filter="loader">${option("", "Любой", catalog.loader)}${Object.entries(loaders).map(([v, label]) => option(v, label, catalog.loader)).join("")}</select></label>
      <label>Категория<select class="field" data-pack-filter="category">${Object.entries({"": "Все категории", ...categories}).map(([v, label]) => option(v, label, catalog.category)).join("")}</select></label>
      <label>Сортировка<select class="field" data-pack-filter="sort">${Object.entries({relevance: "По релевантности", downloads: "По загрузкам", follows: "По подписчикам", newest: "Сначала новые", updated: "Недавно обновлённые"}).map(([v, label]) => option(v, label, catalog.sort)).join("")}</select></label></div>
      <div class="pack-results">${sharing ? `<section class="community-results" aria-label="Сборки игроков BlockHarbor">${communityResults()}</section>` : ""}
      <section class="pack-modrinth" aria-label="Модпаки"${exactCommunityId() ? " hidden" : ""}><h3 class="pack-source-title">Модпаки</h3><p class="pack-status${catalog.error ? " pack-error" : ""}" role="status">${esc(catalog.error || (catalog.busy ? "Загружаем каталог…" : ""))}</p><div class="pack-source-results" aria-busy="${catalog.busy}">${results()}</div></section></div><footer class="pack-pagination">${sharing ? `<div class="community-page-host">${communityPager()}</div>` : ""}<div class="pack-page-host">${pager()}</div></footer></section>`;
  }

  function loadArt() {
    for (const slot of document.querySelectorAll("[data-pack-icon]")) {
      if (slot.querySelector("img")) continue;
      const url = slot.dataset.packIcon;
      if (!/^https:\/\/cdn\.modrinth\.com\//.test(url)) continue;
      const image = new Image(48, 48);
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => image.remove(), {once: true});
      slot.append(image);
      image.src = url;
    }
  }

  function updateResults() {
    const host = document.querySelector(".pack-hub");
    if (!host) return;
    host.querySelector(".pack-modrinth").hidden = exactCommunityId();
    host.querySelector('[data-pack-filter="sort"]').disabled = exactCommunityId();
    host.querySelector(".pack-source-results").innerHTML = results();
    host.querySelector(".pack-source-results").setAttribute("aria-busy", String(catalog.busy));
    host.querySelector(".pack-page-host").innerHTML = pager();
    const status = host.querySelector(".pack-modrinth .pack-status");
    status.textContent = catalog.error || (catalog.busy ? "Загружаем каталог…" : "");
    status.classList.toggle("pack-error", !!catalog.error);
    loadArt();
  }

  async function search(offset = 0) {
    const generation = ++searchGeneration;
    searchController?.abort();
    if (exactCommunityId()) {
      searchController = null;
      catalog.busy = false;
      catalog.needsSearch = false;
      updateResults();
      return;
    }
    const controller = new AbortController();
    searchController = controller;
    catalog.nextOffset = offset;
    catalog.needsSearch = true;
    catalog.busy = true;
    catalog.error = "";
    // Keep the previous page visible until a replacement has arrived.
    const host = document.querySelector(".pack-hub");
    if (host) {
      host.querySelector(".pack-source-results").setAttribute("aria-busy", "true");
      host.querySelector(".pack-modrinth .pack-status").textContent = "Загружаем каталог…";
      host.querySelector(".pack-page-host").innerHTML = pager();
    }
    try {
      const page = await feature("packSearch", {q: catalog.q, version: catalog.version, loader: catalog.loader, category: catalog.category, sort: catalog.sort, offset}, {signal: controller.signal});
      if (generation !== searchGeneration || controller.signal.aborted) return;
      catalog.page = page;
      catalog.offset = offset;
      catalog.needsSearch = false;
      catalogScroll = 0;
    } catch (error) {
      if (generation === searchGeneration && !controller.signal.aborted) catalog.error = error.message;
    } finally {
      if (generation === searchGeneration) {
        catalog.busy = false;
        searchController = null;
        updateResults();

      }
    }
  }

  async function showReleases(project) {
    rememberCatalog();
    stopSearch();
    discardPrepared();
    releaseController?.abort();
    const controller = new AbortController();
    releaseController = controller;
    selectedProject = project;
    selectedCommunity = null;
    packModal("Выбрать выпуск", '<div class="pack-release" role="status">Получаем версии…</div>', "releases", {
      contentSized: true,
      headerActions: button('data-pack-back="catalog"', "К каталогу", "back") + button("data-pack-prepare disabled", "Подготовить установку", "download", "main"),
    });
    const host = document.querySelector(".pack-release");
    try {
      const data = await feature("packVersions", {project}, {signal: controller.signal});
      if (!host.isConnected || controller.signal.aborted || selectedProject !== project) return;
      releaseRows.set(project, data.versions);
      const selected = data.versions.find(v => v.id === choices.get(project))?.id || data.versions.find(v => v.type === "release")?.id || data.versions[0]?.id;
      if (!selected) {host.innerHTML = empty("У этого модпака нет доступных выпусков .mrpack."); return;}
      choices.set(project, selected);
      host.innerHTML = `<h3>${esc(data.title)}</h3><p>${esc(data.description)}</p><label>Выпуск<select class="field" data-pack-version="${esc(project)}">${data.versions.map(v => option(v.id, `${v.number || v.name} · ${v.minecraft.join(", ")} · ${v.loaders.join(", ")}${v.type !== "release" ? " · " + v.type : ""}`, selected)).join("")}</select></label><p class="note">Выбранный выпуск сразу установится в отдельную сборку. Необязательные компоненты не добавляются.</p><p class="note" data-pack-steam-note></p><p class="pack-status" data-pack-install-status role="status" aria-live="polite"></p>`;
      updateReleaseSelection();
      host.querySelector("select")?.focus({preventScroll: true});
    } catch (error) {
      if (host.isConnected && !controller.signal.aborted) host.textContent = error.message;
    } finally {
      if (releaseController === controller) releaseController = null;
    }
  }

  function updateReleaseSelection() {
    const host = document.querySelector("#dialog .pack-release");
    const select = host?.querySelector("[data-pack-version]");
    if (!select) return;
    const project = select.dataset.packVersion;
    const version = choices.get(project);
    const release = releaseRows.get(project)?.find(row => row.id === version);
    const pending = releaseInstalls.get(`${project}:${version}`);
    const button = document.querySelector("#dialogHeaderActions [data-pack-prepare]");
    if (button) {
      button.disabled = !release || Boolean(pending);
      button.setAttribute("aria-busy", String(Boolean(pending)));
    }
    select.disabled = Boolean(pending);
    host.querySelector("[data-pack-install-status]").textContent = pending
      ? pending.submitting ? "Запускаем установку…" : "Загружаем и проверяем модпак…"
      : "";
    const steam = release?.steam;
    host.querySelector("[data-pack-steam-note]").textContent = steam?.supported
      ? "Если встроенное подключение через Steam недоступно, сборка установится без него."
      : !steam || steam.unknown
        ? "Поддержка Steam уточнится при установке. Если совместимого компонента нет, сборка установится без него."
        : "Встроенное подключение через Steam доступно не для всех вариантов этого выпуска. При необходимости сборка установится без него.";
  }

  async function installRelease() {
    const host = document.querySelector("#dialog .pack-release");
    const project = selectedProject;
    const version = choices.get(project);
    const release = releaseRows.get(project)?.find(row => row.id === version);
    if (!release) throw new Error("Выбери выпуск модпака ещё раз.");
    const key = `${project}:${version}`;
    if (releaseInstalls.has(key)) return;
    const isCurrent = () => host?.isConnected && document.querySelector("#dialog").open
      && selectedProject === project && choices.get(project) === version;
    if (!isCurrent()) return;
    const request = {submitting: false, accepted: false, token: ""};
    releaseInstalls.set(key, request);
    const feedback = host.querySelector("[data-pack-install-status]");
    feedback.classList.remove("pack-error");
    updateReleaseSelection();
    let failure = null;
    window.DownloadProgressUI?.begin("pack-preview:" + key, "Загрузка сборки", "Загружаем и проверяем модпак…");
    try {
      const data = await act("packPreview", {project, version});
      request.token = data.token;
      if (!isCurrent()) return;
      request.submitting = true;
      updateReleaseSelection();
      // Required files, optional defaults and the safe name remain backend-owned.
      // The inline Steam note is accepted together with this installation click.
      await runJob("packInstall", {token: data.token, name: data.name, allowWithoutSteam: true, discardOnFinish: true});
      request.accepted = true;
      if (isCurrent()) closeDialog();
    } catch (error) {
      failure = error;
      if (isCurrent()) notice(error.message, true);
    } finally {
      window.DownloadProgressUI?.end("pack-preview:" + key);
      // This token is deliberately separate from the manual-import preview:
      // closing a dialog during job acceptance must not discard its archive.
      if (request.token && !request.accepted) await act("packDiscard", {token: request.token}).catch(() => {});
      if (releaseInstalls.get(key) === request) releaseInstalls.delete(key);
      updateReleaseSelection();
      if (failure && isCurrent()) {
        feedback.textContent = failure.message;
        feedback.classList.add("pack-error");
      }
    }
  }

  function previewPack(data, {project = null, acceptedPairs = new Set()} = {}) {
    if (prepared && prepared.token !== data.token) discardPrepared();
    prepared = data;
    previewSteamAccepted = acceptedPairs;
    selectedProject = project;
    packModal("Установить модпак", `${button(`data-pack-back="${project ? "releases" : "catalog"}"`, project ? "К выпускам" : "К каталогу", "back", "pack-back")}<section class="pack-preview"><h3>${esc(data.title)}</h3><p>${esc(data.summary)}</p><div class="tags"><span class="pill">Minecraft ${esc(data.minecraft)}</span><span class="pill">${esc(loaders[data.loader] || data.loader)} ${esc(data.loaderVersion || "")}</span><span class="pill">${data.files} файлов</span><span class="pill">${fmt(data.downloadBytes)} загрузка</span></div>
      <label>Название новой сборки<input class="field" data-pack-name maxlength="48" value="${esc(data.name)}"></label>
      ${data.optional.length ? `<h4>Необязательные компоненты</h4><div class="pack-optional">${data.optional.map(row => `<label><input type="checkbox" data-pack-optional value="${esc(row.path)}"><span>${esc(row.path)}</span><small>${fmt(row.size)}</small></label>`).join("")}</div>` : ""}
      ${data.unsupported ? `<p class="note">${data.unsupported} файлов только для выделенного сервера будут пропущены.</p>` : ""}
      <p class="note">Текущие сборки останутся на месте. Файлы проверяются по размеру, SHA-1 и SHA-512.</p><div class="dialog-actions">${button("data-pack-install", "Установить отдельную сборку", "download", "main")}</div></section>`, "preview");
  }

  document.addEventListener("submit", event => {
    const form = event.target;
    if (form.matches("[data-pack-publish]")) {
      event.preventDefault();
      if (!sharingEnabled()) return;
      if (form.classList.contains("is-checking") || form.classList.contains("is-published") || form.dataset.policyVerified !== "true") return;
      const data = new FormData(form);
      const name = data.get("name").trim();
      if (!name) {form.elements.name.focus(); return notice("Введи название сборки.", true);}
      return void publicationAction(form, "communityPublish", {name, category: data.get("category"), includeConfigs: data.has("includeConfigs")});
    }
    if (!form.matches("[data-pack-search]")) return;
    event.preventDefault();
    catalog.q = new FormData(event.target).get("q") || "";
    searchCatalog();
  });
  document.addEventListener("change", event => {
    if (event.target.matches('.community-publish [name="includeConfigs"]')) {
      if (!sharingEnabled()) return;
      event.target.form.querySelector("#communityConfigHelp").hidden = !event.target.checked;
      return;
    }
    const filter = event.target.dataset.packFilter;
    if (filter) {
      catalog[filter] = event.target.value;
      catalog.q = document.querySelector("[data-pack-search] input")?.value || catalog.q;
      searchCatalog();
    }
    const project = event.target.dataset.packVersion;
    if (project) {
      choices.set(project, event.target.value);
      updateReleaseSelection();
    }
  });
  document.addEventListener("click", async event => {
    const target = event.target.closest("button");
    if (!target || target.disabled) return;
    const d = target.dataset;
    if (!Object.keys(d).some(key => key.startsWith("pack"))) return;
    if (!sharingEnabled() && ["packShare", "packCopyId", "packCopyLink", "packWithdraw", "packPublicationRetry", "packPolicyRetry", "packCommunity", "packCommunityRetry", "packCommunityPage"].some(key => key in d)) return;
    try {
      if (d.packShare) return showPublish(d.packShare);
      if ("packCopyId" in d) {
        const input = target.closest(".community-id").querySelector("input");
        input.value = input.dataset.publicationId || input.value;
        input.focus(); input.select();
        try {await navigator.clipboard.writeText(input.value); notice("ID сборки скопирован.");}
        catch {notice("ID выделен. Нажми Ctrl+C, чтобы скопировать.");}
        return;
      }
      if (d.packCopyLink) {
        const input = target.closest(".community-receipt").querySelector(".community-id input");
        input.value = d.packCopyLink;
        input.focus(); input.select();
        try {await navigator.clipboard.writeText(input.value); notice("Ссылка на сборку скопирована.");}
        catch {notice("Ссылка выделена. Нажми Ctrl+C, чтобы скопировать.");}
        return;
      }
      if (d.packWithdraw) return await publicationAction(target.closest("form"), "communityUnpublish", {build: d.packWithdraw});
      if ("packPublicationRetry" in d) return await readPublications(target.closest("form"));
      if ("packPolicyRetry" in d) return await readPublicationCapabilities(target.closest("form"));
      if (d.packCommunity) {
        const item = community.page?.hits.find(hit => hit.id === d.packCommunity);
        if (!item || communityDownloads.has(item.id)) return;
        communityDownloads.add(item.id);
        selectedCommunity = item.id;
        selectedProject = null;
        rememberCatalog();
        const host = target.closest(".pack-dialog");
        target.disabled = true;
        target.setAttribute("aria-busy", "true");
        try {
          const confirmation = await confirmSteamCompatibility({version: item.minecraft, loader: item.loader}, {isCurrent: () => host?.isConnected && document.querySelector("#dialog").open});
          if (!confirmation) return;
          await runJob("communityInstall", {build: item.id, allowWithoutSteam: confirmation.allowWithoutSteam});
          if (host?.isConnected) closeDialog();
        } finally {
          communityDownloads.delete(item.id);
          const current = document.querySelector(`[data-pack-community="${CSS.escape(item.id)}"]`);
          if (current) { current.disabled = false; current.removeAttribute("aria-busy"); }
        }
        return;
      }
      if ("packCommunityRetry" in d) return await searchCommunity(community.page?.offset || 0);
      if (d.packCommunityPage) {
        const page = community.page;
        if (page) await searchCommunity(Math.max(0, page.offset + (d.packCommunityPage === "next" ? page.limit : -page.limit)));
        document.querySelector(".community-results")?.scrollIntoView({block: "start"});
        return;
      }
      if (d.packBack === "catalog") return openCatalog({focusProject: selectedProject, focusCommunity: selectedCommunity});
      if (d.packBack === "releases") return await showReleases(selectedProject);
      if (d.packPage) {
        await search(Math.max(0, catalog.offset + (d.packPage === "next" ? 12 : -12)));
        document.querySelector(".pack-modrinth")?.scrollIntoView({block: "start"});
        return;
      }
      if (d.packProject) return await showReleases(d.packProject);
      if ("packPrepare" in d) return await installRelease();
      if ("packInstall" in d) {
        const current = prepared;
        if (!current) return;
        const host = target.closest(".pack-preview");
        const isCurrent = () => prepared === current && host?.isConnected && document.querySelector("#dialog").open;
        target.disabled = true;
        const confirmation = await confirmSteamCompatibility({version: current.minecraft, loader: current.loader}, {report: current.steam, acceptedPairs: previewSteamAccepted, isCurrent});
        if (!confirmation) return;
        const data = {token: current.token, name: host.querySelector("[data-pack-name]").value, optional: [...host.querySelectorAll("[data-pack-optional]:checked")].map(input => input.value), allowWithoutSteam: confirmation.allowWithoutSteam};
        await runJob("packInstall", data);
        prepared = null;
        previewSteamAccepted = new Set();
        closeDialog();
        return;
      }
    } catch (error) {notice(error.message, true);}
    finally {
      if (target.isConnected && !(d.packWithdraw && target.closest("form")?.getAttribute("aria-busy") === "true")) target.disabled = false;
    }
  });

  document.querySelector("#dialog").addEventListener("close", () => {
    // A native dialog's queued close event can arrive after another modal opens.
    if (document.querySelector("#dialog").open) return;
    rememberCatalog();
    stopSearch();
    releaseController?.abort();
    releaseController = null;
    discardPrepared();
    if (catalogOpen) {
      catalogOpen = false;
      const target = returnFocus?.isConnected ? returnFocus : document.querySelector('[data-act="packCatalog"]');
      target?.focus({preventScroll: true});
      returnFocus = null;
    }
  });

  window.PackHubUI = {
    render: renderPacks,
    openCatalog,
    isPublished(id) {return sharingEnabled() && (publicationCache.get(id)?.publications || []).some(row =>
      !row.statusUnverified && (!row.expiresAt || row.expiresAt * 1000 > Date.now()));},
    refreshShareButtons,
    observeJob,
    mount() {
      refreshShareButtons();
      loadArt();
      if (!document.querySelector(".pack-hub")) return;
      if (!catalog.page && !catalog.busy && !catalog.error) search();
      if (sharingEnabled() && community.needsSearch && !community.busy && !community.error) searchCommunity();
    },
    preview: previewPack,
    async dispatch(action) {
      if (action === "packCatalog") {openCatalog(); return true;}
      return false;
    },
  };

})();
