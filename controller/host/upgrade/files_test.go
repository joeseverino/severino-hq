package upgrade

import (
	"os"
	"path/filepath"
	"testing"
)

// A directory along a path swapped for a link after the path was checked is
// refused at the open, at either end of the copy, and nothing outside is
// read or written.
func TestCopyFileFollowsNoLinkAlongEitherPath(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(root, "outside")
	inside := filepath.Join(root, "stack")
	for _, dir := range []string{outside, inside} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(outside, "app.conf"), []byte("outside\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	snapshot := filepath.Join(root, "snapshot")
	if err := os.WriteFile(snapshot, []byte("snapshot\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	swapped := filepath.Join(root, "swapped")
	if err := os.Symlink(outside, swapped); err != nil {
		t.Fatal(err)
	}

	if err := copyFile(snapshot, filepath.Join(swapped, "app.conf")); err == nil {
		t.Error("a restore through a linked directory was written")
	}
	if body, _ := os.ReadFile(filepath.Join(outside, "app.conf")); string(body) != "outside\n" {
		t.Errorf("the file the link reached was changed: %q", body)
	}

	copied := filepath.Join(inside, "copied")
	if err := copyFile(filepath.Join(swapped, "app.conf"), copied); err == nil {
		t.Error("a snapshot through a linked directory was read")
	}
	if _, err := os.Lstat(copied); err == nil {
		t.Error("a snapshot through a linked directory left a copy")
	}
}
