package install

import (
	"os"
	"syscall"
)

// tmpfsMagic is TMPFS_MAGIC from linux/magic.h.
const tmpfsMagic = 0x01021994

// SystemMounts checks dir against this machine's mount table, and asks the
// filesystem itself as well.
func SystemMounts(dir string) error {
	mountinfo, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return hostError("Controller secret directory must be on tmpfs.")
	}
	if err := CheckMountInfo(mountinfo, dir); err != nil {
		return err
	}
	var stat syscall.Statfs_t
	if err := syscall.Statfs(dir, &stat); err != nil || int64(stat.Type) != tmpfsMagic {
		return hostError("Controller secret directory must be on tmpfs.")
	}
	return nil
}
