package runtime

import (
	"context"
	"errors"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"syscall"
)

// socketMode is the only mode a bridge socket is served with.
const socketMode fs.FileMode = 0o600

// DialTrusted dials the Unix socket at path for the account uid, and only a
// socket that account can trust: in a directory that is uid's own and closed
// to everyone else, itself uid's own socket with mode 0600 and not a link,
// and answered by a process the kernel reports as uid. Each refusal says
// which rule failed; none is retried another way.
func DialTrusted(path string, uid int) func(context.Context, string, string) (net.Conn, error) {
	return func(ctx context.Context, _, _ string) (net.Conn, error) {
		if err := trustedSocket(path, uid); err != nil {
			return nil, err
		}
		conn, err := (&net.Dialer{}).DialContext(ctx, "unix", path)
		if err != nil {
			if ctx.Err() != nil {
				return nil, err
			}
			return nil, &BridgeError{"HQ is not listening on the bridge socket"}
		}
		// The path was checked before the connection existed; the peer is
		// checked on the connection itself, so a socket swapped in between
		// is still answered by uid or refused.
		peer, err := readPeerUID(conn)
		if err != nil || peer != uid {
			conn.Close()
			return nil, &BridgeError{"the bridge socket is answered by another account"}
		}
		return conn, nil
	}
}

// readPeerUID is peerUID; a test stands in an answer the kernel would give
// for another account.
var readPeerUID = peerUID

func ownerOf(info fs.FileInfo) (int, bool) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, false
	}
	return int(stat.Uid), true
}

func trustedSocket(path string, uid int) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return &BridgeError{"the bridge socket path is not an absolute, clean path"}
	}
	directory, err := os.Lstat(filepath.Dir(path))
	if err != nil {
		return &BridgeError{"HQ is not serving the bridge: its socket directory is missing"}
	}
	if !directory.IsDir() {
		return &BridgeError{"the bridge socket's directory is not a directory"}
	}
	if owner, ok := ownerOf(directory); !ok || owner != uid {
		return &BridgeError{"the bridge socket's directory belongs to another account"}
	}
	if directory.Mode().Perm()&0o077 != 0 {
		return &BridgeError{"the bridge socket's directory is open to another account"}
	}
	socket, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return &BridgeError{"HQ is not serving the bridge: no socket at its path"}
	}
	if err != nil {
		return &BridgeError{"the bridge socket could not be examined"}
	}
	if socket.Mode().Type() != fs.ModeSocket {
		return &BridgeError{"the bridge socket path is not a socket"}
	}
	if owner, ok := ownerOf(socket); !ok || owner != uid {
		return &BridgeError{"the bridge socket belongs to another account"}
	}
	if socket.Mode().Perm() != socketMode {
		return &BridgeError{"the bridge socket's mode is not 0600"}
	}
	return nil
}

// rawConn is the connection's descriptor, for the peer-credential read.
func rawConn(conn net.Conn) (syscall.RawConn, error) {
	unix, ok := conn.(*net.UnixConn)
	if !ok {
		return nil, errors.New("not a Unix connection")
	}
	return unix.SyscallConn()
}
