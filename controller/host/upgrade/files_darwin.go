package upgrade

import (
	"os"

	"golang.org/x/sys/unix"
)

// openUnmoved opens path refusing a symlink at any component
// (O_NOFOLLOW_ANY), so a directory swapped for a link after path was checked
// fails the open.
func openUnmoved(path string, flag int, perm os.FileMode) (*os.File, error) {
	return os.OpenFile(path, flag|unix.O_NOFOLLOW_ANY, perm)
}
