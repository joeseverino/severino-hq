package providers

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"encoding/binary"
	"errors"
	"io"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"sync"
	"testing"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
	"github.com/joeseverino/severino-hq/controller/secrets/connecttest"
)

// sshServer is an SSH host on loopback that runs each exec request through
// answer and records what it was asked.
type sshServer struct {
	listener net.Listener
	config   *ssh.ServerConfig
	answer   func(command string, stdin []byte, stdout, stderr io.Writer, closed <-chan struct{}) uint32
	started  chan string

	mu       sync.Mutex
	commands []string
	stdin    [][]byte
}

func newSSHServer(t *testing.T, authorized ssh.PublicKey, hostKeys ...ssh.Signer) *sshServer {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	config := &ssh.ServerConfig{PublicKeyCallback: func(_ ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
		if bytes.Equal(key.Marshal(), authorized.Marshal()) {
			return &ssh.Permissions{}, nil
		}
		return nil, errors.New("not authorized")
	}}
	for _, key := range hostKeys {
		config.AddHostKey(key)
	}
	server := &sshServer{listener: listener, config: config, started: make(chan string, 1)}
	var conns sync.WaitGroup
	t.Cleanup(func() {
		_ = listener.Close()
		conns.Wait()
	})
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			conns.Go(func() { server.serve(conn) })
		}
	}()
	return server
}

// asked is each command the host ran and the input it was sent.
func (s *sshServer) asked() ([]string, [][]byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string{}, s.commands...), append([][]byte{}, s.stdin...)
}

func (s *sshServer) port() int { return s.listener.Addr().(*net.TCPAddr).Port }

func (s *sshServer) serve(conn net.Conn) {
	defer func() { _ = conn.Close() }()
	serverConn, channels, requests, err := ssh.NewServerConn(conn, s.config)
	if err != nil {
		return
	}
	closed := make(chan struct{})
	go func() {
		_ = serverConn.Wait()
		close(closed)
	}()
	go ssh.DiscardRequests(requests)
	for opened := range channels {
		if opened.ChannelType() != "session" {
			_ = opened.Reject(ssh.UnknownChannelType, "session only")
			continue
		}
		channel, requests, err := opened.Accept()
		if err != nil {
			return
		}
		go s.session(channel, requests, closed)
	}
}

func (s *sshServer) session(channel ssh.Channel, requests <-chan *ssh.Request, closed <-chan struct{}) {
	defer func() { _ = channel.Close() }()
	for request := range requests {
		if request.Type != "exec" {
			_ = request.Reply(false, nil)
			continue
		}
		var exec struct{ Command string }
		if ssh.Unmarshal(request.Payload, &exec) != nil {
			_ = request.Reply(false, nil)
			return
		}
		_ = request.Reply(true, nil)
		stdin, _ := io.ReadAll(channel)
		s.mu.Lock()
		s.commands, s.stdin = append(s.commands, exec.Command), append(s.stdin, stdin)
		s.mu.Unlock()
		status := s.answer(exec.Command, stdin, channel, channel.Stderr(), closed)
		_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{status}))
		return
	}
}

// sshFixture is one connection's SSH directory as the renderer writes it: the
// identity in OpenSSH format and known_hosts naming the host bracketed with
// its port.
type sshFixture struct {
	commands *Commands
	dir      string
}

func newSSHFixture(t *testing.T, server *sshServer, identity string, pinned ...ssh.PublicKey) sshFixture {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "edge"), []byte(identity), 0o400); err != nil {
		t.Fatal(err)
	}
	fixture := sshFixture{dir: dir}
	fixture.pin(t, "127.0.0.1", server.port(), pinned...)
	held := supplied(sshConnection("edge", connections.SSHTransport{Host: "127.0.0.1", User: "hq", Port: strconv.Itoa(server.port()), HostKey: "ssh-ed25519 AAAA"}))
	fixture.commands = &Commands{Env: runtime.Environment{SSHDir: dir}, Supplied: held}
	return fixture
}

func (f sshFixture) pin(t *testing.T, host string, port int, keys ...ssh.PublicKey) {
	t.Helper()
	var lines bytes.Buffer
	for _, key := range keys {
		lines.WriteString("[" + host + "]:" + strconv.Itoa(port) + " " + string(ssh.MarshalAuthorizedKey(key)))
	}
	path := filepath.Join(f.dir, "known_hosts")
	if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, lines.Bytes(), 0o444); err != nil {
		t.Fatal(err)
	}
}

func hostSigner(t *testing.T, private any) ssh.Signer {
	t.Helper()
	signer, err := ssh.NewSignerFromKey(private)
	if err != nil {
		t.Fatal(err)
	}
	return signer
}

func ed25519Host(t *testing.T) ssh.Signer {
	t.Helper()
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return hostSigner(t, private)
}

func rsaHost(t *testing.T) ssh.Signer {
	t.Helper()
	private, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return hostSigner(t, private)
}

func ecdsaHost(t *testing.T) ssh.Signer {
	t.Helper()
	private, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return hostSigner(t, private)
}

func authorizedKey(t *testing.T, key connecttest.Key) ssh.PublicKey {
	t.Helper()
	public, _, _, _, err := ssh.ParseAuthorizedKey([]byte(key.Public))
	if err != nil {
		t.Fatal(err)
	}
	return public
}

// echo answers with what it was sent, and fails "fail" with a status of 3.
func echo(command string, stdin []byte, stdout, stderr io.Writer, _ <-chan struct{}) uint32 {
	if command == "fail" {
		_, _ = io.WriteString(stderr, "first line\nrefused by the host\n")
		return 3
	}
	_, _ = io.WriteString(stdout, command+": ")
	_, _ = stdout.Write(stdin)
	return 0
}

func TestSSHRunsTheOperationWithThePayload(t *testing.T) {
	for name, identity := range map[string]connecttest.Key{"ed25519": connecttest.Ed25519Key(t), "rsa": connecttest.RSAKey(t)} {
		t.Run(name, func(t *testing.T) {
			host := ed25519Host(t)
			server := newSSHServer(t, authorizedKey(t, identity), host)
			server.answer = echo
			fixture := newSSHFixture(t, server, identity.OpenSSH, host.PublicKey())
			out, err := fixture.commands.SSH(t.Context(), "edge", "python3 -", []byte("print(1)\n"))
			if err != nil || string(out) != "python3 -: print(1)\n" {
				t.Fatalf("%q %v", out, err)
			}
			// The forced command reads the operation whole, as one command.
			commands, stdin := server.asked()
			if len(commands) != 1 || commands[0] != "python3 -" || string(stdin[0]) != "print(1)\n" {
				t.Fatalf("%q %q", commands, stdin)
			}
			if failures := fixture.commands.StepFailures(); len(failures) != 0 {
				t.Fatalf("%v", failures)
			}
		})
	}
}

// refused asserts a call failed in the SSH connection itself, before any
// operation ran.
func refused(t *testing.T, fixture sshFixture, server *sshServer) {
	t.Helper()
	out, err := fixture.commands.SSH(t.Context(), "edge", "status", nil)
	if provider, ok := errors.AsType[*ProviderError](err); !ok || provider.Message != "SSH status for edge: exit 255" || out != nil {
		t.Fatalf("%q %v", out, err)
	}
	if failures := fixture.commands.StepFailures(); len(failures) != 1 || failures[0].Reason != "exit 255" || failures[0].Subject != "edge" {
		t.Fatalf("%v", failures)
	}
	if commands, _ := server.asked(); len(commands) != 0 {
		t.Fatalf("the host ran %q", commands)
	}
}

func TestSSHRefusesAHostKeyNotPinned(t *testing.T) {
	identity := connecttest.Ed25519Key(t)
	host := ed25519Host(t)
	server := newSSHServer(t, authorizedKey(t, identity), host)
	server.answer = echo

	t.Run("unknown host", func(t *testing.T) {
		fixture := newSSHFixture(t, server, identity.OpenSSH)
		fixture.pin(t, "127.0.0.2", server.port(), host.PublicKey())
		refused(t, fixture, server)
	})
	t.Run("changed key", func(t *testing.T) {
		refused(t, newSSHFixture(t, server, identity.OpenSSH, ed25519Host(t).PublicKey()), server)
	})
	t.Run("another port", func(t *testing.T) {
		fixture := newSSHFixture(t, server, identity.OpenSSH)
		fixture.pin(t, "127.0.0.1", server.port()+1, host.PublicKey())
		refused(t, fixture, server)
	})
}

func TestSSHOffersOnlyItsIdentity(t *testing.T) {
	host := ed25519Host(t)
	server := newSSHServer(t, authorizedKey(t, connecttest.Ed25519Key(t)), host)
	server.answer = echo
	refused(t, newSSHFixture(t, server, connecttest.Ed25519Key(t).OpenSSH, host.PublicKey()), server)
}

func TestSSHAsksForAPinnedKeyType(t *testing.T) {
	// The server holds an RSA key, which an unrestricted client asks for
	// first, and the Ed25519 key that is pinned; known_hosts also pins an
	// ECDSA key the server does not hold.
	identity := connecttest.Ed25519Key(t)
	pinned := ed25519Host(t)
	server := newSSHServer(t, authorizedKey(t, identity), rsaHost(t), pinned)
	server.answer = echo
	fixture := newSSHFixture(t, server, identity.OpenSSH, ecdsaHost(t).PublicKey(), pinned.PublicKey())
	if out, err := fixture.commands.SSH(t.Context(), "edge", "status", nil); err != nil || string(out) != "status: " {
		t.Fatalf("%q %v", out, err)
	}
}

func TestSSHReportsARemoteExit(t *testing.T) {
	identity := connecttest.Ed25519Key(t)
	host := ed25519Host(t)
	server := newSSHServer(t, authorizedKey(t, identity), host)
	server.answer = echo
	fixture := newSSHFixture(t, server, identity.OpenSSH, host.PublicKey())
	out, err := fixture.commands.SSH(t.Context(), "edge", "fail", nil)
	if provider, ok := errors.AsType[*ProviderError](err); !ok || provider.Message != "SSH fail for edge: exit 3" || out != nil {
		t.Fatalf("%q %v", out, err)
	}
	if failures := fixture.commands.StepFailures(); len(failures) != 1 || failures[0].Reason != "exit 3" {
		t.Fatalf("%v", failures)
	}
}

func TestSSHHoldsOutputToTheLimit(t *testing.T) {
	identity := connecttest.Ed25519Key(t)
	host := ed25519Host(t)
	server := newSSHServer(t, authorizedKey(t, identity), host)
	for _, stream := range []string{"stdout", "stderr"} {
		t.Run(stream, func(t *testing.T) {
			server.answer = func(_ string, _ []byte, stdout, stderr io.Writer, _ <-chan struct{}) uint32 {
				flooded := map[string]io.Writer{"stdout": stdout, "stderr": stderr}[stream]
				_, _ = flooded.Write(bytes.Repeat([]byte("x"), CommandOutputLimit+1))
				return 0
			}
			fixture := newSSHFixture(t, server, identity.OpenSSH, host.PublicKey())
			out, err := fixture.commands.SSH(t.Context(), "edge", "flood", nil)
			if provider, ok := errors.AsType[*ProviderError](err); !ok || provider.Message != "SSH flood for edge: output over limit" || out != nil {
				t.Fatalf("%d bytes, %v", len(out), err)
			}
		})
	}
}

func TestSSHStopsWhenItsContextEnds(t *testing.T) {
	identity := connecttest.Ed25519Key(t)
	host := ed25519Host(t)

	t.Run("a running operation", func(t *testing.T) {
		server := newSSHServer(t, authorizedKey(t, identity), host)
		server.answer = func(command string, _ []byte, _, _ io.Writer, closed <-chan struct{}) uint32 {
			server.started <- command
			<-closed
			return 0
		}
		fixture := newSSHFixture(t, server, identity.OpenSSH, host.PublicKey())
		ctx, cancel := context.WithCancel(t.Context())
		go func() {
			<-server.started
			cancel()
		}()
		timedOut(ctx, t, fixture)
	})

	t.Run("a host that never answers", func(t *testing.T) {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = listener.Close() })
		ctx, cancel := context.WithCancel(t.Context())
		accepted := make(chan net.Conn, 1)
		go func() {
			conn, err := listener.Accept()
			cancel()
			if err == nil {
				accepted <- conn
			}
		}()
		fixture := newSSHFixture(t, &sshServer{listener: listener}, identity.OpenSSH, host.PublicKey())
		timedOut(ctx, t, fixture)
		_ = (<-accepted).Close()
	})
}

func timedOut(ctx context.Context, t *testing.T, fixture sshFixture) {
	t.Helper()
	out, err := fixture.commands.SSH(ctx, "edge", "hang", nil)
	if provider, ok := errors.AsType[*ProviderError](err); !ok || provider.Message != "SSH hang for edge: timed out" || out != nil {
		t.Fatalf("%q %v", out, err)
	}
}

func TestSSHNeedsItsDirectory(t *testing.T) {
	commands := &Commands{Supplied: supplied(sshConnection("edge", connections.SSHTransport{Host: "192.0.2.1", User: "hq", Port: "22", HostKey: "ssh-ed25519 AAAA"}))}
	if _, err := commands.SSH(t.Context(), "edge", "status", nil); !errors.Is(err, runtime.ErrSettingMissing) {
		t.Fatalf("%v", err)
	}
}

// clientOffer is the algorithm lists of a client's first key exchange
// message, as RFC 4253 section 7.1 lays it out.
type clientOffer struct {
	Cookie                  [16]byte `sshtype:"20"`
	KexAlgos                []string
	ServerHostKeyAlgos      []string
	CiphersClientServer     []string
	CiphersServerClient     []string
	MACsClientServer        []string
	MACsServerClient        []string
	CompressionClientServer []string
	CompressionServerClient []string
	LanguagesClientServer   []string
	LanguagesServerClient   []string
	FirstKexFollows         bool
	Reserved                uint32
}

// readOffer answers the version exchange on conn and reads the client's key
// exchange message, which goes out unencrypted before any key is agreed.
func readOffer(conn net.Conn) (clientOffer, error) {
	var offer clientOffer
	if _, err := io.WriteString(conn, "SSH-2.0-example\r\n"); err != nil {
		return offer, err
	}
	reader := bufio.NewReader(conn)
	if _, err := reader.ReadString('\n'); err != nil {
		return offer, err
	}
	var header [5]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return offer, err
	}
	length, padding := binary.BigEndian.Uint32(header[:4]), int(header[4])
	if length < 1+uint32(padding) || length > 35000 {
		return offer, errors.New("not a key exchange packet")
	}
	packet := make([]byte, length-1)
	if _, err := io.ReadFull(reader, packet); err != nil {
		return offer, err
	}
	err := ssh.Unmarshal(packet[:len(packet)-padding], &offer)
	return offer, err
}

func TestSSHOffersNoInsecureKeyExchangeOrMAC(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	type read struct {
		offer clientOffer
		err   error
	}
	offered := make(chan read, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			offered <- read{err: err}
			return
		}
		defer func() { _ = conn.Close() }()
		offer, err := readOffer(conn)
		offered <- read{offer, err}
	}()
	identity := connecttest.Ed25519Key(t)
	fixture := newSSHFixture(t, &sshServer{listener: listener}, identity.OpenSSH, ed25519Host(t).PublicKey())
	// The host closes once it has the offer, so the call itself fails.
	_, _ = fixture.commands.SSH(t.Context(), "edge", "status", nil)
	got := <-offered
	if got.err != nil {
		t.Fatal(got.err)
	}
	if len(got.offer.KexAlgos) == 0 || len(got.offer.MACsClientServer) == 0 || len(got.offer.MACsServerClient) == 0 {
		t.Fatalf("no offer read: %+v", got.offer)
	}
	insecure := ssh.InsecureAlgorithms()
	for _, kex := range got.offer.KexAlgos {
		if slices.Contains(insecure.KeyExchanges, kex) {
			t.Errorf("offers the insecure key exchange %s", kex)
		}
	}
	for _, mac := range slices.Concat(got.offer.MACsClientServer, got.offer.MACsServerClient) {
		if slices.Contains(insecure.MACs, mac) {
			t.Errorf("offers the insecure MAC %s", mac)
		}
	}
}
