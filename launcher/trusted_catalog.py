"""Reviewed publisher release assets. The desktop never accepts a user-supplied asset URL."""

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

RELEASE_HOSTS = frozenset(("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"))


@lru_cache(maxsize=1)
def _index():
    data = json.loads(Path(__file__).with_name("author_releases.json").read_text(encoding="utf-8"))
    projects = {}
    versions = {}
    hashes = {}
    for project in data["projects"]:
        projects[project["id"]] = project
        for row in project["releases"]:
            file = row["file"]
            location = urlsplit(file["url"])
            prefix = f"/{project['repo']}/releases/download/".casefold()
            if location.scheme != "https" or location.netloc != "github.com" or not location.path.casefold().startswith(prefix):
                raise ValueError("Некорректный адрес в списке авторских выпусков.")
            version = {key: row[key] for key in ("id", "name", "version_number", "version_type", "date_published", "game_versions", "loaders", "dependencies")}
            version.update(project_id=project["id"], community_version=row.get("community_version"), files=[{
                "primary": True, "filename": file["filename"], "size": file["size"],
                "hashes": {"sha512": file["sha512"]}, "url": file["url"],
            }])
            versions[row["id"]] = version
            hashes[file["sha512"]] = version
    return projects, versions, hashes


def project(identifier):
    return _index()[0].get(identifier)


def version(identifier):
    return _index()[1].get(identifier)


def has_version(identifier):
    return bool(identifier and version(identifier))


def canonical_version(identifier):
    record = version(identifier)
    if record and record.get("community_version"):
        return record["community_version"]
    return identifier


def by_hash(digest):
    return _index()[2].get(digest)


def is_publisher_file(project_id, version_id, file):
    if not (release := version(version_id)) or release["project_id"] != project_id:
        return False
    reviewed = release["files"][0]
    return all(file.get(key) == reviewed[key] for key in ("url", "size", "filename")) and file.get("hashes", {}).get("sha512") == reviewed["hashes"]["sha512"]


def compatible(profile, kind, project_id):
    result = []
    record = project(project_id)
    if not record:
        return result
    for row in record["releases"]:
        if profile["version"] in row["game_versions"] and (kind != "mods" or profile["loader"] in row["loaders"]):
            result.append(version(row["id"]))
    return result


def _matches(rows, facets):
    for terms in facets:
        for term in terms:
            negative = "!=" in term
            field, value = term.split("!=" if negative else ":", 1)
            if (value in rows.get(field, set())) != negative:
                break
        else:
            return False
    return True


def search(profile, kind, project_type, query, facets):
    hits = []
    words = query.casefold().split()
    for item in _index()[0].values():
        if item["project_type"] != project_type:
            continue
        text = " ".join((item["slug"], item["title"], item["description"], item["author"])).casefold()
        if not all(word in text for word in words):
            continue
        releases = compatible(profile, kind, item["id"])
        if not releases:
            continue
        categories = set(item["categories"])
        fields = {"environment": set(item["environment"]), "open_source": {str(item["open_source"]).lower()},
                  "all_project_types": set(item.get("all_project_types", [project_type])),
                  "disclosure_types": set(item.get("disclosure_types", []))}
        for release in releases:
            fields["categories"] = categories | set(release["loaders"])
            for typ in ("required", "optional", "embedded"):
                fields[typ + "_dependency_project_ids"] = {d.get("project_id") for d in release["dependencies"] if d.get("dependency_type") == typ}
            if _matches(fields, facets):
                break
        else:
            continue
        hits.append({
            "id": item["id"], "title": item["title"], "author": item["author"], "description": item["description"],
            "icon": item.get("icon_url", ""), "downloads": sum(v["downloads"] for v in item["releases"]),
            "follows": item.get("followers", 0), "newest": item["published"],
            "updated": max(v["date_published"] for v in releases), "source": "github", "sourceTitle": "GitHub автора",
        })
    return hits
