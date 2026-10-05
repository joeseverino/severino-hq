//go:build !linux && !darwin

package runtime

import (
	"errors"
	"net"
)

// peerUID has no answer here, so the bridge is refused: a peer that cannot be
// named is not trusted.
func peerUID(net.Conn) (int, error) {
	return 0, errors.New("peer credentials are not available on this platform")
}
