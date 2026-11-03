package upgrade

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

// openUnmoved opens path refusing a symlink at any component (openat2 with
// RESOLVE_NO_SYMLINKS), so a directory swapped for a link after path was
// checked fails the open. A kernel without openat2 refuses the open, since
// O_NOFOLLOW alone guards only the last component.
func openUnmoved(path string, flag int, perm os.FileMode) (*os.File, error) {
	how := unix.OpenHow{Flags: uint64(flag | unix.O_CLOEXEC | unix.O_NOFOLLOW), Resolve: unix.RESOLVE_NO_SYMLINKS}
	if flag&os.O_CREATE != 0 {
		how.Mode = uint64(perm.Perm())
	}
	for {
		fd, err := unix.Openat2(unix.AT_FDCWD, path, &how)
		switch {
		case errors.Is(err, unix.EINTR):
			continue
		case errors.Is(err, unix.ENOSYS):
			return nil, &os.PathError{Op: "openat2", Path: path, Err: errors.New("this kernel cannot open a path without following its symlinks")}
		case err != nil:
			return nil, &os.PathError{Op: "openat2", Path: path, Err: err}
		}
		return os.NewFile(uintptr(fd), path), nil
	}
}
