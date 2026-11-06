package atomicfile

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
)

// only is the one name dir holds, so a test sees no temporary file was left.
func only(t *testing.T, dir string) string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 1 {
		t.Fatalf("%s holds %v (%v), want one file", dir, entries, err)
	}
	return entries[0].Name()
}

func TestWriteFileReplacesWholeWithItsMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "unit")
	if err := os.WriteFile(path, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := WriteFile(path, []byte("new"), 0o400); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o400 {
		t.Fatalf("mode %v (%v), want 0400", info.Mode(), err)
	}
	if data, _ := os.ReadFile(path); string(data) != "new" {
		t.Fatalf("holds %q", data)
	}
	if name := only(t, dir); name != "unit" {
		t.Fatalf("left %s", name)
	}
}

func TestModeIsSetBeforeAnythingIsWritten(t *testing.T) {
	file, err := Create(filepath.Join(t.TempDir(), "key"), 0o400)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Discard()
	info, err := os.Stat(file.Temporary())
	if err != nil || info.Mode().Perm() != 0o400 {
		t.Fatalf("staged mode %v (%v), want 0400", info.Mode(), err)
	}
}

func TestDiscardLeavesTheDestinationAsItWas(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "log")
	if err := os.WriteFile(path, []byte("kept"), 0o600); err != nil {
		t.Fatal(err)
	}
	file, err := Create(path, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("half"); err != nil {
		t.Fatal(err)
	}
	file.Discard()
	if data, _ := os.ReadFile(path); string(data) != "kept" {
		t.Fatalf("holds %q", data)
	}
	only(t, dir)
	if err := file.Replace(); err == nil {
		t.Fatal("a discarded file was put in place")
	}
}

func TestLinkRefusesADestinationThatExists(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "seed")
	if err := WriteFile(path, []byte("first"), 0o400); err != nil {
		t.Fatal(err)
	}
	file, err := Create(path, 0o400)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("second"); err != nil {
		t.Fatal(err)
	}
	if err := file.Link(); !errors.Is(err, fs.ErrExist) {
		t.Fatalf("Link over a file: %v, want it to exist", err)
	}
	if data, _ := os.ReadFile(path); string(data) != "first" {
		t.Fatalf("holds %q", data)
	}
	only(t, dir)

	fresh := filepath.Join(t.TempDir(), "seed")
	file, err = Create(fresh, 0o400)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("only"); err != nil {
		t.Fatal(err)
	}
	if err := file.Link(); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(fresh); string(data) != "only" {
		t.Fatalf("holds %q", data)
	}
	only(t, filepath.Dir(fresh))
}

func TestWriteFileInStaysBeneathItsRoot(t *testing.T) {
	outside := t.TempDir()
	dir := filepath.Join(outside, "root")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if err := WriteFileIn(root, "../escaped", []byte("x"), 0o600); err == nil {
		t.Fatal("a name outside the root was written")
	}
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	if err := WriteFileIn(root, "link/escaped", []byte("x"), 0o600); err == nil {
		t.Fatal("a link out of the root was followed")
	}
	if err := WriteFileIn(root, "status", []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(filepath.Join(dir, "status")); string(data) != "ok" {
		t.Fatalf("holds %q", data)
	}
	if _, err := os.Stat(filepath.Join(outside, "escaped")); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("wrote outside the root: %v", err)
	}
}
