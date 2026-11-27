package host

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// setupRuns is each run of the setup playbook, the apply's and the check's.
func (f *fakeHost) setupRuns(t *testing.T) (apply, check string) {
	t.Helper()
	for _, call := range f.calls() {
		switch {
		case !isSetup(call):
		case strings.Contains(call, " --check "):
			check = call
		default:
			apply = call
		}
	}
	if apply == "" || check == "" {
		t.Fatalf("no apply and check of the setup:\n%s", strings.Join(f.calls(), "\n"))
	}
	return apply, check
}

// An apply reaches the host as root through nsenter; a check holds no
// capability and no network and is lent only read-only binds. Each runs the
// verified runner by digest, and host.json reaches neither by its command
// line, where Ansible would render a template its value held.
func TestTheSetupAppliesAsRootAndChecksWithNothing(t *testing.T) {
	f := synced(t)
	vault := "{{ 7 * 7 }} Vault"
	if err := os.WriteFile(f.machine.HostConfig, []byte(strings.Replace(hostJSON, "Example Vault", vault, 1)), 0o600); err != nil {
		t.Fatal(err)
	}

	if err := f.machine.Setup(context.Background(), runnerImage, f.machine.Lib); err != nil {
		t.Fatal(err)
	}
	if _, err := f.machine.SetupDrift(context.Background(), f.machine.Lib); err != nil {
		t.Fatal(err)
	}

	apply, check := f.setupRuns(t)
	for _, flag := range []string{"--privileged", "--pid host", "--network none", "--read-only", "--connection community.docker.nsenter"} {
		if !strings.Contains(apply, " "+flag+" ") {
			t.Errorf("the apply ran without %s: %s", flag, apply)
		}
	}
	for _, flag := range []string{"--cap-drop ALL", "no-new-privileges:true", "--network none", "--read-only", "--connection local", "--check"} {
		if !strings.Contains(check, " "+flag+" ") {
			t.Errorf("the check ran without %s: %s", flag, check)
		}
	}
	for _, flag := range []string{"--privileged", "--pid", "--cap-add", "--volume", "docker.sock"} {
		if strings.Contains(check, flag) {
			t.Errorf("the check ran with %s: %s", flag, check)
		}
	}
	words := strings.Fields(check)
	for i, word := range words {
		if word == "--mount" && !strings.HasSuffix(words[i+1], ",readonly") {
			t.Errorf("the check was lent %s to write", words[i+1])
		}
	}
	for _, call := range []string{apply, check} {
		if !strings.Contains(call, " --pull never ") || !strings.Contains(call, " "+runnerImage+" ") {
			t.Errorf("the setup ran something other than the runner it was given: %s", call)
		}
		if strings.Contains(call, "7 * 7") {
			t.Errorf("host.json reached the playbook's command line: %s", call)
		}
		_, vars, _ := strings.Cut(call, "setup_vars=")
		vars, _, _ = strings.Cut(vars, " ")
		if filepath.Dir(filepath.Dir(vars)) != f.machine.Runtime {
			t.Errorf("the setup's variables are not root's own beneath the runtime directory: %s", vars)
		}
		if _, err := os.Stat(filepath.Dir(vars)); err == nil {
			t.Errorf("the setup's variables outlived its run: %s", vars)
		}
	}
}

// A host whose configuration is unreadable or invalid runs no setup.
func TestASetupNeedsAValidHostConfiguration(t *testing.T) {
	for name, change := range map[string]func(path string) error{
		"missing": os.Remove,
		"invalid": func(path string) error { return os.WriteFile(path, []byte(`{"vault": ""}`), 0o600) },
	} {
		t.Run(name, func(t *testing.T) {
			f := synced(t)
			if err := change(f.machine.HostConfig); err != nil {
				t.Fatal(err)
			}

			if err := f.machine.Setup(context.Background(), runnerImage, f.machine.Lib); err == nil {
				t.Fatal("set up with no valid host configuration")
			}
			if slices.ContainsFunc(f.calls(), isSetup) {
				t.Errorf("the setup ran: %q", f.calls())
			}
		})
	}
}

// An install verifies and pulls the runner its tree pins, and only then sets
// the host up in it.
func TestAnInstallSetsUpInTheVerifiedRunner(t *testing.T) {
	f := synced(t)
	d, _ := newDeployer(f)

	if err := d.Install(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	verify := "/verifier/cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp " + source.Signer() + " " + runnerImage
	apply := f.setups()
	if len(apply) != 1 || !strings.Contains(apply[0], " "+runnerImage+" ") {
		t.Fatalf("the install did not set the host up in its runner: %q", apply)
	}
	f.before(t, verify, "docker pull "+runnerImage)
	f.before(t, "docker pull "+runnerImage, apply[0])
	// The playbook starts every unit but what starts a controller pass, which
	// comes last, so the first pass finds the rest up.
	f.before(t, apply[0], "systemctl start severino-hq-controller.path")
}

// A setup that fails part way puts every unit file back as it was.
func TestAFailedSetupPutsTheUnitsBack(t *testing.T) {
	f := synced(t)
	unit := filepath.Join(f.machine.Systemd, "severino-hq-web.service")
	if err := os.WriteFile(unit, []byte("the host's\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	setup := "docker run --rm --pull never --name severino-hq-setup-"
	f.on = map[string]func(){setup: func() {
		_ = os.WriteFile(unit, []byte("the release's\n"), 0o644)
		_ = os.WriteFile(filepath.Join(f.machine.Systemd, "severino-hq-new.timer"), nil, 0o644)
	}}
	f.fail = []string{setup}
	d, _ := newDeployer(f)

	if err := d.Install(context.Background()); err == nil {
		t.Fatal("a failed setup installed")
	}

	if data, _ := os.ReadFile(unit); string(data) != "the host's\n" {
		t.Errorf("a unit the setup replaced was not put back: %q", data)
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, "severino-hq-new.timer")); err == nil {
		t.Error("a unit the setup added was left")
	}
	if !f.called("systemctl daemon-reload") {
		t.Error("systemd was not told the units were put back")
	}
}
