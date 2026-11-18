package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func fixture(t *testing.T) (*os.Root, string) {
	t.Helper()
	dir := t.TempDir()
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { root.Close() })
	return root, dir
}

func writeFixture(t *testing.T, root *os.Root, name, value string) {
	t.Helper()
	if err := root.MkdirAll(filepath.Dir(name), 0700); err != nil {
		t.Fatal(err)
	}
	if err := root.WriteFile(name, []byte(value), 0600); err != nil {
		t.Fatal(err)
	}
}

func validRecord() map[string]any {
	return map[string]any{
		"format": 1, "session": strings.Repeat("a", 24),
		"owner":    map[string]any{"pid": 100, "birth": "111"},
		"profiles": map[string]any{},
	}
}

func marshalled(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestSessionRecordsDoNotTreatCorruptionAsIdle(t *testing.T) {
	dead := func(identity) (bool, error) { return false, nil }
	for _, bad := range []any{nil, []any{}, "empty", 0, map[string]any{strings.Repeat("b", 32): nil}} {
		record := validRecord()
		record["profiles"] = bad
		if err := checkSession(marshalled(t, record), dead); err == nil {
			t.Fatalf("accepted corrupt profiles %#v", bad)
		}
	}
	for _, bad := range []any{map[string]any{"pid": true, "birth": "1"}, map[string]any{"pid": 123, "birth": ""}, map[string]any{"pid": 0, "birth": "1"}, map[string]any{"pid": 1, "birth": 1}, map[string]any{"pid": 1, "birth": "-1"}} {
		record := validRecord()
		record["owner"] = bad
		if err := checkSession(marshalled(t, record), dead); err == nil {
			t.Fatalf("accepted corrupt identity %#v", bad)
		}
	}
	record := validRecord()
	record["profiles"] = map[string]any{strings.Repeat("b", 32): map[string]any{"stage": "game", "process": nil}}
	if err := checkSession(marshalled(t, record), dead); err == nil {
		t.Fatal("game with missing PID considered idle")
	}
}

func TestOrphanGameBlocksAndReusedPIDDoesNot(t *testing.T) {
	record := validRecord()
	record["owner"] = nil
	record["profiles"] = map[string]any{strings.Repeat("b", 32): map[string]any{"stage": "game", "process": identity{222, "123456"}}}
	probe := func(id identity) (bool, error) { return id == (identity{222, "123456"}), nil }
	if err := checkSession(marshalled(t, record), probe); err == nil {
		t.Fatal("orphan JVM accepted")
	}
	probe = func(id identity) (bool, error) { return id == (identity{222, "654321"}), nil }
	if err := checkSession(marshalled(t, record), probe); err != nil {
		t.Fatal("recycled PID blocked:", err)
	}
	probe = func(identity) (bool, error) { return false, os.ErrPermission }
	if err := checkSession(marshalled(t, record), probe); !errors.Is(err, os.ErrPermission) {
		t.Fatalf("permission uncertainty not propagated: %v", err)
	}
}

func TestRecordFilesAreBoundedAndLinksAreRejected(t *testing.T) {
	root, dir := fixture(t)
	writeFixture(t, root, "launcher-sessions/0.json", string(marshalled(t, validRecord())))
	if err := checkRecords(root, func(identity) (bool, error) { return false, nil }); err != nil {
		t.Fatal(err)
	}
	writeFixture(t, root, "launcher-sessions/update.json", `{"format":1,"session":"aaaaaaaaaaaaaaaaaaaaaaaa","helper":{"pid":123,"birth":"10"}}`)
	if err := checkRecords(root, func(id identity) (bool, error) { return id.PID == 123, nil }); err == nil {
		t.Fatal("live update helper accepted")
	}
	writeFixture(t, root, "launcher-sessions/update.json", strings.Repeat(" ", 65537))
	if _, err := readRecord(root, "launcher-sessions/update.json"); err == nil {
		t.Fatal("oversized record accepted")
	}
	if err := root.Remove("launcher-sessions/update.json"); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "external.json")
	if err := os.WriteFile(out, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(out, filepath.Join(dir, "launcher-sessions/update.json")); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	if _, err := readRecord(root, "launcher-sessions/update.json"); err == nil {
		t.Fatal("redirected record accepted")
	}
}

func TestProtectedRemovalKeepsBarriersAndExternalLinks(t *testing.T) {
	root, dir := fixture(t)
	preserve := map[string]bool{
		"logs/startup.log":                   true,
		"launcher-sessions/operations.lock":  true,
		"launcher-sessions/registry.lock":    true,
		"launcher-sessions/update.json":      true,
		"activation-v10/.session/owner.lock": true,
	}
	for name := range preserve {
		writeFixture(t, root, name, "barrier")
	}
	for _, name := range []string{"instances/a/saves/world/level.dat", "deleted-instances/b/world", "settings.sqlite3", "game-cache/runtime/java.exe", "app/runtime/python.exe", "prism/data/accounts.json", "skins/player.png", "backups/world.zip", "launcher-sessions/0.json", "activation-v10/.session/session.json"} {
		writeFixture(t, root, name, "owned data")
	}
	external := t.TempDir()
	if err := os.WriteFile(filepath.Join(external, "keep.txt"), []byte("external data"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(external, filepath.Join(dir, "external-link")); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	if err := removeProtected(root, preserve); err != nil {
		t.Fatal(err)
	}
	for name := range preserve {
		data, err := root.ReadFile(name)
		if err != nil || string(data) != "barrier" {
			t.Fatalf("barrier lost %s: %v", name, err)
		}
	}
	entries, err := readDir(root, ".")
	if err != nil {
		t.Fatal(err)
	}
	names := []string{}
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if !reflect.DeepEqual(names, []string{"activation-v10", "launcher-sessions", "logs"}) {
		t.Fatalf("remaining entries %v", names)
	}
	if data, err := os.ReadFile(filepath.Join(external, "keep.txt")); err != nil || string(data) != "external data" {
		t.Fatalf("external target changed: %v", err)
	}
}

func TestGamePreflightChecksAllFilesBeforeRemovalAndSkipsLinks(t *testing.T) {
	root, dir := fixture(t)
	for _, name := range []string{"instances/a/Logs/LATEST.log", "instances/a/saves/a/Session.lock", "instances/b/saves/b/session.lock", "prism/data/instances/c/.minecraft/logs/latest.log", "deleted-instances/z/saves/z/session.lock"} {
		writeFixture(t, root, name, "active")
	}
	writeFixture(t, root, "instances/a/saves/a/level.dat", "world remains")
	external := t.TempDir()
	if err := os.WriteFile(filepath.Join(external, "session.lock"), []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(external, filepath.Join(dir, "instances/a/saves/link")); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	var checked []string
	err := gameFiles(root, func(name string) error { checked = append(checked, name); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if len(checked) != 5 {
		t.Fatalf("checked %v", checked)
	}
	err = gameFiles(root, func(name string) error {
		if strings.HasSuffix(name, "b/session.lock") {
			return os.ErrPermission
		}
		return nil
	})
	if err == nil {
		t.Fatal("busy world allowed")
	}
	data, err := root.ReadFile("instances/a/saves/a/level.dat")
	if err != nil || string(data) != "world remains" {
		t.Fatal("preflight changed a world")
	}
}

func TestBarrierNamesUseWindowsCaseComparison(t *testing.T) {
	root, _ := fixture(t)
	writeFixture(t, root, "Logs/Startup.Log", "bootstrap barrier")
	writeFixture(t, root, "Launcher-Sessions/Registry.lock", "registry barrier")
	writeFixture(t, root, "Launcher-Sessions/0.JSON", "stale row")
	preserve := map[string]bool{"logs/startup.log": true, "launcher-sessions/registry.lock": true}
	if err := removeProtected(root, preserve); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"Logs/Startup.Log", "Launcher-Sessions/Registry.lock"} {
		if _, err := root.Stat(name); err != nil {
			t.Fatal("case variant barrier lost:", name, err)
		}
	}
	if _, err := root.Stat("Launcher-Sessions/0.JSON"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("stale row left:", err)
	}
}

func TestEmptyFinalRemoveDoesNotSweepNewData(t *testing.T) {
	root, _ := fixture(t)
	preserve := map[string]bool{"launcher-sessions/update.json": true}
	writeFixture(t, root, "launcher-sessions/update.json", "gate")
	writeFixture(t, root, "instances/a/world", "old")
	if err := removeProtected(root, preserve); err != nil {
		t.Fatal(err)
	}
	if err := root.Remove("launcher-sessions/update.json"); err != nil {
		t.Fatal(err)
	}
	writeFixture(t, root, "launcher-sessions/0.json", "new launcher")
	if err := root.Remove("launcher-sessions"); err == nil {
		t.Fatal("nonempty final directory removed")
	}
	if data, err := root.ReadFile("launcher-sessions/0.json"); err != nil || string(data) != "new launcher" {
		t.Fatal("new state was swept")
	}
}
