//go:build windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
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

func shortPathFixture(t *testing.T, name string) string {
	t.Helper()
	pointer, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]uint16, 32768)
	n, _, nativeErr := kernel.NewProc("GetShortPathNameW").Call(uintptr(unsafe.Pointer(pointer)), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	if n == 0 || n >= uintptr(len(buf)) {
		t.Fatal("GetShortPathNameW:", nativeError(nativeErr))
	}
	short := normalizePath(syscall.UTF16ToString(buf[:n]))
	if strings.EqualFold(short, normalizePath(name)) {
		t.Skip("temporary volume does not expose DOS short-name aliases")
	}
	return short
}

func TestWindowsDirectoryPinsAcceptShortAncestorAndKeepIdentity(t *testing.T) {
	base := t.TempDir()
	parentName := filepath.Join(base, "Directory pin parent with spaces")
	data := filepath.Join(parentName, "BlockHarbor")
	if err := os.MkdirAll(data, 0700); err != nil {
		t.Fatal(err)
	}
	parentName, err := canonicalDirectory(parentName)
	if err != nil {
		t.Fatal(err)
	}
	data = filepath.Join(parentName, "BlockHarbor")
	shortParent := shortPathFixture(t, parentName)
	parent, err := pinDirectory(shortParent, false, false)
	if err != nil {
		t.Fatal("short ancestor rejected:", err)
	}
	defer parent.close()
	pin, err := pinDirectory(filepath.Join(shortParent, "BlockHarbor"), false, true)
	if err != nil {
		t.Fatal("directory under short ancestor rejected:", err)
	}
	defer pin.close()
	if err := os.Rename(parentName, parentName+"-moved"); err == nil {
		t.Fatal("parent pin allowed rename")
	}
	if err := os.Rename(data, data+"-moved"); err == nil {
		t.Fatal("directory pin allowed rename")
	}
	for _, item := range []struct {
		name string
		same bool
	}{{data, true}, {parentName, false}} {
		h, err := openNative(item.name, readAttributes, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE, syscall.OPEN_EXISTING, backupSemantics|openReparse)
		if err != nil {
			t.Fatal(err)
		}
		err = sameFile(h, pin.info)
		syscall.CloseHandle(h)
		if (err == nil) != item.same {
			t.Fatalf("identity check for %s: %v", item.name, err)
		}
	}
	if err := deletePinned(pin); err != nil {
		t.Fatal(err)
	}
	pin.close()
	if _, err := os.Stat(data); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("short-path pin did not remove its exact directory:", err)
	}
}

func TestWindowsDirectoryPinsRejectLeafAndAncestorJunctions(t *testing.T) {
	base, err := canonicalDirectory(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	child := filepath.Join(outside, "BlockHarbor")
	if err := os.Mkdir(child, 0700); err != nil {
		t.Fatal(err)
	}
	keep := filepath.Join(child, "keep.txt")
	if err := os.WriteFile(keep, []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "Redirected parent")
	command := exec.Command("cmd.exe", "/d", "/c", "mklink", "/J", link, outside)
	if output, err := command.CombinedOutput(); err != nil {
		t.Skipf("junction creation unavailable: %s (%v)", output, err)
	}
	parent, err := pinDirectory(base, false, false)
	if err != nil {
		t.Fatal(err)
	}
	defer parent.close()
	for _, item := range []struct {
		name      string
		allowLink bool
	}{{link, false}, {filepath.Join(link, "BlockHarbor"), false}, {filepath.Join(link, "BlockHarbor"), true}} {
		pin, err := pinDirectory(item.name, item.allowLink, false)
		if err == nil {
			pin.close()
			t.Fatalf("redirected path accepted: %s (allowLink=%v)", item.name, item.allowLink)
		}
	}
	// The explicitly permitted final junction can still be removed as a link;
	// allowLink must never authorize traversing it to a child in another tree.
	pin, err := pinDirectory(link, true, true)
	if err != nil {
		t.Fatal("explicit link-only pin rejected:", err)
	}
	defer pin.close()
	if err := deletePinned(pin); err != nil {
		t.Fatal(err)
	}
	pin.close()
	if _, err := os.Lstat(link); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("junction itself was not removed:", err)
	}
	if data, err := os.ReadFile(keep); err != nil || string(data) != "outside" {
		t.Fatal("junction target changed:", err)
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
