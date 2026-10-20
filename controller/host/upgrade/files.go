package upgrade

import (
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// owner is a file's numeric owner.
func owner(info fs.FileInfo) string {
	if stat, ok := info.Sys().(*syscall.Stat_t); ok {
		return strconv.FormatUint(uint64(stat.Uid), 10)
	}
	return ""
}

// othersWrite is whether anyone but the owner may write path: by its mode,
// or by an access list, which can grant writes the mode does not show.
func othersWrite(path string, info fs.FileInfo) bool {
	return info.Mode().Perm()&0o022 != 0 || hasACL(path)
}

// unmoved is whether a path resolves to itself: no symlink anywhere along it,
// so what root is about to read or replace is the path that was checked, not
// one another writer in the stack swapped in since. Asked when the mounts are
// read and again right before each snapshot and each restore.
func unmoved(path string) bool {
	if path == "" || path == "/" || strings.HasSuffix(path, "/") || !filepath.IsAbs(path) {
		return false
	}
	real, err := filepath.EvalSymlinks(filepath.Dir(path))
	if err != nil || strings.TrimSuffix(real, "/")+"/"+filepath.Base(path) != path {
		return false
	}
	info, err := os.Lstat(path)
	return err != nil || info.Mode()&fs.ModeSymlink == 0
}

// copyFile writes source's bytes, mode, owner and times into target, in
// place when it exists, as `cp -p` does. Neither end is followed through a
// symlink.
func copyFile(source, target string) error {
	return copyFileOwned(source, target, nil)
}

// copyFileOwned is copyFile with target owned as owner is, or as source is
// when owner is nil.
func copyFileOwned(source, target string, owner fs.FileInfo) (err error) {
	in, err := os.OpenFile(source, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New(source + " is not a file")
	}
	out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, info.Mode().Perm())
	if err != nil {
		return err
	}
	defer func() {
		if closeErr := out.Close(); err == nil {
			err = closeErr
		}
	}()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	if err := out.Chmod(info.Mode().Perm()); err != nil {
		return err
	}
	if owner == nil {
		owner = info
	}
	if stat, ok := owner.Sys().(*syscall.Stat_t); ok {
		// Only root can give a file away; an unprivileged drill keeps its own.
		if err := out.Chown(int(stat.Uid), int(stat.Gid)); err != nil && !errors.Is(err, fs.ErrPermission) {
			return err
		}
	}
	modified := unix.NsecToTimeval(info.ModTime().UnixNano())
	return unix.Futimes(int(out.Fd()), []unix.Timeval{modified, modified})
}
