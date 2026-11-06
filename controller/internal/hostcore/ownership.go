package hostcore

import (
	"io/fs"
	"syscall"
)

// Ownership is a file's owner and how many names it has, from what Lstat or
// Stat returned; ok is false where the platform does not say.
func Ownership(info fs.FileInfo) (uid int, links uint64, ok bool) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, false
	}
	return int(stat.Uid), uint64(stat.Nlink), true
}
