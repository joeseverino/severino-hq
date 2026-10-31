package host

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// imageID is the fake image's ID, which docker reports for every image.
const imageID = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

type fakeHost struct {
	t    *testing.T
	root string
	// image is /app in the image being deployed.
	image   string
	mu      sync.Mutex
	log     []string
	fail    []string
	active  []string
	running string
	out     bytes.Buffer
	machine *Machine
	// boot is what starting the web unit does to the host, as an image's
	// entrypoint would.
	boot func()
	// files is what an image holds outside /app, by image then path. An image
	// it names has only those; one it does not holds any file asked for.
	files   map[string]map[string]string
	created string
	// runners, runnerUser and sudoRules are the Actions runner units, the
	// account the one runs as, and what root's `sudo -l -U` lists for it.
	runners    []string
	runnerUser string
	sudoRules  string
}

// sudoListing is sudo-rs's listing of an account's rules, as root sees it.
const sudoListing = "Matching Defaults entries for %[1]s on example:\n    env_reset, secret-marker-example\n\nUser %[1]s may run the following commands on example:\n%[2]s"

// program is what each command an install or a deploy runs does here: it is
// recorded, and answers as the host would.
func (f *fakeHost) program(ctx context.Context, name string, args ...string) *exec.Cmd {
	call := strings.Join(append([]string{name}, args...), " ")
	f.mu.Lock()
	f.log = append(f.log, call)
	f.mu.Unlock()
	if f.boot != nil && call == "systemctl restart "+WebUnit {
		f.boot()
	}
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
	case strings.HasPrefix(call, "systemctl list-units"):
		var lines []string
		for _, unit := range f.runners {
			lines = append(lines, unit+" loaded active running Example runner")
		}
		return answer(strings.Join(lines, "\n")+"\n", 0)
	case strings.HasPrefix(call, "systemctl show -p User"):
		return answer(f.runnerUser+"\n", 0)
	case call == "sudo -n -l -U "+f.runnerUser:
		return answer(f.sudoRules, 0)
	case strings.HasPrefix(call, "docker inspect --format {{.Config.Image}}"):
		return answer(f.running+"\n", 0)
	case strings.HasPrefix(call, "docker inspect --format {{.Image}}"):
		return answer(imageID+"\n", 0)
	case strings.HasPrefix(call, "docker create"):
		f.created = args[len(args)-2]
		return answer("c0ffee\n", 0)
	case strings.HasPrefix(call, "docker volume inspect"):
		return answer(filepath.Join(f.root, "volumes", args[len(args)-1])+"\n", 0)
	case name == "sqlite3":
		return exec.CommandContext(ctx, "sqlite3", args...)
	case name == "age":
		// Its output says it went through age, and for whom, then the input.
		return exec.CommandContext(ctx, "/bin/sh", append([]string{"-c", `echo "age $*"; cat`, "sh"}, args...)...)
	case strings.HasPrefix(call, "docker image inspect"):
		return answer(imageID+"\n", 0)
	case strings.HasPrefix(call, "docker cp"):
		// A path under /app is the fake image's; anything else is one file.
		_, source, _ := strings.Cut(args[len(args)-2], ":")
		if item, ok := strings.CutPrefix(source, "/app/"); ok {
			return exec.CommandContext(ctx, "cp", "-R", filepath.Join(f.image, item), args[len(args)-1])
		}
		if held, ok := f.files[f.created]; ok {
			body, ok := held[source]
			if !ok {
				return answer("", 1)
			}
			if err := os.WriteFile(args[len(args)-1], []byte(body), 0o600); err != nil {
				f.t.Fatal(err)
			}
			return answer("", 0)
		}
		if err := os.WriteFile(args[len(args)-1], []byte("#!/bin/sh\n"), 0o700); err != nil {
			f.t.Fatal(err)
		}
	case name == "cp":
		return exec.CommandContext(ctx, "cp", args...)
	}
	return answer("", 0)
}

func newFakeHost(t *testing.T) *fakeHost {
	t.Helper()
	root := t.TempDir()
	f := &fakeHost{t: t, root: root, image: filepath.Join(root, "image", "app")}
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
	f.machine.Own = func(string) error { return nil }
	f.machine.Verifier = func(context.Context) error { return nil }
	f.machine.Sudoers = filepath.Join(root, "sudoers.d")
	f.runners, f.runnerUser = []string{"actions.runner.example.service"}, "deployer"
	f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) NOPASSWD: "+filepath.Join(lib, "deploy", "bin", "hq-host")+" deploy *\n")
	for _, dir := range []string{"systemd", "sbin", "run", "run/severino-hq", "run/severino-hq-secrets", "sudoers.d", "image/app"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	f.ship("severino-hq-web.service", "[Service]\nExecStart=/bin/true\n\n[Install]\nWantedBy=multi-user.target\n")
	f.ship("severino-hq-secrets.service", "[Service]\nExecStart=/bin/true\n")
	f.ship("severino-hq-secrets.service.d/10-root-owned-exec.conf", "[Service]\n")
	f.ship("severino-hq-secrets.timer", "[Timer]\nOnBootSec=30s\n")
	f.ship("severino-hq-controller.service", "[Service]\nExecStart=/bin/true\n")
	f.ship("severino-hq-controller.path", "[Path]\nPathChanged=/run/severino-hq/controller-doorbell\n")
	f.ship("severino-hq-connect.conf.example", "[Service]\n")
	if err := os.MkdirAll(filepath.Join(lib, "deploy", "tmpfiles"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(lib, "deploy", "tmpfiles", "severino-hq.conf"), []byte("d /run/severino-hq 0755 10001 10001 -\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// The image ships the same tree, its programs and a marker of its own.
	if err := exec.Command("cp", "-R", filepath.Join(lib, "deploy"), filepath.Join(f.image, "deploy")).Run(); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{
		"deploy/bin/hq-host":        "#!/bin/sh\n",
		"hq/config/synced-release":  "",
		"hq/config/controller.json": "{}\n",
	} {
		path := filepath.Join(f.image, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *fakeHost) ship(name, body string) {
	path := filepath.Join(f.machine.UnitDir(), name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		f.t.Fatal(err)
	}
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
		if err := os.WriteFile(filepath.Join(f.machine.Systemd, name), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}

	retired, err := Retired(shipped, f.machine.Systemd)

	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(retired, " ") != "severino-hq-old.timer" {
		t.Errorf("retired %v", retired)
	}
}
