//go:build linux || darwin

package runtime

import (
	"errors"

	"golang.org/x/sys/unix"
)

// unixSocket refuses a descriptor that is not a Unix socket, since the peer
// credential a kernel gives for any other names no account.
func unixSocket(fd int) error {
	address, err := unix.Getsockname(fd)
	if err != nil {
		return err
	}
	if _, local := address.(*unix.SockaddrUnix); !local {
		return errors.New("not a Unix socket")
	}
	return nil
}
