"""Production UI browser checks with deterministic local API fixtures.

Run with an installed Chromium and `python -m pip install playwright`.
The 24 combinations emulate usable CSS viewport size and device pixel ratio;
they do not claim native Windows DPI or actual Minecraft launch coverage.
All production HTML, scripts and styles are loaded without an external network.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import mimetypes
from pathlib import Path
import shutil
import sys
import traceback
from urllib.parse import parse_qs, urlsplit

from smoke_ui_layout import FIXTURE, UI

RESOLUTIONS = ((1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160))
SCALES = (1, 1.25, 1.5, 2)
ORIGIN = "http://blockharbor.test"
TOKEN = "ui-audit-fixture-token"
LONG_NAME = "Сборка с длинным названием — исследование мира и совместная игра"
SETTINGS_CASES = {(1920, 1080, 1), (1280, 720, 2)}


class FixtureAPI:
    def __init__(self):
        self.errors = []
        self.actions = []
        self.requests = []
        self.reads = []
        self.racing_notification = True
        self.publication_enabled = True
        self.preview_error = ""
        self.preferences = {"scale": 100, "reducedMotion": True}
        self.notifications = [{"id": 1, "created": 1770000000, "message": "Срок действия ссылки на сборку истёк. Сборка автоматически удалена с сервера", "level": "info", "read": False}]
        self.job = {"id": "", "busy": False, "ok": True, "message": "", "gameRunning": False, "localGameRunning": False, "runningProfiles": [], "gameExit": None, "launcherCount": 1}
        self.profile = {"id": "profile-a", "name": LONG_NAME, "version": "1.20.1", "loader": "fabric", "favorite": False, "description": "Описание " * 18}
        self.rows = [{"name": "example-long-file-name-" + str(i) + ".jar", "title": "Дополнение " + str(i), "description": "Описание установленного дополнения", "version": "1.0.0", "size": 1024, "enabled": True} for i in range(12)]
        self.contents = {"mods": self.rows, "shaderpacks": [], "resourcepacks": []}
        self.job_counter = 0

    @property
    def unread(self):
        return sum(not item["read"] for item in self.notifications)

    def state(self):
        return {"appVersion": "audit", "profiles": [self.profile, {**self.profile, "id": "profile-b", "name": "Вторая сборка", "loader": "quilt"}],
                "deletedProfiles": [], "active": self.profile["id"], "contents": self.contents,
                "experience": {"preferences": copy.deepcopy(self.preferences), "notificationUnread": self.unread},
                "notificationUnread": self.unread, "steam": {"supported": True, "installed": True, "canEnable": False, "canContinue": True, "message": "Готово"},
                "skinNetwork": {"supported": True}, "skinSupport": True, "canUseFabricSkin": False, "realName": "", "nickname": "Player", "model": "classic", "skin": False,
                "account": "", "memory": 4096, "autoMemory": True, "distanceSettings": True, "recommendedMemory": 4096, "clientId": "", "versions": ["1.20.1", "1.21.1"],
                "job": {**self.job, "notificationUnread": self.unread}, "mode": "local", "gameRunning": False, "gameExit": None, "localGameRunning": False, "runningProfiles": [], "launcherCount": 1}

    def feature(self, action):
        if action == "experienceNotifications":
            return {"items": copy.deepcopy(list(reversed(self.notifications))), "unread": self.unread, "lastId": max((item["id"] for item in self.notifications), default=0)}
        if action == "experienceState":
            return {"wardrobe": [], "preferences": copy.deepcopy(self.preferences), "notificationUnread": self.unread}
        if action == "experienceGallery":
            return {"images": [{"name": "Длинное-название-скриншота-" + str(i) + ".png", "image": "a" * 64, "token": "token-" + str(i), "modified": 1770000000, "size": 1024} for i in range(6)], "trash": [], "total": 6, "limit": 200}
        if action == "careState":
            return {"contents": self.contents, "snapshots": [], "history": [], "retention": {"count": 10, "budgetMiB": 1024}, "recoveryBytes": 0, "autoWorldBackup": True}
        if action == "careWorlds":
            return {"worlds": [{"name": LONG_NAME, "size": 1024, "lastPlayed": 1770000000}]}
        if action == "buildErrors":
            return {"groups": []}
        if action == "communityPublication":
            return {"publications": []}
        if action == "communityCapabilities":
            return {"enabled": self.publication_enabled, "message": "Сервер хранит публикацию 60 минут" if self.publication_enabled else "Новые публикации недоступны: сервер ещё не подтвердил правила"}
        if action == "packSearch":
            return {"hits": [{"id": "pack-" + str(i), "title": "Большая сборка " + str(i), "description": "Описание " * 40, "author": "Автор", "downloads": 1000, "icon": ""} for i in range(8)], "total": 8, "limit": 20, "offset": 0}
        if action == "communitySearch":
            return {"source": "primary", "hits": [{"id": "BH-0123456789A" + str(i), "name": LONG_NAME, "minecraft": "1.20.1", "loader": "fabric", "category": "adventure", "size": 1024, "expiresAt": 9999999999} for i in range(4)], "total": 4, "limit": 20, "offset": 0}
        if action == "updateState":
            return {"current": "audit", "channel": "stable", "autoEnabled": False, "available": False, "windows": True, "checkedAt": 1770000000, "transfers": {"tasks": []}, "trust": "Проверка контрольной суммы"}
        raise AssertionError("Unexpected feature: " + action)

    def action(self, data):
        self.actions.append(copy.deepcopy(data))
        action = data["action"]
        if action == "experienceReadNotifications":
            through = data["throughId"]
            self.reads.append(through)
            if self.racing_notification:
                self.racing_notification = False
                self.notifications.append({"id": 2, "created": 1770000001, "message": "Новое событие после открытия истории", "level": "success", "read": False})
            for item in self.notifications:
                if item["id"] <= through:
                    item["read"] = True
            return {"ok": True, "unread": self.unread, "lastId": max(item["id"] for item in self.notifications)}
        if action == "experienceClearNotifications":
            self.notifications.clear()
            return {"ok": True, "unread": 0}
        if action == "installModrinth":
            self.job_counter += 1
            items = data.get("items") or [data]
            self.job = {**self.job, "id": "batch-" + str(self.job_counter), "kind": "catalog-install", "profileId": data["id"], "busy": True, "message": "Проверяем дополнения", "cancellable": True, "cancelRequested": False,
                        "items": [{**item, "title": "Дополнение " + item["project"], "status": "queued", "error": ""} for item in items], "reviews": []}
            return {"job": self.job["id"]}
        if action == "cancelCatalogInstall":
            assert data["job"] == self.job["id"]
            self.job["cancelRequested"] = True
            for item in self.job["items"]:
                if item["status"] not in {"installed", "already_installed"}:
                    item["status"] = "cancelled"
            self.job.update(busy=False, ok=False, message="Операция отменена")
            return {"ok": True, "cancelRequested": True}
        if action == "select":
            return {"ok": True}
        raise AssertionError("Unexpected action: " + action)

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        query = parse_qs(url.query)
        self.requests.append({"path": path, "query": query})
        first = lambda key, default="": query.get(key, [default])[0]
        try:
            if url.netloc != "blockharbor.test":
                raise AssertionError("Unexpected external request: " + request.url)
            if path.startswith("/api/media/"):
                route.fulfill(body=(UI / "icon.png").read_bytes(), content_type="image/png")
                return
            if not path.startswith("/api/"):
                file = UI / (path.lstrip("/") or "index.html")
                assert file.is_file() and file.parent == UI, path
                route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or "text/plain")
                return
            if path.startswith("/api/window"):
                result = {"ok": True}
            else:
                assert request.headers.get("authorization") == "Bearer " + TOKEN, "Missing local API authentication"
                if path == "/api/state":
                    result = self.state()
                elif path == "/api/job":
                    result = {**self.job, "notificationUnread": self.unread}
                elif path == "/api/feature":
                    result = self.feature(first("action"))
                elif path == "/api/action":
                    result = self.action(request.post_data_json)
                elif path == "/api/catalog/filters":
                    result = {"sort": {"relevance": "По релевантности", "downloads": "По загрузкам"}, "groups": [], "dependencies": False}
                elif path == "/api/catalog/search":
                    source = first("source", "community")
                    hits = [{"id": "mod-" + str(i), "title": "Совместимое дополнение " + str(i), "description": "Очень длинное описание " * 20, "source": source, "sourceTitle": "Источник", "author": "Автор", "downloads": 100 + i, "recommendedVersion": "stable-1", "compatibility": "release-metadata"} for i in range(12)]
                    result = {"hits": [*hits, hits[0]], "offset": 0, "limit": 20, "total": 40, "totalIsEstimate": True, "verified": True}
                elif path == "/api/catalog/check":
                    data = request.post_data_json if request.method == "POST" else {"items": [{"project": first("project"), "version": first("version"), "kind": first("kind"), "source": first("source", "all")} ]}
                    items = [{**item, "version": item.get("version") or "stable-1"} for item in data["items"]]
                    available = [item for item in items if item["project"] != self.preview_error]
                    result = {"items": available, "selected": [{**item, "installed": False, "error": "Внешний каталог временно недоступен" if item["project"] == self.preview_error else ""} for item in items], "blocked": not available, "partial": len(available) < len(items), "required": [], "optional": [], "conflicts": [], "warnings": []}
                elif path == "/api/catalog/versions":
                    result = {"versions": [{"id": "beta-2", "name": "Новый тестовый выпуск", "number": "2.0-beta", "type": "beta", "published": "2026-02-01T00:00:00Z", "filename": "example-beta.jar", "size": 1024, "installed": False},
                                           {"id": "stable-1", "name": "Стабильный выпуск", "number": "1.0", "type": "release", "published": "2026-01-01T00:00:00Z", "filename": "example.jar", "size": 1024, "installed": False}]}
                else:
                    raise AssertionError("Unexpected API: " + path)
            route.fulfill(json=result)
        except Exception as error:
            self.errors.append(str(error))
            route.fulfill(status=500, json={"error": str(error)})


def settle(page):
    page.wait_for_function("window.__pendingRequests === 0")
    page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def geometry(page, dialog=False):
    errors = page.evaluate("""(dialog) => {
      const errors = [];
      const main = document.querySelector('main');
      if (main.scrollWidth > main.clientWidth + 1) errors.push('main horizontal overflow ' + main.scrollWidth + '/' + main.clientWidth);
      if (document.documentElement.scrollWidth > innerWidth + 1) errors.push('document horizontal overflow');
      if (dialog) {
        const d = document.querySelector('#dialog'), r = d.getBoundingClientRect();
        if (!d.open || r.left < -1 || r.right > innerWidth + 1 || r.top < -1 || r.bottom > innerHeight + 1) errors.push('dialog outside viewport ' + JSON.stringify(r));
        if (d.scrollWidth > d.clientWidth + 1) errors.push('dialog horizontal overflow');
        const body = document.querySelector('#dialogBody');
        if (body.scrollWidth > body.clientWidth + 1) errors.push('dialog content horizontal overflow ' + body.scrollWidth + '/' + body.clientWidth);
        const notice = d.querySelector(':scope > #notice:not([hidden])');
        if (notice) {
          const n = notice.getBoundingClientRect(), b = body.getBoundingClientRect();
          if (n.top < b.bottom - 1) errors.push('dialog notice overlaps its content');
          if (n.bottom > r.bottom + 1 || n.right > r.right + 1) errors.push('dialog notice outside dialog');
        }
        const close = document.querySelector('#dialogClose').getBoundingClientRect();
        if (close.top < r.top || close.bottom > r.bottom || close.right > r.right) errors.push('close control inaccessible');
      }
      return errors;
    }""", dialog)
    assert not errors, "; ".join(errors)


def controls_fixture(page, css):
    page.set_content('<!doctype html><html lang="ru"><meta charset="utf-8"><style>' + css + '</style><body class="reduced-motion">' + FIXTURE + '</body></html>')
    page.bring_to_front()
    page.wait_for_function("document.body.dataset.layoutResult")
    assert page.locator("body").get_attribute("data-layout-result") == "PASS", page.locator("body").get_attribute("data-layout-errors")


def readable_batch_row(page):
    result = page.evaluate("""() => {
      const list = document.querySelector('.batch-items');
      const row = list.querySelector('.batch-item');
      const r = row.getBoundingClientRect(), l = list.getBoundingClientRect();
      let top = Math.max(0, l.top), bottom = Math.min(innerHeight, l.bottom);
      for (let parent = list.parentElement; parent; parent = parent.parentElement) {
        if (/auto|scroll|hidden|clip/.test(getComputedStyle(parent).overflowY)) {
          const p = parent.getBoundingClientRect();
          top = Math.max(top, p.top); bottom = Math.min(bottom, p.bottom);
        }
      }
      const actions = document.querySelector('.batch-actions');
      if (['sticky', 'fixed'].includes(getComputedStyle(actions).position)) {
        const a = actions.getBoundingClientRect();
        if (a.top < bottom && a.bottom > top) bottom = Math.min(bottom, a.top);
      }
      const status = row.querySelector('.batch-item-status').getBoundingClientRect();
      return {top, bottom, visibleListHeight: bottom - top, rowTop: r.top, rowBottom: r.bottom,
        rowHeight: r.height, statusTop: status.top, statusBottom: status.bottom,
        status: row.querySelector('.batch-item-status').textContent};
    }""")
    assert result["rowTop"] >= result["top"] - 1 and result["rowBottom"] <= result["bottom"] + 1, "No complete first batch item is visible: " + json.dumps(result, ensure_ascii=False)
    assert result["status"] and result["statusBottom"] > result["statusTop"], "Visible batch row has no readable status"


def settings_field(page, session, selector, output, stem):
    """Measure actual text fragments, including the control's UA shadow tree."""
    field = page.locator(selector)
    field.scroll_into_view_if_needed()
    field.evaluate("el => el.blur()")
    settle(page)
    field.screenshot(path=str(output / (stem + ".png")))
    expected = field.evaluate("el => el.tagName === 'SELECT' ? el.selectedOptions[0].label : (el.value || el.placeholder)")
    assert expected and field.is_enabled(), selector + " has no enabled, readable value"
    # captureSnapshot excludes UA text in current Chromium. The legacy command
    # explicitly includes it; missing fragments are a failure, not a CSS fallback.
    snapshot = session.send("DOMSnapshot.getSnapshot", {
        "computedStyleWhitelist": ["visibility", "opacity", "overflow-x", "overflow-y",
                                  "border-left-width", "border-right-width", "border-top-width", "border-bottom-width",
                                  "padding-left", "padding-right", "padding-top", "padding-bottom"],
        "includeUserAgentShadowTree": True,
    })
    nodes, layouts = snapshot["domNodes"], snapshot["layoutTreeNodes"]
    layout_by_node = {entry["domNodeIndex"]: entry for entry in layouts}
    parents = {child: index for index, node in enumerate(nodes) for child in node.get("childNodeIndexes", [])}
    host_id = field.get_attribute("id")
    host = next(index for index, node in enumerate(nodes)
                if any(attr["name"] == "id" and attr["value"] == host_id for attr in node.get("attributes", [])))

    def style(index):
        style_index = layout_by_node.get(index, {}).get("styleIndex")
        return {} if style_index is None else {p["name"]: p["value"] for p in snapshot["computedStyles"][style_index]["properties"]}

    def px(styles, name):
        return float(styles.get(name, "0px").removesuffix("px"))

    def ancestors(index):
        while index in parents:
            index = parents[index]
            yield index

    def rect(box):
        return {"left": box["x"], "top": box["y"], "right": box["x"] + box["width"], "bottom": box["y"] + box["height"]}

    control = rect(layout_by_node[host]["boundingBox"])
    css = style(host)
    content = {
        "left": control["left"] + px(css, "border-left-width") + px(css, "padding-left"),
        "right": control["right"] - px(css, "border-right-width") - px(css, "padding-right"),
        "top": control["top"] + px(css, "border-top-width") + px(css, "padding-top"),
        "bottom": control["bottom"] - px(css, "border-bottom-width") - px(css, "padding-bottom"),
    }
    candidates = []
    for index, layout in layout_by_node.items():
        if host not in set(ancestors(index)) or expected not in layout.get("layoutText", ""):
            continue
        lineage = [index, *ancestors(index)]
        if any(style(parent).get("visibility") in {"hidden", "collapse"} or style(parent).get("opacity") == "0" for parent in lineage):
            continue
        fragments = [part for part in layout.get("inlineTextNodes", [])
                     if part["boundingBox"]["width"] > 0 and part["boundingBox"]["height"] > 0]
        if not fragments:
            continue
        boxes = [rect(part["boundingBox"]) for part in fragments]
        text = {"left": min(b["left"] for b in boxes), "right": max(b["right"] for b in boxes),
                "top": min(b["top"] for b in boxes), "bottom": max(b["bottom"] for b in boxes)}
        if text["bottom"] > control["top"] and text["top"] < control["bottom"]:
            candidates.append((index, layout, fragments, text))
    metrics = {"selector": selector, "expected": expected, "control": control, "content": content, "candidates": len(candidates)}
    errors = []
    if not candidates:
        errors.append("No rendered UA text fragments for the selected value")
    else:
        index, layout, fragments, text = min(candidates, key=lambda item: abs(item[3]["top"] + item[3]["bottom"] - control["top"] - control["bottom"]))
        prefix = layout["layoutText"].split(expected, 1)[0]
        start = len(prefix.encode("utf-16-le")) // 2
        required, offset = set(), start
        for character in expected:
            length = len(character.encode("utf-16-le")) // 2
            if not character.isspace():
                required.update(range(offset, offset + length))
            offset += length
        covered = {position for part in fragments for position in range(part["startCharacterIndex"], part["startCharacterIndex"] + part["numCharacters"])}
        if not required <= covered:
            errors.append("Rendered fragments omit characters from the expected value")
        if text["left"] < content["left"] - 1 or text["right"] > content["right"] + 1 or text["top"] < content["top"] - 1 or text["bottom"] > content["bottom"] + 1:
            errors.append("Text is clipped by the field content box or the reserved arrow area")
        center_offset = (text["top"] + text["bottom"] - control["top"] - control["bottom"]) / 2
        if abs(center_offset) > 2:
            errors.append("Text is not vertically centered: " + str(round(center_offset, 3)) + " CSS px")
        clips = []
        for parent in ancestors(index):
            parent_layout = layout_by_node.get(parent)
            if not parent_layout:
                continue
            parent_css = style(parent)
            axes = [axis for axis in ("x", "y") if parent_css.get("overflow-" + axis) in {"auto", "scroll", "hidden", "clip"}]
            if not axes:
                continue
            clip = rect(parent_layout["boundingBox"])
            clip["left"] += px(parent_css, "border-left-width")
            clip["right"] -= px(parent_css, "border-right-width")
            clip["top"] += px(parent_css, "border-top-width")
            clip["bottom"] -= px(parent_css, "border-bottom-width")
            clips.append({"node": nodes[parent]["nodeName"], "axes": axes, "bounds": clip})
            if ("x" in axes and (text["left"] < clip["left"] - 1 or text["right"] > clip["right"] + 1)
                    or "y" in axes and (text["top"] < clip["top"] - 1 or text["bottom"] > clip["bottom"] + 1)):
                errors.append("Text exceeds a clipping ancestor: " + nodes[parent]["nodeName"])
        metrics.update(text=text, centerOffsetCssPx=center_offset, fragments=fragments, clips=clips)
    metrics["errors"] = errors
    (output / (stem + ".json")).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        (output / (stem + "-snapshot.json")).write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    assert not errors, selector + ": " + "; ".join(errors)
    return metrics


def settings_panel_geometry(page, descriptions):
    errors = page.evaluate("""(selectors) => {
      const errors = [], panels = [...document.querySelectorAll('#settingsPanel .settings-grid > .panel')];
      const bounds = el => el.getBoundingClientRect();
      if (panels.length !== 2) return ['Expected two settings cards'];
      const [a, b] = panels.map(bounds), wrapper = bounds(document.querySelector('#settingsPanel'));
      if (innerWidth >= 1200) {
        if (Math.abs(a.top - b.top) > 1 || a.right >= b.left) errors.push('Wide settings cards are not side by side');
        if (Math.abs(a.height - b.height) > 1) errors.push('Wide settings cards have unequal heights');
      } else if (b.top < a.bottom - 1) errors.push('Narrow settings cards overlap instead of stacking');
      for (const r of [a, b]) if (r.left < wrapper.left - 1 || r.right > wrapper.right + 1) errors.push('Settings card exceeds its container');
      for (const selector of selectors) {
        const p = document.querySelector(selector);
        if (!p || !p.getClientRects().length) { errors.push('Missing description ' + selector); continue; }
        const card = p.closest('.panel'), r = bounds(p), c = bounds(card), s = getComputedStyle(card);
        const left = c.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft);
        const right = c.right - parseFloat(s.borderRightWidth) - parseFloat(s.paddingRight);
        if (Math.abs(r.left - left) > 1 || Math.abs(r.right - right) > 1) errors.push(selector + ' does not use the card inner width: ' + r.width + '/' + (right - left));
      }
      return errors;
    }""", descriptions)
    assert not errors, "; ".join(errors)
    geometry(page)


def open_select_picker(page, selector, output, stem):
    control = page.locator(selector)
    control.scroll_into_view_if_needed()
    value = control.input_value()
    control.click()
    try:
        if page.evaluate("CSS.supports('appearance', 'base-select')"):
            page.wait_for_function("selector => document.querySelector(selector).matches(':open')", arg=selector)
            page.screenshot(path=str(output / (stem + "-picker.png")))
            options = control.locator("option")
            for option in (options.first, options.last):
                option.scroll_into_view_if_needed()
                assert option.is_visible(), selector + " picker has an inaccessible option"
                assert option.evaluate("""el => {
                  const r = el.getBoundingClientRect();
                  const range = document.createRange(); range.selectNodeContents(el);
                  const text = range.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && r.left >= -1 && r.right <= innerWidth + 1 &&
                    r.top >= -1 && r.bottom <= innerHeight + 1 && text.left >= r.left - 1 && text.right <= r.right + 1 &&
                    text.top >= r.top - 1 && text.bottom <= r.bottom + 1;
                }"""), selector + " option text exceeds the opened picker"
    finally:
        page.keyboard.press("Escape")
        settle(page)
    assert control.input_value() == value, selector + " changed on opening/Escape"


def settings_flows(page, api, output, label, results):
    """Focused visual coverage; preferences arrive through the real state API."""
    original_preferences, original_job = copy.deepcopy(api.preferences), copy.deepcopy(api.job)
    session = page.context.new_cdp_session(page)
    failures, passed = [], 0
    try:
        api.job = copy.deepcopy(FixtureAPI().job)
        for app_scale in (100, 85, 120):
            api.preferences["scale"] = app_scale
            stem = label + "-settings-app" + str(app_scale)
            record = {"case": label, "appTextScale": app_scale, "failures": [], "screenshotsPrefix": stem}
            results.append(record)
            action_start = len(api.actions)

            def check(name, callback):
                try:
                    return callback()
                except Exception as error:
                    record["failures"].append(name + ": " + str(error))
                    page.screenshot(path=str(output / (stem + "-" + name + "-FAIL.png")))

            # app.js removes the token hash. Navigating back to /#token alone
            # can therefore keep the previous document, dialog and preferences.
            page.goto("about:blank")
            page.goto(ORIGIN + "/#" + TOKEN)
            page.wait_for_function("""scale => typeof state !== 'undefined' &&
              state?.experience?.preferences?.scale === scale && window.ExperienceUI &&
              Math.abs(parseFloat(getComputedStyle(document.documentElement).fontSize) - 15 * scale / 100) < .01""",
                                   arg=app_scale)
            settle(page)
            assert abs(page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)") - 15 * app_scale / 100) < .01, "Production text scale was not applied"
            page.locator('aside [data-view="settings"]').click()
            page.locator("#settingsGeneralTab").click()
            settle(page)
            assert page.locator("#settingsGeneralTab").get_attribute("aria-selected") == "true"
            assert page.locator("#settingsPanel").get_attribute("aria-labelledby") == "settingsGeneralTab"
            check("ram-auto", lambda: settings_field(page, session, "#ram", output, stem + "-ram-auto"))
            page.locator("#mode").select_option("direct")
            assert page.locator("#advancedAuth").is_visible()
            page.locator("#ram").fill("8192")
            page.locator("#clientId").fill("12345678-1234-1234-1234-123456789abc")
            check("general-layout", lambda: settings_panel_geometry(page, ["#modeHelp", "#advancedAuth p"]))
            for selector in ("#mode", "#ram", "#clientId", "#uiScale"):
                check(selector[1:], lambda selector=selector: settings_field(page, session, selector, output, stem + "-" + selector[1:]))
            for selector in ("#mode", "#uiScale"):
                check(selector[1:] + "-picker", lambda selector=selector: open_select_picker(page, selector, output, stem + "-" + selector[1:]))
            page.evaluate("document.querySelector('main').scrollTop = 0")
            settle(page)
            page.screenshot(path=str(output / (stem + "-general.png")))
            general_failures = len(record["failures"])
            passed += not general_failures

            page.locator("#settingsDownloadsTab").click()
            settle(page)
            assert page.locator("#settingsDownloadsTab").get_attribute("aria-selected") == "true"
            assert page.locator("#settingsPanel").get_attribute("aria-labelledby") == "settingsDownloadsTab"
            page.wait_for_function("!document.querySelector('#updateChannel').disabled")
            check("downloads-layout", lambda: settings_panel_geometry(page, ["#automaticUpdateHelp", "#automaticUpdateStatus", ".updater-channel > div > p"]))
            check("updateChannel", lambda: settings_field(page, session, "#updateChannel", output, stem + "-updateChannel"))
            check("updateChannel-picker", lambda: open_select_picker(page, "#updateChannel", output, stem + "-updateChannel"))
            page.evaluate("document.querySelector('main').scrollTop = 0")
            settle(page)
            page.screenshot(path=str(output / (stem + "-downloads.png")))
            passed += len(record["failures"]) == general_failures
            assert len(api.actions) == action_start, "Settings visual checks unexpectedly saved preferences or settings"
            record["result"] = "FAIL" if record["failures"] else "PASS"
            failures.extend("app " + str(app_scale) + "% " + message for message in record["failures"])
    finally:
        api.preferences, api.job = original_preferences, original_job
        session.detach()
    return passed, failures


def production_views(page, api, output, label):
    detailed_capture = label in {"1366x768-100pct", "1280x720-200pct"}
    geometry_failures = []

    def check_geometry(name, dialog=False):
        try:
            geometry(page, dialog)
        except AssertionError as error:
            geometry_failures.append(name + ": " + str(error))
            page.screenshot(path=str(output / (label + "-" + name + "-geometry-FAIL.png")))

    page.route("**/*", api.route)
    page.add_init_script("""window.__pendingRequests = 0; const originalFetch = window.fetch;
      window.fetch = async (...args) => { window.__pendingRequests++; try { return await originalFetch(...args); } finally { window.__pendingRequests--; } };""")
    page.goto(ORIGIN + "/#" + TOKEN)
    page.wait_for_function("typeof state !== 'undefined' && state && window.ExperienceUI && window.BatchUI")
    settle(page)
    for destination in ("home", "builds", "friends", "profile", "settings", "gallery", "addons"):
        if destination == "addons":
            page.evaluate("navigate('addons')")
        else:
            # On a short viewport the lower sidebar entry requires scrolling.
            # A real click checks its reachability instead of bypassing the UI.
            page.locator(f'aside [data-view="{destination}"]').click()
        settle(page)
        check_geometry(destination)
        if detailed_capture:
            page.screenshot(path=str(output / (label + "-" + destination + ".png")))
    page.evaluate("showSettingsSection('downloads')")
    settle(page)
    check_geometry("downloads")
    if detailed_capture:
        page.screenshot(path=str(output / (label + "-downloads.png")))
    page.evaluate("kind='worlds';navigate('addons')")
    settle(page)
    check_geometry("worlds")
    if detailed_capture:
        page.screenshot(path=str(output / (label + "-worlds.png")))
    page.evaluate("navigate('builds')")
    settle(page)
    page.screenshot(path=str(output / (label + "-builds.png")))
    page.locator("[data-pack-share]").first.click()
    settle(page)
    check_geometry("publication", True)
    assert page.locator('.community-publish [type="submit"]').is_enabled()
    if detailed_capture:
        page.screenshot(path=str(output / (label + "-publication.png")))
    page.locator("#dialogClose").click()
    page.evaluate("PackHubUI.openCatalog()")
    settle(page)
    check_geometry("packs", True)
    assert page.locator(".pack-card").count() == 12
    assert page.locator(".pack-results").evaluate("el => el.clientHeight >= 24"), "Pack filters collapsed the results viewport"
    if detailed_capture:
        page.screenshot(path=str(output / (label + "-packs.png")))
    page.locator("#dialogClose").click()
    page.evaluate("openModrinth(active(), 'mods')")
    page.wait_for_selector(".catalog-choice")
    settle(page)
    check_geometry("catalog", True)
    assert page.locator(".catalog-context").evaluate("el => el.getBoundingClientRect().top >= document.querySelector('#dialogBody').getBoundingClientRect().top - 1"), "Catalog build context is clipped by the dialog body"
    assert page.locator(".catalog-choice").count() == 12, "Duplicate catalog cards"
    assert "Проверенных: 12" in page.locator(".catalog-pager").inner_text()
    assert page.locator(".catalog-results").evaluate("el => el.clientHeight >= 24"), "Catalog controls collapsed the results viewport"
    page.screenshot(path=str(output / (label + "-catalog.png")))
    page.locator("#dialogClose").click()
    page.evaluate("modal('Большой список', '<div class=stack>' + Array.from({length:100},(_,i)=>'<p>Очень длинное название файла '+i+' '+ 'дополнение'.repeat(20)+'</p>').join('') + '</div>')")
    settle(page)
    check_geometry("large-dialog", True)
    assert page.evaluate("document.querySelector('#dialogBody').scrollHeight > document.querySelector('#dialogBody').clientHeight"), "Large dialog must use internal scrolling"
    if detailed_capture:
        page.screenshot(path=str(output / (label + "-large-dialog.png")))
    page.locator("#dialogClose").click()
    # This is a server fixture snapshot, not a claim of 32 real mod downloads.
    api.job = {**api.job, "kind": "catalog-install", "profileId": "profile-a", "id": "large-batch", "busy": True, "cancellable": True,
               "items": [{"project": "mod-" + str(i), "kind": "mods", "version": "stable-1", "source": "github", "title": "Модификация-" + str(i) + "-" + "длинное-название-" * 8, "status": "queued", "error": ""} for i in range(32)], "reviews": []}
    page.evaluate("tick(true)")
    settle(page)
    page.locator("[data-batch-open]").click()
    settle(page)
    check_geometry("batch", True)
    assert page.locator(".batch-item").count() == 32
    assert page.evaluate("document.querySelector('.batch-items').scrollHeight > document.querySelector('.batch-items').clientHeight"), "Large batch must scroll"
    readable_batch_row(page)
    page.screenshot(path=str(output / (label + "-batch.png")))
    page.locator("[data-batch-hide]").click()
    return 14 - len(geometry_failures), geometry_failures


def interaction_flows(page, api, output, label):
    # The clipped chosen value must not clip the native top-layer option picker.
    page.evaluate("navigate('home')")
    settle(page)
    assert page.locator("#homeBuild").get_attribute("title") == LONG_NAME
    if page.evaluate("CSS.supports('appearance', 'base-select')"):
        page.locator("#homeBuild").click()
        page.locator("#homeBuild option").nth(1).wait_for(state="visible")
        assert page.locator("#homeBuild option").first.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), "Long option text overflows the opened picker"
        page.keyboard.press("Escape")

    # Acknowledging a rendered snapshot must preserve a newer racing event.
    before = page.locator(".notification-bell").bounding_box()
    assert page.locator(".notification-dot").is_visible()
    page.locator(".notification-bell").click()
    settle(page)
    assert api.reads == [1]
    assert api.unread == 1 and page.locator(".notification-dot").is_visible()
    page.locator("[data-notifications-refresh]").click()
    settle(page)
    assert api.reads == [1, 2] and not page.locator(".notification-dot").is_visible()
    page.locator("#dialogClose").click()
    after = page.locator(".notification-bell").bounding_box()
    assert before == after, "Unread dot changed bell geometry"
    page.reload()
    settle(page)
    assert not page.locator(".notification-dot").is_visible(), "Read state lost after browser reload"
    api.notifications.append({"id": 3, "created": 1770000002, "message": "Новая загрузка", "level": "success", "read": False})
    page.evaluate("tick(true)")
    settle(page)
    assert page.locator(".notification-dot").is_visible(), "New event failed to restore indicator"

    page.evaluate("navigate('builds')")
    api.publication_enabled = False
    page.locator("[data-pack-share]").first.click()
    settle(page)
    assert page.locator('.community-publish [type="submit"]').is_disabled(), "Unverified retention allowed publishing"
    api.publication_enabled = True
    page.locator("[data-pack-policy-retry]").click()
    settle(page)
    assert page.locator('.community-publish [type="submit"]').is_enabled()
    page.locator("#dialogClose").click()
    page.evaluate("PackHubUI.openCatalog()")
    settle(page)
    previous_requests = len(api.requests)
    page.locator('[data-pack-filter="loader"]').select_option("quilt")
    settle(page)
    calls = [entry for entry in api.requests[previous_requests:] if entry["path"] == "/api/feature"]
    for action in ("packSearch", "communitySearch"):
        assert any(entry["query"].get("action") == [action] and entry["query"].get("loader") == ["quilt"] for entry in calls), action + " did not receive filter"
    page.locator("#dialogClose").click()

    api.job.update(busy=False, ok=True)
    page.evaluate("tick(true)")
    page.evaluate("openModrinth(active(), 'mods')")
    page.wait_for_selector(".catalog-choice")
    open_select_picker(page, "[data-catalog-source]", output, label + "-catalog-source")
    page.locator("[data-catalog-source]").select_option("github")
    settle(page)
    for index in range(3):
        page.locator("[data-mark]").nth(index).click()
    api.preview_error = "mod-1"
    page.locator("[data-cart-open]").click()
    settle(page)
    assert "Внешний каталог временно недоступен" in page.locator(".catalog-selection-error").inner_text()
    assert "доступные (2)" in page.locator("[data-cart-install]").inner_text()
    page.locator("[data-cart-install]").click()
    settle(page)
    api.preview_error = ""
    assert page.locator(".batch-item").count() == 3
    install = [item for item in api.actions if item["action"] == "installModrinth"][-1]
    assert len(install["items"]) == 3 and any(item["project"] == "mod-1" for item in install["items"]), "Preview failure disappeared from the batch selection"
    assert not install.get("acknowledgement"), "Partial preview reused an acknowledgement for a smaller group"
    assert all(item["source"] == "github" for item in install["items"]), "Source lost from selection"
    assert all(item["version"] == "stable-1" for item in install["items"]), "Verified recommended version was not selected"
    for status in ("checking", "dependencies", "downloading", "installing"):
        api.job["items"][0]["status"] = status
        page.evaluate("tick(true)")
        settle(page)
        assert page.locator(".batch-item").first.get_attribute("data-status") == status
    api.job["items"][0]["status"] = "installed"
    api.job["items"][1].update(status="error", error="Контрольная сумма файла не совпадает")
    api.job["items"][2]["status"] = "installed"
    api.job.update(busy=False, ok=False, message="Установлено 2 из 3")
    page.evaluate("tick(true)")
    settle(page)
    assert "Контрольная сумма" in page.locator(".batch-item-error:visible").inner_text()
    assert "установлено: 2" in page.locator(".batch-summary").inner_text()
    page.screenshot(path=str(output / (label + "-batch-result.png")))
    page.locator("[data-batch-retry='mod-1']").click()
    settle(page)
    retry = [item for item in api.actions if item["action"] == "installModrinth"][-1]
    assert len(retry["items"]) == 1 and retry["items"][0]["project"] == "mod-1", "Retry included successful projects"
    assert page.locator("#dialog > #notice.busy").is_visible(), "Expected a real ongoing job notice beside the cancel action"
    geometry(page, True)
    readable_batch_row(page)
    page.screenshot(path=str(output / (label + "-batch-busy.png")))
    page.locator("[data-batch-cancel]").click()
    settle(page)
    assert page.locator(".batch-item").first.get_attribute("data-status") == "cancelled"

    item = api.job["items"][0]
    item["status"] = "needs_review"
    api.job["reviews"] = [{"request": {"id": "profile-a", "items": [{k: item[k] for k in ("project", "kind", "version", "source")}]}, "token": "explicit-review-token", "warnings": ["Автор не подтвердил совместимость этой зависимости"]}]
    api.job["review"] = api.job["reviews"][0]
    page.evaluate("tick(true)")
    settle(page)
    previous = len(api.actions)
    assert page.locator("[data-batch-review]").is_visible()
    assert len(api.actions) == previous, "Review was accepted automatically"
    page.locator("[data-batch-hide]").click()
    page.evaluate("tick(true)")
    settle(page)
    assert not page.locator("#dialog").evaluate("el => el.open"), "Collapsed review reopened by legacy modal handler"
    page.locator("[data-batch-open]").click()
    page.locator("[data-batch-review]").click()
    settle(page)
    assert api.actions[-1]["acknowledgement"] == "explicit-review-token"

    api.job.update(busy=False, ok=True)
    api.job["items"][0]["status"] = "installed"
    page.evaluate("tick(true)")
    page.locator("[data-batch-hide]").click()
    page.evaluate("openModrinth(active(), 'mods')")
    page.wait_for_selector(".catalog-choice")
    page.locator("[data-catalog-source]").select_option("github")
    settle(page)
    page.locator("[data-project]").first.click()
    settle(page)
    assert page.locator("#catalogVersion").input_value() == "stable-1"
    assert any(entry["path"] == "/api/catalog/check" and entry["query"].get("source") == ["github"] for entry in api.requests), "Single preview lost source"
    page.locator("[data-install-project]").click()
    settle(page)
    assert api.actions[-1]["source"] == "github", "Single installation lost source"
    return 21


def main():
    # Playwright diagnostics include the Russian UI text. Windows redirected
    # stdout defaults to a legacy code page; reporting a failure must not abort
    # the remaining independent cases or obscure the original browser error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/blockharbor-ui-audit"))
    parser.add_argument("--browser", default=next((shutil.which(name) for name in ("chromium", "google-chrome", "chromium-browser") if shutil.which(name)), None))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    css = "\n".join((UI / name).read_text(encoding="utf-8") for name in ("style.css", "experience.css", "build-care.css", "updates.css", "pack-hub.css"))
    report = {"coverage": "Production browser UI with deterministic API fixtures; DPI emulated, native Windows DPI changes and Minecraft launches are outside this test", "platform": sys.platform, "cases": [], "settingsCases": [], "failures": [], "checks": 0}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=args.browser, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        report["browser"] = browser.version
        try:
            for width, height in RESOLUTIONS:
                for scale in SCALES:
                    viewport = {"width": math.floor(width / scale), "height": math.floor(height / scale)}
                    label = f"{width}x{height}-{int(scale * 100)}pct"
                    context = browser.new_context(viewport=viewport, device_scale_factor=scale, color_scheme="dark", reduced_motion="reduce")
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    api = FixtureAPI()
                    case_failed = False
                    geometry_failures = []

                    def record_failure(phase, error):
                        nonlocal case_failed
                        case_failed = True
                        report["failures"].append({"case": label, "phase": phase, "error": str(error), "traceback": traceback.format_exc(), "geometry": list(geometry_failures), "api": list(api.errors), "javascript": list(errors)})
                        page.screenshot(path=str(args.output / (label + "-" + phase + "-FAIL.png")))
                        print(f"FAIL: {label} ({phase}): {error}", flush=True)

                    try:
                        try:
                            controls_fixture(page, css)
                            report["checks"] += 1
                        except Exception as error:
                            record_failure("controls", error)
                        # A fixture failure remains fatal, but must not suppress
                        # independent production-screen coverage and diagnostics.
                        try:
                            passed_checks, geometry_failures = production_views(page, api, args.output, label)
                            report["checks"] += passed_checks
                            if (width, height, scale) in {(1366, 768, 1), (1280, 720, 2)}:
                                report["checks"] += interaction_flows(page, api, args.output, label)
                            assert not geometry_failures, "; ".join(geometry_failures)
                            assert not api.errors, "; ".join(api.errors)
                            assert not errors, "; ".join(errors)
                            observed = page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
                            assert observed == {**viewport, "dpr": scale}, observed
                        except Exception as error:
                            record_failure("production", error)
                        if (width, height, scale) in SETTINGS_CASES:
                            try:
                                passed_checks, settings_failures = settings_flows(page, api, args.output, label, report["settingsCases"])
                                report["checks"] += passed_checks
                                assert not settings_failures, "; ".join(settings_failures)
                                assert not api.errors, "; ".join(api.errors)
                                assert not errors, "; ".join(errors)
                            except Exception as error:
                                record_failure("settings", error)
                        if not case_failed:
                            report["cases"].append({"display": [width, height], "scale": scale, "viewport": observed, "result": "PASS"})
                            print(f"PASS: {label}, CSS viewport {viewport['width']}x{viewport['height']}, production views, dialogs, 32-item batch", flush=True)
                    finally:
                        context.close()
        finally:
            browser.close()
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["failures"]:
        failed_cases = len({entry["case"] for entry in report["failures"]})
        raise SystemExit(f"{failed_cases} browser cases failed; see {args.output / 'report.json'}")
    print(f"PASS: {len(report['cases'])} display/DPI combinations, {report['checks']} scenario checks; {args.output}")


if __name__ == "__main__":
    main()
