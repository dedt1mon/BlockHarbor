"""Focused 2.6.14 UX checks, using production UI and a stateful local API fixture."""
from __future__ import annotations

import argparse
import copy
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import shutil
import sys
import threading
import traceback
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zipfile

from smoke_ui_browser import FixtureAPI, TOKEN, geometry, settle


class RefinementAPI(FixtureAPI):
    def __init__(self):
        super().__init__()
        self.racing_notification = False
        self.profile["name"] = "Экспедиция друзей — Minecraft"
        self.profile["description"] = "Совместная игра и исследование мира."
        self.profiles = [copy.deepcopy(self.profile),
                         {**self.profile, "id": "profile-b", "name": "Вторая сборка"}]
        self.deleted = []
        self.active = "profile-a"
        self.images = super().feature("experienceGallery")["images"][:3]
        self.trash = []
        self.trash_images = {}
        self.trash_counter = 0
        self.exports = []
        self.downloads = {}
        self.download_receipts = []
        self.pending_job = None
        self.steam_supported = True
        self.snapshots = [
            {"id": "copy-a", "label": "Перед изменением модов", "scope": "build", "world": "", "created": 1770000000, "size": 2048},
            {"id": "copy-b", "label": "Копия мира после игры", "scope": "world", "world": "Мой мир", "created": 1770000001, "size": 4096},
        ]
        self.snapshot_delete_fail_once = True
        self.build_error_rows = {"profile-a": [{"id": 1, "evidence": "Старая ошибка сборки"}],
                                 "profile-b": [{"id": 2, "evidence": "Ошибка другой сборки"}]}
        self.racing_build_error = True
        self.publications = {"profile-b": [
            {"build": "BH-0123456789AB", "name": "Вторая сборка", "expiresAt": 9999999999}]}

    def state(self):
        data = super().state()
        data.update(appVersion="2.6.14", profiles=copy.deepcopy(self.profiles),
                    deletedProfiles=copy.deepcopy(self.deleted), active=self.active)
        return data

    def feature(self, action, query=None):
        if action == "buildErrors":
            pid = (query or {}).get("id", [""])[0]
            rows = self.build_error_rows.get(pid, [])
            return {"lastId": max((row["id"] for row in rows), default=0), "limit": 50,
                    "groups": [{"title": "Не удалось получить файл", "advice": "Повтори загрузку.",
                                "events": [{"created": 1770000000 + row["id"], "stage": "Скачивание", "evidence": row["evidence"]}
                                           for row in reversed(rows)]}] if rows else []}
        if action == "packVersions":
            return {"title": "Небольшая сборка", "description": "Короткое описание выпуска.",
                    "versions": [{"id": "pack-release", "number": "1.0", "name": "1.0", "type": "release",
                                  "minecraft": ["1.20.1"], "loaders": ["fabric"]}]}
        if action == "experienceGallery":
            return {"images": copy.deepcopy(self.images), "trash": copy.deepcopy(self.trash),
                    "total": len(self.images), "limit": 200}
        if action == "communityPublication":
            pid = (query or {}).get("id", [""])[0]
            return {"publications": copy.deepcopy(self.publications.get(pid, []))}
        result = super().feature(action)
        if action == "experienceState":
            result["wardrobe"] = [{"id": "skin-a", "name": "Исследователь", "model": "classic", "preview": "a" * 64},
                                  {"id": "skin-b", "name": "Строитель", "model": "slim", "preview": "b" * 64}]
        if action == "careState":
            result["snapshots"] = copy.deepcopy(self.snapshots)
        if action == "updateState":
            result.update(current="2.6.14", rollback={"version": "2.6.13"})
        return result

    def start_job(self, action, data):
        assert not self.job["busy"], "Fixture received a second background job while busy"
        self.job_counter += 1
        self.pending_job = (action, copy.deepcopy(data))
        self.job = {**self.job, "id": "ux-job-" + str(self.job_counter), "kind": action,
                    "busy": True, "ok": False, "message": "Выполняется операция…"}
        return {"job": self.job["id"]}

    def finish_job(self):
        assert self.pending_job, "No pending fixture job"
        action, data = self.pending_job
        if action == "purgeBuild":
            self.deleted = [row for row in self.deleted if row["id"] != data["id"]]
        elif action == "communityUnpublish":
            self.publications[data["id"]] = [
                row for row in self.publications.get(data["id"], []) if row["build"] != data["build"]]
        elif action == "communityInstall":
            self.profiles.append({**self.profile, "id": "community-installed", "name": "Сборка игрока"})
        elif action == "packInstall":
            self.profiles.append({**self.profile, "id": "pack-installed", "name": data["name"]})
        elif action == "communityPublish":
            self.publications.setdefault(data["id"], []).append({"build": "BH-FEDCBA987654", "name": data["name"], "expiresAt": 9999999999})
        self.pending_job = None
        self.job.update(busy=False, ok=True, message="Операция завершена.")

    def action(self, data):
        action = data["action"]
        custom = {"create", "deleteBuild", "restoreBuild", "purgeBuild", "communityInstall", "communityUnpublish", "communityPublish", "careDeleteSnapshot",
                  "packPreview", "packInstall", "experienceClearBuildErrors",
                  "experienceDeleteScreenshots", "experienceRestoreScreenshots", "experiencePurgeScreenshots"}
        if action not in custom:
            return super().action(data)
        self.actions.append(copy.deepcopy(data))
        if action == "experienceClearBuildErrors":
            assert data["id"] == self.active and type(data["throughId"]) is int
            if self.racing_build_error:
                self.racing_build_error = False
                self.build_error_rows[data["id"]].append({"id": 3, "evidence": "Новая ошибка после открытия истории"})
            rows = self.build_error_rows[data["id"]]
            remaining = [row for row in rows if row["id"] > data["throughId"]]
            self.build_error_rows[data["id"]] = remaining
            return {"ok": True, "cleared": len(rows) - len(remaining), "message": "История ошибок сборки очищена."}
        if action == "packPreview":
            assert data["project"].startswith("pack-") and data["version"] == "pack-release"
            return {"token": "preview-token", "name": "Небольшая сборка"}
        if action == "packInstall":
            assert data["token"] == "preview-token" and data["name"] == "Небольшая сборка"
            assert data["allowWithoutSteam"] is True and not data.get("optional")
            return self.start_job(action, data)
        if action == "create":
            assert data["allowWithoutSteam"] is True, "Creation must use the visible inline Steam notice"
            self.active = "profile-created"
            self.profiles.append({**self.profile, "id": self.active, "name": data["name"],
                                  "version": data["version"], "loader": data["loader"]})
        elif action == "careDeleteSnapshot":
            assert data["id"] == self.active
            row = next(row for row in self.snapshots if row["id"] == data["snapshot"])
            self.snapshots.remove(row)
            return {"ok": True, "message": "Резервная копия удалена."}
        elif action == "deleteBuild":
            row = next(row for row in self.profiles if row["id"] == data["id"])
            self.profiles.remove(row)
            self.deleted.append({**row, "purging": False})
            if self.active == row["id"]:
                self.active = self.profiles[0]["id"] if self.profiles else ""
        elif action == "restoreBuild":
            row = next(row for row in self.deleted if row["id"] == data["id"])
            assert not row["purging"], "Restored a build while its files were being removed"
            self.deleted.remove(row)
            row = {key: value for key, value in row.items() if key != "purging"}
            self.profiles.append(row)
        elif action == "purgeBuild":
            row = next(row for row in self.deleted if row["id"] == data["id"])
            row["purging"] = True
            return self.start_job(action, data)
        elif action == "communityUnpublish":
            assert any(row["build"] == data["build"] for row in self.publications.get(data["id"], []))
            return self.start_job(action, data)
        elif action == "communityPublish":
            assert data["name"] and data["category"], "Publishing lost required form values"
            return self.start_job(action, data)
        elif action == "communityInstall":
            assert data["allowWithoutSteam"] is False, "Supported Steam unexpectedly bypassed compatibility"
            return self.start_job(action, data)
        elif action == "experienceDeleteScreenshots":
            assert data["id"] == self.active and data["images"]
            selected = []
            for image in data["images"]:
                row = next(row for row in self.images if row["name"] == image["name"])
                assert image["token"] == row["token"], "Deletion lost the visible screenshot token"
                selected.append(row)
            for row in selected:
                self.images.remove(row)
            self.trash_counter += 1
            batch = "trash-" + str(self.trash_counter)
            self.trash.append({"id": batch, "token": batch + "-token", "created": 1770000000,
                               "count": len(selected), "size": sum(row["size"] for row in selected),
                               "names": [row["name"] for row in selected]})
            self.trash_images[batch] = selected
        else:
            assert data["id"] == self.active
            batch = next(row for row in self.trash if row["id"] == data["batch"])
            if action == "experiencePurgeScreenshots":
                assert data["token"] == batch["token"], "Purge lost the visible trash-row token"
            else:
                self.images.extend({**row, "token": row["token"] + "-restored"}
                                   for row in self.trash_images[batch["id"]])
            self.trash.remove(batch)
            del self.trash_images[batch["id"]]
        return {"ok": True, "message": "Изменения сохранены.", "skipped": []}

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        query = parse_qs(url.query)
        if url.path == "/api/action" and request.post_data_json.get("action") == "careDeleteSnapshot" and self.snapshot_delete_fail_once:
            assert request.headers.get("authorization") == "Bearer " + TOKEN
            self.snapshot_delete_fail_once = False
            self.actions.append(copy.deepcopy(request.post_data_json))
            self.requests.append({"path": url.path, "query": query})
            route.fulfill(status=409, json={"error": "Копия используется другой операцией."})
            return
        custom = {"/api/feature", "/api/releases", "/api/loaders",
                  "/api/steam/compatibility", "/api/build-export"}
        download = url.path.startswith("/api/build-download/")
        if url.path not in custom and not download:
            return super().route(route)
        self.requests.append({"path": url.path, "query": query})
        try:
            assert url.netloc == urlsplit(self.origin).netloc, "Unexpected external request: " + request.url
            response_headers = {}
            if download:
                # Browser attachment navigation uses the issued ticket and cookie,
                # while API fetches carry the Authorization header.
                name, archive, expected_cookie = self.downloads[url.path]
                assert request.method == "GET"
                cookie = SimpleCookie(request.headers.get("cookie", "")).get("bh_export")
                assert cookie and cookie.value == expected_cookie, "Download lost its export cookie"
                self.download_receipts.append({"path": url.path, "name": name,
                                               "bytes": len(archive), "cookieValidated": True})
                route.fulfill(body=archive, content_type="application/zip",
                              headers={"Content-Disposition": 'attachment; filename="' + name + '"'})
                return
            assert request.headers.get("authorization") == "Bearer " + TOKEN
            if url.path == "/api/feature":
                result = self.feature(query["action"][0], query)
            elif url.path == "/api/releases":
                result = {"versions": ["1.20.1"], "allVersions": [{"id": "1.20.1", "type": "release"}]}
            elif url.path == "/api/loaders":
                result = {"version": query["version"][0],
                          "loaders": [{"id": name, "supported": True, "message": "Готово"}
                                      for name in ("vanilla", "fabric", "forge", "neoforge", "quilt")]}
            elif url.path == "/api/steam/compatibility":
                result = {"supported": self.steam_supported, "acknowledged": self.steam_supported, "variants": [],
                          "version": query.get("version", [""])[0], "loader": query.get("loader", [""])[0]}
            else:
                assert request.method == "POST"
                payload = request.post_data_json
                assert payload["worlds"] is False and any(row["id"] == payload["id"] for row in self.profiles)
                self.exports.append(copy.deepcopy(payload))
                name = "blockharbor-" + payload["id"] + ".zip"
                ticket = "/api/build-download/export-" + str(len(self.exports))
                contents = io.BytesIO()
                with zipfile.ZipFile(contents, "w") as archive:
                    archive.writestr("fixture.json", json.dumps(payload))
                cookie = "fixture-export-" + str(len(self.exports))
                self.downloads[ticket] = (name, contents.getvalue(), cookie)
                response_headers["Set-Cookie"] = "bh_export=" + cookie + "; Path=" + ticket + "; Max-Age=300; HttpOnly; SameSite=Strict"
                result = {"download": ticket, "name": name}
            route.fulfill(json=result, headers=response_headers)
        except Exception as error:
            self.errors.append(str(error))
            route.fulfill(status=500, json={"error": str(error)})


class LoopbackFixture:
    """Serve the fixture through HTTP, including genuine attachment responses.

    Avoid route.fulfill's attachment interception: the production app also
    serves its downloads from a loopback HTTP server.
    """
    def __init__(self, api):
        encode_json = json.dumps

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_fixture(self):
                if self.headers.get("Host") != urlsplit(api.origin).netloc:
                    self.send_error(403)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request = SimpleNamespace(url=api.origin + self.path, method=self.command,
                                          headers={key.lower(): value for key, value in self.headers.items()},
                                          post_data_json=json.loads(body) if body else None)
                handler = self

                class Response:
                    def fulfill(self, *, status=200, body=b"", json=None, content_type=None, headers=None):
                        if json is not None:
                            body = encode_json(json, ensure_ascii=False).encode("utf-8")
                            content_type = "application/json; charset=utf-8"
                        elif isinstance(body, str):
                            body = body.encode("utf-8")
                        handler.send_response(status)
                        handler.send_header("Content-Type", content_type or "application/octet-stream")
                        handler.send_header("Content-Length", str(len(body)))
                        for key, value in (headers or {}).items():
                            handler.send_header(key, value)
                        handler.end_headers()
                        handler.wfile.write(body)

                response = Response()
                response.request = request
                api.route(response)

            do_GET = handle_fixture
            do_POST = handle_fixture

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        api.origin = self.origin
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


def check_layout(page, *, dialog=False):
    settle(page)
    geometry(page, dialog)


def navigate(page, view, ready):
    page.locator('aside [data-view="' + view + '"]').click()
    page.locator(ready).first.wait_for(state="visible")
    settle(page)


def last_action(api, action, **expected):
    rows = [row for row in api.actions if row["action"] == action]
    assert rows, "No recorded action: " + action
    row = rows[-1]
    for key, value in expected.items():
        assert row.get(key) == value, (key, row)
    return row


def no_confirmation(page):
    assert not page.locator("#dialog").evaluate("el => el.open"), "Direct action opened a confirmation dialog"


def complete_job(page, api):
    page.wait_for_function("!activePoll")
    api.finish_job()
    page.evaluate("tick(true)")
    settle(page)
    page.wait_for_function("state.job && !state.job.busy")


def open_archive(page):
    box = page.locator(".deleted-builds")
    box.wait_for(state="visible")
    if not box.evaluate("el => el.open"):
        box.locator("summary").click()


def header_metrics(page, dialog="#dialog"):
    top = page.locator(dialog + " > .dialog-top")
    top.wait_for(state="visible")
    settle(page)
    result = top.evaluate("""top => {
      const title = top.querySelector('h2'), close = top.querySelector(':scope > .icon-button');
      const controls = [...top.querySelectorAll('.dialog-header-actions .btn')].filter(el => !el.hidden);
      const row = [title, ...controls, close].map(el => {
        const r = el.getBoundingClientRect();
        return {text: el.textContent.trim(), y: r.y, height: r.height, centerY: r.y + r.height / 2};
      });
      const centers = row.map(item => item.centerY), c = close.getBoundingClientRect(), t = top.getBoundingClientRect();
      const hit = document.elementFromPoint(c.x + c.width / 2, c.y + c.height / 2);
      return {row, maxCenterOffset: Math.max(...centers) - Math.min(...centers),
        scrollWidth: top.scrollWidth, clientWidth: top.clientWidth,
        closeAccessible: c.left >= t.left - 1 && c.right <= t.right + 1 && hit?.closest('button') === close};
    }""")
    assert result["maxCenterOffset"] <= 1, result
    assert result["closeAccessible"], result
    return result


def row_metrics(container, selector):
    result = container.evaluate("""(host, selector) => {
      const elements = [...host.querySelectorAll(selector)].filter(el => el.getClientRects().length);
      return elements.map(el => { const r = el.getBoundingClientRect();
        return {text:el.textContent.trim(), x:r.x, y:r.y, width:r.width, height:r.height, centerY:r.y+r.height/2}; });
    }""", selector)
    assert len(result) >= 2, result
    assert max(item["centerY"] for item in result) - min(item["centerY"] for item in result) <= 1, result
    assert all(a["x"] + a["width"] <= b["x"] + 1 for a, b in zip(result, result[1:])), result
    return result


def effective_background(locator):
    return locator.evaluate("""el => {
      for (let node=el; node; node=node.parentElement) {
        const value=getComputedStyle(node).backgroundColor;
        if(value !== 'rgba(0, 0, 0, 0)' && value !== 'transparent') return value;
      }
      return 'transparent';
    }""")


def notice_in_history(page, api, message, level):
    page.wait_for_function("!savingNotice && pendingNotices.length === 0")
    assert any(row["message"] == message and row["level"] == level for row in api.notifications), api.notifications
    assert page.locator("#notice").is_hidden(), "Operation outcome appeared as a popup"


def check_screens(page, api, output, width, height, case):
    case["stage"] = "updates"
    navigate(page, "settings", "#settingsDownloadsTab")
    page.locator("#settingsDownloadsTab").click()
    page.wait_for_function("document.querySelector('#automaticUpdates') && !document.querySelector('#automaticUpdates').disabled")
    check_layout(page)
    assert page.locator("#updatesScreen > .panel:not(.transfer-panel)").count() == 1
    assert page.locator("#updateChannel, .updater-channel, .updater-current").count() == 0
    assert "Установлена" not in page.locator("#updatesScreen").inner_text()
    assert page.locator('[data-updates-action="rollbackDialog"]').count() == 0
    assert page.locator(".updater-automatic #updateRelease").count() == 1
    assert page.locator('[data-updates-action="updateCheck"]').is_visible()
    assert "SHA-256" not in page.locator("#updatesScreen").inner_text()
    assert "Подписи издателя" not in page.locator("#updatesScreen").inner_text()
    page.screenshot(path=str(output / f"{width}-updates.png"))

    case["stage"] = "support"
    tabs = page.locator(".settings-tabs [data-settings-section]")
    assert tabs.evaluate_all("els => els.map(el => el.dataset.settingsSection)") == ["general", "downloads", "support"]
    page.locator('[data-settings-section="support"]').click()
    amount = page.locator("#supportAmount")
    amount.wait_for(state="visible")
    assert page.locator("#settingsPanel").get_attribute("aria-labelledby") == "settingsSupportTab"
    assert page.locator("#settingsSupportTab").get_attribute("aria-selected") == "true"
    assert amount.input_value() == "100" and amount.get_attribute("min") == "50"
    amount.fill("49")
    assert amount.evaluate("el => el.validity.rangeUnderflow && !el.checkValidity()")
    amount.fill("175")
    assert amount.evaluate("el => el.checkValidity()"), "A custom donation amount was rejected"
    page.locator('[data-support-amount="50"]').click()
    assert amount.input_value() == "50"
    assert page.locator('[data-support-amount="50"]').get_attribute("aria-pressed") == "true"
    card = page.locator("#supportCard")
    assert card.input_value() == "2200 2418 7795 1305" and card.evaluate("el => el.readOnly")
    assert page.locator('[data-act="copySupportCard"]').is_visible()
    for href in ("https://t.me/Ded_Timon", "https://dedt1mon.github.io/"):
        assert page.locator('#settingsPanel a[href="' + href + '"]').is_visible()
    case["supportPresets"] = row_metrics(page.locator(".support-presets"), ":scope > button")
    check_layout(page)
    page.screenshot(path=str(output / f"{width}-support.png"))
    page.locator('[data-settings-section="general"]').click()
    page.locator("#mode").wait_for(state="visible")
    assert page.locator("#supportAmount").count() == 0

    case["stage"] = "profile-friends"
    navigate(page, "profile", ".profile-panels")
    panels = page.locator(".profile-panels > .panel")
    assert panels.count() == 2
    character, wardrobe = (panels.nth(i).bounding_box() for i in range(2))
    assert wardrobe["y"] >= character["y"] + character["height"], (character, wardrobe)
    assert abs(wardrobe["x"] - character["x"]) <= 1
    page.locator(".wardrobe-card").first.wait_for(state="visible")
    heading = page.locator(".wardrobe-heading")
    case["wardrobeHeading"] = row_metrics(heading, ":scope > h3, :scope > button")
    assert heading.evaluate("""el => {const card=el.closest('.panel'), c=card.getBoundingClientRect(), s=getComputedStyle(card);
      return Math.abs(el.getBoundingClientRect().top - c.top - parseFloat(s.paddingTop) - parseFloat(s.borderTopWidth)) <= 1;}"""), "Wardrobe heading has extra top spacing"
    for buttons in page.locator(".wardrobe-actions").all():
        metrics = row_metrics(buttons, ":scope > button")
        assert [row["text"] for row in metrics] == ["Надеть", "Удалить"], metrics
        assert abs(metrics[0]["height"] - metrics[1]["height"]) <= 1, metrics
    check_layout(page)
    page.locator(".profile-panels").screenshot(path=str(output / f"{width}-profile.png"))
    navigate(page, "friends", ".friends-panels")
    assert page.locator('.friends-panels [data-act="checkSteam"]').count() == 0
    steam_download = page.locator('.steam-actions a[href="https://store.steampowered.com/about/"]')
    assert steam_download.is_visible() and steam_download.inner_text().strip() == "Скачать Steam"
    cards = page.locator(".friends-panels > .panel:not(.friends-join)")
    first, second = (cards.nth(i).bounding_box() for i in range(2))
    assert abs(first["y"] - second["y"]) <= 1 and abs(first["height"] - second["height"]) <= 1, (first, second)
    headings = [cards.nth(i).locator("h3").first.bounding_box() for i in range(2)]
    assert abs(headings[0]["y"] - headings[1]["y"]) <= 1, headings
    check_layout(page)
    page.screenshot(path=str(output / f"{width}-friends.png"))

    case["stage"] = "screenshots"
    navigate(page, "gallery", ".gallery-delete")
    for cycle in range(2):
        first_image = copy.deepcopy(api.images[0])
        page.locator(".gallery-delete").first.click()
        settle(page)
        last_action(api, "experienceDeleteScreenshots", id=api.active,
                    images=[{"name": first_image["name"], "token": first_image["token"]}])
        no_confirmation(page)
        assert page.locator(".gallery-card").count() == 2 and len(api.images) == 2
        page.locator('[data-experience="screenshotTrash"]').click()
        row = page.locator(".gallery-trash-batch")
        row.wait_for(state="visible")
        controls = row.locator(".gallery-trash-actions > .btn")
        restore, remove = (controls.nth(i).bounding_box() for i in range(2))
        assert abs(restore["y"] - remove["y"]) <= 1 and abs(restore["height"] - remove["height"]) <= 1
        assert controls.nth(1).inner_text().strip() == "Удалить"
        check_layout(page, dialog=True)
        batch = copy.deepcopy(api.trash[0])
        if cycle == 0:
            page.screenshot(path=str(output / f"{width}-screenshot-trash.png"))
            row.locator('[data-experience="restoreScreenshots"]').click()
            settle(page)
            last_action(api, "experienceRestoreScreenshots", id=api.active, batch=batch["id"])
            no_confirmation(page)
            assert page.locator(".gallery-card").count() == 3 and not api.trash
        else:
            token = row.locator('[data-experience="purgeScreenshots"]').get_attribute("data-gallery-token")
            assert token == batch["token"]
            row.locator('[data-experience="purgeScreenshots"]').click()
            settle(page)
            last_action(api, "experiencePurgeScreenshots", id=api.active, batch=batch["id"], token=token)
            assert not api.trash and len(api.images) == 2
            assert page.locator(".gallery-trash-batch").count() == 0
            assert page.locator("#confirmScreenshotPurge, #confirmScreenshotDelete").count() == 0
            if page.locator("#dialog").evaluate("el => el.open"):
                page.locator("#dialogClose").click()

    case["stage"] = "addon-filters-and-pagination"
    page.evaluate("navigate('addons')")
    page.locator("[data-care-content]").wait_for(state="visible")
    page.evaluate("openModrinth(active(), 'mods')")
    page.locator(".catalog-choice").first.wait_for(state="visible")
    assert page.locator(".catalog-external-sources").count() == 0
    query = "оптимизация & свет"
    page.locator(".catalog-search input").fill(query)
    page.locator(".catalog-search input").press("Enter")
    settle(page)
    searches = [row for row in api.requests if row["path"] == "/api/catalog/search"]
    assert searches[-1]["query"]["q"] == [query]
    assert page.locator('.catalog a[href*="curseforge.com"], .catalog a[href*="planetminecraft.com"]').count() == 0
    pager = page.locator(".catalog-pager")
    case["addonPagerBackground"] = effective_background(pager)
    case["addonPager"] = row_metrics(pager, ":scope > button, :scope > span")
    assert pager.evaluate("""el => {const r=el.getBoundingClientRect(), a=el.firstElementChild.getBoundingClientRect(), b=el.lastElementChild.getBoundingClientRect();
      return Math.abs((a.left+b.right)/2-(r.left+r.right)/2)<=1 && Math.abs((a.top+a.bottom)/2-(r.top+r.bottom)/2)<=1; }"""), "Add-on pagination is not centered"
    page.locator("[data-catalog-filters]").click()
    footer = page.locator(".catalog-filter-footer")
    footer.wait_for(state="visible")
    metrics = row_metrics(footer, ":scope > button")
    assert [row["text"] for row in metrics] == ["К каталогу", "Сбросить фильтр", "Применить"], metrics
    assert max(row["width"] for row in metrics) - min(row["width"] for row in metrics) <= 1, metrics
    check_layout(page, dialog=True)
    page.screenshot(path=str(output / f"{width}-addon-filters.png"))
    page.locator('[data-group="category"][data-value="optimization"]').click()
    page.locator("[data-filter-apply]").click()
    settle(page)
    searches = [row for row in api.requests if row["path"] == "/api/catalog/search"]
    assert json.loads(searches[-1]["query"]["filters"][0]) == {"category": ["optimization"]}
    page.locator("[data-catalog-filters]").click()
    page.locator("[data-filter-reset]").click()
    page.locator("[data-filter-apply]").click()
    settle(page)
    assert page.evaluate("catalogPreferences.get(active().id + '/mods').preset.filters") == "{}"
    previous_searches = sum(row["path"] == "/api/catalog/search" for row in api.requests)
    page.locator("[data-catalog-filters]").click()
    page.locator('[data-group="category"][data-value="utility"]').click()
    page.locator("[data-filter-cancel]").click()
    settle(page)
    assert page.locator("#catalogFilterPanel").is_hidden()
    assert page.evaluate("catalogPreferences.get(active().id + '/mods').preset.filters") == "{}"
    assert sum(row["path"] == "/api/catalog/search" for row in api.requests) == previous_searches
    page.locator("#dialogClose").click()

    case["stage"] = "build-error-clear"
    page.evaluate("kind='errors';navigate('addons')")
    clear_errors = page.locator('[data-experience="clearBuildErrors"]')
    page.wait_for_function("document.querySelector('[data-experience=clearBuildErrors]')?.disabled === false")
    case["errorActions"] = row_metrics(page.locator(".build-error-actions"), ":scope > button")
    assert [row["text"] for row in case["errorActions"]] == ["Проверить сборку", "Очистить"]
    clear_errors.click()
    settle(page)
    last_action(api, "experienceClearBuildErrors", id="profile-a", throughId=1)
    no_confirmation(page)
    assert "Новая ошибка после открытия истории" in page.locator("[data-error-list]").text_content()
    assert "Старая ошибка сборки" not in page.locator("[data-error-list]").text_content()
    assert clear_errors.is_enabled()
    clear_errors.click()
    settle(page)
    last_action(api, "experienceClearBuildErrors", id="profile-a", throughId=3)
    assert clear_errors.is_disabled() and "Пока нет записанных ошибок." in page.locator("[data-error-list]").inner_text()
    assert len(api.build_error_rows["profile-b"]) == 1, "Clear affected another build"
    check_layout(page)

    case["stage"] = "backup-delete-and-notification-history"
    page.evaluate("kind='worlds';navigate('addons')")
    page.locator('[data-care="history"]').first.wait_for(state="visible")
    page.locator('[data-care="history"]').first.click()
    delete_copy = page.locator('[data-care-delete-snapshot="copy-a"]')
    delete_copy.wait_for(state="visible")
    assert delete_copy.locator("svg").count() == 1, "Backup removal needs a trash icon"
    assert delete_copy.get_attribute("aria-label"), "Backup removal needs an accessible name"
    check_layout(page, dialog=True)
    page.screenshot(path=str(output / f"{width}-world-copies.png"))
    delete_copy.click()
    settle(page)
    last_action(api, "careDeleteSnapshot", id=api.active, snapshot="copy-a")
    assert len(api.snapshots) == 2 and delete_copy.is_enabled(), "Failed deletion removed a copy or left its button locked"
    notice_in_history(page, api, "Копия используется другой операцией.", "error")
    delete_copy.click()
    page.wait_for_function("!document.querySelector('[data-care-delete-snapshot=\"copy-a\"]')")
    settle(page)
    assert len(api.snapshots) == 1 and page.locator(".care-point").count() == 1
    notice_in_history(page, api, "Резервная копия удалена.", "success")
    page.locator("#dialogClose").click()
    page.locator(".notification-bell").click()
    page.locator(".notification-history").wait_for(state="visible")
    settle(page)
    assert "Копия используется другой операцией." in page.locator(".notification-history .error").inner_text()
    assert "Резервная копия удалена." in page.locator(".notification-history").inner_text()
    assert not page.locator(".notification-dot").is_visible()
    assert page.locator("#notice").is_hidden()
    page.screenshot(path=str(output / f"{width}-notification-history.png"))
    page.locator("#dialogClose").click()

    case["stage"] = "build-actions"
    navigate(page, "builds", '[data-export-build="profile-a"]')
    case["download"] = {"expectedFilename": "blockharbor-profile-a.zip", "status": "waiting-for-browser"}
    print(f"DOWNLOAD: {width}x{height}: requesting ZIP export without worlds", flush=True)
    with page.expect_download() as download_event:
        page.locator('[data-export-build="profile-a"]').click()
    download = download_event.value
    case["download"].update(url=download.url, suggestedFilename=download.suggested_filename, status="started")
    case["download"]["failure"] = download.failure()
    case["download"]["status"] = "failed" if case["download"]["failure"] else "completed"
    print("DOWNLOAD: " + json.dumps(case["download"], ensure_ascii=False), flush=True)
    assert case["download"]["failure"] is None, "Browser download failed: " + repr(case["download"])
    assert download.suggested_filename == "blockharbor-profile-a.zip", "Wrong attachment name: " + repr(case["download"])
    with zipfile.ZipFile(download.path()) as archive:
        case["download"]["zipEntries"] = archive.namelist()
        case["download"]["exportPayload"] = json.loads(archive.read("fixture.json"))
        assert case["download"]["exportPayload"] == {"id": "profile-a", "worlds": False}, case["download"]
    settle(page)
    assert api.exports == [{"id": "profile-a", "worlds": False}]
    no_confirmation(page)
    for cycle in range(2):
        page.locator('[data-remove-build="profile-a"]').click()
        settle(page)
        last_action(api, "deleteBuild", id="profile-a")
        no_confirmation(page)
        assert page.locator('[data-profile="profile-a"]').count() == 0
        open_archive(page)
        restore = page.locator('[data-restore-build="profile-a"]')
        purge = page.locator('[data-purge-build="profile-a"]')
        a, b = restore.bounding_box(), purge.bounding_box()
        assert abs(a["height"] - b["height"]) <= 1, (a, b)
        check_layout(page)
        if cycle == 0:
            page.screenshot(path=str(output / f"{width}-deleted-builds.png"))
            restore.click()
            settle(page)
            last_action(api, "restoreBuild", id="profile-a")
            no_confirmation(page)
            assert page.locator('[data-profile="profile-a"]').count() == 1 and not api.deleted
        else:
            purge.click()
            settle(page)
            last_action(api, "purgeBuild", id="profile-a")
            page.wait_for_function("state.job && state.job.busy")
            assert purge.is_disabled() and restore.is_disabled(), "Purge unlocked before the job completed"
            page.evaluate("render()")
            settle(page)
            open_archive(page)
            assert purge.is_disabled() and restore.is_disabled(), "Re-render lost the purge lock"
            no_confirmation(page)
            complete_job(page, api)
            assert not api.deleted and page.locator('[data-purge-build="profile-a"]').count() == 0

    case["stage"] = "publication-and-headers"
    api.steam_supported = False
    page.locator('[data-act="create"]').click()
    page.wait_for_function("document.querySelector('[name=createBuildSubmit]') && !document.querySelector('[name=createBuildSubmit]').disabled")
    assert page.locator("#loaderStatus").get_attribute("data-state") == "supported"
    assert page.locator("[name=createBuildSubmit]").evaluate("el => el.form.id") == "newForm"
    page.wait_for_function("document.querySelector('#buildSteamStatus')?.textContent.includes('недоступно')")
    case["createActions"] = row_metrics(page.locator("#newForm .new-build-actions"), ":scope > button")
    assert [row["text"] for row in case["createActions"]] == ["Создать сборку", "Обновить список версий"]
    assert page.locator("#dialogHeaderActions [name=createBuildSubmit]").count() == 0
    requests_before_refresh = len(api.requests)
    page.locator('#newForm [data-act="versions"]').click()
    page.wait_for_function("!document.querySelector('[name=createBuildSubmit]').disabled")
    settle(page)
    assert any(row["path"] == "/api/releases" for row in api.requests[requests_before_refresh:]), "Refresh versions did not reload its catalog"
    case["headers"]["new-build"] = header_metrics(page)
    page.screenshot(path=str(output / f"{width}-new-build.png"))
    page.locator("#buildName").fill("Сборка без лишних подтверждений")
    page.locator("#newForm [name=createBuildSubmit]").click()
    page.wait_for_function("!document.querySelector('#dialog').open")
    settle(page)
    last_action(api, "create", name="Сборка без лишних подтверждений", allowWithoutSteam=True)
    assert sum(row["action"] == "create" for row in api.actions) == 1
    assert any(row["id"] == "profile-created" for row in api.profiles)
    assert page.locator("#steamCompatibilityDialog").count() == 0
    notice_in_history(page, api, "Сборка создана.", "success")
    api.steam_supported = True
    navigate(page, "builds", '[data-pack-share="profile-b"]')
    page.locator('[data-pack-share="profile-b"]').click()
    withdraw = page.locator('[data-pack-withdraw="BH-0123456789AB"]')
    withdraw.wait_for(state="visible")
    withdraw.click()
    settle(page)
    last_action(api, "communityUnpublish", id="profile-b", build="BH-0123456789AB")
    assert withdraw.is_disabled(), "Unpublish unlocked before its job completed"
    assert page.locator(".community-confirm").count() == 0
    complete_job(page, api)
    page.locator("#communityPublishSubmit").wait_for(state="visible")
    settle(page)
    assert page.locator("#communityPublishSubmit").is_enabled() and not api.publications["profile-b"]
    case["headers"]["publish"] = header_metrics(page)
    case["publishActions"] = row_metrics(page.locator(".community-publish-footer"), ":scope > button, :scope > .community-publication-status")
    assert page.locator("#dialogHeaderActions #communityPublishSubmit").count() == 0
    assert page.locator("#communityPublishSubmit").evaluate("el => el.form.id") == "communityPublishForm"
    page.locator('#communityPublishForm [name="category"]').select_option("adventure")
    page.screenshot(path=str(output / f"{width}-publish.png"))
    page.locator("#communityPublishSubmit").click()
    page.wait_for_function("state.job && state.job.busy")
    last_action(api, "communityPublish", id="profile-b", category="adventure", includeConfigs=False)
    assert page.locator("#communityPublishSubmit").is_disabled()
    complete_job(page, api)
    page.locator('[data-pack-withdraw="BH-FEDCBA987654"]').wait_for(state="visible")
    assert "опубликовано" in page.locator(".community-publication-status").inner_text().lower()
    page.locator("#dialogClose").click()

    case["stage"] = "catalog-and-compact-header"
    page.locator('[data-act="packCatalog"]').click()
    page.locator("[data-pack-project]").first.wait_for(state="visible")
    settle(page)
    pager = page.locator(".pack-pagination")
    pager_top = pager.bounding_box()["y"]
    assert effective_background(pager) == case["addonPagerBackground"], "Pack pager background differs from the add-on catalog"
    results = page.locator(".pack-results")
    assert results.bounding_box()["y"] + results.bounding_box()["height"] <= pager_top + 1, "Pack results overlap pagination"
    results.evaluate("el => el.scrollTop = el.scrollHeight")
    settle(page)
    assert abs(pager.bounding_box()["y"] - pager_top) <= 1, "Scrolling moved pack pagination"
    page.screenshot(path=str(output / f"{width}-pack-catalog.png"))
    page.locator("[data-pack-project]").first.click()
    page.locator("[data-pack-version]").wait_for(state="visible")
    check_layout(page, dialog=True)
    case["headers"]["release"] = header_metrics(page)
    page.set_viewport_size({"width": 640, "height": height})
    check_layout(page, dialog=True)
    compact = header_metrics(page)
    case["headers"]["release-640-initial"] = compact
    # Both layouts are valid: controls may fit naturally, or remain reachable
    # by scrolling the one-line header. Overflow itself is not a requirement.
    header = page.locator("#dialog > .dialog-top")
    header.evaluate("el => el.scrollLeft = el.scrollWidth")
    settle(page)
    case["headers"]["release-640"] = header_metrics(page)
    for selector in ("[data-pack-back]", "[data-pack-prepare]"):
        button = page.locator("#dialogHeaderActions " + selector)
        button.evaluate("el => { el.focus(); el.scrollIntoView({block:'nearest', inline:'nearest'}); }")
        settle(page)
        assert button.evaluate("""el => {const r = el.getBoundingClientRect();
          return document.elementFromPoint(r.x + r.width/2, r.y + r.height/2)?.closest('button') === el;}"""), selector + " is not reachable in the compact header"
    page.screenshot(path=str(output / f"{width}-header-640.png"))
    page.locator("[data-pack-prepare]").click()
    page.wait_for_function("state.job && state.job.busy")
    settle(page)
    last_action(api, "packPreview", project="pack-0", version="pack-release")
    last_action(api, "packInstall", token="preview-token", name="Небольшая сборка", allowWithoutSteam=True)
    assert sum(row["action"] == "packInstall" for row in api.actions) == 1
    assert page.locator(".pack-preview, #steamCompatibilityDialog").count() == 0
    no_confirmation(page)
    complete_job(page, api)
    assert any(row["id"] == "pack-installed" for row in api.profiles)
    page.evaluate("""() => { window.__steamHeaderPrompt = showSteamCompatibilityWarning(
      {unknown:true}, [{version:'1.20.1', loader:'quilt'}]); }""")
    page.locator("#steamCompatibilityDialog").wait_for(state="visible")
    case["headers"]["steam-640"] = header_metrics(page, "#steamCompatibilityDialog")
    page.locator("#steamCompatibilityDialog > .dialog-top [data-steam-cancel]").click()
    assert page.evaluate("window.__steamHeaderPrompt") is False
    page.set_viewport_size({"width": width, "height": height})
    page.locator('[data-act="packCatalog"]').click()
    community = page.locator("[data-pack-community]").first
    community.wait_for(state="visible")
    settle(page)
    build_id = community.get_attribute("data-pack-community")
    community.click()
    settle(page)
    last_action(api, "communityInstall", build=build_id, allowWithoutSteam=False)
    no_confirmation(page)
    assert page.locator("#steamCompatibilityDialog").count() == 0
    complete_job(page, api)
    assert page.locator('[data-profile="community-installed"]').count() == 1
    assert not api.errors, api.errors
    case["stage"] = "complete"


def main():
    from playwright.sync_api import sync_playwright

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
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
                case = {"viewport": [width, height], "result": "FAIL", "stage": "startup", "headers": {}}
                report.append(case)
                context = browser.new_context(viewport={"width": width, "height": height},
                                              reduced_motion="reduce", accept_downloads=True)
                page = context.new_page()
                server = LoopbackFixture(api)
                case["origin"] = server.origin
                errors = []
                case["requestFailures"] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("requestfailed", lambda request: case["requestFailures"].append(
                    {"url": request.url, "method": request.method, "failure": request.failure}))
                page.add_init_script("""window.__pendingRequests = 0;
                  const originalFetch = window.fetch;
                  window.fetch = async (...args) => { window.__pendingRequests++;
                    try { return await originalFetch(...args); } finally { window.__pendingRequests--; } };""")
                def block_external(route):
                    api.errors.append("Unexpected external request: " + route.request.url)
                    route.abort()
                # The page's HTTP requests, especially downloads, reach the real
                # fixture server. Intercept only traffic outside this origin.
                page.route(re.compile(r"^(?!" + re.escape(server.origin + "/") + r")"), block_external)
                try:
                    page.goto(server.origin + "/#" + TOKEN)
                    page.wait_for_function("typeof state !== 'undefined' && state && window.ExperienceUI")
                    check_screens(page, api, args.output, width, height, case)
                    assert not errors, "; ".join(errors)
                    case["result"] = "PASS"
                    print(f"PASS: 2.6.14 UX at {width}x{height}", flush=True)
                except Exception as error:
                    case["error"] = type(error).__name__ + ": " + str(error)
                    case["traceback"] = traceback.format_exc()
                    try:
                        page.screenshot(path=str(args.output / f"{width}-failure.png"))
                        (args.output / f"{width}-failure.html").write_text(page.content(), encoding="utf-8")
                    except Exception as capture_error:
                        case["captureError"] = repr(capture_error)
                    print(f"FAIL: {width}x{height}, {case['stage']}: {error}", flush=True)
                    print(case["traceback"], flush=True)
                finally:
                    case["pageErrors"], case["apiErrors"] = errors, api.errors
                    case["actions"], case["exports"] = api.actions, api.exports
                    case["requests"], case["downloadReceipts"] = api.requests, api.download_receipts
                    context.close()
                    server.close()
        finally:
            browser.close()
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if all(case["result"] == "PASS" for case in report) else 1)


if __name__ == "__main__":
    main()
