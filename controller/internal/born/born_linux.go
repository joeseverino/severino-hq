package born

import (
	"time"

	"golang.org/x/sys/unix"
)

// At is when the file at path was created, as statx(2) states it, or when
// it was last written where the filesystem keeps no birth time.
func At(path string) (time.Time, error) {
	var stat unix.Statx_t
	if err := unix.Statx(unix.AT_FDCWD, path, unix.AT_SYMLINK_NOFOLLOW, unix.STATX_BTIME|unix.STATX_MTIME, &stat); err != nil {
		return time.Time{}, err
	}
	at := stat.Mtime
	if stat.Mask&unix.STATX_BTIME != 0 {
		at = stat.Btime
	}
	return time.Unix(at.Sec, int64(at.Nsec)), nil
}
