package hostcore

import "syscall"

// Statfs is a filesystem's space as statfs(2) states it: the bytes a process
// other than root can still write, as df's Avail counts them, and its size.
// Both counts are in fragments, f_frsize, where the kernel gives one.
func Statfs(path string) (free, total uint64, err error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, 0, err
	}
	size := uint64(stat.Frsize) //nolint:gosec // G115: the kernel's fragment size is never negative
	if size == 0 {
		size = uint64(stat.Bsize) //nolint:gosec // G115: the kernel's block size is never negative
	}
	return stat.Bavail * size, stat.Blocks * size, nil
}
