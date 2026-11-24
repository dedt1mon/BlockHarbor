"""Verify search hits against a release for the exact Minecraft/loader tuple.

Project-level facets can match a Forge release for one game version and a Fabric
release for another. Only the per-release metadata can confirm their combination.
The bounded, cached lookups never download a JAR or mutate a build.
"""

import concurrent.futures
import time
from functools import lru_cache

from . import lifecycle, modrinth

VERIFY_BUDGET = 20.0


@lru_cache(maxsize=512)
def _confirmed(game, loader, kind, project, source, period):
    profile = {"version": game, "loader": loader}
    releases = modrinth._releases(profile, kind, project, source=source)
    preferred = next((release for release in releases if release.channel == "release"), next(iter(releases), None))
    if preferred is None:
        return None
    return {"version": preferred.version, "channel": preferred.channel}


def search(profile, kind, query, offset, filters=None, sort="relevance", source="community"):
    page = modrinth.search(profile, kind, query, offset, filters, sort, source)
    unique = {}
    for hit in page["hits"]:
        unique.setdefault(hit["id"], hit)
    if not unique:
        return {**page, "verified": True, "totalIsEstimate": True}
    owner = lifecycle.current_scope()
    scope = lifecycle.CancellationScope()
    remove = owner.on_cancel(scope.cancel) if owner else lambda: None
    pool = None
    accepted, unavailable = {}, False
    try:
        with lifecycle.bind(scope):
            pool = lifecycle.Executor(max_workers=min(4, len(unique)), thread_name_prefix="bh-catalog-check")
            jobs = {pool.submit(_confirmed, profile["version"], profile["loader"], kind, project,
                                hit.get("source", source), int(time.monotonic() // 300)): project
                    for project, hit in unique.items()}
            pending = set(jobs)
            deadline = time.monotonic() + VERIFY_BUDGET
            while pending and time.monotonic() < deadline:
                lifecycle.check()
                done, pending = concurrent.futures.wait(pending, timeout=min(.1, max(0, deadline - time.monotonic())),
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    try:
                        match = future.result()
                    except lifecycle.OperationCancelled:
                        raise
                    except (ValueError, OSError, KeyError, TypeError):
                        unavailable = True
                    else:
                        if match:
                            accepted[jobs[future]] = match
            unavailable |= bool(pending)
    finally:
        scope.cancel()
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
        remove()
    if owner:
        owner.check()
    result = {**page, "hits": [{**hit, "compatibility": "release-metadata", "recommendedVersion": accepted[project]["version"]}
                               for project, hit in unique.items() if project in accepted],
              "verified": True, "totalIsEstimate": True}
    if unavailable:
        result.update(partial=True, message="Часть выпусков не удалось проверить. Показаны только дополнения с подтверждённой версией Minecraft и загрузчиком; повтори поиск позже.")
    return result
