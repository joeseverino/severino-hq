package providers

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"slices"
	"strconv"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// sshFailed is the exit of a call that failed before or outside the remote
// command, as ssh reports its own failures, so a refused connection and a
// remote command's own exit stay apart.
const sshFailed = 255

// sshCall is one forced operation on a connection's host, made in process:
// the host key is checked against known_hosts alone, the one identity file is
// the only credential offered, and no agent or config is read.
type sshCall struct {
	target               runtime.SSHTarget
	identity, knownHosts string
	connectTimeout       time.Duration
}

// run makes the call with execProcess's bounds and results: the command
// timeout over the whole session, each stream held to CommandOutputLimit, and
// a remote non-zero exit as exit.
func (call sshCall) run(ctx context.Context, operation string, payload []byte) ([]byte, []byte, int, error) {
	ctx, cancel := context.WithTimeout(ctx, runtime.CommandTimeout)
	defer cancel()
	stdout, stderr := runtime.BoundedBuffer{Limit: CommandOutputLimit}, runtime.BoundedBuffer{Limit: CommandOutputLimit}
	exit, failure := call.session(ctx, operation, payload, &stdout, &stderr)
	if ctx.Err() != nil {
		return nil, nil, 0, context.DeadlineExceeded
	}
	if failure != "" {
		_, _ = fmt.Fprintln(&stderr, failure)
	}
	if stdout.Overflow || stderr.Overflow {
		return nil, nil, 0, errOutputLimit
	}
	return stdout.Bytes(), stderr.Bytes(), exit, nil
}

// session returns the remote command's exit, or sshFailed and why.
func (call sshCall) session(ctx context.Context, operation string, payload []byte, stdout, stderr io.Writer) (int, string) {
	address := net.JoinHostPort(call.target.Host, strconv.Itoa(call.target.Port))
	pinned, err := knownhosts.New(call.knownHosts)
	if err != nil {
		return sshFailed, "known_hosts cannot be read"
	}
	// The parser's own errors are not repeated: they describe key material.
	key, err := os.ReadFile(call.identity)
	if err != nil {
		return sshFailed, "the identity cannot be read"
	}
	signer, err := ssh.ParsePrivateKey(key)
	if err != nil {
		return sshFailed, "the identity is not a usable private key"
	}
	config := &ssh.ClientConfig{
		User:              call.target.User,
		Auth:              []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback:   pinned,
		HostKeyAlgorithms: pinnedAlgorithms(pinned, address),
	}
	dialer := net.Dialer{Timeout: call.connectTimeout}
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return sshFailed, err.Error()
	}
	defer func() { _ = conn.Close() }()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	// The connect timeout bounds the handshake and authentication too, so a
	// host that accepts and then stalls fails as one that never answered.
	_ = conn.SetDeadline(time.Now().Add(call.connectTimeout))
	clientConn, channels, requests, err := ssh.NewClientConn(conn, address, config)
	if err != nil {
		if _, ok := errors.AsType[*knownhosts.KeyError](err); ok {
			return sshFailed, "Host key verification failed: " + err.Error()
		}
		return sshFailed, err.Error()
	}
	_ = conn.SetDeadline(time.Time{})
	client := ssh.NewClient(clientConn, channels, requests)
	defer func() { _ = client.Close() }()
	session, err := client.NewSession()
	if err != nil {
		return sshFailed, err.Error()
	}
	session.Stdin, session.Stdout, session.Stderr = bytes.NewReader(payload), stdout, stderr
	err = session.Run(operation)
	if err == nil {
		return 0, ""
	}
	if exited, ok := errors.AsType[*ssh.ExitError](err); ok && exited.Signal() == "" {
		return exited.ExitStatus(), ""
	}
	// A remote killed by a signal, gone without an exit status, or a broken
	// session: ssh exits 255 for each.
	return sshFailed, err.Error()
}

// pinnedAlgorithms is the host key algorithms of the keys known_hosts pins for
// address. Offering only those makes the server present a pinned type, where
// the default list could draw a key of another type and fail as a mismatch.
// A host with none pinned gets the default list, and the callback refuses it.
func pinnedAlgorithms(pinned ssh.HostKeyCallback, address string) []string {
	// A key no host holds draws the pinned keys out as the refusal's Want.
	probe, err := ssh.NewPublicKey(ed25519.PublicKey(make([]byte, ed25519.PublicKeySize)))
	if err != nil {
		return nil
	}
	refused, ok := errors.AsType[*knownhosts.KeyError](pinned(address, &net.TCPAddr{IP: net.IPv4zero}, probe))
	if !ok {
		return nil
	}
	supported := ssh.SupportedAlgorithms().HostKeys
	algorithms := []string{}
	for _, known := range refused.Want {
		names := []string{known.Key.Type()}
		if known.Key.Type() == ssh.KeyAlgoRSA {
			names = []string{ssh.KeyAlgoRSASHA512, ssh.KeyAlgoRSASHA256}
		}
		for _, name := range names {
			if slices.Contains(supported, name) && !slices.Contains(algorithms, name) {
				algorithms = append(algorithms, name)
			}
		}
	}
	return algorithms
}
