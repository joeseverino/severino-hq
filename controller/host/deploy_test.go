package host

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const next = "ghcr.io/joeseverino/severino-hq/composition@sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"

type handover struct {
	argv0 string
	argv  []string
	env   []string
}

func newDeployer(f *fakeHost) (*Deployer, *handover) {
	exec := &handover{}
	return &Deployer{
		Machine: f.machine,
		Cosign:  "/verifier/cosign",
		Exec: func(argv0 string, argv, env []string) error {
			exec.argv0, exec.argv, exec.env = argv0, argv, env
			return nil
		},
		Drain: 50 * time.Millisecond, Poll: 10 * time.Millisecond,
		FreeBytes: 512 << 20, FreeOn: "/",
		Free: func(string) (uint64, error) { return 1 << 30, nil },
	}, exec
}

func TestOnlyTheCompositionByDigestIsDeployable(t *testing.T) {
	if _, err := Deployable(next); err != nil {
		t.Errorf("refused %s: %v", next, err)
	}
	for _, image := range []string{
		"ghcr.io/joeseverino/severino-hq/composition:latest",
		"ghcr.io/joeseverino/severino-hq@" + "sha256:" + strings.Repeat("a", 64),
		"ghcr.io/someone-else/severino-hq/composition@sha256:" + strings.Repeat("a", 64),
		next + "\n--privileged",
	} {
		if _, err := Deployable(image); err == nil {
			t.Errorf("accepted %q", image)
		}
	}
}

func TestADeployVerifiesPullsAndHandsOverToTheRelease(t *testing.T) {
	f := newFakeHost(t)
	d, exec := newDeployer(f)

	if err := d.Deploy(context.Background(), next, strings.NewReader("deployer\nsecret-token\n")); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	f.before(t, "/verifier/cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp "+SignerIdentity+" "+next, "docker pull "+next)
	f.before(t, "docker pull "+next, "docker create --pull never "+next)
	if filepath.Base(exec.argv0) != "hq-host" || strings.Join(exec.argv[1:], " ") != "release --stage "+filepath.Dir(exec.argv0)+" "+next {
		t.Fatalf("handed over as %s %v", exec.argv0, exec.argv)
	}
	config := ""
	for _, variable := range exec.env {
		if value, ok := strings.CutPrefix(variable, "DOCKER_CONFIG="); ok {
			config = value
		}
	}
	if filepath.Dir(config) != filepath.Dir(exec.argv0) {
		t.Errorf("the credential is not in the stage the release removes: %q", config)
	}
	data, err := os.ReadFile(filepath.Join(config, "config.json"))
	if err != nil || !strings.Contains(string(data), `"ghcr.io"`) {
		t.Errorf("no registry credential: %s %v", data, err)
	}
	for _, call := range f.calls() {
		if strings.Contains(call, "secret-token") {
			t.Errorf("the token reached a command line: %s", call)
		}
	}
	if strings.Contains(f.out.String(), "secret-token") {
		t.Error("the token reached the public log")
	}
}

func TestAnUnsignedImageIsNotPulled(t *testing.T) {
	f := newFakeHost(t)
	f.fail = []string{"/verifier/cosign verify"}
	d, exec := newDeployer(f)

	err := d.Deploy(context.Background(), next, nil)

	if err == nil || !strings.Contains(err.Error(), "not signed") {
		t.Fatalf("got %v", err)
	}
	if f.called("docker pull") || exec.argv0 != "" {
		t.Error("an unsigned image went further than its check")
	}
	if entries, _ := os.ReadDir(f.machine.Runtime); len(entries) > 2 {
		t.Errorf("the stage was left behind: %v", entries)
	}
}

func TestAFullDiskStopsADeployBeforeThePull(t *testing.T) {
	f := newFakeHost(t)
	d, _ := newDeployer(f)
	d.Free = func(string) (uint64, error) { return 100 << 20, nil }

	if err := d.Deploy(context.Background(), next, nil); err == nil {
		t.Fatal("deployed onto a full disk")
	}
	if f.called("docker pull") {
		t.Error("pulled onto a full disk")
	}
}

func TestAHealthyReleaseIsSyncedInstalledAndReleasesTheHold(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer", "severino-hq-controller.path"}
	f.machine.Record(running)
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != next {
		t.Errorf("the release is %q", data)
	}
	f.before(t, "systemctl stop", "sh ")
	// The new tree and its web unit are in place before HQ restarts, so HQ
	// comes up as the release being deployed defines it.
	f.before(t, "sh ", "systemctl restart "+WebUnit)
	if !f.called("sh " + filepath.Join(f.machine.Runtime, "severino-hq-sync.")) {
		t.Error("the sync program is not the image's")
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, WebUnit)); err != nil {
		t.Error("the release's web unit was not installed")
	}
	f.before(t, "systemctl restart "+WebUnit, filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")+" install")
	f.before(t, filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")+" install", "systemctl start severino-hq-controller.path")
	if _, err := os.Stat(filepath.Join(f.machine.Sbin, "severino-hq-sync-scripts")); err != nil {
		t.Error("the release's sync program was not installed")
	}
}

func TestAnUnhealthyReleaseIsRolledBack(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer"}
	f.machine.Record(running)
	marker := filepath.Join(f.machine.Lib, "previous-release")
	os.WriteFile(marker, []byte("kept"), 0o644)
	previousUnit := filepath.Join(f.machine.Systemd, "severino-hq-controller.service")
	os.WriteFile(previousUnit, []byte("previous\n"), 0o644)
	f.fail = []string{"systemctl restart " + WebUnit}
	d, _ := newDeployer(f)

	err := d.Release(context.Background(), next)

	if err == nil {
		t.Fatal("an unhealthy release deployed")
	}
	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != running {
		t.Errorf("the release was not put back: %q", data)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Error("the previous tree was not restored")
	}
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "synced-release")); err == nil {
		t.Error("the unhealthy release's tree was left in place")
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, WebUnit)); err == nil {
		t.Error("the unhealthy release's web unit was left installed")
	}
	if data, _ := os.ReadFile(previousUnit); string(data) != "previous\n" {
		t.Errorf("a unit the release replaced was not put back: %q", data)
	}
	if !f.called("systemctl start severino-hq-secrets.timer") {
		t.Error("the held timer was not started again")
	}
	if !f.called("journalctl --unit " + WebUnit) {
		t.Error("no diagnostics were kept")
	}
}

func TestAFailedInstallRollsTheTreeAndImageBack(t *testing.T) {
	f := newFakeHost(t)
	f.machine.Record(running)
	marker := filepath.Join(f.machine.Lib, "previous-release")
	os.WriteFile(marker, []byte("kept"), 0o644)
	f.fail = []string{filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host") + " install"}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("a failed install deployed")
	}
	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != running {
		t.Errorf("the release was not put back: %q", data)
	}
	restarts := 0
	for _, call := range f.calls() {
		if call == "systemctl restart "+WebUnit {
			restarts++
		}
	}
	if restarts != 2 {
		t.Errorf("HQ was restarted %d times; once onto the release and once back", restarts)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Error("the previous tree was not restored")
	}
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "synced-release")); err == nil {
		t.Error("the failed release's tree was left in place")
	}
}

func TestNoRecordedReleaseNoDeploy(t *testing.T) {
	f := newFakeHost(t)
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("deployed with nothing to roll back to")
	}
	if f.called("systemctl") {
		t.Error("touched the host with nothing to roll back to")
	}
}
