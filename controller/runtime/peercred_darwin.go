package runtime

import (
	"net"

	"golang.org/x/sys/unix"
)

// peerUID is the uid of the process at the other end, as the kernel recorded
// it when the connection was made (LOCAL_PEERCRED). The controller ships for
// Linux; this keeps its tests honest on a developer's machine.
func peerUID(conn net.Conn) (int, error) {
	raw, err := rawConn(conn)
	if err != nil {
		return 0, err
	}
	var credentials *unix.Xucred
	var failure error
	if err := raw.Control(func(fd uintptr) {
		credentials, failure = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if failure != nil {
		return 0, failure
	}
	return int(credentials.Uid), nil
}
