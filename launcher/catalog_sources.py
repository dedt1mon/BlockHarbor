"""Capabilities of the built-in catalog, without website-search substitutes.

CurseForge requires an approved integration key, including for CDN downloads.
Planet Minecraft does not grant a general permission to import its listings
into applications. Providers without approved access stay explicitly disabled.
No credentials, network probes, scraping, or arbitrary origins are involved.
"""

_KINDS = frozenset({"mods", "shaderpacks", "resourcepacks"})


def describe(profile, kind, query="", source="all", *, primary_unavailable=False):
    if kind not in _KINDS:
        raise ValueError("Выбери категорию дополнения.")
    if not isinstance(query, str) or len(query) > 180:
        raise ValueError("Поисковый запрос длиннее 180 символов.")
    if source not in {"all", "community", "github"}:
        raise ValueError("Неизвестный источник дополнений.")
    statuses = []
    if source != "github":
        statuses.append({"id": "modrinth", "title": "Modrinth", "mode": "integrated",
                         "status": "unavailable" if primary_unavailable else "enabled"})
    if source != "community":
        statuses.append({"id": "github", "title": "GitHub автора", "mode": "integrated",
                         "status": "available"})
    statuses.extend([
        {"id": "curseforge", "title": "CurseForge", "mode": "disabled", "status": "disabled",
         "reason": "Встроенный поиск и скачивание не подключены: нужен одобренный ключ CurseForge для BlockHarbor."},
        {"id": "planetminecraft", "title": "Planet Minecraft", "mode": "disabled", "status": "disabled",
         "reason": "Каталог не подключён: нужно письменное разрешение Planet Minecraft на импорт данных."},
    ])
    # Retain the empty field for clients using the previous response schema.
    return {"sourceStatus": statuses, "externalSources": []}
