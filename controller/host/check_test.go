package host

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// checkHost is a deploy host as the previous release left it, answering a
// check the way systemd and sudo would.
type checkHost struct {
	t       *testing.T
	checker *Checker
	out     bytes.Buffer
	runners []string
	account string
	rules   string
	// verify is what `sudo -n hq-host verify` prints and whether it passed;
	// password makes sudo ask for one instead.
	verify    string
	verifyErr bool
	password  bool
	// drift is the drift unit's last run, as systemctl shows it; empty when
	// it has none.
	drift string
	free  uint64
}

const goodRules = "Matching Defaults entries:\n    env_reset, secret-marker-example\nUser deployer may run the following commands:\n    (root) NOPASSWD: %s/scripts/deploy-image.sh\n"

func newCheckHost(t *testing.T) *checkHost {
	t.Helper()
	root := t.TempDir()
	h := &checkHost{t: t, runners: []string{"actions.runner.example.service"}, account: "deployer",
		verify: "The host is its release.", free: 1 << 40}
	lib := filepath.Join(root, "lib")
	h.rules = fmt.Sprintf(goodRules, lib)
	h.checker = &Checker{
		Lib: lib, Systemd: filepath.Join(root, "systemd"), RootUID: uint32(os.Getuid()),
		Release: filepath.Join(root, "release"), Main: filepath.Join(root, "main"),
		FreeBytes: DeployFreeBytes, Free: func(string) (uint64, error) { return h.free, nil },
		Command: h.command, Out: &h.out,
	}
	for _, path := range []string{"scripts/deploy-image.sh", "scripts/upgrade-container.sh", "deploy/bin/hq-host", ".source"} {
		h.file(filepath.Join(lib, path), 0o755)
	}
	for _, unit := range []string{"severino-hq-web.service", "severino-hq-backup.timer"} {
		h.file(filepath.Join(h.checker.Main, unit), 0o644)
		h.file(filepath.Join(h.checker.Release, unit), 0o644)
		h.file(filepath.Join(h.checker.Systemd, unit), 0o644)
	}
	// The release adds a unit main does not ship: the release installs it.
	h.file(filepath.Join(h.checker.Release, "severino-hq-example.timer"), 0o644)
	return h
}

func (h *checkHost) file(path string, mode os.FileMode) {
	h.t.Helper()
	os.MkdirAll(filepath.Dir(path), 0o755)
	if err := os.WriteFile(path, nil, mode); err != nil {
		h.t.Fatal(err)
	}
	os.Chmod(path, mode)
}

func (h *checkHost) command(ctx context.Context, name string, args ...string) *exec.Cmd {
	call := strings.Join(append([]string{name}, args...), " ")
	answer := func(out string, code int) *exec.Cmd {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, fmt.Sprint(code))
	}
	switch {
	case name == "sudo" && args[0] != "-n":
		h.t.Errorf("a sudo that could prompt: %s", call)
	case strings.HasPrefix(call, "systemctl list-units"):
		var lines []string
		for _, unit := range h.runners {
			lines = append(lines, unit+" loaded active running Example runner")
		}
		return answer(strings.Join(lines, "\n")+"\n", 0)
	case strings.HasPrefix(call, "systemctl show -p User"):
		return answer(h.account+"\n", 0)
	case strings.HasPrefix(call, "systemctl show --timestamp=unix"):
		return answer("Id="+DriftUnit+"\n"+h.drift, 0)
	case call == "sudo -n -l -U "+h.account:
		return answer(h.rules, 0)
	case strings.HasSuffix(call, "/deploy/bin/hq-host verify"):
		switch {
		case h.password:
			return answer("sudo: a password is required\n", 1)
		case h.verifyErr:
			return answer(h.verify, 1)
		}
		return answer(h.verify, 0)
	}
	h.t.Errorf("ran %s", call)
	return answer("", 127)
}

func (h *checkHost) check(want int) string {
	h.t.Helper()
	h.out.Reset()
	h.checker.failed, h.checker.unverified = false, false
	if got := h.checker.Check(context.Background()); got != want {
		h.t.Fatalf("exit %d, want %d:\n%s", got, want, h.out.String())
	}
	return h.out.String()
}

func (h *checkHost) says(out, want string) {
	h.t.Helper()
	if !strings.Contains(out, want) {
		h.t.Errorf("output lacks %q:\n%s", want, out)
	}
}

// ranAt is a drift unit run that started at started and passed or not.
func ranAt(started time.Time, passed bool) string {
	result, status := "success", "0"
	if !passed {
		result, status = "exit-code", "1"
	}
	return fmt.Sprintf("ActiveState=inactive\nResult=%s\nExecMainStatus=%s\nExecMainStartTimestamp=@%d\nExecMainExitTimestamp=@%d\n",
		result, status, started.Unix(), started.Add(time.Minute).Unix())
}

func TestACurrentHostIsReady(t *testing.T) {
	h := newCheckHost(t)

	out := h.check(CheckReady)

	h.says(out, "ok      the host is the release it runs")
	h.says(out, "ok      every established unit is installed")
	for _, program := range []string{"scripts/deploy-image.sh", "scripts/upgrade-container.sh", "deploy/bin/hq-host"} {
		h.says(out, filepath.Join(h.checker.Lib, program)+" is root's alone")
	}
	if strings.Contains(out, "secret-marker-example") {
		t.Error("the check printed the sudo rules")
	}
}

func TestEachHostFaultFailsAndSaysWhatItIs(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(*checkHost)
		says    string
	}{
		"a runner with unrestricted sudo": {func(h *checkHost) { h.rules = "    (ALL : ALL) ALL\n" }, "may run any command"},
		"a runner without the rule":       {func(h *checkHost) { h.rules = "    (root) NOPASSWD: /usr/bin/true\n" }, "has no NOPASSWD sudo rule"},
		"a runner that runs as root":      {func(h *checkHost) { h.account = "root" }, "the Actions runner runs as 'root'"},
		"two runners":                     {func(h *checkHost) { h.runners = append(h.runners, "actions.runner.other.service") }, "expected one Actions runner unit"},
		"a full disk":                     {func(h *checkHost) { h.free = 1 << 20 }, "the deploy needs 512 MiB"},
		"an established unit missing": {func(h *checkHost) {
			os.Remove(filepath.Join(h.checker.Systemd, "severino-hq-backup.timer"))
		}, "severino-hq-backup.timer is not installed"},
		"a host that is not its release": {func(h *checkHost) {
			h.verify, h.verifyErr = "drift: scripts/deploy-image.sh differs from the image's\n", true
		}, "FAIL    scripts/deploy-image.sh differs from the image's"},
		"a sudo target others can write": {func(h *checkHost) {
			os.Chmod(filepath.Join(h.checker.Lib, "scripts", "upgrade-container.sh"), 0o775)
		}, "upgrade-container.sh is writable by its group or others"},
		"a directory above a sudo target others can write": {func(h *checkHost) {
			os.Chmod(filepath.Join(h.checker.Lib, "deploy", "bin"), 0o777)
		}, "bin is writable by its group or others; a sudo rule runs " + "<lib>/deploy/bin/hq-host"},
		"a sudo target that is a link": {func(h *checkHost) {
			path := filepath.Join(h.checker.Lib, "scripts", "deploy-image.sh")
			os.Remove(path)
			os.Symlink("/bin/sh", path)
		}, "deploy-image.sh is a symlink"},
	} {
		t.Run(name, func(t *testing.T) {
			h := newCheckHost(t)
			c.arrange(h)

			h.says(h.check(CheckFailed), strings.ReplaceAll(c.says, "<lib>", h.checker.Lib))
		})
	}
}

// Where sudo needs a password, the drift unit's run since the release was
// installed is the evidence; without one there is no verdict on the host.
func TestWithoutSudoTheDriftUnitIsTheEvidence(t *testing.T) {
	h := newCheckHost(t)
	h.password = true
	installed := time.Now().Add(-time.Hour).Truncate(time.Second)
	os.Chtimes(filepath.Join(h.checker.Lib, ".source"), installed, installed)

	h.says(h.check(CheckUnverified), "UNKNOWN hq-host verify needs a password here")

	h.drift = ranAt(installed.Add(time.Minute), true)
	h.says(h.check(CheckReady), "ok      the host is the release it runs ("+DriftUnit)

	h.drift = ranAt(installed.Add(-time.Minute), true)
	h.check(CheckUnverified)

	h.drift = ranAt(installed.Add(time.Minute), false)
	h.says(h.check(CheckFailed), DriftUnit+" failed since this release was installed")

	h.drift = ranAt(installed.Add(time.Minute), true) + "ActiveState=activating\n"
	h.check(CheckUnverified)

	h.drift = ranAt(installed.Add(time.Minute), true)
	os.Remove(filepath.Join(h.checker.Lib, ".source"))
	h.says(h.check(CheckUnverified), "when this release was installed cannot be read")
}

func TestATreeWithoutTheShimsChecksWhatItHas(t *testing.T) {
	h := newCheckHost(t)
	os.Remove(filepath.Join(h.checker.Lib, "scripts", "upgrade-container.sh"))

	out := h.check(CheckReady)

	if strings.Contains(out, "upgrade-container.sh") {
		t.Errorf("a program the tree does not have was checked:\n%s", out)
	}
}
