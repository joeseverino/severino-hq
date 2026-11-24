package host

import (
	"bytes"
	"context"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// imageID is the fake image's ID, which docker reports for every image.
const imageID = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

// running is the image a host runs before a deploy.
const running = "ghcr.io/joeseverino/severino-hq/composition@" + digest

type fakeHost struct {
	t    *testing.T
	root string
	// image is /app in the image being deployed.
	image string
	mu    sync.Mutex
	log   []string
	fail  []string
	// failSays is what each failing command writes to stderr.
	failSays string
	active   []string
	running  string
	out      bytes.Buffer
	machine  *Machine
	// boot is what starting the web unit does to the host, as an image's
	// entrypoint would.
	boot func()
	// on is what each command, by a prefix of its line, does to the host as it
	// runs.
	on map[string]func()
	// files is what an image holds outside /app, by image then path. An image
	// it names has only those; one it does not holds any file asked for.
	files   map[string]map[string]string
	created string
	// runners, runnerUser and sudoRules are the Actions runner units, the
	// account the one runs as, and what root's `sudo -l -U` lists for it.
	runners    []string
	runnerUser string
	sudoRules  string
	// setupReport is what a check of the host's setup prints
	// (ansible.posix.json).
	setupReport string
}

// sudoListing is sudo-rs's listing of an account's rules, as root sees it.
const sudoListing = "Matching Defaults entries for %[1]s on example:\n    env_reset, secret-marker-example\n\nUser %[1]s may run the following commands on example:\n%[2]s"

// program is what each command an install or a deploy runs does here: it is
// recorded, and answers as the host would.
func (f *fakeHost) program(ctx context.Context, name string, args ...string) *exec.Cmd {
	args = f.ours(name, args)
	call := strings.Join(append([]string{name}, args...), " ")
	f.mu.Lock()
	f.log = append(f.log, call)
	f.mu.Unlock()
	if f.boot != nil && call == "systemctl restart "+hostcore.WebUnit {
		f.boot()
	}
	for prefix, do := range f.on {
		if strings.HasPrefix(call, prefix) {
			do()
		}
	}
	answer := func(out string, code int) *exec.Cmd {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
	}
	for _, prefix := range f.fail {
		if strings.HasPrefix(call, prefix) {
			return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1" >&2; exit 1`, "sh", f.failSays)
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
	case strings.HasPrefix(call, "docker run") && strings.Contains(call, "ANSIBLE_STDOUT_CALLBACK=ansible.posix.json"):
		return answer(f.setupReport, 0)
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
	f := &fakeHost{t: t, root: root, image: filepath.Join(root, "image", "app"), setupReport: `{"plays": [], "stats": {}}`}
	lib := filepath.Join(root, "lib")
	f.machine = &Machine{
		Lib:        lib,
		Systemd:    filepath.Join(root, "systemd"),
		Tmpfiles:   filepath.Join(root, "tmpfiles.d"),
		LogDir:     filepath.Join(root, "log"),
		State:      filepath.Join(root, "state"),
		Seal:       filepath.Join(root, "seal"),
		SealPublic: filepath.Join(root, "state", "seal", "public.key"),
		Runtime:    filepath.Join(root, "run"),
		Runs:       filepath.Join(root, "run", "severino-hq-runs"),
		Out:        &f.out,
	}
	f.machine.Command = f.program
	t.Cleanup(func() {
		if left, _ := os.ReadDir(f.machine.Runs); len(left) != 0 {
			t.Errorf("runs outlived the work they registered for: %v", left)
		}
	})
	f.machine.Own = func(string) error { return nil }
	f.machine.Owner = func(*os.Root, string) error { return nil }
	f.machine.Verifier = func(context.Context) error { return nil }
	f.machine.Sudoers = filepath.Join(root, "sudoers.d")
	f.machine.HostConfig = filepath.Join(root, "host.json")
	if err := os.WriteFile(f.machine.HostConfig, []byte(hostJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	f.runners, f.runnerUser = []string{"actions.runner.example.service"}, "deployer"
	f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) NOPASSWD: "+filepath.Join(lib, "deploy", "bin", "hq-host")+" deploy *\n")
	for _, dir := range []string{"systemd", "run", "run/severino-hq", "run/severino-hq-secrets", "sudoers.d", "image/app"} {
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
	if err := exec.CommandContext(t.Context(), "cp", "-R", filepath.Join(lib, "deploy"), filepath.Join(f.image, "deploy")).Run(); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{
		"deploy/bin/hq-host":        "#!/bin/sh\n",
		SetupPlaybook:               "- hosts: all\n",
		AutomationImage:             runnerImage + "\n",
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

// installed is which of names are in the systemd directory now.
// isSetup is whether call runs the host's setup playbook.
func isSetup(call string) bool {
	return strings.HasPrefix(call, "docker run") && strings.Contains(call, " --name severino-hq-setup-")
}

// setups is each run of the setup playbook that applies, in order.
func (f *fakeHost) setups() []string {
	var applies []string
	for _, call := range f.calls() {
		if isSetup(call) && strings.Contains(call, " --privileged ") {
			applies = append(applies, call)
		}
	}
	return applies
}

// ours is a docker run or create's arguments without the run label, which
// must name a run registered as the container is made: every container a run
// of hq-host makes is HQ's own to a pass that finds it (RunsDir).
func (f *fakeHost) ours(name string, args []string) []string {
	if name != "docker" || len(args) == 0 || (args[0] != "run" && args[0] != "create") {
		return args
	}
	for i, arg := range args {
		if nonce, ok := strings.CutPrefix(arg, "--label="+RunLabel+"="); ok {
			if _, err := os.Stat(filepath.Join(f.machine.Runs, nonce)); err != nil {
				f.t.Errorf("a container is labeled with no registered run: %v", args)
			}
			return slices.Delete(slices.Clone(args), i, i+1)
		}
	}
	f.t.Errorf("a container is not labeled as a run of HQ's: %v", args)
	return args
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
