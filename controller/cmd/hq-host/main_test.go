package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/host"
)

func TestARunStagesWhereItsUnitMayWrite(t *testing.T) {
	for _, c := range []struct{ given, want string }{
		{"", "/run"},
		{"/run/severino-hq-verify", "/run/severino-hq-verify"},
		{"/run/first:/run/second", "/run/first"},
	} {
		t.Setenv("RUNTIME_DIRECTORY", c.given)
		if got := runtimeDir(); got != c.want {
			t.Errorf("RUNTIME_DIRECTORY=%q: staged in %q, want %q", c.given, got, c.want)
		}
	}
}

// The deploy rule admits any arguments after `deploy`, so the count is held
// here, before anything runs as root.
func TestADeployTakesExactlyOneImage(t *testing.T) {
	for _, args := range [][]string{{"deploy"}, {"deploy", "a", "--stage", "/tmp"}} {
		var stderr strings.Builder
		if code := run(context.Background(), args, func(string) string { return "" }, 0, strings.NewReader(""), io.Discard, &stderr); code != 2 {
			t.Errorf("%q exited %d, want 2", args, code)
		}
	}
}

// A unit request is answered in JSON before the root check every other
// command makes, so its caller reads a refusal the same way from any account.
func TestAUnitRequestIsAnsweredInJSONAsAnyAccount(t *testing.T) {
	var stdout strings.Builder
	code := run(context.Background(), []string{"unit", "--serve"}, func(string) string { return "" }, 1000, strings.NewReader("stop example.service\n"), &stdout, io.Discard)
	if code != 0 || !strings.Contains(stdout.String(), `"outcome":"refused"`) {
		t.Errorf("exited %d with %q", code, stdout.String())
	}
}

// An administrator hands the edge receiver over from their own account, so
// edge-receiver is parsed before the root check, and takes no arguments.
func TestEdgeReceiverIsParsedAsAnyAccount(t *testing.T) {
	var stderr strings.Builder
	code := run(context.Background(), []string{"edge-receiver", "extra"}, func(string) string { return "" }, 1000, strings.NewReader(""), io.Discard, &stderr)
	if code != 2 || strings.Contains(stderr.String(), "must run as root") {
		t.Errorf("exited %d with %q", code, stderr.String())
	}
}

// CI's lint:units verifies from this list, as a runner's own account: each
// unit install verifies, by its path, and none of their drop-ins.
func TestVerifiedListsTheUnitsInstallVerifiesAsAnyAccount(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"example.service", "example.service.d/10-example.conf"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	var stdout strings.Builder
	code := run(context.Background(), []string{"verified", dir}, func(string) string { return "" }, 1000, strings.NewReader(""), &stdout, io.Discard)
	if want := filepath.Join(dir, "example.service") + "\n"; code != 0 || stdout.String() != want {
		t.Errorf("exited %d with %q, want %q", code, stdout.String(), want)
	}
}

type ended struct {
	error
	outcome host.DeployOutcome
}

func (e ended) Outcome() host.DeployOutcome { return e.outcome }

// The Deploy workflow tells what production runs by this status alone, so a
// deploy's error exits with the status its outcome declares however it is
// wrapped, and every other failure exits 1.
func TestADeployExitsWithItsOutcomesStatus(t *testing.T) {
	for _, outcome := range []host.DeployOutcome{host.DeployLiveCheckFailed, host.DeployRollbackFailed} {
		err := fmt.Errorf("deploy: %w", ended{errors.New("a check failed"), outcome})
		if got := exitStatus(err); got != outcome.Exit || got == 1 || got == 2 {
			t.Errorf("%s exited %d, want a status of its own", outcome.Name, got)
		}
	}
	if got := exitStatus(errors.New("the new image did not become healthy")); got != 1 {
		t.Errorf("a release that did not go live exited %d, want 1", got)
	}
}

// A run that fails tells systemd why, which the unit's reading carries until
// the unit next starts.
func TestAFailedRunTellsSystemdWhy(t *testing.T) {
	// A socket's path has a short limit (104 bytes on macOS), which a test's
	// own temporary directory can pass.
	dir, err := os.MkdirTemp("/tmp", "hq-notify")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "notify")
	listener, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	getenv := func(name string) string { return map[string]string{"NOTIFY_SOCKET": path}[name] }
	if code := run(context.Background(), []string{"release"}, getenv, 0, strings.NewReader(""), io.Discard, io.Discard); code == 0 {
		t.Fatal("a release without an image succeeded")
	}
	// The datagram is queued before run returns; the deadline only keeps a run that sent none from hanging.
	if err := listener.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	buffer := make([]byte, 512)
	n, _, err := listener.ReadFromUnix(buffer)
	if err != nil {
		t.Fatal(err)
	}
	if said := string(buffer[:n]); said != "STATUS="+usage {
		t.Errorf("told systemd %q", said)
	}
}
