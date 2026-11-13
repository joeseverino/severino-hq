//go:build !linux

package born

import (
	"os"
	"time"
)

// At is when the file at path was last written: off Linux, where an HQ host
// never runs, there is no statx to ask for its birth.
func At(path string) (time.Time, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return time.Time{}, err
	}
	return info.ModTime(), nil
}
