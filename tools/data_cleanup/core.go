// The portable part of the Windows uninstaller. All filesystem operations are
// relative to an os.Root; the production entrypoint never accepts a data path.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type identity struct {
	PID   uint32 `json:"pid"`
	Birth string `json:"birth"`
}

type processProbe func(identity) (bool, error)

var slotName = regexp.MustCompile(`(?i)^[0-9]+\.json$`)
var profileID = regexp.MustCompile(`^[a-f0-9]{32}$`)
var sessionID = regexp.MustCompile(`^[a-f0-9]{24}$`)

// Invalid records are not proof that a game has stopped. In particular, do not
// silently coerce null/list profiles or an invalid process identity to empty.
func parseIdentity(raw json.RawMessage, nullable bool) (*identity, error) {
	if len(raw) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		if nullable {
			return nil, nil
		}
		return nil, errors.New("отсутствует идентификатор процесса")
	}
	var id identity
	if err := json.Unmarshal(raw, &id); err != nil || id.PID == 0 {
		return nil, errors.New("повреждён идентификатор процесса")
	}
	if id.Birth != "unknown" {
		birth, err := strconv.ParseUint(id.Birth, 10, 64)
		if err != nil || birth == 0 || strconv.FormatUint(birth, 10) != id.Birth {
			return nil, errors.New("повреждено время запуска процесса")
		}
	}
	return &id, nil
}

func checkIdentity(raw json.RawMessage, nullable bool, probe processProbe) error {
	id, err := parseIdentity(raw, nullable)
	if err != nil || id == nil {
		return err
	}
	alive, err := probe(*id)
	if err != nil {
		return fmt.Errorf("не удалось проверить процесс %d: %w", id.PID, err)
	}
	if alive {
		return fmt.Errorf("BlockHarbor, Minecraft или его компонент ещё работает (процесс %d). Закрой его и повтори удаление", id.PID)
	}
	return nil
}

func decodeRecord(data []byte) (map[string]json.RawMessage, error) {
	var record map[string]json.RawMessage
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&record); err != nil || record == nil {
		return nil, errors.New("повреждён файл состояния сеанса")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("лишние данные в файле состояния сеанса")
	}
	var format int
	var session string
	if json.Unmarshal(record["format"], &format) != nil || format != 1 || json.Unmarshal(record["session"], &session) != nil || !sessionID.MatchString(session) {
		return nil, errors.New("неизвестный формат состояния сеанса")
	}
	return record, nil
}

func checkSession(data []byte, probe processProbe) error {
	record, err := decodeRecord(data)
	if err != nil {
		return err
	}
	if _, present := record["owner"]; !present {
		return errors.New("в сеансе отсутствует поле owner")
	}
	if err := checkIdentity(record["owner"], true, probe); err != nil {
		return err
	}
	var profiles map[string]json.RawMessage
	if json.Unmarshal(record["profiles"], &profiles) != nil || profiles == nil {
		return errors.New("повреждён список запущенных сборок")
	}
	for id, raw := range profiles {
		var claim map[string]json.RawMessage
		if !profileID.MatchString(id) || json.Unmarshal(raw, &claim) != nil || claim == nil {
			return errors.New("повреждена запись запущенной сборки")
		}
		var stage string
		if json.Unmarshal(claim["stage"], &stage) != nil || (stage != "preparing" && stage != "game" && stage != "prism") {
			return errors.New("неизвестное состояние запущенной сборки")
		}
		if _, present := claim["process"]; !present {
			return errors.New("в записи сборки отсутствует процесс")
		}
		if err := checkIdentity(claim["process"], stage == "preparing", probe); err != nil {
			return err
		}
	}
	return nil
}

func checkUpdate(data []byte, probe processProbe) error {
	record, err := decodeRecord(data)
	if err != nil {
		return err
	}
	if len(record["owner"]) == 0 && len(record["helper"]) == 0 {
		return errors.New("в состоянии обновления отсутствует процесс")
	}
	for _, key := range []string{"owner", "helper"} {
		if err := checkIdentity(record[key], true, probe); err != nil {
			return err
		}
	}
	return nil
}

func readDir(root *os.Root, name string) ([]os.DirEntry, error) {
	f, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	entries, err := f.ReadDir(-1)
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	return entries, err
}

func readRecord(root *os.Root, name string) ([]byte, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || linkedEntry(info) || info.Size() > 65536 {
		return nil, errors.New("файл состояния перенаправлен или имеет неверный размер")
	}
	f, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 65537))
	if err == nil && len(data) > 65536 {
		err = errors.New("файл состояния слишком большой")
	}
	return data, err
}

func checkRecords(root *os.Root, probe processProbe) error {
	entries, err := readDir(root, "launcher-sessions")
	if err != nil {
		return err
	}
	for _, entry := range entries {
		name := entry.Name()
		if !strings.EqualFold(name, "update.json") && !slotName.MatchString(name) {
			continue
		}
		file := path.Join("launcher-sessions", name)
		data, err := readRecord(root, file)
		if err == nil {
			if strings.EqualFold(name, "update.json") {
				err = checkUpdate(data, probe)
			} else {
				err = checkSession(data, probe)
			}
		}
		if err != nil {
			return fmt.Errorf("%s: %w", file, err)
		}
	}
	return nil
}

// preserve contains only pinned coordination paths. Every other entry is removed
// via handle-relative Root.RemoveAll, which does not follow symlinks/junctions.
// Call this once, while all launch barriers are held. Never call it after release.
func removeProtected(root *os.Root, preserve map[string]bool) error {
	// Windows names are case-insensitive even when directory entries retain an
	// earlier spelling (Logs, Launcher-Sessions, Owner.lock). Match protection
	// by the same rule so a renamed coordination file can never be swept early.
	protectedPaths := make(map[string]bool, len(preserve))
	for name := range preserve {
		protectedPaths[strings.ToLower(name)] = true
	}
	var walk func(string) error
	walk = func(dir string) error {
		entries, err := readDir(root, dir)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			name := path.Join(dir, entry.Name())
			key := strings.ToLower(name)
			if protectedPaths[key] {
				continue
			}
			containsProtected := false
			for protected := range protectedPaths {
				if strings.HasPrefix(protected, key+"/") {
					containsProtected = true
					break
				}
			}
			if containsProtected {
				info, err := root.Lstat(name)
				if err != nil || !info.IsDir() || linkedEntry(info) {
					return fmt.Errorf("каталог блокировок %s изменён", name)
				}
				if err := walk(name); err != nil {
					return err
				}
			} else if err := root.RemoveAll(name); err != nil {
				return err
			}
		}
		return nil
	}
	return walk(".")
}

// Check game-owned files as well as published PID records: legacy Prism may
// outlive its parent. Links (including Prism's link to a managed instance) are
// skipped; the managed instances themselves are checked at their original path.
func gameFiles(root *os.Root, probe func(string) error) error {
	var walk func(string, int) error
	walk = func(dir string, depth int) error {
		if depth > 128 {
			return fmt.Errorf("слишком глубокая структура сборки: %s", dir)
		}
		info, err := root.Lstat(dir)
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if linkedEntry(info) || !info.IsDir() {
			return nil
		}
		entries, err := readDir(root, dir)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			name := path.Join(dir, entry.Name())
			info, err := root.Lstat(name)
			if err != nil {
				return err
			}
			if linkedEntry(info) {
				continue
			}
			if info.IsDir() {
				if err := walk(name, depth+1); err != nil {
					return err
				}
			} else if info.Mode().IsRegular() && (strings.EqualFold(entry.Name(), "session.lock") || (strings.EqualFold(entry.Name(), "latest.log") && strings.EqualFold(path.Base(dir), "logs"))) {
				if err := probe(name); err != nil {
					return fmt.Errorf("файлы Minecraft заняты или недоступны (%s): %w", name, err)
				}
			}
		}
		return nil
	}
	for _, name := range []string{"instances", "deleted-instances", "prism/data/instances"} {
		if err := walk(name, 0); err != nil {
			return err
		}
	}
	return nil
}
