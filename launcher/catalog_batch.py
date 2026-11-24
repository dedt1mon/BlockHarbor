"""Observable add-on batches, atomic within each connected dependency group.

The existing verified installer remains the sole publisher of files. Independent
groups may succeed after another group fails; dependencies, shared filenames and
declared conflicts keep related roots in one transaction. Review tokens retain
their exact request/inventory binding and are never automatically accepted.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from http.client import HTTPException

from . import compatibility, lifecycle, modrinth
from .transfers import TransferCancelled

TERMINAL = frozenset({"installed", "already_installed", "incompatible", "error", "cancelled", "needs_review"})


def summary(items):
    counts = {state: sum(row["status"] == state for row in items) for state in TERMINAL}
    completed = sum(counts.values())
    return {
        "selected": len(items), "installed": counts["installed"],
        "alreadyInstalled": counts["already_installed"],
        "incompatible": counts["incompatible"], "failed": counts["error"],
        "cancelled": counts["cancelled"], "needsReview": counts["needs_review"],
        "remaining": len(items) - completed, "completed": completed,
        "notInstalled": sum(counts[k] for k in ("incompatible", "error", "cancelled", "needs_review")),
        "progress": round(100 * completed / len(items)) if items else 100,
    }


def _row(item):
    return {**item, "title": item["project"], "status": "queued", "error": "", "detail": ""}


def initial_snapshot(pid, items):
    rows = [_row(item) for item in modrinth._selections(items)]
    return {"kind": "catalog-install", "profileId": pid,
            "items": rows, "summary": summary(rows), "reviews": []}


def _error(exc):
    if isinstance(exc, (KeyError, TypeError)):
        return "Каталог вернул неполные сведения о выпуске. Повтори проверку позже."
    return str(exc)[:1000] or "Не удалось установить дополнение. Повтори попытку."


def _status_for_error(exc):
    if isinstance(exc, (lifecycle.OperationCancelled, TransferCancelled)):
        return "cancelled"
    return "incompatible" if isinstance(exc, modrinth.PlanConflict) else "error"


def _components(store, profile, selected, emit=lambda *_args: None):
    """Discover connected roots without downloading or changing the build."""
    plans, failures = {}, {}
    for item in selected:
        lifecycle.check()
        project = item["project"]
        emit(project, "checking")
        try:
            plan = modrinth._plan_items(store, profile, [item])
            plans[project] = plan
            chosen = next(release for release in plan if release.project == project)
            emit(project, "dependencies", title=chosen.title or chosen.name,
                 version=chosen.version, filename=chosen.filename)
        except lifecycle.OperationCancelled:
            raise
        except (ValueError, OSError, HTTPException, KeyError, TypeError) as exc:
            failures[project] = exc
            emit(project, _status_for_error(exc), error=_error(exc))

    parent = {project: project for project in plans}

    def find(project):
        while parent[project] != project:
            parent[project] = parent[parent[project]]
            project = parent[project]
        return project

    def union(left, right):
        parent[find(left)] = find(right)

    keys, projects, versions = {}, {}, {}
    for root, plan in plans.items():
        for release in plan:
            projects.setdefault(release.project, []).append(root)
            versions.setdefault(modrinth.trusted_catalog.canonical_version(release.version), []).append(root)
            for key in (("project", release.project), ("file", release.kind, release.filename.casefold())):
                if key in keys:
                    union(root, keys[key])
                keys[key] = root
    for root, plan in plans.items():
        for release in plan:
            for dependency in release.deps:
                if dependency.get("dependency_type") != "incompatible":
                    continue
                target, pin = dependency.get("project_id"), dependency.get("version_id")
                related = projects.get(target, []) if target else versions.get(modrinth.trusted_catalog.canonical_version(pin), []) if pin else []
                for other in related:
                    union(root, other)
    groups = {}
    for item in selected:
        if item["project"] in plans:
            groups.setdefault(find(item["project"]), []).append(item)
    return list(groups.values()), failures


def check_selection(store, pid, items):
    """Preview feasible groups while preserving individual planning failures."""
    selected = modrinth._selections(items)
    profile = compatibility.installation_profile(store, pid)
    rows = {item["project"]: _row(item) for item in selected}

    def emit(project, state, **values):
        rows[project].update(status=state, **values)

    groups, failures = _components(store, profile, selected, emit)
    result = compatibility.report()
    result.update(items=[], selected=[], reviewGroups=[], reviewToken="", batch=True)
    seen = {key: set() for key in ("required", "optional", "embedded", "warnings")}
    feasible = []
    for group in groups:
        lifecycle.check()
        try:
            checked = modrinth.check_selection(store, pid, group)
            modrinth._require_no_conflicts(checked)
        except lifecycle.OperationCancelled:
            raise
        except (ValueError, OSError, HTTPException, KeyError, TypeError) as exc:
            for item in group:
                emit(item["project"], _status_for_error(exc), error=_error(exc))
            continue
        feasible.append(checked)
        result["items"].extend(checked["items"])
        for item in checked["selected"]:
            emit(item["project"], "ready", title=item["name"], filename=item["filename"],
                 installed=item["installed"], error="")
        for key in seen:
            for item in checked[key]:
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker not in seen[key]:
                    seen[key].add(marker)
                    result[key].append(item)
        if checked["reviewToken"]:
            result["reviewGroups"].append({
                "request": {"id": pid, "items": checked["items"]},
                "token": checked["reviewToken"], "warnings": checked["warnings"],
            })
    if len(feasible) == 1:
        result["reviewToken"] = feasible[0]["reviewToken"]
    result["selected"] = list(rows.values())
    result["blocked"] = not bool(result["items"])
    result["partial"] = any(row["status"] != "ready" for row in rows.values())
    result["itemErrors"] = [{"project": row["project"], "message": row["error"]}
                            for row in rows.values() if row["error"]]
    if result["blocked"]:
        result["conflicts"] = [row["message"] for row in result["itemErrors"]]
    return result


def install(store, pid, items, status, mutation_lock=None, acknowledgement="", *,
            before_publish=None, transfer=None, after_publish=None,
            on_progress=lambda _snapshot: None, review_request=None, publisher=None):
    selected = modrinth._selections(items)
    profile = compatibility.installation_profile(store, pid)
    rows = {item["project"]: _row(item) for item in selected}
    reviews = []
    requested = {item["project"]: item for item in selected}
    post_install_warnings = []
    backed_up = False

    def snapshot():
        values = list(rows.values())
        return {"kind": "catalog-install", "profileId": pid,
                "items": copy.deepcopy(values), "summary": summary(values),
                "reviews": copy.deepcopy(reviews)}

    def emit(project, state, **values):
        rows[project].update(status=state, **values)
        on_progress(snapshot())

    def before():
        nonlocal backed_up
        if not backed_up and before_publish is not None:
            before_publish()
        backed_up = True

    def matches_requested(release):
        item = requested.get(release.project)
        if not item or item["kind"] != release.kind:
            return False
        canonical = modrinth.trusted_catalog.canonical_version
        if item["version"] and canonical(item["version"]) != canonical(release.version):
            return False
        source = item.get("source", "all")
        actual_source = "github" if modrinth.trusted_catalog.has_version(release.version) else "community"
        return source == "all" or source == actual_source

    on_progress(snapshot())
    try:
        groups, failures = _components(store, profile, selected, emit)
        for group in groups:
            lifecycle.check()
            roots = {item["project"] for item in group}
            initial = {}

            def stage(stage_name, release=None):
                if stage_name == "published":
                    for actual in release:
                        if matches_requested(actual):
                            previous = rows[actual.project]["status"]
                            state = previous if previous in {"installed", "already_installed"} else "already_installed" if initial.get(actual.project) else "installed"
                            emit(actual.project, state, version=actual.version,
                                 title=actual.title or actual.name, filename=actual.filename, error="", detail="")
                    return
                detail = release.filename if release else ""
                for project in roots:
                    emit(project, stage_name, detail=detail)

            def complete(releases):
                if after_publish is not None:
                    after_publish(releases)

            try:
                # A preceding independent group may have changed the inventory.
                # Rebuild and recheck against it before any next publication.
                for release in modrinth._plan_items(store, profile, group):
                    if matches_requested(release):
                        existing = modrinth._files(store.content_folder(pid, release.kind), release.kind)
                        initial[release.project] = bool(modrinth._installed(existing, release))
                        rows[release.project].update(title=release.title or release.name,
                                                     version=release.version, filename=release.filename)
                request = review_request if len(groups) == 1 and len(group) == len(selected) and review_request else {"id": pid, "items": group}
                # A token for a whole basket must never approve a smaller group.
                accepted = acknowledgement if len(groups) == 1 and len(group) == len(selected) else ""
                modrinth.install_selection(
                    store, pid, group, status, mutation_lock, accepted,
                    before_publish=before, transfer=transfer, after_publish=complete,
                    request=request, on_stage=stage, publisher=publisher,
                )
                for project in roots:
                    emit(project, "already_installed" if initial.get(project) else "installed", error="", detail="")
            except lifecycle.OperationCancelled:
                raise
            except modrinth.NeedsReview as exc:
                reviews.append(exc.review)
                for project in roots:
                    emit(project, "needs_review", error="Перед установкой проверь предупреждения.", detail="")
            except (ValueError, OSError, HTTPException, KeyError, TypeError, sqlite3.Error) as exc:
                for project in roots:
                    if rows[project]["status"] in {"installed", "already_installed"}:
                        warning = "Файлы установлены, но не удалось сохранить дополнительные сведения: " + _error(exc)
                        post_install_warnings.append(warning)
                        emit(project, rows[project]["status"], warning=warning)
                    else:
                        emit(project, _status_for_error(exc), error=_error(exc), detail="")
    except lifecycle.OperationCancelled:
        for project, row in rows.items():
            if row["status"] not in TERMINAL:
                emit(project, "cancelled", error="Установка отменена. Уже установленные группы сохранены.", detail="")
        raise
    result = snapshot()
    result["reviews"] = [review for review in reviews if any(
        rows.get(item["project"], {}).get("status") == "needs_review"
        for item in review.get("request", {}).get("items", [])
    )]
    totals = result["summary"]
    result["ok"] = not bool(totals["notInstalled"])
    result["message"] = (
        f"Установлено: {totals['installed']}; уже установлено: {totals['alreadyInstalled']}; "
        f"не установлено: {totals['notInstalled']}."
    )
    if post_install_warnings:
        result["warnings"] = sorted(set(post_install_warnings))
        result["message"] += " Дополнительные сведения сохранены не полностью."
    if len(result["reviews"]) == 1:
        result["review"] = result["reviews"][0]
    return result
