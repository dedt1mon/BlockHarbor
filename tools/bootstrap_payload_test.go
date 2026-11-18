// Run on any build host with: go test bootstrap_payload.go bootstrap_payload_test.go
package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func example(t *testing.T, name string) []byte {
	t.Helper()
	buf := new(bytes.Buffer)
	z := zip.NewWriter(buf)
	w, err := z.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = w.Write([]byte("launcher code")); err != nil {
		t.Fatal(err)
	}
	if err = z.Close(); err != nil {
		t.Fatal(err)
	}
	payload := buf.Bytes()
	out := new(bytes.Buffer)
	out.WriteString("native-executable")
	out.Write(payload)
	if err = binary.Write(out, binary.LittleEndian, uint64(len(payload))); err != nil {
		t.Fatal(err)
	}
	h := sha256.Sum256(payload)
	out.Write(h[:])
	out.WriteString(trailerMagic)
	return out.Bytes()
}
func openBytes(t *testing.T, b []byte) (payload, error) {
	t.Helper()
	name := filepath.Join(t.TempDir(), "BlockHarbor.exe")
	if err := os.WriteFile(name, b, 0600); err != nil {
		t.Fatal(err)
	}
	return openExample(t, name)
}
func openExample(t *testing.T, name string) (payload, error) {
	t.Helper()
	f, err := os.Open(name)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	return readPayload(f)
}
func TestSetupAndRepair(t *testing.T) {
	p, err := openBytes(t, example(t, "main.py"))
	if err != nil {
		t.Fatal(err)
	}
	user := t.TempDir()
	world := filepath.Join(user, "world-data")
	os.WriteFile(world, []byte("world"), 0600)
	base := filepath.Join(user, "app")
	var lines []string
	report := func(s string) { lines = append(lines, s) }
	first, err := provision(base, p, report)
	if err != nil || len(lines) == 0 {
		t.Fatalf("fresh: %v %v", err, lines)
	}
	lines = nil
	next, err := provision(base, p, report)
	if err != nil || first != next || len(lines) != 0 {
		t.Fatalf("warm: %v %v", err, lines)
	}
	file := filepath.Join(next, "main.py")
	if err = os.WriteFile(file, []byte("altered code!"), 0600); err != nil {
		t.Fatal(err)
	}
	past := time.Now().Add(-time.Hour)
	if err = os.Chtimes(file, past, past); err != nil {
		t.Fatal(err)
	}
	next, err = provision(base, p, report)
	if err != nil || first == next {
		t.Fatalf("repair: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(next, "main.py"))
	if err != nil || string(data) != "launcher code" {
		t.Fatalf("restored code: %q %v", data, err)
	}

	// Some copies and corruption preserve both size and file timestamps.
	// The content digest must still trigger a new, intact generation.
	previous := next
	file = filepath.Join(previous, "main.py")
	stat, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(file, []byte("altered code!"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.Chtimes(file, stat.ModTime(), stat.ModTime()); err != nil {
		t.Fatal(err)
	}
	next, err = provision(base, p, report)
	if err != nil || next == previous {
		t.Fatalf("same-timestamp repair: %v", err)
	}
	data, err = os.ReadFile(filepath.Join(next, "main.py"))
	if err != nil || string(data) != "launcher code" {
		t.Fatalf("digest restoration: %q %v", data, err)
	}
	if err = os.Remove(filepath.Join(next, "main.py")); err != nil {
		t.Fatal(err)
	}
	next, err = provision(base, p, report)
	if err != nil {
		t.Fatal(err)
	}
	// A dangling pointer represents a previously interrupted or externally cleaned installation.
	if err = os.WriteFile(filepath.Join(filepath.Dir(next), "current"), []byte("files-missing"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = provision(base, p, report); err != nil {
		t.Fatal(err)
	}
	if kept, err := os.ReadFile(world); err != nil || string(kept) != "world" {
		t.Fatalf("world: %q %v", kept, err)
	}
}
func TestIncompleteAndInvalidArchive(t *testing.T) {
	b := example(t, "main.py")
	if _, err := openBytes(t, b[:len(b)-1]); err == nil {
		t.Fatal("accepted incomplete footer")
	}
	b[38] ^= 8
	p, err := openBytes(t, b)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = provision(t.TempDir(), p, func(string) {}); err == nil || !strings.Contains(err.Error(), "SHA-256") {
		t.Fatalf("damaged bytes: %v", err)
	}
	p, err = openBytes(t, example(t, "../outside"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = provision(t.TempDir(), p, func(string) {}); err == nil {
		t.Fatal("accepted path outside install")
	}
}

func TestSilentPreparationKeepsLogsAndReportsOnlyCompletedWork(t *testing.T) {
	p, err := openBytes(t, example(t, "main.py"))
	if err != nil {
		t.Fatal(err)
	}
	base := t.TempDir()
	log := new(bytes.Buffer)
	first, prepared, err := provisionLogged(base, p, log)
	if err != nil || !prepared || !strings.Contains(log.String(), "Компоненты готовы") {
		t.Fatalf("cold preparation/log: %v %v %q", prepared, err, log.String())
	}
	log.Reset()
	again, prepared, err := provisionLogged(base, p, log)
	if err != nil || prepared || first != again || log.Len() != 0 {
		t.Fatalf("warm launch produced a stale preparation notice: %v %v %q", prepared, err, log.String())
	}
	corrupted := p
	corrupted.digest = strings.Repeat("a", 64)
	if _, prepared, err := provisionLogged(t.TempDir(), corrupted, log); err == nil || prepared {
		t.Fatalf("failed preparation reported success: %v %v", prepared, err)
	}
}

func TestBootstrapNoticeIsIsolatedToTheActualChild(t *testing.T) {
	input := []string{"PATH=kept", "blockharbor_bootstrap_prepared=1", "BLOCKHARBOR_BOOTSTRAP_PREPARED=1", "BLOCKHARBOR_BOOTSTRAP_PID=111", "BLOCKHARBOR_LAUNCHER_EXE=old.exe"}
	for _, prepared := range []bool{false, true} {
		output := bootstrapEnvironment(input, `C:\BlockHarbor.exe`, 222, prepared)
		if len(output) != 4 || output[0] != "PATH=kept" {
			t.Fatalf("environment was not deduplicated/preserved: %q", output)
		}
		flag := "0"
		if prepared {
			flag = "1"
		}
		if output[1] != `BLOCKHARBOR_LAUNCHER_EXE=C:\BlockHarbor.exe` || output[2] != "BLOCKHARBOR_BOOTSTRAP_PID=222" || output[3] != "BLOCKHARBOR_BOOTSTRAP_PREPARED="+flag {
			t.Fatalf("wrong child state: %q", output)
		}
	}
	if input[1] != "blockharbor_bootstrap_prepared=1" {
		t.Fatal("modified parent environment")
	}
}
func TestShippedExecutable(t *testing.T) {
	name := os.Getenv("BLOCKHARBOR_RELEASE_EXE")
	if name == "" {
		t.Skip("set BLOCKHARBOR_RELEASE_EXE to check the release")
	}
	p, err := openExample(t, name)
	if err != nil {
		t.Fatal(err)
	}
	base := t.TempDir()
	first, err := provision(base, p, func(s string) { t.Log(s) })
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"main.py", "runtime/pythonw.exe", "runtime/python.exe", "runtime/python313.dll", "ui/index.html"} {
		if _, err = os.Stat(filepath.Join(first, f)); err != nil {
			t.Fatal(err)
		}
	}
	again, err := provision(base, p, func(s string) { t.Fatalf("warm extracted again: %s", s) })
	if err != nil || again != first {
		t.Fatalf("warm: %s %v", again, err)
	}
	if err = os.Remove(filepath.Join(first, "runtime", "pythonw.exe")); err != nil {
		t.Fatal(err)
	}
	fixed, err := provision(base, p, func(s string) { t.Log(s) })
	if err != nil || fixed == first {
		t.Fatalf("runtime repair: %s %v", fixed, err)
	}
}
