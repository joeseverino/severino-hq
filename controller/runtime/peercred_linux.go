package runtime

import (
	"syscall"

	"golang.org/x/sys/unix"
)

// PeerUIDOf is the uid of the process at the other end of a Unix socket, as
// the kernel recorded it when the connection was made (SO_PEERCRED).
func PeerUIDOf(raw syscall.RawConn) (int, error) {
	var credentials *unix.Ucred
	var failure error
	if err := raw.Control(func(fd uintptr) {
		if failure = unixSocket(int(fd)); failure == nil {
			credentials, failure = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
		}
	}); err != nil {
		return 0, err
	}
	if failure != nil {
		return 0, failure
	}
	return int(credentials.Uid), nil
}
