// A release carries a ZIP after its native executable and a fixed-size footer.
package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const trailerMagic = "BlockHarborPKG18"
const trailerLen = int64(8 + sha256.Size + len(trailerMagic))

type payload struct {
	data   *io.SectionReader
	digest string
}
type entry struct {
	Name string
	Size int64
	Hash string
}

func readPayload(file *os.File) (payload, error) {
	info, err := file.Stat()
	if err != nil {
		return payload{}, err
	}
	var footer [trailerLen]byte
	if info.Size() <= trailerLen {
		return payload{}, errors.New("У файла нет встроенного пакета BlockHarbor.")
	}
	if _, err = file.ReadAt(footer[:], info.Size()-trailerLen); err != nil {
		return payload{}, err
	}
	size := int64(binary.LittleEndian.Uint64(footer[:8]))
	if string(footer[40:]) != trailerMagic || size <= 0 || size > info.Size()-trailerLen {
		return payload{}, errors.New("Файл BlockHarbor повреждён или передан не полностью. Скопируйте EXE повторно.")
	}
	return payload{io.NewSectionReader(file, info.Size()-trailerLen-size, size), hex.EncodeToString(footer[8:40])}, nil
}

func localPath(name string) bool {
	return name != "" && !strings.ContainsAny(name, "\\:") && filepath.IsLocal(filepath.FromSlash(name)) && filepath.ToSlash(filepath.Clean(filepath.FromSlash(name))) == name && name != "."
}

func fileDigest(file string) string {
	f, err := os.Open(file)
	if err != nil {
		return ""
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil {
		return ""
	}
	return hex.EncodeToString(h.Sum(nil))
}

func cached(root string, p payload) string {
	pointer, err := os.ReadFile(filepath.Join(root, "current"))
	if err != nil {
		return ""
	}
	gen := string(pointer)
	if !strings.HasPrefix(gen, "files-") || filepath.Base(gen) != gen || !localPath(gen) {
		return ""
	}
	dir := filepath.Join(root, gen)
	raw, err := os.ReadFile(filepath.Join(dir, ".installed.json"))
	if err != nil {
		return ""
	}
	var receipt struct {
		Package string
		Entries []entry
	}
	if json.Unmarshal(raw, &receipt) != nil || receipt.Package != p.digest || len(receipt.Entries) == 0 {
		return ""
	}
	archive, err := zip.NewReader(p.data, p.data.Size())
	if err != nil || len(archive.File) != len(receipt.Entries) {
		return ""
	}
	for i, e := range receipt.Entries {
		if !localPath(e.Name) || e.Name != archive.File[i].Name || uint64(e.Size) != archive.File[i].UncompressedSize64 {
			return ""
		}
		name := filepath.Join(dir, filepath.FromSlash(e.Name))
		info, err := os.Lstat(name)
		if err != nil || !info.Mode().IsRegular() || info.Size() != e.Size {
			return ""
		}
		if fileDigest(name) != e.Hash {
			return ""
		}
	}
	return dir
}

func unpackMember(f *zip.File, dir string) (entry, error) {
	if !localPath(f.Name) || !f.Mode().IsRegular() || f.Name == ".installed.json" {
		return entry{}, errors.New("Некорректный путь во встроенном архиве.")
	}
	name := filepath.Join(dir, filepath.FromSlash(f.Name))
	if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
		return entry{}, err
	}
	src, err := f.Open()
	if err != nil {
		return entry{}, err
	}
	defer src.Close()
	dst, err := os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return entry{}, err
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(dst, h), src)
	closeErr := dst.Close()
	if err != nil {
		return entry{}, err
	}
	if closeErr != nil {
		return entry{}, closeErr
	}
	if uint64(n) != f.UncompressedSize64 {
		return entry{}, io.ErrUnexpectedEOF
	}
	return entry{f.Name, n, hex.EncodeToString(h.Sum(nil))}, nil
}

// The Windows caller holds a per-release mutex across this operation. New code
// lives in a new directory, so a repair never replaces a DLL of a running game.
func provision(base string, p payload, progress func(string)) (string, error) {
	root := filepath.Join(base, "2.0-"+p.digest[:16])
	if err := os.MkdirAll(root, 0700); err != nil {
		return "", err
	}
	if dir := cached(root, p); dir != "" {
		return dir, nil
	}
	progress("Проверяем встроенный пакет BlockHarbor…")
	h := sha256.New()
	if _, err := io.Copy(h, io.NewSectionReader(p.data, 0, p.data.Size())); err != nil {
		return "", err
	}
	if hex.EncodeToString(h.Sum(nil)) != p.digest {
		return "", errors.New("Проверка SHA-256 не пройдена. Скопируйте исходный EXE повторно.")
	}
	archive, err := zip.NewReader(p.data, p.data.Size())
	if err != nil {
		return "", err
	}
	if len(archive.File) == 0 {
		return "", errors.New("Пакет BlockHarbor пуст.")
	}
	dir, err := os.MkdirTemp(root, "files-")
	if err != nil {
		return "", err
	}
	committed := false
	defer func() {
		if !committed {
			os.RemoveAll(dir)
		}
	}()
	entries := make([]entry, 0, len(archive.File))
	step := -1
	for i, f := range archive.File {
		e, err := unpackMember(f, dir)
		if err != nil {
			return "", fmt.Errorf("%s: %w", f.Name, err)
		}
		entries = append(entries, e)
		pct := (i + 1) * 100 / len(archive.File)
		if pct/10 != step {
			step = pct / 10
			progress(fmt.Sprintf("Подготовка файлов лаунчера · %d%%", pct))
		}
	}
	receipt, err := json.Marshal(struct {
		Package string
		Entries []entry
	}{p.digest, entries})
	if err != nil {
		return "", err
	}
	if err = os.WriteFile(filepath.Join(dir, ".installed.json"), receipt, 0600); err != nil {
		return "", err
	}
	pointer, err := os.CreateTemp(root, ".current-")
	if err != nil {
		return "", err
	}
	defer os.Remove(pointer.Name())
	_, err = pointer.WriteString(filepath.Base(dir))
	closeErr := pointer.Close()
	if err != nil {
		return "", err
	}
	if closeErr != nil {
		return "", closeErr
	}
	if err = os.Rename(pointer.Name(), filepath.Join(root, "current")); err != nil {
		return "", err
	}
	committed = true
	progress("Компоненты готовы. Открываем BlockHarbor.")
	return dir, nil
}
