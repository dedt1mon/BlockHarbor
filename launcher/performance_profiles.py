"""Reviewed, reversible settings presets; no unverifiable FPS promises or JVM flags."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import distances, modrinth
from .memory import automatic_heap

PRESETS = {
    "economy": {"name": "Экономный", "description": "Меньше нагрузка на слабый компьютер.", "options": {"renderDistance": "6", "simulationDistance": "5", "graphicsMode": "0", "particles": "2", "maxFps": "60", "mipmapLevels": "2"}},
    "balanced": {"name": "Сбалансированный", "description": "Умеренная дальность и полные эффекты частиц.", "options": {"renderDistance": "10", "simulationDistance": "6", "graphicsMode": "1", "particles": "0", "maxFps": "120", "mipmapLevels": "4"}},
    "quality": {"name": "Качество", "description": "Больше дальность и визуальные эффекты; нужна более мощная система.", "options": {"renderDistance": "16", "simulationDistance": "10", "graphicsMode": "1", "particles": "0", "maxFps": "120", "mipmapLevels": "4"}},
    "friends": {"name": "Мир с друзьями", "description": "Оставляет больше ресурсов для симуляции открытого мира.", "options": {"renderDistance": "8", "simulationDistance": "5", "graphicsMode": "0", "particles": "1", "maxFps": "60", "mipmapLevels": "2"}},
}
LABELS = {"renderDistance": "Прорисовка, чанки", "simulationDistance": "Симуляция, чанки", "graphicsMode": "Графика: 0 — быстрая, 1 — детальная", "particles": "Частицы: 0 — все, 1 — меньше, 2 — минимум", "maxFps": "Ограничение FPS", "mipmapLevels": "Уровни mipmap"}
PROJECTS = (("sodium", "Sodium", "Изменяет рендеринг Minecraft."), ("lithium", "Lithium", "Оптимизирует игровую логику."), ("ferrite-core", "FerriteCore", "Снижает расход памяти некоторыми структурами игры."))


class PerformanceProfiles:
    READ_ACTIONS = {"performanceState", "performancePreview", "performanceRecommendations"}
    ACTIONS = READ_ACTIONS | {"performanceApply"}
    JOBS = {"performanceApply"}

    def __init__(self, store, before_change=None, metrics=None):
        self.store, self.before_change, self.metrics = store, before_change, metrics

    def memory(self, pid, fallback):
        value = self.store.get("performanceMemory:" + pid, fallback)
        return automatic_heap() if value == "auto" else max(1024, min(32768, int(value)))

    def state(self, pid):
        profile = self.store.profile(pid)
        return {"supported": distances.supported(profile["version"]), "presets": [{"id": key, "name": item["name"], "description": item["description"]} for key, item in PRESETS.items()], "selected": self.store.get("performancePreset:" + pid), "memory": self.store.get("performanceMemory:" + pid, "auto"), "metrics": self.metrics.state(pid) if self.metrics else None}

    def preview(self, pid, preset):
        profile = self.store.profile(pid)
        if preset not in PRESETS or not distances.supported(profile["version"]):
            raise ValueError("Профили графики доступны для Minecraft 1.18 и новее.")
        raw = distances._read(self.store.folder(pid).resolve(), "options.txt", 8 * 1024 * 1024)
        text = (raw or b"").decode("utf-8-sig")
        current = dict(re.findall(r"(?m)^([A-Za-z][\w]*):([^\r\n]*)", text))
        options = PRESETS[preset]["options"]
        memory = str(min(automatic_heap(), 3072)) if preset == "economy" else "auto"
        previous_memory = self.store.get("performanceMemory:" + pid, self.store.get("memory", "auto"))
        fingerprint = hashlib.sha256((raw or b"") + json.dumps([profile, previous_memory, preset], sort_keys=True).encode()).hexdigest()
        return {"id": pid, "preset": preset, "title": PRESETS[preset]["name"], "token": fingerprint, "changes": [{"key": key, "label": LABELS[key], "before": current.get(key, "По умолчанию"), "after": value} for key, value in options.items() if current.get(key) != value], "memory": {"before": previous_memory, "after": memory}, "newFile": raw is None}

    def apply(self, p, status):
        plan = self.preview(p["id"], p["preset"])
        if plan["token"] != p.get("token"):
            raise ValueError("Параметры сборки изменились. Открой предварительный просмотр заново.")
        if self.before_change:
            status("Создаём точку перед настройкой производительности…")
            self.before_change(p["id"], "Профиль производительности: " + plan["title"])
        folder = self.store.folder(p["id"]).resolve()
        raw = distances._read(folder, "options.txt", 8 * 1024 * 1024) or b""
        text = raw.decode("utf-8-sig")
        for key, value in PRESETS[p["preset"]]["options"].items():
            pattern = re.compile(r"(?m)^" + re.escape(key) + r":[^\r\n]*")
            if pattern.search(text):
                text = pattern.sub(key + ":" + value, text)
            else:
                text = text.rstrip("\r\n") + ("\n" if text else "") + key + ":" + value + "\n"
        if self.preview(p["id"], p["preset"])["token"] != p["token"]:
            raise ValueError("Minecraft изменил настройки. Закрой игру и повтори.")
        distances._replace(folder / "options.txt", text.encode("utf-8"))
        self.store.set_many({"performanceMemory:" + p["id"]: plan["memory"]["after"], "performancePreset:" + p["id"]: p["preset"]})
        return "Применён профиль «" + plan["title"] + "». Предыдущие настройки доступны в точках восстановления."

    def recommendations(self, pid):
        profile = self.store.profile(pid)
        if profile["loader"] == "vanilla":
            return {"items": [], "message": "Для модов оптимизации нужна сборка с загрузчиком модов."}

        def one(item):
            slug, title, description = item
            try:
                project = modrinth._get("/project/" + slug)
                versions = modrinth.versions(self.store, pid, "mods", project["id"])["versions"]
                release = next((v for v in versions if v["type"] == "release"), None)
                return {"title": title, "description": description, "project": project["id"], "version": release, "available": bool(release)}
            except (ValueError, KeyError, OSError) as exc:
                return {"title": title, "description": description, "available": False, "error": str(exc)[:200]}

        with ThreadPoolExecutor(max_workers=3) as pool:
            return {"items": list(pool.map(one, PROJECTS)), "message": "Совместимость версии подтверждается каталогом. Перед установкой будет проверен состав этой сборки; результат зависит от компьютера и остальных модов."}

    def handle(self, action, p, status=lambda _: None):
        if action == "performanceState":
            return self.state(p["id"])
        if action == "performancePreview":
            return self.preview(p["id"], p["preset"])
        if action == "performanceApply":
            return self.apply(p, status)
        if action == "performanceRecommendations":
            return self.recommendations(p["id"])
        raise ValueError("Неизвестное действие производительности.")
