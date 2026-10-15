package host

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
)

type fakeHost struct {
	t       *testing.T
	root    string
	mu      sync.Mutex
	log     []string
	fail    []string
	active  []string
	running string
	out     bytes.Buffer
	machine *Machine
}

// program is what each command an install or a deploy runs does here: it is
// recorded, and answers as the host would.
func (f *fakeHost) program(ctx context.Context, name string, args ...string) *exec.Cmd {
	call := strings.Join(append([]string{name}, args...), " ")
	f.mu.Lock()
	f.log = append(f.log, call)
	f.mu.Unlock()
	answer := func(out string, code int) *exec.Cmd {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
	}
	for _, prefix := range f.fail {
		if strings.HasPrefix(call, prefix) {
			return answer("", 1)
		}
	}
	switch {
	case strings.HasPrefix(call, "systemctl is-active"):
		for _, unit := range f.active {
			if strings.HasSuffix(call, " "+unit) {
				return answer("", 0)
			}
		}
		return answer("", 3)
	case strings.HasPrefix(call, "docker inspect --format {{.Config.Image}}"):
		return answer(f.running+"\n", 0)
	case strings.HasPrefix(call, "docker create"):
		return answer("c0ffee\n", 0)
	case strings.HasPrefix(call, "docker cp"):
		os.WriteFile(args[len(args)-1], []byte("#!/bin/sh\n"), 0o700)
	case name == "cp":
		return exec.CommandContext(ctx, "cp", args...)
	case name == "sh":
		// The sync replaces the tree with the release's.
		os.Remove(filepath.Join(f.machine.Lib, "previous-release"))
		os.WriteFile(filepath.Join(f.machine.Lib, "synced-release"), nil, 0o644)
	}
	return answer("", 0)
}

func newFakeHost(t *testing.T) *fakeHost {
	t.Helper()
	root := t.TempDir()
	f := &fakeHost{t: t, root: root}
	lib := filepath.Join(root, "lib")
	f.machine = &Machine{
		Lib:      lib,
		Systemd:  filepath.Join(root, "systemd"),
		Tmpfiles: filepath.Join(root, "tmpfiles.d"),
		LogDir:   filepath.Join(root, "log"),
		State:    filepath.Join(root, "state"),
		Sbin:     filepath.Join(root, "sbin"),
		Runtime:  filepath.Join(root, "run"),
		Out:      &f.out,
	}
	f.machine.Command = f.program
	for _, dir := range []string{"systemd", "sbin", "run", "run/severino-hq", "run/severino-hq-secrets"} {
		os.MkdirAll(filepath.Join(root, dir), 0o755)
	}
	f.ship("severino-hq-web.service", "[Service]\nExecStart=/bin/true\n\n[Install]\nWantedBy=multi-user.target\n")
	f.ship("severino-hq-secrets.service", "[Service]\nExecStart=/bin/true\n")
	f.ship("severino-hq-secrets.service.d/10-root-owned-exec.conf", "[Service]\n")
	f.ship("severino-hq-secrets.timer", "[Timer]\nOnBootSec=30s\n")
	f.ship("severino-hq-controller.service", "[Service]\nExecStart=/bin/true\n")
	f.ship("severino-hq-controller.path", "[Path]\nPathChanged=/run/severino-hq/controller-doorbell\n")
	f.ship("severino-hq-connect.conf.example", "[Service]\n")
	os.MkdirAll(filepath.Join(lib, "deploy", "tmpfiles"), 0o755)
	os.WriteFile(filepath.Join(lib, "deploy", "tmpfiles", "severino-hq.conf"), []byte("d /run/severino-hq 0755 10001 10001 -\n"), 0o644)
	return f
}

func (f *fakeHost) ship(name, body string) {
	path := filepath.Join(f.machine.UnitDir(), name)
	os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fakeHost) calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.log...)
}

func (f *fakeHost) called(prefix string) bool {
	return f.index(prefix) >= 0
}

func (f *fakeHost) index(prefix string) int {
	for i, call := range f.calls() {
		if strings.HasPrefix(call, prefix) {
			return i
		}
	}
	return -1
}

func (f *fakeHost) before(t *testing.T, first, second string) {
	t.Helper()
	a, b := f.index(first), f.index(second)
	if a < 0 || b < 0 || a > b {
		t.Errorf("%q does not come before %q:\n%s", first, second, strings.Join(f.calls(), "\n"))
	}
}

func TestShippedIsWhatSystemdReads(t *testing.T) {
	f := newFakeHost(t)

	shipped, err := Shipped(f.machine.UnitDir())

	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"severino-hq-controller.path",
		"severino-hq-controller.service",
		"severino-hq-secrets.service",
		"severino-hq-secrets.service.d/10-root-owned-exec.conf",
		"severino-hq-secrets.timer",
		"severino-hq-web.service",
	}
	if strings.Join(shipped, " ") != strings.Join(want, " ") {
		t.Errorf("shipped %v", shipped)
	}
	if _, err := Shipped(filepath.Join(f.root, "absent")); err == nil {
		t.Error("a missing directory read as nothing shipped")
	}
}

func TestTimersPathsAndBootServicesAreEnabled(t *testing.T) {
	f := newFakeHost(t)
	shipped, _ := Shipped(f.machine.UnitDir())

	enabled, err := Enabled(f.machine.UnitDir(), shipped)

	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(enabled, " ") != "severino-hq-controller.path severino-hq-secrets.timer severino-hq-web.service" {
		t.Errorf("enabled %v", enabled)
	}
}

func TestADroppedUnitIsRetiredAndTheHostsOwnAreNot(t *testing.T) {
	f := newFakeHost(t)
	shipped, _ := Shipped(f.machine.UnitDir())
	for _, name := range []string{"severino-hq-old.timer", "severino-hq-web.service", "other.service", "severino-hq-notes.txt"} {
		os.WriteFile(filepath.Join(f.machine.Systemd, name), nil, 0o644)
	}

	retired, err := Retired(shipped, f.machine.Systemd)

	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(retired, " ") != "severino-hq-old.timer" {
		t.Errorf("retired %v", retired)
	}
}
