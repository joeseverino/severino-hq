package runtime

import (
	"syscall"

	"golang.org/x/sys/unix"
)

// PeerUIDOf is the uid of the process at the other end of a Unix socket, as
// the kernel recorded it when the connection was made (LOCAL_PEERCRED). The
// controller ships for Linux; this keeps its tests honest on a developer's
// machine.
func PeerUIDOf(raw syscall.RawConn) (int, error) {
	var credentials *unix.Xucred
	var failure error
	if err := raw.Control(func(fd uintptr) {
		if failure = unixSocket(int(fd)); failure == nil {
			credentials, failure = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
		}
	}); err != nil {
		return 0, err
	}
	if failure != nil {
		return 0, failure
	}
	return int(credentials.Uid), nil
}
