"""Modrinth Discover options and translation into the v2 search facet grammar."""

import re

SORT = {
    "relevance": "По релевантности",
    "downloads": "По скачиваниям",
    "follows": "По подписчикам",
    "newest": "По дате публикации",
    "updated": "По дате обновления",
}
_GROUP_LABELS = {
    "category": "Категория",
    "feature": "Особенности",
    "resolution": "Разрешение",
    "performance": "Нагрузка на компьютер",
    "renderer": "Загрузчик шейдеров",
    "environment": "Сторона установки",
    "license": "Лицензия",
    "exclude": "Дополнительные исключения",
}
_GROUPS = {
    "mods": {
        "category": "adventure cursed decoration economy equipment food game-mechanics library magic management minigame mobs optimization social storage technology transportation utility worldgen",
        "environment": "client server",
    },
    "shaderpacks": {
        "category": "cartoon cursed fantasy realistic semi-realistic vanilla-like",
        "feature": "atmosphere bloom colored-lighting foliage path-tracing pbr reflections shadows",
        "performance": "potato low medium high screenshot",
        "renderer": "iris optifine vanilla canvas",
    },
    "resourcepacks": {
        "category": "combat cursed decoration modded realistic simplistic themed tweaks utility vanilla-like",
        "feature": "audio blocks core-shaders entities environment equipment fonts gui items locale models",
        "resolution": "8x- 16x 32x 48x 64x 128x 256x 512x+",
    },
}
_COMMON_EXCLUSIONS = "ai_content ai_content_code ai_content_assets ai_content_text ai_functionality advertisements epilepsy_triggers paid_features archived"
_MOD_EXCLUSIONS = "system_interactions telemetry telemetry_opt_in telemetry_opt_out telemetry_always_active plugin datapack"
_LABELS = {
    "adventure": "Приключения",
    "cursed": "Необычные",
    "decoration": "Декор",
    "economy": "Экономика",
    "equipment": "Экипировка",
    "food": "Еда",
    "game-mechanics": "Игровая механика",
    "library": "Библиотеки",
    "magic": "Магия",
    "management": "Управление",
    "minigame": "Мини-игры",
    "mobs": "Мобы",
    "optimization": "Оптимизация",
    "social": "Общение",
    "storage": "Хранение",
    "technology": "Технологии",
    "transportation": "Транспорт",
    "utility": "Утилиты",
    "worldgen": "Генерация мира",
    "client": "Клиент",
    "server": "Сервер",
    "cartoon": "Мультяшные",
    "fantasy": "Фэнтези",
    "realistic": "Реализм",
    "semi-realistic": "Полуреализм",
    "vanilla-like": "В стиле Minecraft",
    "atmosphere": "Атмосфера",
    "bloom": "Свечение",
    "colored-lighting": "Цветное освещение",
    "foliage": "Растительность",
    "path-tracing": "Трассировка путей",
    "pbr": "PBR",
    "reflections": "Отражения",
    "shadows": "Тени",
    "potato": "Минимальная",
    "low": "Низкая",
    "medium": "Средняя",
    "high": "Высокая",
    "screenshot": "Для скриншотов",
    "iris": "Iris",
    "optifine": "OptiFine",
    "vanilla": "Vanilla Shader",
    "canvas": "Canvas",
    "combat": "Бой",
    "modded": "Для модов",
    "simplistic": "Упрощённые",
    "themed": "Тематические",
    "tweaks": "Небольшие изменения",
    "audio": "Звуки",
    "blocks": "Блоки",
    "core-shaders": "Встроенные шейдеры",
    "entities": "Сущности",
    "environment": "Окружение",
    "fonts": "Шрифты",
    "gui": "Интерфейс",
    "items": "Предметы",
    "locale": "Переводы",
    "models": "Модели",
    "8x-": "8× и ниже",
    "512x+": "512× и выше",
    "open_source": "Открытый исходный код",
    "ai_content": "Контент, созданный ИИ",
    "ai_content_code": "Код, созданный ИИ",
    "ai_content_assets": "Ресурсы, созданные ИИ",
    "ai_content_text": "Текст, созданный ИИ",
    "ai_functionality": "Функции ИИ",
    "advertisements": "Реклама",
    "epilepsy_triggers": "Мигающие эффекты",
    "paid_features": "Платные функции",
    "archived": "Архивные проекты",
    "system_interactions": "Взаимодействие с системой",
    "telemetry": "Любая телеметрия",
    "telemetry_opt_in": "Телеметрия по согласию",
    "telemetry_opt_out": "Телеметрия с возможностью отключения",
    "telemetry_always_active": "Неотключаемая телеметрия",
    "plugin": "Плагины",
    "datapack": "Дата-паки",
}
_DEP_TYPES = {"required", "optional", "embedded"}
_ENV = {
    frozenset(["client"]): (
        "client_only",
        "client_only_server_optional",
        "client_or_server",
        "client_or_server_prefers_both",
    ),
    frozenset(["server"]): (
        "server_only",
        "dedicated_server_only",
        "server_only_client_optional",
        "client_or_server",
        "client_or_server_prefers_both",
    ),
    frozenset(["client", "server"]): (
        "client_only_server_optional",
        "server_only_client_optional",
        "client_and_server",
        "client_or_server",
        "client_or_server_prefers_both",
    ),
}


def _groups(kind):
    if kind not in _GROUPS:
        raise ValueError("Выбери категорию дополнения.")
    exclude = _COMMON_EXCLUSIONS
    if kind == "mods":
        exclude += " " + _MOD_EXCLUSIONS
    return {**_GROUPS[kind], "license": "open_source", "exclude": exclude}


def schema(kind):
    return {
        "sort": SORT,
        "groups": [
            {
                "id": key,
                "name": _GROUP_LABELS[key],
                "mode": {"exclude": "exclude", "environment": "include"}.get(
                    key, "both"
                ),
                "options": [
                    {"id": value, "name": _LABELS.get(value, value.replace("x", "×"))}
                    for value in words.split()
                ],
            }
            for key, words in _groups(kind).items()
        ],
    }


def _dependencies(items):
    if not isinstance(items, list) or len(items) > 16:
        raise ValueError("Некорректный список зависимостей фильтра.")
    facets = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.keys() - {"id", "types", "exclude"}:
            raise ValueError("Некорректный фильтр по зависимости.")
        project = item.get("id")
        types = item.get("types")
        negative = item.get("exclude", False)
        if (
            not isinstance(project, str)
            or not re.fullmatch(r"[A-Za-z0-9]{1,32}", project)
            or project in seen
            or type(negative) is not bool
            or not isinstance(types, list)
            or not 1 <= len(types) <= len(_DEP_TYPES)
            or not all(isinstance(t, str) and t in _DEP_TYPES for t in types)
        ):
            raise ValueError("Проверь проект и типы зависимости в фильтрах.")
        seen.add(project)
        operator = "!=" if negative else ":"
        fields = [
            f"{t}_dependency_project_ids{operator}{project}" for t in sorted(set(types))
        ]
        facets.extend([[field] for field in fields] if negative else [fields])
    return facets


def facets(kind, filters, sort):
    groups = _groups(kind)
    if not isinstance(sort, str) or sort not in SORT:
        raise ValueError("Неизвестная сортировка каталога.")
    if not isinstance(filters, dict) or filters.keys() - (
        groups.keys() | {"dependencies"}
    ):
        raise ValueError("Неизвестный фильтр для этой категории.")
    result = _dependencies(filters.get("dependencies", []))
    for group, words in groups.items():
        items = filters.get(group, [])
        allowed = set(words.split())
        if not isinstance(items, list) or len(items) > len(allowed):
            raise ValueError("Некорректное число параметров фильтра.")
        seen = set()
        alternatives = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("Некорректное значение фильтра.")
            value = item.removeprefix("!")
            negative = item.startswith("!") or group == "exclude"
            if (
                value not in allowed
                or value in seen
                or (group == "environment" and negative)
            ):
                raise ValueError("Некорректное значение фильтра для этой категории.")
            seen.add(value)
            if group == "environment":
                continue
            field = "categories"
            if group == "exclude":
                field = (
                    "all_project_types"
                    if value in ("plugin", "datapack")
                    else "disclosure_types"
                )
            elif group == "license":
                field, value = "open_source", "true"
            facet = field + ("!=" if negative else ":") + value
            if group in ("resolution", "renderer") and not negative:
                alternatives.append(facet)
            else:
                result.append([facet])
        if group == "environment" and seen:
            alternatives = ["environment:" + env for env in _ENV[frozenset(seen)]]
        if alternatives:
            result.append(alternatives)
    return result
