package host

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// synced is a host whose tree and units are exactly the image's.
func synced(t *testing.T) *fakeHost {
	t.Helper()
	f := newFakeHost(t)
	f.running = running
	if err := f.machine.Sync(context.Background(), next); err != nil {
		t.Fatal(err)
	}
	shipped, _ := Shipped(f.machine.UnitDir())
	for _, name := range shipped {
		if err := InstallUnit(f.machine.UnitDir(), f.machine.Systemd, name); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *fakeHost) verify(t *testing.T) []string {
	t.Helper()
	drift, err := f.machine.Verify(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return drift
}

func TestAHostThatIsItsReleaseHasNoDrift(t *testing.T) {
	f := synced(t)

	if drift := f.verify(t); len(drift) != 0 {
		t.Errorf("drift on a synced host: %v", drift)
	}
}

func TestEveryKindOfDriftIsNamed(t *testing.T) {
	f := synced(t)
	if err := os.WriteFile(filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host"), []byte("#!/bin/sh\necho edited\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.machine.Lib, "deploy", "bin", "planted"), nil, 0o755); err != nil {
		t.Fatal(err)
	}
	os.Remove(filepath.Join(f.machine.Lib, "hq", "config", "controller.json"))
	if err := os.WriteFile(filepath.Join(f.machine.Systemd, "severino-hq-web.service"), []byte("edited\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.machine.Systemd, "severino-hq-old.timer"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.machine.Sbin, "severino-hq-sync-scripts"), nil, 0o755); err != nil {
		t.Fatal(err)
	}

	drift := strings.Join(f.verify(t), "\n")

	for _, want := range []string{
		"deploy/bin/hq-host differs from the image's",
		"deploy/bin/planted is not the image's",
		"hq/config/controller.json is missing from the tree",
		"severino-hq-web.service is not the release's",
		"severino-hq-old.timer is not shipped by the release",
		"severino-hq-sync-scripts is installed by no release",
	} {
		if !strings.Contains(drift, want) {
			t.Errorf("not reported: %s\n%s", want, drift)
		}
	}
}

func TestATreeFromAnotherImageIsDrift(t *testing.T) {
	f := synced(t)
	if err := os.WriteFile(filepath.Join(f.machine.Lib, ".source"), []byte("image sha256:other\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	drift := f.verify(t)

	if !slices.ContainsFunc(drift, func(line string) bool { return strings.Contains(line, "sha256:other") }) {
		t.Errorf("a tree from another image was not reported: %v", drift)
	}
}

func TestAHostWhereHQIsNotRunningCannotBeVerified(t *testing.T) {
	f := synced(t)
	f.fail = []string{"docker inspect"}

	if _, err := f.machine.Verify(context.Background()); err == nil {
		t.Fatal("verified against no running image")
	}
}

func TestTheRunnersRenderedRulesAreNoDrift(t *testing.T) {
	f := synced(t)
	// The upgrade rule HQ's container-upgrade plan renders is HQ's too.
	program := filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")
	f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) NOPASSWD: "+program+" deploy *, "+program+" upgrade *\n")

	if drift := f.verify(t); len(drift) != 0 {
		t.Errorf("drift on a runner with HQ's rules: %v", drift)
	}
}

func TestEachRunnerSudoFaultIsDrift(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(f *fakeHost, program string)
		says    string
	}{
		"a runner with unrestricted sudo": {func(f *fakeHost, _ string) {
			f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (ALL : ALL) ALL\n")
		}, "deployer may run any command through sudo"},
		"a runner without the rule": {func(f *fakeHost, _ string) {
			f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) NOPASSWD: /usr/bin/true\n")
		}, "deployer has no NOPASSWD sudo rule for <program> deploy *"},
		"a runner whose rule asks for a password": {func(f *fakeHost, program string) {
			f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) "+program+" deploy *\n")
		}, "deployer has no NOPASSWD sudo rule"},
		"a runner whose rule runs another subcommand": {func(f *fakeHost, program string) {
			f.sudoRules = fmt.Sprintf(sudoListing, f.runnerUser, "    (root) NOPASSWD: "+program+" deployed, "+program+" upgrade *\n")
		}, "deployer has no NOPASSWD sudo rule"},
		"a runner granted a command HQ does not render": {func(f *fakeHost, _ string) {
			f.sudoRules += "    (root) NOPASSWD: /usr/local/lib/example/retired *\n"
		}, "deployer may run /usr/local/lib/example/retired * through sudo, which HQ does not render"},
		"a runner whose HQ rule is widened": {func(f *fakeHost, program string) {
			f.sudoRules += "    (root) NOPASSWD: " + program + " *\n"
		}, "deployer may run <program> * through sudo, which HQ does not render"},
		"a listing without commands": {func(f *fakeHost, _ string) {
			f.sudoRules = "User deployer is not allowed to run sudo on example.\n"
		}, "has no list of commands"},
		"a runner whose sudo rules cannot be listed": {func(f *fakeHost, _ string) {
			f.fail = []string{"sudo -n -l"}
		}, "could not list deployer's sudo rules"},
		"a runner that runs as root": {func(f *fakeHost, _ string) { f.runnerUser = "root" }, "the Actions runner runs as 'root'"},
		"two runners": {func(f *fakeHost, _ string) {
			f.runners = append(f.runners, "actions.runner.other.service")
		}, "expected one Actions runner unit"},
		"no runner": {func(f *fakeHost, _ string) { f.runners = nil }, "expected one Actions runner unit, found: none"},
	} {
		t.Run(name, func(t *testing.T) {
			f := synced(t)
			program := filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")
			c.arrange(f, program)

			drift := strings.Join(f.verify(t), "\n")

			if want := strings.ReplaceAll(c.says, "<program>", program); !strings.Contains(drift, want) {
				t.Errorf("not reported: %s\n%s", want, drift)
			}
			if strings.Contains(drift, "secret-marker-example") {
				t.Error("verify printed the sudo rules")
			}
		})
	}
}
