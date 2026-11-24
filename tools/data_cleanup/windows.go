//go:build windows

// The helper runs from NSIS's private temporary directory, not from the Python
// runtime being removed. No command-line option can choose a data directory.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

const (
	readAttributes  = 0x80
	deleteAccess    = 0x10000
	synchronize     = 0x100000
	openReparse     = 0x00200000
	backupSemantics = 0x02000000
	reparsePoint    = 0x400
)

var (
	kernel             = syscall.NewLazyDLL("kernel32.dll")
	shell              = syscall.NewLazyDLL("shell32.dll")
	ole                = syscall.NewLazyDLL("ole32.dll")
	user               = syscall.NewLazyDLL("user32.dll")
	getFinalPath       = kernel.NewProc("GetFinalPathNameByHandleW")
	getLongPath        = kernel.NewProc("GetLongPathNameW")
	lockFile           = kernel.NewProc("LockFileEx")
	setFileInformation = kernel.NewProc("SetFileInformationByHandle")
	getProcessTimes    = kernel.NewProc("GetProcessTimes")
	queryImage         = kernel.NewProc("QueryFullProcessImageNameW")
)

func linkedEntry(info os.FileInfo) bool {
	attrs, ok := info.Sys().(*syscall.Win32FileAttributeData)
	return info.Mode()&os.ModeSymlink != 0 || (ok && attrs.FileAttributes&reparsePoint != 0)
}

func nativeError(err error) error {
	if err == nil || err == syscall.Errno(0) {
		return syscall.EINVAL
	}
	return err
}

type guid struct {
	D1     uint32
	D2, D3 uint16
	D4     [8]byte
}

func localAppData() (string, error) {
	// FOLDERID_LocalAppData, current user; deliberately ignores environment and
	// registry overrides of the application's data directory.
	id := guid{0xF1B32785, 0x6FBA, 0x4FCF, [8]byte{0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91}}
	var value *uint16
	result, _, _ := shell.NewProc("SHGetKnownFolderPath").Call(uintptr(unsafe.Pointer(&id)), 0, 0, uintptr(unsafe.Pointer(&value)))
	if int32(result) < 0 || value == nil {
		return "", fmt.Errorf("Windows не вернула папку LocalAppData (0x%08x)", uint32(result))
	}
	defer ole.NewProc("CoTaskMemFree").Call(uintptr(unsafe.Pointer(value)))
	return syscall.UTF16ToString(unsafe.Slice(value, 32768)), nil
}

func normalizePath(value string) string {
	if strings.HasPrefix(value, `\\?\UNC\`) {
		value = `\\` + value[8:]
	} else {
		value = strings.TrimPrefix(value, `\\?\`)
	}
	return strings.TrimRight(filepath.Clean(value), `\/`)
}

func within(parent, child string) bool {
	parent, child = strings.ToLower(normalizePath(parent)), strings.ToLower(normalizePath(child))
	return child == parent || strings.HasPrefix(child, parent+`\`)
}

func finalPath(handle syscall.Handle) (string, error) {
	buf := make([]uint16, 32768)
	n, _, err := getFinalPath.Call(uintptr(handle), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0)
	if n == 0 || n >= uintptr(len(buf)) {
		return "", nativeError(err)
	}
	return normalizePath(syscall.UTF16ToString(buf[:n])), nil
}

func longNamePath(name string) (string, error) {
	pointer, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return "", err
	}
	buf := make([]uint16, 32768)
	n, _, nativeErr := getLongPath.Call(uintptr(unsafe.Pointer(pointer)), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	if n == 0 || n >= uintptr(len(buf)) {
		return "", nativeError(nativeErr)
	}
	return normalizePath(syscall.UTF16ToString(buf[:n])), nil
}

func openNative(name string, access, sharing, creation, flags uint32) (syscall.Handle, error) {
	pointer, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return syscall.InvalidHandle, err
	}
	return syscall.CreateFile(pointer, access, sharing, nil, creation, flags, 0)
}

type directoryPin struct {
	handle syscall.Handle
	info   syscall.ByHandleFileInformation
	name   string
}

func pinDirectory(name string, allowLink, deletable bool) (*directoryPin, error) {
	access := uint32(readAttributes | synchronize | syscall.FILE_LIST_DIRECTORY)
	if deletable {
		access |= deleteAccess
	}
	h, err := openNative(name, access, syscall.FILE_SHARE_READ, syscall.OPEN_EXISTING, backupSemantics|openReparse)
	if err != nil {
		return nil, err
	}
	pin := &directoryPin{handle: h, name: name}
	if err = syscall.GetFileInformationByHandle(h, &pin.info); err != nil || pin.info.FileAttributes&syscall.FILE_ATTRIBUTE_DIRECTORY == 0 || (!allowLink && pin.info.FileAttributes&reparsePoint != 0) {
		pin.close()
		if err == nil {
			err = errors.New("каталог перенаправлен или заменён файлом")
		}
		return nil, err
	}
	actual, err := finalPath(h)
	expected := normalizePath(name)
	if err == nil && !strings.EqualFold(actual, expected) {
		// Expand DOS 8.3 component names, not reparse targets. The original
		// OPEN_REPARSE_POINT handle stays pinned throughout this check. An
		// ancestor junction must still differ from its resolved finalPath;
		// resolving both sides through another handle would hide that redirect.
		expected, err = longNamePath(name)
	}
	if err != nil || !strings.EqualFold(actual, expected) {
		pin.close()
		return nil, errors.New("канонический путь каталога изменён")
	}
	return pin, nil
}

func (pin *directoryPin) close() {
	if pin != nil && pin.handle != syscall.InvalidHandle {
		syscall.CloseHandle(pin.handle)
		pin.handle = syscall.InvalidHandle
	}
}

func sameFile(handle syscall.Handle, other syscall.ByHandleFileInformation) error {
	var current syscall.ByHandleFileInformation
	if err := syscall.GetFileInformationByHandle(handle, &current); err != nil {
		return err
	}
	if current.VolumeSerialNumber != other.VolumeSerialNumber || current.FileIndexHigh != other.FileIndexHigh || current.FileIndexLow != other.FileIndexLow {
		return errors.New("каталог был подменён")
	}
	return nil
}

func deletePinned(pin *directoryPin) error {
	// FileDispositionInfo, not a path-based remove. A nonempty directory fails:
	// newly created files are never recursively removed after releasing barriers.
	disposition := byte(1)
	ok, _, err := setFileInformation.Call(uintptr(pin.handle), 4, uintptr(unsafe.Pointer(&disposition)), 1)
	if ok == 0 {
		return nativeError(err)
	}
	return nil
}

func currentIdentity(pid uint32) (*identity, error) {
	h, err := syscall.OpenProcess(0x1000|synchronize, false, pid)
	if errors.Is(err, syscall.Errno(87)) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer syscall.CloseHandle(h)
	state, err := syscall.WaitForSingleObject(h, 0)
	if err != nil {
		return nil, err
	}
	if state == syscall.WAIT_OBJECT_0 {
		return nil, nil
	}
	if state != syscall.WAIT_TIMEOUT {
		return nil, errors.New("неизвестное состояние процесса")
	}
	var created, exited, kernelTime, userTime syscall.Filetime
	ok, _, err := getProcessTimes.Call(uintptr(h), uintptr(unsafe.Pointer(&created)), uintptr(unsafe.Pointer(&exited)), uintptr(unsafe.Pointer(&kernelTime)), uintptr(unsafe.Pointer(&userTime)))
	if ok == 0 {
		return nil, nativeError(err)
	}
	birth := uint64(created.HighDateTime)<<32 | uint64(created.LowDateTime)
	return &identity{PID: pid, Birth: strconv.FormatUint(birth, 10)}, nil
}

func processAlive(saved identity) (bool, error) {
	current, err := currentIdentity(saved.PID)
	return current != nil && (saved.Birth == "unknown" || saved.Birth == current.Birth), err
}

// Legacy releases could lose their JSON parent record while an owned runtime
// survived. Inspect executable paths as an additional read-only preflight; never
// terminate a process and never infer ownership from a PID or basename alone.
func checkOwnedProcesses(dataRoot string) error {
	snapshot, err := syscall.CreateToolhelp32Snapshot(syscall.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(snapshot)
	var entry syscall.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = syscall.Process32First(snapshot, &entry); err == nil; err = syscall.Process32Next(snapshot, &entry) {
		name := strings.ToLower(syscall.UTF16ToString(entry.ExeFile[:]))
		if name != "java.exe" && name != "javaw.exe" && name != "python.exe" && name != "pythonw.exe" && name != "prismlauncher.exe" && name != "blockharbor.exe" {
			continue
		}
		h, openErr := syscall.OpenProcess(0x1000|synchronize, false, entry.ProcessID)
		if errors.Is(openErr, syscall.Errno(87)) {
			continue // exited between snapshot and OpenProcess
		}
		if openErr != nil {
			return fmt.Errorf("Windows не разрешила проверить процесс %s (%d): %w", name, entry.ProcessID, openErr)
		}
		state, waitErr := syscall.WaitForSingleObject(h, 0)
		if waitErr != nil || state == syscall.WAIT_FAILED {
			syscall.CloseHandle(h)
			return fmt.Errorf("не удалось проверить процесс %d", entry.ProcessID)
		}
		if state == syscall.WAIT_OBJECT_0 {
			syscall.CloseHandle(h)
			continue
		}
		buffer := make([]uint16, 32768)
		size := uint32(len(buffer))
		ok, _, queryErr := queryImage.Call(uintptr(h), 0, uintptr(unsafe.Pointer(&buffer[0])), uintptr(unsafe.Pointer(&size)))
		syscall.CloseHandle(h)
		if ok == 0 {
			return fmt.Errorf("не удалось определить путь процесса %d: %w", entry.ProcessID, nativeError(queryErr))
		}
		image := syscall.UTF16ToString(buffer[:size])
		// Resolve benign aliases such as redirected user folders before comparing.
		imageHandle, imageErr := openNative(image, readAttributes, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE, syscall.OPEN_EXISTING, backupSemantics)
		if imageErr == nil {
			if canonical, finalErr := finalPath(imageHandle); finalErr == nil {
				image = canonical
			}
			syscall.CloseHandle(imageHandle)
		}
		if within(dataRoot, image) {
			return fmt.Errorf("компонент BlockHarbor ещё работает (%s, процесс %d). Закрой Minecraft и лаунчер и повтори удаление", name, entry.ProcessID)
		}
	}
	if errors.Is(err, syscall.ERROR_NO_MORE_FILES) {
		return nil
	}
	return err
}

func readRegistry(name string) (string, error) {
	keyName, _ := syscall.UTF16PtrFromString(name)
	var key syscall.Handle
	if err := syscall.RegOpenKeyEx(syscall.HKEY_CURRENT_USER, keyName, 0, syscall.KEY_QUERY_VALUE|syscall.KEY_WOW64_64KEY, &key); err != nil {
		return "", err
	}
	defer syscall.RegCloseKey(key)
	valueName, _ := syscall.UTF16PtrFromString("InstallLocation")
	var kind, size uint32
	if err := syscall.RegQueryValueEx(key, valueName, nil, &kind, nil, &size); err != nil {
		return "", err
	}
	if kind != syscall.REG_SZ || size < 4 || size > 65536 || size%2 != 0 {
		return "", errors.New("неверная запись InstallLocation")
	}
	buffer := make([]uint16, size/2)
	if err := syscall.RegQueryValueEx(key, valueName, nil, &kind, (*byte)(unsafe.Pointer(&buffer[0])), &size); err != nil {
		return "", err
	}
	return syscall.UTF16ToString(buffer), nil
}

func canonicalDirectory(name string) (string, error) {
	if name == "" || !filepath.IsAbs(name) {
		return "", errors.New("путь установки не является абсолютным")
	}
	h, err := openNative(name, readAttributes, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE, syscall.OPEN_EXISTING, backupSemantics)
	if err != nil {
		return "", err
	}
	defer syscall.CloseHandle(h)
	return finalPath(h)
}

func registeredInstall(requested string) (string, error) {
	canonical, err := canonicalDirectory(requested)
	if err != nil {
		return "", err
	}
	for _, key := range []string{`Software\BlockHarbor`, `Software\Microsoft\Windows\CurrentVersion\Uninstall\BlockHarbor`} {
		registered, err := readRegistry(key)
		if err != nil {
			return "", errors.New("установка не зарегистрирована для текущего пользователя")
		}
		actual, err := canonicalDirectory(registered)
		if err != nil || !strings.EqualFold(canonical, actual) {
			return "", errors.New("этот деинсталлятор относится к другой установке BlockHarbor. Запусти удаление текущей установки через «Параметры → Приложения» Windows")
		}
	}
	return canonical, nil
}

type coordinator struct {
	root        *os.Root
	canonical   string
	pins        map[string]*directoryPin
	locks       map[string]syscall.Handle
	gate        string
	gateWritten bool
}

func (c *coordinator) close() {
	// Remove only our own gate, while the other barriers are still held. A stale
	// gate is harmless after this helper exits, but should not be left on errors.
	if c.gateWritten {
		if data, err := readRecord(c.root, "launcher-sessions/update.json"); err == nil {
			if row, err := decodeRecord(data); err == nil {
				var value string
				if json.Unmarshal(row["session"], &value) == nil && value == c.gate {
					_ = c.root.Remove("launcher-sessions/update.json")
				}
			}
		}
	}
	for name := range c.locks {
		c.releaseLock(name)
	}
	for _, pin := range c.pins {
		pin.close()
	}
}

func (c *coordinator) pinDir(name string) error {
	if _, ok := c.pins[name]; ok {
		return nil
	}
	if err := c.root.MkdirAll(name, 0700); err != nil {
		return err
	}
	pin, err := pinDirectory(filepath.Join(c.canonical, filepath.FromSlash(name)), false, false)
	if err != nil {
		return fmt.Errorf("не удалось закрепить каталог %s: %w", name, err)
	}
	c.pins[name] = pin
	return nil
}

func (c *coordinator) acquire(name string) error {
	sharing := uint32(syscall.FILE_SHARE_READ | syscall.FILE_SHARE_WRITE | syscall.FILE_SHARE_DELETE)
	if name == "logs/startup.log" {
		// Every released Go bootstrap opens this file before provisioning runtime
		// and retains it through Python exit. Share=0 therefore blocks even older
		// portable launchers that have not reached SessionCoordinator yet.
		sharing = 0
	}
	h, err := openNative(filepath.Join(c.canonical, filepath.FromSlash(name)), syscall.GENERIC_READ|syscall.GENERIC_WRITE, sharing, syscall.OPEN_ALWAYS, openReparse)
	if err != nil {
		return err
	}
	var info syscall.ByHandleFileInformation
	if err = syscall.GetFileInformationByHandle(h, &info); err == nil && info.FileAttributes&(reparsePoint|syscall.FILE_ATTRIBUTE_DIRECTORY) != 0 {
		err = errors.New("файл блокировки перенаправлен")
	}
	if err == nil {
		var overlapped syscall.Overlapped
		ok, _, nativeErr := lockFile.Call(uintptr(h), 3, 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
		if ok == 0 {
			if errors.Is(nativeErr, syscall.Errno(33)) {
				err = errors.New("BlockHarbor ещё работает или завершает операцию. Закрой все окна лаунчера и Minecraft, затем повтори удаление")
			} else {
				err = nativeError(nativeErr)
			}
		}
	}
	if err != nil {
		syscall.CloseHandle(h)
		return fmt.Errorf("%s: %w", name, err)
	}
	c.locks[name] = h
	return nil
}

func (c *coordinator) releaseLock(name string) {
	if h, ok := c.locks[name]; ok {
		syscall.CloseHandle(h) // closing the handle releases LockFileEx's byte range
		delete(c.locks, name)
	}
}

func (c *coordinator) prepare() error {
	if err := c.pinDir("logs"); err != nil {
		return err
	}
	if err := c.acquire("logs/startup.log"); err != nil {
		return fmt.Errorf("bootstrap BlockHarbor ещё работает или журнал недоступен: %w", err)
	}
	if err := c.pinDir("launcher-sessions"); err != nil {
		return err
	}
	for _, name := range []string{"launcher-sessions/operations.lock", "launcher-sessions/registry.lock"} {
		if err := c.acquire(name); err != nil {
			return err
		}
	}
	entries, err := readDir(c.root, ".")
	if err != nil {
		return err
	}
	legacy := map[string]string{"activation-v10": "activation-v10"}
	for _, entry := range entries {
		if strings.HasPrefix(strings.ToLower(entry.Name()), "activation-") {
			info, err := c.root.Lstat(entry.Name())
			if err != nil {
				return err
			}
			// A redirected legacy directory is deleted as a link later. Its
			// external target cannot be part of this cleanup's synchronization.
			if info.IsDir() && !linkedEntry(info) {
				legacy[strings.ToLower(entry.Name())] = entry.Name()
			} else if strings.EqualFold(entry.Name(), "activation-v10") {
				return errors.New("основной каталог состояния BlockHarbor перенаправлен")
			}
		}
	}
	for _, name := range legacy {
		for _, dir := range []string{name, path.Join(name, ".session")} {
			if err := c.pinDir(dir); err != nil {
				return err
			}
		}
		if err := c.acquire(path.Join(name, ".session/owner.lock")); err != nil {
			return err
		}
	}
	if err := checkRecords(c.root, processAlive); err != nil {
		return err
	}
	if err := checkOwnedProcesses(c.canonical); err != nil {
		return err
	}
	if err := gameFiles(c.root, c.probeGameFile); err != nil {
		return err
	}
	own, err := currentIdentity(uint32(os.Getpid()))
	if err != nil || own == nil {
		return errors.New("не удалось определить собственный процесс удаления")
	}
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	c.gate = hex.EncodeToString(nonce)
	data, err := json.Marshal(map[string]any{"format": 1, "session": c.gate, "helper": own, "purpose": "uninstall"})
	if err != nil {
		return err
	}
	staged := "launcher-sessions/uninstall-" + c.gate + ".tmp"
	f, err := c.root.OpenFile(staged, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer c.root.Remove(staged)
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err := c.root.Rename(staged, "launcher-sessions/update.json"); err != nil {
		return err
	}
	c.gateWritten = true
	return nil
}

func (c *coordinator) probeGameFile(name string) error {
	// Pin every ancestor before opening a path-based native handle. No reparse
	// point can redirect this check outside the already pinned data root.
	dirs := strings.Split(path.Dir(name), "/")
	var pins []*directoryPin
	defer func() {
		for i := len(pins) - 1; i >= 0; i-- {
			pins[i].close()
		}
	}()
	for i := range dirs {
		pin, err := pinDirectory(filepath.Join(c.canonical, filepath.FromSlash(strings.Join(dirs[:i+1], "/"))), false, false)
		if err != nil {
			return err
		}
		pins = append(pins, pin)
	}
	h, err := openNative(filepath.Join(c.canonical, filepath.FromSlash(name)), syscall.GENERIC_READ, 0, syscall.OPEN_EXISTING, openReparse)
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(h)
	var info syscall.ByHandleFileInformation
	if err := syscall.GetFileInformationByHandle(h, &info); err != nil {
		return err
	}
	if info.FileAttributes&(reparsePoint|syscall.FILE_ATTRIBUTE_DIRECTORY) != 0 {
		return errors.New("проверяемый файл был перенаправлен")
	}
	var overlapped syscall.Overlapped
	ok, _, err := lockFile.Call(uintptr(h), 3, 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
	if ok == 0 {
		return nativeError(err)
	}
	return nil
}

func (c *coordinator) finish() error {
	// Protected data is already gone. Keep update.json live while removing the
	// other barriers; after its removal perform only exact, empty removals.
	names := make([]string, 0, len(c.locks))
	for name := range c.locks {
		if name != "logs/startup.log" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		c.releaseLock(name)
		if err := c.root.Remove(name); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
	}
	dirs := make([]string, 0, len(c.pins))
	for name := range c.pins {
		if name != "launcher-sessions" && name != "logs" {
			dirs = append(dirs, name)
		}
	}
	sort.Slice(dirs, func(i, j int) bool { return len(dirs[i]) > len(dirs[j]) })
	for _, name := range dirs {
		c.pins[name].close()
		delete(c.pins, name)
		if err := c.root.Remove(name); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
	}
	if err := c.root.Remove("launcher-sessions/update.json"); err != nil {
		return err
	}
	c.gateWritten = false
	c.pins["launcher-sessions"].close()
	delete(c.pins, "launcher-sessions")
	if err := c.root.Remove("launcher-sessions"); err != nil {
		return err
	}
	// The last barrier is bootstrap's own log handle. From this release onward
	// only remove these exact known entries and the empty pinned data directory.
	c.releaseLock("logs/startup.log")
	if err := c.root.Remove("logs/startup.log"); err != nil {
		return err
	}
	c.pins["logs"].close()
	delete(c.pins, "logs")
	return c.root.Remove("logs")
}

func removeData(installDir string) (notice string, changed bool, result error) {
	installed, err := registeredInstall(installDir)
	if err != nil {
		return "", false, err
	}
	// Prevent a new installed EXE from entering bootstrap before Python's session
	// gate. DELETE sharing permits removal when installation is inside data root.
	program, err := openNative(filepath.Join(installed, "BlockHarbor.exe"), syscall.GENERIC_READ|deleteAccess, syscall.FILE_SHARE_DELETE, syscall.OPEN_EXISTING, openReparse)
	if err != nil && !errors.Is(err, syscall.ERROR_FILE_NOT_FOUND) {
		return "", false, fmt.Errorf("файл приложения занят или недоступен; закрой BlockHarbor: %w", err)
	}
	defer func() {
		if program != syscall.InvalidHandle {
			syscall.CloseHandle(program)
		}
	}()
	known, err := localAppData()
	if err != nil {
		return "", false, err
	}
	parent, err := os.OpenRoot(known)
	if err != nil {
		return "", false, err
	}
	defer parent.Close()
	parentFile, err := parent.Open(".")
	if err != nil {
		return "", false, err
	}
	canonicalParent, err := finalPath(syscall.Handle(parentFile.Fd()))
	if err != nil {
		parentFile.Close()
		return "", false, err
	}
	parentPin, err := pinDirectory(canonicalParent, false, false)
	if err == nil {
		err = sameFile(syscall.Handle(parentFile.Fd()), parentPin.info)
	}
	parentFile.Close()
	if parentPin != nil {
		defer parentPin.close()
	}
	if err != nil {
		return "", false, err
	}
	canonical := filepath.Join(canonicalParent, "BlockHarbor")
	pin, err := pinDirectory(canonical, true, true)
	if errors.Is(err, syscall.ERROR_FILE_NOT_FOUND) || errors.Is(err, syscall.ERROR_PATH_NOT_FOUND) {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("не удалось открыть стандартную папку данных: %w", err)
	}
	defer pin.close()
	if pin.info.FileAttributes&reparsePoint != 0 {
		if err := deletePinned(pin); err != nil {
			return "", false, err
		}
		return "Стандартная папка BlockHarbor была ссылкой. Удалена только ссылка; файлы в её внешней целевой папке сохранены.", true, nil
	}
	executable, err := os.Executable()
	if err != nil {
		return "", false, err
	}
	actualExe, err := filepath.EvalSymlinks(executable)
	if err != nil || within(canonical, actualExe) {
		return "", false, errors.New("компонент удаления должен выполняться из временной папки Windows")
	}
	root, err := parent.OpenRoot("BlockHarbor")
	if err != nil {
		return "", false, err
	}
	defer root.Close()
	rootFile, err := root.Open(".")
	if err != nil {
		return "", false, err
	}
	err = sameFile(syscall.Handle(rootFile.Fd()), pin.info)
	rootFile.Close()
	if err != nil {
		return "", false, err
	}
	c := &coordinator{root: root, canonical: canonical, pins: map[string]*directoryPin{}, locks: map[string]syscall.Handle{}}
	defer c.close()
	if err := c.prepare(); err != nil {
		return "", false, err
	}
	preserve := map[string]bool{"launcher-sessions/update.json": true}
	for name := range c.locks {
		preserve[name] = true
	}
	// NSIS itself is running its temporary uninstaller copy. If the registered
	// program lies inside data, unlink its EXE and close our pin before walking its
	// parent, including filesystems without immediate POSIX unlink semantics.
	if program != syscall.InvalidHandle && within(canonical, installed) {
		relative, err := filepath.Rel(canonical, filepath.Join(installed, "BlockHarbor.exe"))
		if err != nil {
			return "", false, err
		}
		if err := root.Remove(relative); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return "", false, err
		}
		syscall.CloseHandle(program)
		program = syscall.InvalidHandle
	}
	changed = true
	if err := removeProtected(root, preserve); err != nil {
		return "", changed, err
	}
	if err := c.finish(); err != nil {
		return "", changed, err
	}
	// No recursive operations beyond this point. A competing old portable
	// bootstrap may have created files before it reached the Python gate. Report
	// that condition instead of sweeping newly created files in a second pass.
	root.Close()
	if err := deletePinned(pin); err != nil {
		return "", changed, err
	}
	return "", changed, nil
}

func message(text string, errorMessage bool) {
	title, _ := syscall.UTF16PtrFromString("Удаление BlockHarbor")
	body, _ := syscall.UTF16PtrFromString(text)
	flags := uintptr(0x40)
	if errorMessage {
		flags = 0x10
	}
	user.NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(body)), uintptr(unsafe.Pointer(title)), flags)
}

func main() {
	remove := flag.Bool("remove-data", false, "Remove standard current-user data")
	install := flag.String("install-dir", "", "Registered application directory")
	flag.Parse()
	if !*remove || *install == "" || flag.NArg() != 0 {
		message("Компонент удаления запускается только из деинсталлятора BlockHarbor.", true)
		os.Exit(2)
	}
	notice, changed, err := removeData(*install)
	if err != nil {
		prefix := "Удаление данных не начато. "
		if changed {
			prefix = "Удалены не все данные BlockHarbor. Часть файлов уже удалена, оставшиеся сохранены. "
		}
		message(prefix+"\n\n"+err.Error()+"\n\nЗакрой окна лаунчера и Minecraft и проверь доступ к папке. Если ошибка относится к состоянию сеанса, запусти и закрой актуальный BlockHarbor, затем повтори удаление.", true)
		os.Exit(2)
	}
	if notice != "" {
		message(notice, false)
	}
}
