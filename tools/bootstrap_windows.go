// BlockHarbor native Windows entry point. Build with tools/build_windows.py.
package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"syscall"
	"unsafe"
)

var kernel = syscall.NewLazyDLL("kernel32.dll")

func wide(s string) *uint16 { p, _ := syscall.UTF16PtrFromString(s); return p }
func showError(msg string) {
	syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(wide(msg))), uintptr(unsafe.Pointer(wide("BlockHarbor"))), 0x10)
}

func prepare(data string, source payload, log io.Writer) (string, error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	name, _ := syscall.UTF16PtrFromString("Local\\BlockHarbor-Setup-" + source.digest[:16])
	handle, _, err := kernel.NewProc("CreateMutexW").Call(0, 0, uintptr(unsafe.Pointer(name)))
	if handle == 0 {
		return "", err
	}
	defer syscall.CloseHandle(syscall.Handle(handle))
	wait, _, err := kernel.NewProc("WaitForSingleObject").Call(handle, 0xffffffff)
	if wait != 0 && wait != 0x80 {
		return "", err
	}
	defer kernel.NewProc("ReleaseMutex").Call(handle)
	var terminal *os.File
	opened := false
	defer func() {
		if terminal != nil {
			terminal.Close()
		}
		if opened {
			kernel.NewProc("FreeConsole").Call()
		}
	}()
	progress := func(line string) {
		if terminal == nil {
			r, _, _ := kernel.NewProc("AllocConsole").Call()
			opened = r != 0
			kernel.NewProc("SetConsoleOutputCP").Call(65001)
			kernel.NewProc("SetConsoleTitleW").Call(uintptr(unsafe.Pointer(wide("BlockHarbor — подготовка лаунчера"))))
			terminal, _ = os.OpenFile("CONOUT$", os.O_WRONLY, 0)
		}
		if terminal != nil {
			fmt.Fprintln(terminal, line)
		}
		fmt.Fprintln(log, line)
	}
	return provision(filepath.Join(data, "app"), source, progress)
}

func run() error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	f, err := os.Open(executable)
	if err != nil {
		return err
	}
	defer f.Close()
	source, err := readPayload(f)
	if err != nil {
		return err
	}
	base, err := os.UserCacheDir()
	if err != nil {
		return err
	}
	data := filepath.Join(base, "BlockHarbor")
	logs := filepath.Join(data, "logs")
	if err = os.MkdirAll(logs, 0700); err != nil {
		return err
	}
	logPath := filepath.Join(logs, "startup.log")
	log, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	defer log.Close()
	root, err := prepare(data, source, log)
	if err != nil {
		fmt.Fprintln(log, err)
		return fmt.Errorf("Не удалось подготовить BlockHarbor — %w\nЖурнал %s", err, logPath)
	}
	arguments := []string{"-B", "-X", "utf8", filepath.Join(root, "main.py")}
	arguments = append(arguments, os.Args[1:]...)
	cmd := exec.Command(filepath.Join(root, "runtime", "pythonw.exe"), arguments...)
	cmd.Env = append(os.Environ(), "BLOCKHARBOR_LAUNCHER_EXE="+executable, fmt.Sprintf("BLOCKHARBOR_BOOTSTRAP_PID=%d", os.Getpid()))
	cmd.Dir, cmd.Stdout, cmd.Stderr = root, log, log
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("Ошибка запуска BlockHarbor (%v). Журнал %s", err, logPath)
	}
	return nil
}
func main() {
	if err := run(); err != nil {
		showError(err.Error())
		os.Exit(1)
	}
}
