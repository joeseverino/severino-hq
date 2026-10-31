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
	"testing"
	"time"
)

// checkHost is a deploy host as the previous release left it, answering a
// check the way systemd and sudo would.
type checkHost struct {
	t       *testing.T
	checker *Checker
	out     bytes.Buffer
	// verify is what `sudo -n hq-host verify` prints and whether it passed;
	// password makes sudo ask for one instead.
	verify    string
	verifyErr bool
	password  bool
	// drift is the drift unit's last run, as systemctl shows it; empty when
	// it has none.
	drift string
	free  uint64
	// tables and table are what root's nft lists; nftDenied makes sudo ask
	// for a password instead.
	tables, table string
	nftDenied     bool
}

func newCheckHost(t *testing.T) *checkHost {
	t.Helper()
	root := t.TempDir()
	h := &checkHost{t: t, verify: "The host is its release.", free: 1 << 40,
		tables: "table inet host_filter\ntable ip filter\ntable inet " + EgressTable + "\n",
		// nft lists with a tab per level and wraps a long set.
		table: strings.Replace(EgressRules(), "100.64.0.0/10, ", "100.64.0.0/10,\n\t\t\t\t     ", 1)}
	lib := filepath.Join(root, "lib")
	h.checker = &Checker{
		Lib: lib, Systemd: filepath.Join(root, "systemd"), RootUID: uint32(os.Getuid()),
		Release: filepath.Join(root, "release"), Main: filepath.Join(root, "main"),
		FreeBytes: DeployFreeBytes, Free: func(string) (uint64, error) { return h.free, nil },
		Command: h.command, Out: &h.out,
	}
	for _, path := range []string{"deploy/bin/hq-host", ".source"} {
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
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		h.t.Fatal(err)
	}
	if err := os.WriteFile(path, nil, mode); err != nil {
		h.t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		h.t.Fatal(err)
	}
}

func (h *checkHost) command(ctx context.Context, name string, args ...string) *exec.Cmd {
	call := strings.Join(append([]string{name}, args...), " ")
	answer := func(out string, code int) *exec.Cmd {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
	}
	switch {
	case name == "sudo" && args[0] != "-n":
		h.t.Errorf("a sudo that could prompt: %s", call)
	case strings.HasPrefix(call, "systemctl show --timestamp=unix"):
		return answer("Id="+DriftUnit+"\n"+h.drift, 0)
	case strings.HasPrefix(call, "sudo -n nft list") && h.nftDenied:
		return answer("sudo: a password is required\n", 1)
	case call == "sudo -n nft list tables":
		return answer(h.tables, 0)
	case call == "sudo -n nft list table inet "+EgressTable:
		return answer(h.table, 0)
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

	h.says(out, "ok      the host is the release it runs, the runner's sudo rules with it")
	h.says(out, "ok      every established unit is installed")
	h.says(out, filepath.Join(h.checker.Lib, "deploy", "bin", "hq-host")+" is root's alone")
}

func TestEachHostFaultFailsAndSaysWhatItIs(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(*checkHost)
		says    string
	}{
		"a full disk": {func(h *checkHost) { h.free = 1 << 20 }, "the deploy needs 512 MiB"},
		"an established unit missing": {func(h *checkHost) {
			os.Remove(filepath.Join(h.checker.Systemd, "severino-hq-backup.timer"))
		}, "severino-hq-backup.timer is not installed"},
		"a host that is not its release": {func(h *checkHost) {
			h.verify, h.verifyErr = "drift: deploy/bin/hq-host differs from the image's\n", true
		}, "FAIL    deploy/bin/hq-host differs from the image's"},
		"a runner sudo rule verify finds": {func(h *checkHost) {
			h.verify, h.verifyErr = "drift: deployer may run any command through sudo\n", true
		}, "FAIL    deployer may run any command through sudo"},
		"a sudo target others can write": {func(h *checkHost) {
			if err := os.Chmod(filepath.Join(h.checker.Lib, "deploy", "bin", "hq-host"), 0o775); err != nil {
				h.t.Fatal(err)
			}
		}, "hq-host is writable by its group or others"},
		"a directory above a sudo target others can write": {func(h *checkHost) {
			if err := os.Chmod(filepath.Join(h.checker.Lib, "deploy", "bin"), 0o777); err != nil {
				h.t.Fatal(err)
			}
		}, "bin is writable by its group or others; a sudo rule runs " + "<lib>/deploy/bin/hq-host"},
		"a sudo target that is a link": {func(h *checkHost) {
			path := filepath.Join(h.checker.Lib, "deploy", "bin", "hq-host")
			os.Remove(path)
			if err := os.Symlink("/bin/sh", path); err != nil {
				h.t.Fatal(err)
			}
		}, "hq-host is a symlink"},
		"a sudo target that is missing": {func(h *checkHost) {
			os.Remove(filepath.Join(h.checker.Lib, "deploy", "bin", "hq-host"))
		}, "hq-host cannot be read"},
	} {
		t.Run(name, func(t *testing.T) {
			h := newCheckHost(t)
			c.arrange(h)

			h.says(h.check(CheckFailed), strings.ReplaceAll(c.says, "<lib>", h.checker.Lib))
		})
	}
}

// The egress table is checked where root's nft can be read: the rendered one
// passes, another fails, an absent one waits for the next run, and an
// unreadable one is said and leaves the verdict to the rest.
func TestTheRenovateEgressTableIsTheRenderedOne(t *testing.T) {
	h := newCheckHost(t)
	h.says(h.check(CheckReady), "ok      the Renovate egress table is the one hq-host renders")

	h.table = strings.Replace(EgressRules(), "\t\tiifname \"hq-renovate0\" drop\n", "", 1)
	h.says(h.check(CheckFailed), "FAIL    the Renovate egress table "+EgressTable+" is not the one hq-host renders")

	h.tables = "table inet host_filter\n"
	h.says(h.check(CheckReady), "ok      the Renovate egress table is not loaded; each run loads it before it starts")

	h.nftDenied = true
	out := h.check(CheckReady)
	h.says(out, "skip    the Renovate egress table cannot be read without root here")
	if strings.Contains(out, "drop") {
		t.Error("the check printed the table")
	}
}

// Where sudo needs a password, the drift unit's run since the release was
// installed is the evidence, for the runner's sudo rules as for the tree, and
// the check lists no account's rules itself (the fake refuses any other
// call); without such a run there is no verdict on the host.
func TestWithoutSudoTheDriftUnitIsTheEvidence(t *testing.T) {
	h := newCheckHost(t)
	h.password = true
	installed := time.Now().Add(-time.Hour).Truncate(time.Second)
	if err := os.Chtimes(filepath.Join(h.checker.Lib, ".source"), installed, installed); err != nil {
		t.Fatal(err)
	}

	h.says(h.check(CheckUnverified), "UNKNOWN hq-host verify needs a password here")

	h.drift = ranAt(installed.Add(time.Minute), true)
	h.says(h.check(CheckReady), "ok      the host is the release it runs, the runner's sudo rules with it ("+DriftUnit)

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
