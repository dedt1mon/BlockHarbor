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
import traceback
from urllib.parse import parse_qs, urlsplit

from smoke_ui_layout import FIXTURE, UI

RESOLUTIONS = ((1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160))
SCALES = (1, 1.25, 1.5, 2)
ORIGIN = "http://blockharbor.test"
TOKEN = "ui-audit-fixture-token"
LONG_NAME = "Сборка с длинным названием — исследование мира и совместная игра"


class FixtureAPI:
    def __init__(self):
        self.errors = []
        self.actions = []
        self.reads = []
        self.racing_notification = True
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
                "experience": {"preferences": {"scale": 100, "reducedMotion": True}, "notificationUnread": self.unread},
                "notificationUnread": self.unread, "steam": {"supported": True, "installed": True, "canEnable": False, "canContinue": True, "message": "Готово"},
                "skinNetwork": {"supported": True}, "skinSupport": True, "canUseFabricSkin": False, "realName": "", "nickname": "Player", "model": "classic", "skin": False,
                "account": "", "memory": 4096, "autoMemory": True, "distanceSettings": True, "recommendedMemory": 4096, "clientId": "", "versions": ["1.20.1", "1.21.1"],
                "job": {**self.job, "notificationUnread": self.unread}, "mode": "local", "gameRunning": False, "gameExit": None, "localGameRunning": False, "runningProfiles": [], "launcherCount": 1}

    def feature(self, action):
        if action == "experienceNotifications":
            return {"items": copy.deepcopy(list(reversed(self.notifications))), "unread": self.unread, "lastId": max((item["id"] for item in self.notifications), default=0)}
        if action == "experienceState":
            return {"wardrobe": [], "preferences": {"scale": 100, "reducedMotion": True}, "notificationUnread": self.unread}
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
            return {"enabled": True, "message": "Сервер хранит публикацию 60 минут"}
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
                    hits = [{"id": "mod-" + str(i), "title": "Совместимое дополнение " + str(i), "description": "Очень длинное описание " * 20, "source": source, "sourceTitle": "Источник", "author": "Автор", "downloads": 100 + i} for i in range(12)]
                    result = {"hits": [*hits, hits[0]], "offset": 0, "limit": 20, "total": 12}
                elif path == "/api/catalog/check":
                    data = request.post_data_json
                    items = [{**item, "version": item.get("version") or "stable-1"} for item in data["items"]]
                    result = {"items": items, "selected": [{**item, "installed": False} for item in items], "blocked": False, "required": [], "optional": [], "conflicts": [], "warnings": []}
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
        const close = document.querySelector('#dialogClose').getBoundingClientRect();
        if (close.top < r.top || close.bottom > r.bottom || close.right > r.right) errors.push('close control inaccessible');
      }
      return errors;
    }""", dialog)
    assert not errors, "; ".join(errors)


def controls_fixture(page, css):
    page.set_content('<!doctype html><html lang="ru"><meta charset="utf-8"><style>' + css + '</style><body class="reduced-motion">' + FIXTURE + '</body></html>')
    assert page.locator("body").get_attribute("data-layout-result") == "PASS", page.locator("body").get_attribute("data-layout-errors")


def production_views(page, api, output, label):
    page.route("**/*", api.route)
    page.add_init_script("""window.__pendingRequests = 0; const originalFetch = window.fetch;
      window.fetch = async (...args) => { window.__pendingRequests++; try { return await originalFetch(...args); } finally { window.__pendingRequests--; } };""")
    page.goto(ORIGIN + "/#" + TOKEN)
    page.wait_for_function("typeof state !== 'undefined' && state && window.ExperienceUI && window.BatchUI")
    settle(page)
    for destination in ("home", "builds", "friends", "profile", "settings", "gallery", "addons"):
        page.evaluate("destination => navigate(destination)", destination)
        settle(page)
        geometry(page)
    page.evaluate("showSettingsSection('downloads')")
    settle(page)
    geometry(page)
    page.evaluate("kind='worlds';navigate('addons')")
    settle(page)
    geometry(page)
    page.evaluate("navigate('builds')")
    settle(page)
    page.screenshot(path=str(output / (label + "-builds.png")))
    page.evaluate("openModrinth(active(), 'mods')")
    page.wait_for_selector(".catalog-choice")
    settle(page)
    geometry(page, True)
    assert page.locator(".catalog-choice").count() == 12, "Duplicate catalog cards"
    page.screenshot(path=str(output / (label + "-catalog.png")))
    page.locator("#dialogClose").click()
    page.evaluate("modal('Большой список', '<div class=stack>' + Array.from({length:100},(_,i)=>'<p>Очень длинное название файла '+i+' '+ 'дополнение'.repeat(20)+'</p>').join('') + '</div>')")
    settle(page)
    geometry(page, True)
    assert page.evaluate("document.querySelector('#dialogBody').scrollHeight > document.querySelector('#dialogBody').clientHeight"), "Large dialog must use internal scrolling"
    page.locator("#dialogClose").click()
    # This is a server fixture snapshot, not a claim of 32 real mod downloads.
    api.job = {**api.job, "kind": "catalog-install", "profileId": "profile-a", "id": "large-batch", "busy": True, "cancellable": True,
               "items": [{"project": "mod-" + str(i), "kind": "mods", "version": "stable-1", "source": "github", "title": "Модификация-" + str(i) + "-" + "длинное-название-" * 8, "status": "queued", "error": ""} for i in range(32)], "reviews": []}
    page.evaluate("tick(true)")
    settle(page)
    page.locator("[data-batch-open]").click()
    settle(page)
    geometry(page, True)
    assert page.locator(".batch-item").count() == 32
    assert page.evaluate("document.querySelector('.batch-items').scrollHeight > document.querySelector('.batch-items').clientHeight"), "Large batch must scroll"
    page.screenshot(path=str(output / (label + "-batch.png")))
    page.locator("[data-batch-hide]").click()
    return 12


def interaction_flows(page, api):
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

    api.job.update(busy=False, ok=True)
    page.evaluate("tick(true)")
    page.evaluate("openModrinth(active(), 'mods')")
    page.wait_for_selector(".catalog-choice")
    page.locator("[data-catalog-source]").select_option("github")
    settle(page)
    for index in range(3):
        page.locator("[data-mark]").nth(index).click()
    page.locator("[data-cart-open]").click()
    settle(page)
    page.locator("[data-cart-install]").click()
    settle(page)
    assert page.locator(".batch-item").count() == 3
    install = [item for item in api.actions if item["action"] == "installModrinth"][-1]
    assert all(item["source"] == "github" for item in install["items"]), "Source lost from selection"
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
    page.locator("[data-batch-retry='mod-1']").click()
    settle(page)
    retry = [item for item in api.actions if item["action"] == "installModrinth"][-1]
    assert len(retry["items"]) == 1 and retry["items"][0]["project"] == "mod-1", "Retry included successful projects"
    page.locator("[data-batch-cancel]").click()
    settle(page)
    assert page.locator(".batch-item").first.get_attribute("data-status") == "cancelled"

    item = api.job["items"][0]
    item["status"] = "needs_review"
    api.job["reviews"] = [{"request": {"id": "profile-a", "items": [{k: item[k] for k in ("project", "kind", "version", "source")}]}, "token": "explicit-review-token", "warnings": ["Автор не подтвердил совместимость этой зависимости"]}]
    page.evaluate("tick(true)")
    settle(page)
    previous = len(api.actions)
    assert page.locator("[data-batch-review]").is_visible()
    assert len(api.actions) == previous, "Review was accepted automatically"
    page.locator("[data-batch-review]").click()
    settle(page)
    assert api.actions[-1]["acknowledgement"] == "explicit-review-token"
    return 14


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/blockharbor-ui-audit"))
    parser.add_argument("--browser", default=next((shutil.which(name) for name in ("chromium", "google-chrome", "chromium-browser") if shutil.which(name)), None))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    css = "\n".join((UI / name).read_text(encoding="utf-8") for name in ("style.css", "experience.css", "build-care.css", "updates.css", "pack-hub.css"))
    report = {"coverage": "Production browser UI with deterministic API fixtures; DPI emulated, native Windows and Minecraft launches are outside this test", "cases": [], "failures": [], "checks": 0}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=args.browser, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
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
                    try:
                        controls_fixture(page, css)
                        report["checks"] += 1
                        report["checks"] += production_views(page, api, args.output, label)
                        if (width, height, scale) in {(1366, 768, 1), (1280, 720, 2)}:
                            report["checks"] += interaction_flows(page, api)
                        assert not api.errors, "; ".join(api.errors)
                        assert not errors, "; ".join(errors)
                        observed = page.evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
                        assert observed == {**viewport, "dpr": scale}, observed
                        report["cases"].append({"display": [width, height], "scale": scale, "viewport": observed, "result": "PASS"})
                        print(f"PASS: {label}, CSS viewport {viewport['width']}x{viewport['height']}, production views, dialogs, 32-item batch", flush=True)
                    except Exception as error:
                        report["failures"].append({"case": label, "error": str(error), "traceback": traceback.format_exc(), "api": api.errors, "javascript": errors})
                        page.screenshot(path=str(args.output / (label + "-FAIL.png")))
                        print(f"FAIL: {label}: {error}", flush=True)
                    finally:
                        context.close()
        finally:
            browser.close()
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["failures"]:
        raise SystemExit(f"{len(report['failures'])} browser cases failed; see {args.output / 'report.json'}")
    print(f"PASS: {len(report['cases'])} display/DPI combinations, {report['checks']} scenario checks; {args.output}")


if __name__ == "__main__":
    main()
