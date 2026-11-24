"""Honest catalog capabilities and contextual links to official websites.

CurseForge requires an approved integration key, including for CDN downloads.
Planet Minecraft does not grant a general permission to import its listings
into applications. These websites are links, never synthetic installable hits.
No credentials, network probes, scraping, or arbitrary origins are involved.
"""

from urllib.parse import urlencode


_SECTIONS = {
    "mods": ("mc-mods", "mods"),
    "shaderpacks": ("shaders", "texture-packs/tag/shaders"),
    "resourcepacks": ("texture-packs", "texture-packs"),
}


def describe(profile, kind, query="", source="all", *, primary_unavailable=False):
    if kind not in _SECTIONS:
        raise ValueError("Выбери категорию дополнения.")
    if not isinstance(query, str) or len(query) > 180:
        raise ValueError("Поисковый запрос длиннее 180 символов.")
    if source not in {"all", "community", "github"}:
        raise ValueError("Неизвестный источник дополнений.")
    curseforge, planet = _SECTIONS[kind]
    query = query.strip()
    curse_params = {"class": curseforge, "page": 1, "pageSize": 20,
                    "sortBy": "relevancy", "search": query,
                    "version": str(profile["version"])}
    sources = [
        {"id": "curseforge", "title": "CurseForge", "mode": "website",
         "url": "https://www.curseforge.com/minecraft/search?" + urlencode(curse_params)},
        {"id": "planetminecraft", "title": "Planet Minecraft", "mode": "website",
         "url": "https://www.planetminecraft.com/" + planet + "/?" + urlencode({"keywords": query})},
    ]
    statuses = []
    if source != "github":
        statuses.append({"id": "modrinth", "title": "Modrinth", "mode": "integrated",
                         "status": "unavailable" if primary_unavailable else "enabled"})
    if source != "community":
        statuses.append({"id": "github", "title": "GitHub автора", "mode": "integrated",
                         "status": "available"})
    statuses.extend([
        {"id": "curseforge", "title": "CurseForge", "mode": "website", "status": "external",
         "reason": "Для встроенного поиска и скачивания требуется одобренный ключ CurseForge."},
        {"id": "planetminecraft", "title": "Planet Minecraft", "mode": "website", "status": "external",
         "reason": "Поиск на сайте; импорт каталога в приложение требует разрешения Planet Minecraft."},
    ])
    return {"sourceStatus": statuses, "externalSources": sources}
