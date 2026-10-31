//go:build !linux && !darwin

package runtime

import (
	"errors"
	"syscall"
)

// PeerUIDOf has no answer here, so the bridge is refused: a peer that cannot
// be named is not trusted.
func PeerUIDOf(syscall.RawConn) (int, error) {
	return 0, errors.New("peer credentials are not available on this platform")
}
