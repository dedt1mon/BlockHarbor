# Runtime, installation and transfer audit

Audit date: **2026-11-24**. Original comparison base: `9a8b93c0ffa98c44c6340841c3db6a49e78dcfb1`, release version **2.6.8**. Actual runs used the **modified shared working tree**, not that unchanged base. No version change or production release is part of this component work. [Source provenance](evidence/runtime/source-provenance.json) records final file hashes captured **after** the attempts, with explicit timing and retained-checkpoint comparisons; it is not presented as a per-launch source snapshot.

This report distinguishes real publisher downloads, verified installed artifacts, running Java processes, visible Minecraft screens, and gameplay. They are separate observations. The retained [live matrix](evidence/runtime/live-matrix.json) contains **32 completed attempts across 17 loader/game-version pairs**, including earlier failures and later recovery; [catalog evidence](evidence/runtime/latest-supported-metadata.json) records the actual current release and loader metadata.

## Confirmed defects and implemented changes

| Defect and evidence | Root cause | Correction |
| --- | --- | --- |
| A real Windows CI run hung for seven minutes in destination validation | `Path.resolve()` expanded a short `RUNNER~1` alias in the root, while the destination retained its short spelling. A lexical parent loop could never reach the resolved root and repeated the drive root forever | Finite ancestor traversal, canonical ancestor comparison, and symlink/junction rejection. Native Windows short-path regression has an eight-second subprocess bound |
| Parent cancellation did not cancel the active catalog transfer | The transfer manager rebound its own cancellation scope and lost the enclosing operation | The parent scope is captured before rebinding; its callback cancels only the owned task and aborts its response. The manager remains usable for a subsequent request |
| Windows stalled-body cancellation still waited in `SocketIO.recv_into` after `shutdown` | `HTTPResponse` retained a `makefile()` reference. Python's socket wrapper was logically closed while its native handle remained open | Abort invalidates and closes the owned Python socket through `_real_close()`, after shutdown. No bare OS descriptor close is used. A regression verifies that a later stream close cannot close an unrelated reused socket. Native verification is tracked separately below |
| Same-size corruption with restored mtime could evade cached hashes on Windows | `st_ctime` is creation time on Windows; a first attempted `FILE_BASIC_INFO.ChangeTime` fix also collided in actual rapid-write CI | The shared file stamp reads a supported file USN plus file-reference identity. Unsupported filesystems/API responses use a real SHA-256 fingerprint. Typed 64-bit Windows handles and bounded V2/V3 record parsing are used; no journal is created or changed |
| Client/library corruption escaped readiness checks | Presence and matching file size were treated as readiness | Declared hashes are verified with the common file stamp. Corruption triggers repair, while correct unchanged files reuse the verification cache |
| Java and content-icon caches used incompatible or weak stamps | Java retained size/mtime only; several callers constructed raw-stat tuples | Managed/legacy Java cache and Store content icons use the common stamp. BuildCare caller migration is covered by the main audit |
| Disk-full, permissions and path errors were retried as network failures | Transfer code caught broad `OSError` in its retry branch | Specific storage diagnostics separate disk/quota, permissions/read-only storage, long paths and descriptor exhaustion. A typed `TransferNetworkError` identifies exhausted transport/HTTP failures for narrowly allowed fallback |
| A trickling peer could keep a download alive indefinitely | Per-read socket timeout did not bound total receiving time | Transfers have one 30-minute deadline across attempts; preparation downloads have a 20-minute receive budget. `read1` allows cancellation/deadline checks between received chunks for both urllib and requests |
| Correct final files were downloaded again; one bad queue record discarded unrelated recovery state | Reuse considered only partial files; queue parsing had an outer exception handler | Declared hashes/size are checked before offline reuse. Saved queue records are validated independently |
| Asset/runtime preparation repeatedly established HTTPS connections | The adapter ignored the supplied session | Bounded sessions per worker reuse connections only for checksum-pinned Mojang asset/runtime hosts, preserve system TLS/proxy settings, and close with the job |
| Interrupted preparation left private scratch downloads | Cleanup depended on normal stack unwinding | The next exclusive installation removes only exact private temporary-file patterns inside the cache; unrelated and published files remain |
| Actual Forge 1.18.2 installation failed with `FileExistsError` at `natives/META-INF` | Parallel JAR extraction raced between an existence check and directory creation in the upstream library | Only native extraction is serialized with a cancellation-aware scoped lock; downloads remain parallel. An actual parallel ZIP regression reproduces the old race |
| Forge's Java `DOWNLOAD_MOJMAPS` processor failed behind the execution environment's proxy | The simple mapping download occurred inside Java rather than the launcher's verified transport | A narrow adapter recognizes only two known main classes and the exact client mapping-download argument shape. It uses the official base manifest's HTTPS URL, SHA-1 and size, checks the exact Maven target path, and publishes through the existing verified downloader. Other processor tasks still run Java |
| Mod Java/loader constraints were skipped | Platform dependency IDs were excluded from checks | Known incompatible bounds block; unknown declared bounds require review. Dedicated-server Fabric/Quilt descriptors block client installation |

The mapping adapter was corrected after actual Forge installer metadata exposed timestamped Maven coordinates such as `1.20.1-20230612.114412`. Both the ordinary and timestamped official forms are checked; arbitrary paths remain rejected. The initial failed guard is retained in the matrix. SHA-1 is normalized after validating its shape. The adapter does not produce transformed JARs, skip arbitrary processors, or trust a mirror without an authoritative checksum.

`compatibility.installation_profile(store, pid)` returns a copy, enriched only when the local install marker, cache location, base manifest and installed manifest agree. It neither changes the Store schema nor executes imported Java paths. A custom Java path leaves its major version unknown. Quilt's loader version is not substituted for its exported Fabric compatibility version. Physical JAR `environment=server` is distinct from Modrinth's new `server_only` label, which can support singleplayer.

The 2.6.8 NeoForge primary/alternate transport and official-checksum mirror rules remain in place. TLS verification and artifact integrity checks remain enabled.

## Automated regression evidence

The most recent focused local reruns after the native socket-close change used Python 3.12.14 and the repository dependencies:

| Suites | Run | Passed | Skipped | Failed |
| --- | ---: | ---: | ---: | ---: |
| `test_transfer*.py` | 26 | 25 | 1 | 0 |
| `test_lifecycle.py`, `test_preparation*.py`, `test_asset_pooling.py` | 39 | 39 | 0 | 0 |
| **These two reruns** | **65** | **64** | **1** | **0** |

The local skip requires actual Windows short-path aliases. Earlier focused passes additionally covered game/loader compatibility, direct launch, platform metadata, native extraction, processor mapping downloads and file stamps; the main audit records final whole-suite counts rather than adding overlapping runs together.

Important tests use real local HTTP sockets: a disconnect followed by `Range` and `If-Range` recovery; a blocked body read cancelled by its parent; a slow trickle bounded by a deadline; and HTTP/1.1 connection reuse. Fault injection covers full disk, access denial, TLS verification failure, altered hashes and malformed queue records. These are meaningful mechanism tests, not evidence of arbitrary public CDN reliability or gameplay.

Native Windows CI checkpoint 4 ran 668 tests and confirmed the **USN strategy** detects same-size, restored-mtime modifications. It also exposed the `recv_into` cancellation failure, which motivated the owned-socket close correction. **Checkpoint 6's actual Windows Python suite passed**, including the original stalled-body cancellation gate and the new socket ownership regression; its portable/NSIS builds also passed. Checkpoint 7 passed native cancellation again; its separate multi-session fixture failed because a Windows file was concurrently open, with follow-up owned by the main audit. Final whole-suite/package counts and any remaining intermittent failure belong in the main audit report.

## Actual current versions and network behavior

The live Mojang manifest reported release **26.3** and snapshot **26.4-snapshot-10**. All four live loader catalogs listed 26.3 as supported. Preferred Fabric/Quilt loaders were 0.20.1/0.30.2; current Forge metadata included 26.3-66.1.9; NeoForge metadata included 26.3.0.117. A successful NeoForge installation used the build selected by the production path, 26.3.0.113. These exact observations are retained in [latest-supported-metadata.json](evidence/runtime/latest-supported-metadata.json).

Availability varied: an early plain urllib Forge request returned 403, a NeoForge quick probe hit its five-second bound, and later production catalog requests succeeded. Forge 26.3's first preparation failed downloading from `launchermeta.mojang.com`; one bounded repeat completed installation and repeat preparation. No blanket service-availability claim is made.

## Real installation and graphics matrix

All cases use isolated data, real publisher artifacts and a temporary local `BHAuditPlayer` identity. No Microsoft credentials are used. These are Linux launches. Java 17, 21 and 25 were selected and downloaded from Mojang runtime metadata; system Java was not assumed to satisfy all cases.

| Configuration | Install / repeat / delete | Java | Actual startup evidence |
| --- | --- | --- | --- |
| Vanilla 1.18.2 | Passed / passed / passed | 17 | Two real processes reached LWJGL/texture initialization; this early run did not capture a PNG |
| Vanilla 1.20.1 | Passed / passed / passed | 17 | Initial no-display exits 255; later real Welcome/Accessibility frame |
| Vanilla 1.21.11 | Passed / passed / passed | 21 | Welcome/Accessibility frame, settings change and second launch completed |
| Vanilla 26.3 | Passed / passed / passed | 25 | Actual graphics-backend error; no usable GLX/Vulkan in this environment |
| Fabric 1.18.2 / 0.20.1 | Passed / passed / passed | 17 | Two real processes reached renderer/texture initialization; no PNG in this early run |
| Fabric 1.20.1 / 0.20.1 | Passed / passed / passed | 17 | Real Welcome frame; initial no-display failures retained |
| Fabric 1.21.11 / 0.20.1 | Passed / passed / passed | 21 | Welcome frame after actual FerriteCore install and settings change; loader log lists FerriteCore 8.2.0 |
| Fabric 26.3 / 0.20.1 | Passed / passed / passed | 25 | Retained real GLX/Vulkan error dialog |
| Forge 1.18.2 / 40.3.0 | Passed / passed / passed after fixes | 17 | Retained real main menu with Forge version and 2 mods loaded |
| Forge 1.20.1 / 47.4.28 | Passed / passed / passed after fixes | 17 | Real Welcome frame after FerriteCore 6.0.1 installation and settings; Forge reports the actual mod JAR |
| Forge 26.3 / 66.1.9 | Passed / passed / passed after one retry | 25 | Two actual starts report GLX/Vulkan backend failure; initial metadata-download failure is retained |
| NeoForge 1.21.1 / 21.1.267 | Passed / passed / passed after retry | 21 | Real Welcome frame; loader initialization in log |
| NeoForge 1.21.11 / 21.11.48 | Passed / passed / passed | 21 | Retained real Welcome frame after EntityCulling 1.12.2 installation and settings; loader log lists the mod |
| NeoForge 26.3 / 26.3.0.113 | Passed / passed / passed | 25 | Real NeoForge error dialog: failed window creation / GLX visual |
| Quilt 1.18.2 / 0.30.2 | Passed / passed / passed | 17 | Two real processes reached renderer/texture initialization; no PNG in this early run |
| Quilt 1.20.1 / 0.30.2 | Passed / passed / passed | 17 | Real Welcome frame and settings readback; QSL was safely held for compatibility review |
| Quilt 26.3 / 0.30.2 | Passed / passed / passed | 25 | Actual GLX/Vulkan backend error |

The retained [screenshots manifest](evidence/runtime/screenshots.json) ties three original screenshots to their attempt timestamps and SHA-256. Equal Welcome screens from different loaders can be byte-identical; loader identity is established by the installed manifest and loader log, not the logo alone. Earlier raw `running-observed` records are preserved, with a separate graphics assessment from logs/PNG. An alive Java process displaying an error dialog is not a successful graphical launch.

Later Java installer experiments use an explicit existing credential-free proxy and the existing OS Java truststore **only in the isolated test environment**, retaining TLS verification. The whole NeoForge installer still runs in Java; no product proxy/truststore modification was introduced to make a sandbox test pass. The initial environment had no display, and isolated Xorg later supplied a software display for versions that it can render. Audio, account services, multiplayer and world gameplay remain unverified.

## Thirteen lifecycle steps and real content

The opt-in lifecycle mode checks profile creation, production version catalogs and selected-version presence; downloads/loader/libraries and declared artifact validation; manifest-selected Java; two real starts; actual profile rename and economy settings through `Experience`/`PerformanceProfiles`; real Modrinth planning, transfer and publication; profile removal; and interruption recovery. [Lifecycle coverage](evidence/runtime/lifecycle-coverage.json) maps the actual observations to all thirteen requested steps for each loader, including the unclosed Quilt mod step.

Completed extended cases:

| Case | Actual settings readback | Mod and recovery | Second-start evidence |
| --- | --- | --- | --- |
| Vanilla 1.21.11 | Rename, distances 6/5, economy graphics, 60 FPS cap and 3072 MiB heap | Mods not applicable; real preparation cancelled at one second and recovered | Actual Welcome screen with the changed heap |
| Fabric 1.21.11 | Same production settings/readback | FerriteCore 8.2.0-fabric from Modrinth; cancelled after 16,384 received bytes, zero JARs published, partial preserved, real range resume succeeded; SHA-512 matched, local compatibility had no conflicts/warnings | Loader log lists `ferritecore 8.2.0`; actual Welcome screen |
| Forge 1.20.1 | Same production settings/readback | FerriteCore 6.0.1; cancelled after 16,384 bytes, zero JARs published, actual range resume and hash verification passed | Forge log reports the mod JAR; Welcome frame after installation |
| NeoForge 1.21.11 | Same production settings/readback | EntityCulling 1.12.2 (1,535,395 bytes): cancelled after 7,533 bytes, zero JARs published, actual range resume passed, SHA-512 and local metadata verified with no warnings | NeoForge lists `EntityCulling 1.12.2`; retained actual Welcome frame after installation |
| Quilt 1.20.1 | Same production settings/readback | QSL cancelled after 16,384 bytes, range recovery completed, then metadata required review; it was not installed | Second clean-game start completed. Successful mod installation/load is **not verified** |

No unknown-bound warning is automatically acknowledged by the harness. Actual published releases and SHA-512, partial-byte counts, post-cancel publication count, retry result and loader-log mentions are retained in the machine-readable matrix. A log mention is not presented as proof of a mod's complete gameplay behavior. One harness measurement initially took the maximum byte count from the whole saved queue; the corrected harness scopes metrics to the current operation. Existing evidence explicitly records the correction from matching artifact/time-specific cancelled tasks while retaining the original reported count.

**Known compatibility limitation:** the conservative version matcher does not yet implement every Maven/SemVer qualifier. Actual FerriteCore metadata declared NeoForge `[21.11.37-beta,)`; QSL's bundled modules declared snapshot/prerelease/build-metadata bounds. These produce explicit review warnings, despite a later numeric release likely satisfying the author's intent. The audit does not silently accept them or expand comparison semantics without separate validation. The actual warning strings are in the matrix.

The initial NeoForge FerriteCore attempt recovered its actual transfer after 1,378 bytes, then stopped at review without publication; the later EntityCulling attempt passed. A bounded Quilt alternative inspection did not produce a complete warning-free dependency plan: candidates either declared unsupported bounds, an unconfirmed Fabric compatibility version, or dependencies on QSL/Fabric API. No confirmation was fabricated. For Quilt, requirement 11 (a successfully installed and loaded compatible mod) remains an explicit acceptance gap; the other lifecycle operations above were exercised.

### Measured asset recovery

The initial Vanilla 1.20.1 preparation downloaded 625 unique asset objects after approximately 487 seconds, including earlier libraries. It was actually interrupted with exit code 130. With connection reuse and existing valid objects preserved, the remaining 2952 unique objects, **530,494,545 bytes**, completed in **51.2 seconds**. This is one environment and different preparation phases/cache contents, not a universal speed ratio. The independent HTTP/1.1 fixture confirms the connection-reuse mechanism.

## Reproducing the opt-in checks

`tools/smoke_loader_matrix.py` refuses an existing unmarked data root, archives earlier attempts on rerun, only terminates its own game process, and never borrows account credentials. Run against an isolated directory:

```powershell
python tools/smoke_loader_matrix.py --root C:/AI/BlockHarbor-runtime-audit --launch
```

For real settings, a small compatible mod, cancellation and retry:

```powershell
python tools/smoke_loader_matrix.py --root C:/AI/BlockHarbor-runtime-audit --case fabric:1.21.11 --launch --exercise-lifecycle --interrupt-first 1 --mod-project fabric:ferrite-core --cancel-first-mod
```

`--case loader:version` is repeatable. `--capture-display` is opt-in and restricted to an isolated Linux test display; do not use it on a personal desktop. A manually visible Windows run is still needed for native desktop acceptance. Per-case JSON, preparation/game logs and optional screenshots remain under the isolated audit root; only sanitized result summaries and the representative screenshots are retained in the repository.

## Primary technical references

- [Fabric dependency/environment specification](https://wiki.fabricmc.net/documentation:fabric_mod_json_spec): Fabric caret ranges have no npm-style major-zero special case; `^0.3` matching `0.4.2` is preserved and tested.
- [Quilt metadata specification](https://github.com/QuiltMC/rfcs/blob/main/specification/0002-quilt.mod.json.md): dedicated-server environment is `minecraft.environment=dedicated_server`.
- [Windows per-file USN query](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_read_file_usn_data) and [input record](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-read_file_usn_data): read-only native change evidence, with content-fingerprint fallback.
- [CPython socket ownership](https://github.com/python/cpython/blob/3.13/Lib/socket.py) and [native close implementation](https://github.com/python/cpython/blob/3.13/Modules/socketmodule.c): deferred `makefile()` ownership and invalidating the owned descriptor before closing it.

## Remaining acceptance limits

The real matrix is complete for the selected cases. Remaining acceptance gaps are a warning-free Quilt mod installation/load, the broader unsupported version-expression grammar, actual clean Windows 11 game launch, native DPI, physical audio, multiplayer, authentication and world gameplay. Current 26.3 graphics remain blocked by this execution environment even though all five loaders installed and passed repeat preparation. These limits must remain explicit in release review; this component report does not authorize a release.
