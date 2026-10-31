package runtime

import (
	"encoding/json"
	"errors"
	"go/parser"
	"go/token"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// socketDir is a directory only this account can enter, short enough for a
// Unix socket path on every platform the tests run on.
func socketDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "hqb")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	return dir
}

// listen binds a socket the way HQ serves one: this account's, mode 0600.
func listen(t *testing.T, path string) *net.UnixListener {
	t.Helper()
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	return listener
}

// served is a real HTTP server on a real Unix socket, answering as HQ would.
func served(t *testing.T, handler http.HandlerFunc) *SocketBridge {
	t.Helper()
	path := filepath.Join(socketDir(t), "bridge.sock")
	server := &http.Server{Handler: handler}
	go func(l net.Listener) { _ = server.Serve(l) }(listen(t, path))
	t.Cleanup(func() { server.Close() })
	return bridgeAt(t, path)
}

func bridgeAt(t *testing.T, path string) *SocketBridge {
	t.Helper()
	bridge, err := NewSocketBridge(path)
	if err != nil {
		t.Fatal(err)
	}
	bridge.Timeout = 5 * time.Second
	return bridge
}

func answer(body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
}

func refusedWith(t *testing.T, err error, want string) {
	t.Helper()
	if _, ok := errors.AsType[*BridgeError](err); !ok || !strings.Contains(err.Error(), want) {
		t.Fatalf("got %v, want a bridge error containing %q", err, want)
	}
}

func TestEachCallIsTheContractsRequest(t *testing.T) {
	type seen struct {
		Method, Path, Kind string
		Query              map[string][]string
		Bytes              int
	}
	var mu sync.Mutex
	requests := []seen{}
	b := served(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		requests = append(requests, seen{r.Method, r.URL.Path, r.Header.Get("Content-Type"), r.URL.Query(), len(body)})
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok": true}`)
	})
	ctx := t.Context()
	if _, err := b.Claim(ctx, "example-controller", []string{"example.kind:reconcile", "example.kind:renew"}); err != nil {
		t.Fatal(err)
	}
	inventory := Inventory{"adguard.rewrite": KindReport{OK: true, Error: strings.Repeat("x", 300000)}}
	if err := b.Inventory(ctx, "example-controller", inventory); err != nil {
		t.Fatal(err)
	}
	if err := b.Report(ctx, "example-controller", "7", ControllerReport{Status: Object{}, Conditions: []Condition{}}); err != nil {
		t.Fatal(err)
	}
	claim, sent, report := requests[0], requests[1], requests[2]
	if claim.Method != http.MethodPost || claim.Path != "/claim" || claim.Bytes != 0 ||
		!slices.Equal(claim.Query["controller-id"], []string{"example-controller"}) ||
		!slices.Equal(claim.Query["capability"], []string{"example.kind:reconcile", "example.kind:renew"}) ||
		!slices.Equal(claim.Query["lease-seconds"], []string{"300"}) {
		t.Fatalf("claim %+v", claim)
	}
	if sent.Path != "/inventory" || sent.Kind != "application/json" || sent.Bytes < 300000 {
		t.Fatalf("inventory %+v", sent)
	}
	for _, values := range sent.Query {
		if len(strings.Join(values, "")) > 4096 {
			t.Fatal("payload in the query")
		}
	}
	if report.Path != "/report" || !slices.Equal(report.Query["operation"], []string{"7"}) {
		t.Fatalf("report %+v", report)
	}
}

func TestBridgeRefusesInvalidAndTrailingJSON(t *testing.T) {
	for name, body := range map[string]string{"bad": "not json", "trailing": "{} {}"} {
		t.Run(name, func(t *testing.T) {
			if _, err := served(t, answer(body)).Registry(t.Context()); err == nil {
				t.Fatal("accepted invalid output")
			}
		})
	}
}

// A field the contract does not declare is refused, not silently dropped.
func TestBridgeRefusesFieldsTheContractDoesNotDeclare(t *testing.T) {
	_, err := served(t, answer(`{"ok": true, "undeclared": 1}`)).GlancePlan(t.Context(), "example-controller")
	refusedWith(t, err, "undeclared")
}

func TestBridgeRefusesAnAnswerThatIsNotJSON(t *testing.T) {
	b := served(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, `{"ok": true}`)
	})
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "not JSON")
}

func TestBridgeExplainsARefusal(t *testing.T) {
	problem := func(status int, body string) http.HandlerFunc {
		return func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/problem+json")
			w.WriteHeader(status)
			_, _ = io.WriteString(w, body)
		}
	}
	b := served(t, problem(400, `{"title": "Bad Request", "status": 400, "detail": "Unknown kind."}`))
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "bridge failed: Unknown kind.")
	// Without a problem to read, the status says what happened.
	b = served(t, problem(503, "busy"))
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "bridge failed: Service Unavailable")
	// A long detail is clipped to what a verdict carries.
	long, _ := json.Marshal(Problem{Title: "Bad Request", Status: 400, Detail: strings.Repeat("x", 10*VerdictLimit)})
	b = served(t, problem(400, string(long)))
	if err := b.Schedule(t.Context(), "example-controller"); err == nil || len(err.Error()) > VerdictLimit+len("bridge failed: ") {
		t.Fatalf("%d bytes", len(err.Error()))
	}
}

func TestBridgeDoesNotFollowARedirect(t *testing.T) {
	b := served(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://example.com/elsewhere", http.StatusFound)
	})
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "bridge failed")
}

func TestBridgeBoundsACallThatHangs(t *testing.T) {
	release := make(chan struct{})
	b := served(t, func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	})
	t.Cleanup(func() { close(release) })
	b.Timeout = 50 * time.Millisecond
	started := time.Now()
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "deadline")
	if time.Since(started) > 3*time.Second {
		t.Fatal("the deadline did not bound the call")
	}
}

// An answer that starts and never ends is bounded by the same deadline.
func TestBridgeBoundsAnAnswerThatStalls(t *testing.T) {
	release := make(chan struct{})
	b := served(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok": `)
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
		}
	})
	t.Cleanup(func() { close(release) })
	b.Timeout = 50 * time.Millisecond
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "deadline")
}

func limitMessages(t *testing.T, limit int) {
	t.Helper()
	previous := MaxBridgeOutput
	MaxBridgeOutput = limit
	t.Cleanup(func() { MaxBridgeOutput = previous })
}

func TestBridgeMessagesAreBoundedBothWays(t *testing.T) {
	limitMessages(t, 1<<10)
	calls := 0
	b := served(t, func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(make([]byte, MaxBridgeOutput+1))
	})
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "too much data")
	inventory := Inventory{"adguard.rewrite": KindReport{Error: strings.Repeat("x", MaxBridgeOutput)}}
	refusedWith(t, b.Inventory(t.Context(), "example-controller", inventory), "payload is too large")
	if calls != 1 {
		t.Fatalf("an oversized payload was sent: %d calls", calls)
	}
}

func TestTheLimitIsTheContracts(t *testing.T) {
	if MaxBridgeOutput != 64<<20 {
		t.Fatalf("the contract's BridgeBody limit is %d", MaxBridgeOutput)
	}
}

func TestCallsMayRunTogether(t *testing.T) {
	b := served(t, answer(`{"ok": true}`))
	var wait sync.WaitGroup
	failures := make(chan error, 32)
	for range 32 {
		wait.Go(func() {
			if err := b.Schedule(t.Context(), "example-controller"); err != nil {
				failures <- err
			}
		})
	}
	wait.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
}

// Who may be called: only a socket this account can trust.

func TestASocketThisAccountCannotTrustIsRefused(t *testing.T) {
	uid := os.Geteuid()
	for name, tc := range map[string]struct {
		arrange func(t *testing.T, dir, path string) string
		want    string
	}{
		"no socket": {func(*testing.T, string, string) string { return "" }, "no socket at its path"},
		"no directory": {func(_ *testing.T, dir, _ string) string {
			return filepath.Join(dir, "absent", "bridge.sock")
		}, "directory is missing"},
		"a directory others can enter": {func(t *testing.T, dir, path string) string {
			listen(t, path)
			if err := os.Chmod(dir, 0o755); err != nil {
				t.Fatal(err)
			}
			return ""
		}, "open to another account"},
		"a directory the group can write": {func(t *testing.T, dir, path string) string {
			listen(t, path)
			if err := os.Chmod(dir, 0o720); err != nil {
				t.Fatal(err)
			}
			return ""
		}, "open to another account"},
		"a linked directory": {func(t *testing.T, dir, path string) string {
			listen(t, path)
			link := filepath.Join(socketDir(t), "link")
			if err := os.Symlink(dir, link); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(link, "bridge.sock")
		}, "not a directory"},
		"a socket others can use": {func(t *testing.T, _, path string) string {
			listen(t, path)
			if err := os.Chmod(path, 0o666); err != nil {
				t.Fatal(err)
			}
			return ""
		}, "mode is not 0600"},
		"a link to a socket": {func(t *testing.T, dir, path string) string {
			listen(t, filepath.Join(dir, "real.sock"))
			if err := os.Symlink(filepath.Join(dir, "real.sock"), path); err != nil {
				t.Fatal(err)
			}
			return ""
		}, "not a socket"},
		"a file at the path": {func(t *testing.T, _, path string) string {
			if err := os.WriteFile(path, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			return ""
		}, "not a socket"},
		"a path that is not clean": {func(t *testing.T, dir, path string) string {
			listen(t, path)
			return dir + "/./bridge.sock"
		}, "not an absolute, clean path"},
		"a relative path": {func(*testing.T, string, string) string { return "bridge.sock" }, "not an absolute, clean path"},
	} {
		t.Run(name, func(t *testing.T) {
			dir := socketDir(t)
			path := filepath.Join(dir, "bridge.sock")
			if other := tc.arrange(t, dir, path); other != "" {
				path = other
			}
			_, err := DialTrusted(path, uid)(t.Context(), "", "")
			refusedWith(t, err, tc.want)
			refusedWith(t, bridgeAt(t, path).Schedule(t.Context(), "example-controller"), tc.want)
		})
	}
}

func TestAnotherAccountsSocketIsRefused(t *testing.T) {
	dir := socketDir(t)
	path := filepath.Join(dir, "bridge.sock")
	listen(t, path)
	// The same files, asked about on behalf of an account that owns neither.
	_, err := DialTrusted(path, os.Geteuid()+1)(t.Context(), "", "")
	refusedWith(t, err, "directory belongs to another account")
}

func TestASocketNobodyListensOnIsRefused(t *testing.T) {
	path := filepath.Join(socketDir(t), "bridge.sock")
	listener := listen(t, path)
	listener.SetUnlinkOnClose(false)
	listener.Close()
	refusedWith(t, bridgeAt(t, path).Schedule(t.Context(), "example-controller"), "not listening")
}

// The peer is asked of the kernel on the connection itself, after the path
// was checked: a listener that is not this account is hung up on.
func TestThePeerIsThisAccountOrTheCallIsRefused(t *testing.T) {
	path := filepath.Join(socketDir(t), "bridge.sock")
	server := &http.Server{Handler: answer(`{"ok": true}`)}
	go func(l net.Listener) { _ = server.Serve(l) }(listen(t, path))
	t.Cleanup(func() { server.Close() })

	conn, err := DialTrusted(path, os.Geteuid())(t.Context(), "", "")
	if err != nil {
		t.Fatal(err)
	}
	if peer, err := PeerUID(conn); err != nil || peer != os.Geteuid() {
		t.Fatalf("peer %d %v", peer, err)
	}
	conn.Close()

	previous := readPeerUID
	t.Cleanup(func() { readPeerUID = previous })
	readPeerUID = func(net.Conn) (int, error) { return os.Geteuid() + 1, nil }
	refusedWith(t, bridgeAt(t, path).Schedule(t.Context(), "example-controller"), "answered by another account")
	readPeerUID = func(net.Conn) (int, error) { return 0, errors.New("unreadable") }
	refusedWith(t, bridgeAt(t, path).Schedule(t.Context(), "example-controller"), "answered by another account")
}

// HQ restarting replaces its socket; anything else in its place is refused.
func TestAReplacedSocketIsCheckedAgainOnEveryCall(t *testing.T) {
	path := filepath.Join(socketDir(t), "bridge.sock")
	first := &http.Server{Handler: answer(`{"ok": true}`)}
	go func(l net.Listener) { _ = first.Serve(l) }(listen(t, path))
	b := bridgeAt(t, path)
	if err := b.Schedule(t.Context(), "example-controller"); err != nil {
		t.Fatal(err)
	}
	first.Close()
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "no socket at its path")

	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	refusedWith(t, b.Schedule(t.Context(), "example-controller"), "not a socket")
	os.Remove(path)

	second := &http.Server{Handler: answer(`{"ok": true}`)}
	go func(l net.Listener) { _ = second.Serve(l) }(listen(t, path))
	t.Cleanup(func() { second.Close() })
	if err := b.Schedule(t.Context(), "example-controller"); err != nil {
		t.Fatalf("HQ's new socket was refused: %v", err)
	}
}

func TestABridgeWithNoSocketNamedIsNotBuilt(t *testing.T) {
	_, err := NewSocketBridge("")
	refusedWith(t, err, "SEVERINO_BRIDGE_SOCKET")
}

// A bridge call is one request on one checked connection, never a process
// (the test below holds that no file here can start one): counted, not timed,
// so a loaded machine cannot fail it.
func TestACallCostsARequestNotAProcess(t *testing.T) {
	path := filepath.Join(socketDir(t), "bridge.sock")
	var opened atomic.Int32
	server := &http.Server{Handler: answer(`{"ok": true}`), ConnState: func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			opened.Add(1)
		}
	}}
	go func(l net.Listener) { _ = server.Serve(l) }(listen(t, path))
	t.Cleanup(func() { server.Close() })
	b := bridgeAt(t, path)
	const calls = 200
	for range calls {
		if err := b.Schedule(t.Context(), "example-controller"); err != nil {
			t.Fatal(err)
		}
	}
	if n := opened.Load(); n != calls {
		t.Fatalf("%d calls opened %d connections, want one each", calls, n)
	}
}

// Nothing that reaches HQ may start a process: the bridge, the worker and the
// command that builds them import no way to.
func TestTheBridgeCannotStartAProcess(t *testing.T) {
	for _, file := range []string{"bridge.go", "bridge_socket.go", "worker.go", "sweep.go", "../cmd/hq-controller/main.go"} {
		parsed, err := parser.ParseFile(token.NewFileSet(), file, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatal(err)
		}
		for _, spec := range parsed.Imports {
			if spec.Path.Value == `"os/exec"` {
				t.Errorf("%s imports os/exec", file)
			}
		}
	}
}

func BenchmarkBridgeCall(b *testing.B) {
	dir, err := os.MkdirTemp("", "hqb")
	if err != nil {
		b.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "bridge.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		b.Fatal(err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		b.Fatal(err)
	}
	server := &http.Server{Handler: answer(`{"ok": true}`)}
	go func() { _ = server.Serve(listener) }()
	defer server.Close()
	bridge, err := NewSocketBridge(path)
	if err != nil {
		b.Fatal(err)
	}
	for b.Loop() {
		if err := bridge.Schedule(b.Context(), "example-controller"); err != nil {
			b.Fatal(err)
		}
	}
}
