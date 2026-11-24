"""One focused pass over the changed UX, at desktop and compact widths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlsplit

from smoke_ui_browser import FixtureAPI, ORIGIN, TOKEN, geometry, settle


class RefinementAPI(FixtureAPI):
    def __init__(self):
        super().__init__()
        self.racing_notification = False

    def feature(self, action):
        if action == "packVersions":
            return {"title": "Небольшая сборка", "description": "Короткое описание выпуска.",
                    "versions": [{"id": "pack-release", "number": "1.0", "name": "1.0", "type": "release",
                                  "minecraft": ["1.20.1"], "loaders": ["fabric"]}]}
        if action == "diagnostics":
            return {"notice": "Проверка завершена", "findings": [], "empty": False, "report": "Проверка сборки\n" * 70}
        result = super().feature(action)
        if action == "updateState":
            result["current"] = "2.6.10"
            result["transfers"]["tasks"] = [
                {"id": str(i), "title": "Дополнение " + str(i), "state": "completed", "total": 1024,
                 "bytes": 1024, "host": "Каталог", "message": "Проверено"} for i in range(8)]
        if action == "experienceGallery":
            result["trash"] = [{"id": "batch-" + str(i), "token": "trash-token-" + str(i),
                                "created": 1770000000 + i, "count": 2, "size": 2048,
                                "names": ["Снимок 1.png", "Снимок 2.png"]} for i in range(18)]
        if action == "careState":
            result["snapshots"] = [{"id": "point-" + str(i), "label": "Точка восстановления " + str(i),
                                    "scope": "build", "world": "", "created": 1770000000 + i, "size": 1024}
                                   for i in range(18)]
        return result

    def route(self, route):
        url = urlsplit(route.request.url)
        query = parse_qs(url.query)
        if url.path == "/api/releases":
            route.fulfill(json={"versions": ["1.20.1"], "allVersions": [{"id": "1.20.1", "type": "release"}]})
        elif url.path == "/api/loaders":
            route.fulfill(json={"loaders": [{"id": k, "supported": True, "message": "Готово"}
                                           for k in ("vanilla", "fabric", "forge", "neoforge", "quilt")]})
        elif url.path == "/api/catalog/search":
            route.fulfill(json={"hits": [{"id": "installed", "title": "Уже установлен", "description": "Этот файл есть в сборке.",
                                         "installed": True, "installedVersion": "stable-1", "recommendedVersion": "stable-1",
                                         "source": "community", "author": "Автор", "downloads": 100},
                                        {"id": "available", "title": "Доступное дополнение", "description": "Короткое описание.",
                                         "installed": False, "recommendedVersion": "stable-1", "source": "community", "author": "Автор", "downloads": 100}],
                                "offset": 0, "limit": 12, "total": 24, "verified": True, "totalIsEstimate": True})
        elif url.path == "/api/catalog/versions":
            route.fulfill(json={"versions": [{"id": "stable-1", "number": "1.0", "name": "1.0", "type": "release",
                                              "published": "2026-11-24T00:00:00Z", "filename": "example.jar", "size": 1024,
                                              "installed": query.get("project") == ["installed"]}]})
        else:
            super().route(route)


def check_layout(page, *, dialog=False):
    settle(page)
    errors = geometry(page, dialog)
    assert not errors, "; ".join(errors)


def header_action(page, label):
    action = page.locator("#dialogHeaderActions").get_by_role("button", name=label, exact=False)
    assert action.count() == 1 and action.is_visible(), label + " is missing from the dialog header"
    check_layout(page, dialog=True)
    return action


def main():
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/ui-refinements"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    executable = next((shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser") if shutil.which(name)), None)
    assert executable, "Chromium is required"
    report = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable, headless=True,
                                         args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            for width, height in ((1440, 900), (820, 700)):
                api = RefinementAPI()
                context = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("""window.__pendingRequests = 0;
                  const originalFetch = window.fetch;
                  window.fetch = async (...args) => { window.__pendingRequests++;
                    try { return await originalFetch(...args); } finally { window.__pendingRequests--; } };""")
                page.route("**/*", api.route)
                try:
                    page.goto(ORIGIN + "/#" + TOKEN)
                    page.wait_for_function("typeof state !== 'undefined' && state && window.ExperienceUI")
                    page.locator('aside [data-view="settings"]').click()
                    page.locator("#settingsDownloadsTab").click()
                    page.wait_for_function("!document.querySelector('#updateChannel').disabled")
                    check_layout(page)
                    cards = page.locator(".update-overview-grid > *")
                    assert cards.count() == 3
                    if width == 1440:
                        boxes = [cards.nth(i).bounding_box() for i in range(3)]
                        assert max(b["y"] for b in boxes) - min(b["y"] for b in boxes) < 2
                    history = page.locator("[data-transfer-history]")
                    assert history.evaluate("el => el.nextElementSibling.dataset.updatesAction") == "transferClear"
                    history.click()
                    assert page.locator("#transferHistory .transfer-row").count() == 4
                    history.click()
                    page.screenshot(path=str(args.output / f"{width}-updates.png"))

                    page.locator('aside [data-view="profile"]').click()
                    check_layout(page)
                    assert page.locator(".profile-panels > .panel").count() == 2
                    assert page.locator('[data-act="pickSkin"]').evaluate("el => el.nextElementSibling.dataset.act") == "saveProfile"
                    page.locator('aside [data-view="friends"]').click()
                    check_layout(page)
                    assert page.locator(".friends-panels > .friends-join").count() == 1

                    page.locator('[data-experience="notifications"]').click()
                    header_action(page, "Очистить")
                    page.locator("#dialogClose").click()
                    page.locator('aside [data-view="gallery"]').click()
                    page.locator("[data-gallery-all]").check()
                    page.locator('[data-experience="deleteSelectedScreenshots"]').click()
                    header_action(page, "Удалить")
                    assert not page.locator("#dialogBody").get_by_role("button", name="Отмена").count()
                    page.locator("#dialogClose").click()
                    page.locator('[data-experience="screenshotTrash"]').click()
                    page.wait_for_selector(".gallery-trash-batch")
                    check_layout(page, dialog=True)
                    assert page.get_by_role("button", name="Удалить окончательно", exact=True).count()
                    scroller = page.locator(".gallery-trash")
                    assert scroller.evaluate("el => { el.scrollTop = el.scrollHeight; return el.scrollTop > 0; }")
                    page.locator("#dialogClose").click()

                    page.locator('aside [data-view="builds"]').click()
                    page.locator('[data-act="create"]').click()
                    create = header_action(page, "Создать сборку")
                    page.wait_for_function("!document.querySelector('[name=createBuildSubmit]').disabled")
                    assert create.evaluate("el => el.form?.id") == "newForm"
                    page.locator("#dialogClose").click()
                    page.locator("[data-export-build]").first.click()
                    header_action(page, "Скачать ZIP")
                    page.locator("#dialogClose").click()
                    page.locator("[data-remove-build]").first.click()
                    header_action(page, "Удалить сборку")
                    page.locator("#dialogClose").click()
                    page.locator("[data-pack-share]").first.click()
                    page.wait_for_function("document.querySelector('#communityPublishSubmit') && !document.querySelector('#communityPublishSubmit').disabled")
                    header_action(page, "Опубликовать")
                    page.locator("#dialogClose").click()
                    page.locator('[data-act="packCatalog"]').click()
                    page.wait_for_selector("[data-pack-project]")
                    check_layout(page, dialog=True)
                    pager_y = page.locator(".pack-pagination").bounding_box()["y"]
                    page.locator(".pack-results").evaluate("el => el.scrollTop = el.scrollHeight")
                    assert abs(page.locator(".pack-pagination").bounding_box()["y"] - pager_y) < 1
                    page.locator("[data-pack-project]").first.click()
                    page.wait_for_selector("[data-pack-version]")
                    header_action(page, "Подготовить установку")
                    header_action(page, "К каталогу")
                    assert page.locator("#dialog").bounding_box()["height"] < height * .85
                    page.screenshot(path=str(args.output / f"{width}-release.png"))
                    page.locator("#dialogClose").click()

                    page.locator("[data-build-settings]").first.click()
                    for category in ("mods", "shaderpacks", "resourcepacks"):
                        page.locator(f'[data-kind="{category}"]').click()
                        page.locator('[data-act="openCatalog"]').click()
                        page.wait_for_selector('[data-mark="installed"]')
                        assert page.locator('[data-mark="installed"]').is_disabled()
                        assert not page.locator(".catalog-search select").count()
                        page.locator('[data-project="installed"]').click()
                        page.wait_for_selector("#catalogVersion")
                        check_layout(page, dialog=True)
                        assert page.locator("#dialogHeaderActions [data-cart-add]").is_disabled()
                        assert page.locator("#dialogHeaderActions [data-install-project]").is_disabled()
                        page.locator("[data-catalog-back]").click()
                        page.locator('[data-project="available"]').click()
                        page.wait_for_selector("#catalogVersion")
                        header_action(page, "Добавить в выбранное")
                        header_action(page, "Установить с зависимостями")
                        assert page.locator("#dialog").bounding_box()["height"] < height * .85
                        if category == "mods":
                            page.screenshot(path=str(args.output / f"{width}-addon.png"))
                        page.locator("#dialogClose").click()
                    page.locator('[data-kind="worlds"]').click()
                    check_layout(page)
                    assert not page.locator("[data-care-auto-backup]").count()
                    assert page.locator('[data-care="backup-all"]').evaluate("el => el.nextElementSibling.dataset.care") == "history"
                    page.locator('[data-care="history"]').click()
                    header_action(page, "Создать точку")
                    page.locator("[data-care-restore]").first.click()
                    header_action(page, "Восстановить")
                    page.locator("#dialogClose").click()
                    page.locator('[data-kind="errors"]').click()
                    page.locator('.build-errors-heading [data-experience="diagnostics"]').click()
                    page.locator('[data-experience="report"]').click()
                    check_layout(page, dialog=True)
                    assert page.locator("#dialog").bounding_box()["width"] >= min(1000, width - 40)
                    page.screenshot(path=str(args.output / f"{width}-report.png"))
                    page.locator("#dialogClose").click()
                    assert not errors, "; ".join(errors)
                    assert not api.errors, "; ".join(api.errors)
                    report.append({"viewport": [width, height], "result": "PASS"})
                    print(f"PASS: changed screens and dialog actions at {width}x{height}", flush=True)
                except Exception:
                    page.screenshot(path=str(args.output / f"{width}-failure.png"))
                    (args.output / f"{width}-failure.html").write_text(page.content(), encoding="utf-8")
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
            (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
