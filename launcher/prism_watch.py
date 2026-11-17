"""Observe the JVM of the selected Prism instance, including launches forwarded by IPC."""

import ctypes
import os
import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()
_WATCHING = set()

# Prism supplies its per-instance natives directory and org.prismlauncher.EntryPoint
# to Java. Check those together. Only a start signal and the exit code cross stdout.
_WATCH = r"""
$ErrorActionPreference = 'Stop'
$owner = [Diagnostics.Process]::GetProcessById([int]$env:BH_OWNER_PID)
$needles = $env:BH_PRISM_NATIVES.Split([char]10)
$filter = "(Name='javaw.exe' OR Name='java.exe') AND CommandLine LIKE '%org.prismlauncher.EntryPoint%'"
$passes = 0
while (-not $owner.HasExited) {
    foreach ($row in Get-CimInstance Win32_Process -Filter $filter -Property ProcessId,CommandLine) {
        $line = ([string]$row.CommandLine).Replace('\','/')
        $matchesDir = $false
        foreach ($dir in $needles) {
            $pattern = '(?i)(?:^|[\s"])-Djava\.library\.path="?' + [regex]::Escape($dir.TrimEnd('/')) + '/?"?(?=\s|$)'
            if ([regex]::IsMatch($line, $pattern)) {
                $matchesDir = $true
                break
            }
        }
        if (-not $matchesDir) { continue }
        try {
            $game = [Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
            $null = $game.Handle
        } catch { continue }
        [Console]::WriteLine('GAME')
        [Console]::Out.Flush()
        $game.WaitForExit()
        exit $game.ExitCode
    }
    $passes++
    if ($passes % 5 -eq 0) {
        $open = Get-CimInstance Win32_Process -Filter "Name='prismlauncher.exe'" -Property ExecutablePath |
            Where-Object { $_.ExecutablePath -eq $env:BH_PRISM_EXE }
        if (-not $open) { break }
    }
    Start-Sleep -Milliseconds 1300
}
"""


def _names(instance):
    # Prism may use a DOS path if a Unicode path is not representable in the
    # current system code page. The natives subfolder can be created later.
    paths = [instance / "natives"]
    short = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(str(instance), short, len(short)):
        paths.append(Path(short.value) / "natives")
    return "\n".join(dict.fromkeys(p.as_posix() for p in paths))


def observe(instance, engine, on_game):
    key = str(instance)
    with _LOCK:
        if key in _WATCHING:
            return
        _WATCHING.add(key)

    def watch():
        try:
            shell = (
                Path(os.environ["SystemRoot"])
                / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            env = dict(
                os.environ,
                BH_OWNER_PID=str(os.getpid()),
                BH_PRISM_NATIVES=_names(instance),
                BH_PRISM_EXE=str(engine),
            )
            with subprocess.Popen(
                [
                    str(shell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _WATCH,
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ) as watcher:
                if watcher.stdout.readline().strip() == b"GAME":
                    on_game(watcher)
                watcher.wait()
        except (OSError, KeyError):
            # Optional window observation must not interrupt an authenticated game.
            return
        finally:
            with _LOCK:
                _WATCHING.discard(key)

    threading.Thread(target=watch, name="blockharbor-prism-game", daemon=True).start()
