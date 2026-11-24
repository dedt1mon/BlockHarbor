"""Verify search hits against a release for the exact Minecraft/loader tuple.

Project-level facets can match a Forge release for one game version and a Fabric
release for another. Only the per-release metadata can confirm their combination.
The bounded, cached lookups never download a JAR or mutate a build.
"""

import concurrent.futures
import time
import threading
from functools import lru_cache
from http.client import HTTPException

from . import lifecycle, modrinth

VERIFY_BUDGET = 20.0
MAX_SEARCH_TASKS = 16


@lru_cache(maxsize=512)
def _confirmed(game, loader, kind, project, source, period):
    profile = {"version": game, "loader": loader}
    releases = modrinth._releases(profile, kind, project, source=source)
    preferred = next((release for release in releases if release.channel == "release"), next(iter(releases), None))
    if preferred is None:
        return None
    return {"version": preferred.version, "channel": preferred.channel, "releases": tuple(releases)}


class SearchService:
    """One bounded pool per window; a new query supersedes the previous query."""

    def __init__(self, owner=None):
        self.scope = lifecycle.CancellationScope()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_SEARCH_TASKS)
        self.pool = None
        self.active = None
        self.closed = False
        self.remove_owner = lambda: None
        self.remove_owner = owner.on_cancel(self.close) if owner else lambda: None

    def close(self):
        with self.lock:
            self.closed = True
            pool = self.pool
        self.scope.cancel()
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
        self.remove_owner()

    @staticmethod
    def _invoke(scope, function, args):
        with lifecycle.bind(scope):
            return function(*args)

    def _submit(self, scope, function, *args):
        if not self.slots.acquire(blocking=False):
            raise modrinth.CatalogError("Каталог завершает предыдущие запросы. Повтори поиск через несколько секунд.")
        try:
            with self.lock:
                self.scope.check()
                if self.closed:
                    raise lifecycle.OperationCancelled("Поиск закрыт.")
                if self.pool is None:
                    with lifecycle.bind(self.scope):
                        self.pool = lifecycle.Executor(max_workers=4, thread_name_prefix="bh-catalog-check")
                future = self.pool.submit(self._invoke, scope, function, args)
        except BaseException:
            self.slots.release()
            raise
        # Do not release slots merely because an obsolete query was cancelled:
        # its work may still be queued behind an uninterruptible DNS lookup.
        future.add_done_callback(lambda _future: self.slots.release())
        return future

    @staticmethod
    def _result(future, deadline):
        while True:
            lifecycle.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise modrinth.CatalogError("Каталог не ответил за отведённое время. Повтори поиск.")
            try:
                return future.result(timeout=min(.1, remaining))
            except concurrent.futures.TimeoutError:
                if future.done():
                    return future.result()

    def search(self, profile, kind, query, offset, filters=None, sort="relevance", source="community", *, installed_files=()):
        owner = lifecycle.current_scope()
        scope = lifecycle.CancellationScope()
        remove = self.scope.on_cancel(scope.cancel)
        remove_caller = owner.on_cancel(scope.cancel) if owner else lambda: None
        with self.lock:
            previous, self.active = self.active, scope
        if previous:
            previous.cancel()
        accepted, unavailable = {}, False
        # This deadline includes the original project search as well as the
        # per-release checks, rather than granting each phase its own budget.
        deadline = time.monotonic() + VERIFY_BUDGET
        try:
            with lifecycle.bind(scope):
                page = self._result(self._submit(scope, modrinth.search, profile, kind, query, offset, filters, sort, source), deadline)
                unique = {}
                for hit in page["hits"]:
                    unique.setdefault(hit["id"], hit)
                jobs = {}
                for project, hit in unique.items():
                    lifecycle.check()
                    try:
                        future = self._submit(scope, _confirmed, profile["version"], profile["loader"], kind, project,
                                              hit.get("source", source), int(time.monotonic() // 300))
                    except modrinth.CatalogError:
                        unavailable = True
                        break
                    jobs[future] = project
                pending = set(jobs)
                while pending and time.monotonic() < deadline:
                    lifecycle.check()
                    done, pending = concurrent.futures.wait(pending, timeout=min(.1, max(0, deadline - time.monotonic())),
                                                            return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        try:
                            match = future.result()
                        except lifecycle.OperationCancelled:
                            raise
                        except (ValueError, OSError, HTTPException, KeyError, TypeError):
                            unavailable = True
                        else:
                            if match:
                                accepted[jobs[future]] = match
                unavailable |= bool(pending)
                lifecycle.check()
        finally:
            scope.cancel()
            remove()
            remove_caller()
            with self.lock:
                if self.active is scope:
                    self.active = None
        hits = []
        for project, hit in unique.items():
            if project not in accepted:
                continue
            match = accepted[project]
            # Release metadata has already been fetched for compatibility. Match
            # local bytes against it without one more network request per card.
            installed = next((release for release in match["releases"]
                              if installed_files and any(modrinth._installed(installed_files, candidate)
                                                         for candidate in modrinth._choices(release))), None)
            hits.append({**hit, "compatibility": "release-metadata", "recommendedVersion": match["version"],
                         "installed": installed is not None, "installedVersion": installed.version if installed else ""})
        result = {**page, "hits": hits,
                  "verified": True, "totalIsEstimate": True}
        if unavailable:
            result.update(partial=True, message="Часть выпусков не удалось проверить. Показаны только дополнения с подтверждённой версией Minecraft и загрузчиком; повтори поиск позже.")
        return result


def search(profile, kind, query, offset, filters=None, sort="relevance", source="community"):
    """Convenience for non-window callers; the HTTP API retains its own service."""
    service = SearchService(lifecycle.current_scope())
    try:
        return service.search(profile, kind, query, offset, filters, sort, source)
    finally:
        service.close()
