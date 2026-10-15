package host

import (
	"errors"
	"net"
	"strings"
)

// Notify sends state to the service manager over $NOTIFY_SOCKET, the
// sd_notify protocol: one datagram. An unset socket is no service manager,
// which is not an error; anything else that fails is.
func Notify(socket, state string) error {
	if socket == "" {
		return nil
	}
	if !strings.HasPrefix(socket, "/") && !strings.HasPrefix(socket, "@") {
		return errors.New("NOTIFY_SOCKET is neither a path nor an abstract socket")
	}
	if strings.HasPrefix(socket, "@") {
		socket = "\x00" + socket[1:]
	}
	conn, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: socket, Net: "unixgram"})
	if err != nil {
		return err
	}
	defer conn.Close()
	_, err = conn.Write([]byte(state))
	return err
}
