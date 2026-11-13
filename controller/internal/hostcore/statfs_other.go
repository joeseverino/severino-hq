//go:build !linux

package hostcore

import "errors"

// Statfs answers only on Linux, the one system an HQ host runs.
func Statfs(string) (free, total uint64, err error) {
	return 0, 0, errors.ErrUnsupported
}
