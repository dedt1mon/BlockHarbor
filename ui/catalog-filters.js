"use strict";

// Filter choices belong to the build and category that the user was browsing.
const catalogPreferences = new Map();
const catalogFilterSchemas = new Map();

function setupCatalogFilters(catalog, profile, category, pause, search) {
  const scope = profile.id + "/" + category;
  const browse = catalog.querySelector(".catalog-browse");
  const panel = catalog.querySelector(".catalog-filter-panel");
  const toggle = catalog.querySelector("[data-catalog-filters]");
  const dialog = catalog.closest("dialog");
  const empty = { filters: "{}" };
  const saved = catalogPreferences.get(scope) || {
    preset: empty,
    titles: new Map(),
  };
  catalogPreferences.set(scope, saved);
  const find = (selector) => panel.querySelector(selector);
  const url = "/api/catalog/filters?kind=" + encodeURIComponent(category);
  let schema, schemaRequest, draft, suggestions, timer;

  function restore(preset) {
    saved.preset = {filters: preset.filters};
    const total = Object.values(JSON.parse(preset.filters)).reduce(
      (n, items) => n + items.length,
      0,
    );
    toggle.textContent = total ? "Фильтры · " + total : "Фильтры";
  }

  async function loadSchema() {
    try {
      schema = catalogFilterSchemas.get(category);
      if (!schema) {
        if (!schemaRequest) schemaRequest = api(url);
        schema = await schemaRequest;
        catalogFilterSchemas.set(category, schema);
      }
      if (!catalog.isConnected) return;
      restore(saved.preset);
    } catch (error) {
      schemaRequest = null;
      throw error;
    }
  }

  function stopSuggestions() {
    clearTimeout(timer);
    suggestions?.abort();
    suggestions = null;
  }
  whenDialogClosed(dialog, catalog, stopSuggestions);

  function updateChip(button) {
    const { group, value } = button.dataset;
    const selected = draft[group] || [];
    const mode = schema.groups.find((g) => group === g.id).mode;
    const excluded =
      selected.includes("!" + value) ||
      (mode === "exclude" && selected.includes(value));
    const included = !excluded && selected.includes(value);
    const state = excluded ? "exclude" : included ? "include" : "";
    button.dataset.state = state;
    button.setAttribute("aria-pressed", excluded ? "mixed" : String(included));
    button.setAttribute(
      "aria-label",
      button.textContent.trim() +
        (excluded ? " · исключено" : included ? " · включено" : ""),
    );
  }
  function renderGroups() {
    find(".catalog-filter-groups").innerHTML = schema.groups
      .map(
        (group) =>
          `<fieldset><legend>${esc(group.name)}</legend><div class="catalog-filter-options">${group.options
            .map(
              (option) =>
                `<button type="button" class="catalog-filter-chip" data-group="${esc(group.id)}" data-value="${esc(option.id)}">${esc(option.name)}</button>`,
            )
            .join("")}</div></fieldset>`,
      )
      .join("");
    for (const button of panel.querySelectorAll("[data-group]"))
      updateChip(button);
  }
  function renderDependencies() {
    find(".catalog-dependencies").innerHTML = (draft.dependencies || [])
      .map(
        (dep) =>
          `<div class="catalog-dependency" data-dep-id="${esc(dep.id)}">
        <strong>${esc(saved.titles.get(dep.id) || dep.id)}</strong>
        <button class="linkbtn" type="button" data-dep-remove aria-label="Убрать зависимость ${esc(saved.titles.get(dep.id) || dep.id)}">${ic("close")}</button>
        <div class="catalog-dep-types">${Object.entries({
          required: "Обязательная",
          optional: "Необязательная",
          embedded: "Встроенная",
        })
          .map(
            ([key, name]) =>
              `<label><input type="checkbox" data-dep-type="${key}" ${dep.types.includes(key) ? "checked" : ""}>${name}</label>`,
          )
          .join(
            "",
          )}<label><input type="checkbox" data-dep-negative ${dep.exclude ? "checked" : ""}>Исключить зависящие проекты</label></div>
      </div>`,
      )
      .join("");
  }
  function closePanel() {
    stopSuggestions();
    panel.hidden = true;
    browse.hidden = false;
    catalog.dataset.catalogView = "browse";
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus({ preventScroll: true });
  }
  async function openPanel() {
    pause();
    draft = JSON.parse(saved.preset.filters);
    browse.hidden = true;
    panel.hidden = false;
    catalog.dataset.catalogView = "filters";
    toggle.setAttribute("aria-expanded", "true");
    panel.innerHTML = `
      <div class="catalog-filter-heading"><h3>Фильтры</h3></div>
      <div class="catalog-filter-scroll">
        <p class="catalog-filter-hint">Знак «+» включает параметр, знак «−» исключает. Нажми на параметр ещё раз, чтобы сменить его состояние.</p>
        <div class="catalog-filter-groups"></div>
        <fieldset class="catalog-dep-group"><legend>Зависит от проекта</legend>
          <div class="catalog-dependencies"></div>
          <input type="text" class="field" data-dep-query autocomplete="off" aria-label="Название проекта-зависимости" maxlength="120" placeholder="Найти проект в каталоге">
          <div class="catalog-dep-options" role="group" aria-label="Найденные проекты"></div>
          <span class="catalog-dep-status" role="status"></span>
        </fieldset>
      </div>
      <div class="catalog-filter-footer"><button class="btn" type="button" data-filter-cancel>${ic("back")}К каталогу</button>
        <button class="btn" type="button" data-filter-reset>${ic("refresh")}Сбросить фильтр</button>
        <button class="btn main" type="button" data-filter-apply>${ic("check")}Применить</button></div>`;
    find("[data-filter-cancel]").focus({ preventScroll: true });
    if (!schema) {
      try {
        await loadSchema();
      } catch (error) {
        if (!panel.hidden)
          find(".catalog-filter-groups").textContent =
            error.message + " Открой фильтры ещё раз.";
        return;
      }
    }
    if (!panel.hidden && catalog.isConnected) {
      renderGroups();
      renderDependencies();
    }
  }
  async function suggest(query) {
    const controller = new AbortController();
    suggestions = controller;
    const options = find(".catalog-dep-options");
    const status = find(".catalog-dep-status");
    status.textContent = "Ищем проекты…";
    try {
      const data = await api(url + "&q=" + encodeURIComponent(query), {
        signal: controller.signal,
      });
      if (suggestions !== controller || panel.hidden) return;
      const existing = new Set((draft.dependencies || []).map((dep) => dep.id));
      const hits = data.hits.filter((item) => !existing.has(item.id));
      options.innerHTML = hits
        .map(
          (item) =>
            `<button type="button" class="catalog-dep-hit" data-add-dep="${esc(item.id)}" data-dep-title="${esc(item.title)}"><strong>${esc(item.title)}</strong> <small>${esc(item.author)}</small></button>`,
        )
        .join("");
      status.textContent = hits.length ? "" : "Подходящих проектов нет.";
    } catch (error) {
      if (suggestions === controller) status.textContent = error.message;
    }
  }
  panel.addEventListener("input", (event) => {
    if (!event.target.matches("[data-dep-query]")) return;
    stopSuggestions();
    find(".catalog-dep-options").replaceChildren();
    find(".catalog-dep-status").textContent = "";
    const query = event.target.value.trim();
    if (query) timer = setTimeout(() => suggest(query), 260);
  });
  panel.addEventListener("change", (event) => {
    const input = event.target;
    const row = input.closest("[data-dep-id]");
    if (!row) return;
    const dep = draft.dependencies.find((d) => d.id === row.dataset.depId);
    if (input.hasAttribute("data-dep-negative")) dep.exclude = input.checked;
    if (input.hasAttribute("data-dep-type")) {
      if (input.checked) dep.types.push(input.dataset.depType);
      else if (dep.types.length > 1)
        dep.types = dep.types.filter((t) => t !== input.dataset.depType);
      else input.checked = true;
    }
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    closePanel();
  });
  panel.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const { group, value, addDep, depTitle } = button.dataset;
    if (group) {
      const mode = schema.groups.find((g) => g.id === group).mode;
      const list = draft[group] || [];
      const included = list.includes(value),
        excluded = list.includes("!" + value);
      const values = list.filter((v) => v !== value && v !== "!" + value);
      if (included && mode === "both") values.push("!" + value);
      else if (!included && !excluded) values.push(value);
      if (values.length) draft[group] = values.sort();
      else delete draft[group];
      updateChip(button);
    } else if (addDep) {
      const deps = (draft.dependencies ||= []);
      if (deps.length >= 16) {
        find(".catalog-dep-status").textContent =
          "Можно выбрать до 16 проектов.";
        return;
      }
      deps.push({ id: addDep, types: ["required"], exclude: false });
      saved.titles.set(addDep, depTitle);
      stopSuggestions();
      find("[data-dep-query]").value = "";
      find(".catalog-dep-options").replaceChildren();
      find(".catalog-dep-status").textContent = "";
      renderDependencies();
      find("[data-dep-query]").focus();
    } else if (button.hasAttribute("data-dep-remove")) {
      const id = button.closest("[data-dep-id]").dataset.depId;
      draft.dependencies = draft.dependencies.filter((d) => d.id !== id);
      if (!draft.dependencies.length) delete draft.dependencies;
      renderDependencies();
      find("[data-dep-query]").focus({ preventScroll: true });
    } else if (button.hasAttribute("data-filter-reset")) {
      draft = {};
      stopSuggestions();
      find("[data-dep-query]").value = "";
      find(".catalog-dep-options").replaceChildren();
      find(".catalog-dep-status").textContent = "";
      if (schema) renderGroups();
      renderDependencies();
    } else if (button.hasAttribute("data-filter-cancel")) closePanel();
    else if (button.hasAttribute("data-filter-apply")) {
      restore({ filters: JSON.stringify(draft) });
      closePanel();
      search();
    }
  });
  toggle.addEventListener("click", openPanel);
  restore(saved.preset);
  loadSchema().catch(() => {
    /* Opening the editor retries the local request. */
  });
  return { read: () => saved.preset, restore };
}
