package providers

import (
	"errors"

	"golang.org/x/sys/unix"
)

// exchangeDirs swaps two paths in one step (renameat2 RENAME_EXCHANGE), so a
// reader finds the old lineage or the new one under the name, never neither.
// unsupported says the filesystem cannot, and nothing was changed.
func exchangeDirs(a, b string) (unsupported bool, err error) {
	err = unix.Renameat2(unix.AT_FDCWD, a, unix.AT_FDCWD, b, unix.RENAME_EXCHANGE)
	if errors.Is(err, unix.ENOSYS) || errors.Is(err, unix.EINVAL) || errors.Is(err, unix.EOPNOTSUPP) {
		return true, err
	}
	return false, err
}
