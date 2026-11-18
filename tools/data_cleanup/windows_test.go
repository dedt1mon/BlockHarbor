//go:build windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"unsafe"
)

// These tests use temporary fixtures only. Cross-compilation is checked on other
// platforms; executing this file requires a real Windows runner.
func TestWindowsBootstrapHandleBlocksAppend(t *testing.T) {
	root, dir := fixture(t)
	writeFixture(t, root, "logs/startup.log", "old")
	c := &coordinator{root: root, canonical: dir, locks: map[string]syscall.Handle{}, pins: map[string]*directoryPin{}}
	defer c.close()
	if err := c.acquire("logs/startup.log"); err != nil {
		t.Fatal(err)
	}
	if f, err := os.OpenFile(filepath.Join(dir, "logs/startup.log"), os.O_WRONLY|os.O_APPEND, 0600); err == nil {
		f.Close()
		t.Fatal("bootstrap can append while cleanup holds the barrier")
	}
	c.releaseLock("logs/startup.log")
	f, err := os.OpenFile(filepath.Join(dir, "logs/startup.log"), os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal("barrier not released:", err)
	}
	f.Close()
}

func TestWindowsLegacyByteLockMatchesLauncher(t *testing.T) {
	root, dir := fixture(t)
	name := "activation-v10/.session/owner.lock"
	writeFixture(t, root, name, "")
	c := &coordinator{root: root, canonical: dir, locks: map[string]syscall.Handle{}, pins: map[string]*directoryPin{}}
	defer c.close()
	h, err := openNative(filepath.Join(dir, filepath.FromSlash(name)), syscall.GENERIC_READ|syscall.GENERIC_WRITE, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE, syscall.OPEN_EXISTING, 0)
	if err != nil {
		t.Fatal(err)
	}
	var ov syscall.Overlapped
	ok, _, nativeErr := lockFile.Call(uintptr(h), 1, 0, 1, 0, uintptr(unsafe.Pointer(&ov)))
	if ok == 0 {
		syscall.CloseHandle(h)
		t.Fatal(nativeErr)
	}
	if err := c.acquire(name); err == nil {
		syscall.CloseHandle(h)
		t.Fatal("cleanup ignored live shared launcher lock")
	}
	syscall.CloseHandle(h)
	if err := c.acquire(name); err != nil {
		t.Fatal("stale lock file blocks cleanup:", err)
	}
}

func TestWindowsRootPinsAndJunctionRemoval(t *testing.T) {
	dir := t.TempDir()
	data := filepath.Join(dir, "BlockHarbor")
	outside := t.TempDir()
	if err := os.Mkdir(data, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outside, "keep.txt"), []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("cmd.exe", "/d", "/c", "mklink", "/J", filepath.Join(data, "junction"), outside)
	if output, err := command.CombinedOutput(); err != nil {
		t.Skipf("junction creation unavailable: %s (%v)", output, err)
	}
	parent, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer parent.Close()
	pin, err := pinDirectory(data, false, true)
	if err != nil {
		t.Fatal(err)
	}
	defer pin.close()
	root, err := parent.OpenRoot("BlockHarbor")
	if err != nil {
		t.Fatal("Root's sharing is incompatible with DELETE pin:", err)
	}
	if err := removeProtected(root, map[string]bool{}); err != nil {
		root.Close()
		t.Fatal(err)
	}
	root.Close()
	if data, err := os.ReadFile(filepath.Join(outside, "keep.txt")); err != nil || string(data) != "outside" {
		t.Fatal("junction target changed", err)
	}
	if err := deletePinned(pin); err != nil {
		t.Fatal(err)
	}
	pin.close()
	if _, err := os.Stat(data); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("root not deleted", err)
	}
}

func TestWindowsProcessBirthProtectsAgainstPIDReuse(t *testing.T) {
	id, err := currentIdentity(uint32(os.Getpid()))
	if err != nil || id == nil {
		t.Fatal(id, err)
	}
	if alive, err := processAlive(*id); err != nil || !alive {
		t.Fatal("own process not recognized", err)
	}
	birth, _ := strconv.ParseUint(id.Birth, 10, 64)
	id.Birth = strconv.FormatUint(birth+1, 10)
	if alive, err := processAlive(*id); err != nil || alive {
		t.Fatal("PID reused identity considered alive", err)
	}
}
