package host

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

// Rendered reports whether dir holds the application environment as the
// renderer installs it: a directory only root can enter, holding one regular
// file of the web account's with no other name. Anything else (missing, a
// link, a directory docker made at the file's name) is not it, and says why.
func Rendered(dir string, rootUID, webUID int) error {
	info, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("%s is not there yet", dir)
	}
	if uid, _, ok := Ownership(info); !ok || !info.IsDir() || uid != rootUID || info.Mode().Perm() != 0o700 {
		return fmt.Errorf("%s is not a directory only root can enter", dir)
	}
	file := filepath.Join(dir, AppEnvName)
	info, err = os.Lstat(file)
	if err != nil {
		return fmt.Errorf("%s is not rendered yet", file)
	}
	if uid, links, ok := Ownership(info); !ok || !info.Mode().IsRegular() || uid != webUID || links != 1 {
		return fmt.Errorf("%s is not the file the renderer installs", file)
	}
	return nil
}

// Ownership is a file's owner and how many names it has, from what Lstat or
// Stat returned; ok is false where the platform does not say.
func Ownership(info fs.FileInfo) (uid int, links uint64, ok bool) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, false
	}
	return int(stat.Uid), uint64(stat.Nlink), true
}
