package providers

import (
	"errors"

	"golang.org/x/sys/unix"
)

// exchangeDirs swaps two paths in one step (renamex_np RENAME_SWAP), so a
// reader finds the old lineage or the new one under the name, never neither.
// unsupported says the filesystem cannot, and nothing was changed.
func exchangeDirs(a, b string) (unsupported bool, err error) {
	err = unix.RenamexNp(a, b, unix.RENAME_SWAP)
	if errors.Is(err, unix.ENOTSUP) || errors.Is(err, unix.EINVAL) {
		return true, err
	}
	return false, err
}
