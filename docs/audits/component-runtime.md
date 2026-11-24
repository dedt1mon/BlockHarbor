# Runtime, installation and transfer audit

Audit date: 2026-11-24. Base: `9a8b93c0ffa98c44c6340841c3db6a49e78dcfb1` (2.6.8).
These changes do not change the release version or publish a release.

## Confirmed defects and changes

| Defect | Root cause | Implemented correction |
| --- | --- | --- |
| Disk-full and access errors were reported as broken internet and retried | `TransferManager` caught every `OSError` in the network retry branch | Shared storage diagnostics distinguish full disk/quota, permission/read-only storage, path length and file-descriptor exhaustion; these errors do not enter network retries |
| A server could keep a transfer alive indefinitely by trickling bytes | Only socket timeout and maximum size were bounded | One 30-minute transfer budget spans retries; `read1` permits checks between received chunks; cancellation and partial-file recovery remain available |
| Downloading an already correct destination required network again | Only partial files were considered reusable | All declared sizes/hashes are checked before reusing the destination, with no network request when it is correct |
| One malformed saved transfer erased the entire recovered queue | Exception handling surrounded the whole record loop | Each saved record is validated independently; bad records cannot discard unrelated resumable tasks |
| Same-size corruption of a game/client/library JAR escaped pre-launch validation | Readiness checked size alone | Declared SHA-1/SHA-256 is checked using the existing bounded cache keyed by size, mtime and ctime; changed corrupt files trigger repair; unchanged files are not rehashed |
| Java verification could overlook same-size changes with a restored mtime | Durable Java index retained only size and mtime | ctime joins the verification stamp |
| Thousands of game objects caused repeated HTTPS handshakes | The preparation adapter ignored launcher-lib's supplied session and used a new `urlopen` per object | A separate bounded session per download worker reuses connections to checksum-pinned Mojang asset/runtime hosts, preserves proxy/TLS verification and closes with the job |
| A hard process interruption could leave private scratch downloads indefinitely | Only normal stack unwinding removed scratch files | The next installation, under its exclusive install lock, removes only the exact private temporary-file naming patterns; published files and unrelated files are preserved |
| Java and loader constraints in mod metadata were silently ignored | All platform IDs were excluded from dependency checks | Known incompatible platform versions block installation; unknown declared bounds require explicit review; platform dependencies are not sent to a mod search |
| Dedicated-server Fabric/Quilt files were presented as merely unreadable metadata | Environment-specific descriptors returned an empty dependency set | Explicit `fabric.mod.json` `environment=server` and Quilt `minecraft.environment=dedicated_server` block client installation |

The scoped NeoForge primary/alternate transport and authoritative-checksum mirror rules from 2.6.8 remain in place. No TLS verification, checksum validation or existing loader support was disabled.

`compatibility.installation_profile(store, pid)` returns a copy of a stored profile, enriched with loader and managed-Java versions only when the local install marker, base manifest and installed manifest agree. It does not change the database or execute Java paths from imported metadata. Quilt's version is deliberately not treated as its exported Fabric compatibility version.

## Automated runtime checks

The following focused suites were run with the repository's pinned dependencies in Python 3.12.14:

| Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| `test_game*.py` | 34 | 0 | 0 |
| `test_direct.py` | 9 | 0 | 0 |
| `test_loader*.py` | 36 | 0 | 0 |
| `test_preparation.py` | 18 | 0 | 0 |
| `test_asset_pooling.py` | 2 | 0 | 0 |
| `test_transfer*.py` | 22 | 0 | 0 |
| `test_compatibility.py` | 6 | 0 | 0 |
| `test_platform_compatibility.py` | 7 | 0 | 0 |
| **Focused total** | **134** | **0** | **0** |

The transfer integration fixture uses an actual local HTTP socket: the server drops the connection after 4096 bytes, and the next request supplies both `Range: bytes=4096-` and the original `If-Range` validator before checksum-verified publication. Another actual HTTP/1.1 fixture confirms that two requests reuse one TCP connection. These tests do not claim to simulate an arbitrary public CDN, Windows kernel, or graphics driver.

Additional regressions cover specific storage failures, TLS verification failure, a trickling response, hash-corrupt cache files, corrupted queue records, JAR repair after same-size corruption, confirmed/unknown Java and loader bounds, server-only descriptors, and installation markers that disagree with the selected build.

## Live external-service observations

Public publisher endpoints were queried from this execution environment without account credentials. Availability varied between attempts:

| Service | Observation |
| --- | --- |
| Mojang version manifest | HTTP 200; current manifest reported release `26.3` and snapshot `26.4-snapshot-10` |
| Fabric | Native `_fetch_provider` succeeded: 537 game versions; stable loader `0.20.1` |
| Quilt | Native `_fetch_provider` succeeded: 451 game versions; stable loader `0.30.2` |
| Forge | Plain urllib probe first returned HTTP 403; the launcher's later `_fetch_provider` succeeded with 63 game versions |
| NeoForge | Plain urllib probe returned HTTP 200; the later bounded quick catalog probe timed out at 5 seconds |

The contradictory availability observations are preserved rather than replaced with a blanket “all services are working” claim. The bundled/cache fallback is necessary in such conditions.

## Actual installation evidence

Real publisher files are being installed in an isolated scratch data directory with Cyrillic/spaces in instance names. Only temporary `BHAuditPlayer` local identity is used. No Microsoft session or user library is involved.

| Configuration | First installation | Repeat preparation | Java | Actual launch observation | Removal |
| --- | --- | --- | --- | --- | --- |
| Vanilla 1.20.1 | Passed, after an actual interrupted first download | Passed, 1.931 s | Managed Java 17 downloaded automatically | Two real Java processes reached the render thread/LWJGL 3.3.1 and exited 255 in the initial environment without a display; successful graphics/gameplay not verified | Passed |
| Fabric 1.20.1 / 0.20.1 | Passed | Passed, 2.569 s | Managed Java 17 | Two real Java processes exited 255 in the initial environment without a display; successful graphics/gameplay not verified | Passed |
| Quilt 1.20.1 | In progress at this checkpoint | Pending | Pending | Pending | Pending |
| Forge 1.20.1 | Pending at this checkpoint | Pending | Pending | Pending | Pending |

This table is a checkpoint and must be updated before treating the component report as final. These runs do not establish clean Windows installation, all supported version combinations, or gameplay with arbitrary mods.

### Measured download observation

Before connection reuse, the initial live preparation had downloaded 625 unique asset objects after approximately 487 seconds, including earlier library work. That run was actually interrupted. After enabling reuse and preserving those valid objects, the remaining 2952 unique objects (530,494,545 bytes) completed in 51.2 seconds in the same environment. This is one network observation, not a universal speed multiplier; object sets, cached bytes and phases differ. The deterministic connection-reuse test independently confirms the intended mechanism.

## Reproducible live matrix

`tools/smoke_loader_matrix.py` runs real creation, installation, repeat preparation and deletion in a new or explicitly marked audit directory. `--launch` additionally starts each game twice and observes its process for a bounded time. `--interrupt-first` cancels preparation through the production cancellation scope, then retries it. Per-case preparation logs, game logs and machine-readable results are saved inside that isolated directory.

```powershell
python tools/smoke_loader_matrix.py --root C:/AI/BlockHarbor-runtime-audit --launch
```

Default cases include two game versions per loader: Vanilla/Fabric/Quilt 1.20.1 and 26.3; Forge 1.20.1 and 1.21.11; NeoForge 1.21.1 and 26.3. `--case loader:version` selects a smaller matrix. The tool refuses an existing unmarked data directory, never borrows credentials, and only terminates the Java process it created. An observed running process is explicitly **not** reported as verified gameplay.

## Vendor semantics checked against primary documentation

- [Fabric dependency and environment specification](https://wiki.fabricmc.net/documentation:fabric_mod_json_spec): Fabric caret ranges have no npm-style special case for major zero. Existing `^0.3` matching `0.4.2` is preserved and tested.
- [Quilt metadata specification](https://github.com/QuiltMC/rfcs/blob/main/specification/0002-quilt.mod.json.md): dedicated-server environment is explicitly `minecraft.environment=dedicated_server`.

## Remaining release gates

Actual Windows 11 x64 first-launch/gameplay, native display scaling, several real versions per loader, multiplayer, and end-to-end mod combinations still require their corresponding real environments. A passing unit suite, prepared game files, or an alive Java process does not close those gates. The overall report and CI results determine the final aggregate counts and readiness; no release is authorized by this component audit.
