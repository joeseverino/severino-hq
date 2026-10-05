package runtime

import (
	"net"

	"golang.org/x/sys/unix"
)

// peerUID is the uid of the process at the other end, as the kernel recorded
// it when the connection was made (SO_PEERCRED).
func peerUID(conn net.Conn) (int, error) {
	raw, err := rawConn(conn)
	if err != nil {
		return 0, err
	}
	var credentials *unix.Ucred
	var failure error
	if err := raw.Control(func(fd uintptr) {
		credentials, failure = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if failure != nil {
		return 0, failure
	}
	return int(credentials.Uid), nil
}
