package host

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const source Source = "joeseverino/severino-hq"

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
		Source:  source,
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
	if _, err := source.Deployable(next); err != nil {
		t.Errorf("refused %s: %v", next, err)
	}
	for _, image := range []string{
		"ghcr.io/joeseverino/severino-hq/composition:latest",
		"ghcr.io/joeseverino/severino-hq@" + "sha256:" + strings.Repeat("a", 64),
		"ghcr.io/someone-else/severino-hq/composition@sha256:" + strings.Repeat("a", 64),
		next + "\n--privileged",
	} {
		if _, err := source.Deployable(image); err == nil {
			t.Errorf("accepted %q", image)
		}
	}
}

func TestAForkDeploysItsOwnImagesAndAnUnnamedBuildNone(t *testing.T) {
	fork := Source("Someone-Else/hq-fork")
	own := "ghcr.io/someone-else/hq-fork/composition@sha256:" + strings.Repeat("b", 64)

	if _, err := fork.Deployable(own); err != nil {
		t.Errorf("a fork refused its own image: %v", err)
	}
	if _, err := fork.Deployable(next); err == nil {
		t.Error("a fork accepted this repository's image")
	}
	if fork.Signer() != `^https://github\.com/Someone-Else/hq-fork/\.github/workflows/compose\.yml@refs/heads/main$` {
		t.Errorf("signer %s", fork.Signer())
	}
	for _, unnamed := range []Source{"", "no-slash", "owner/name/extra", "owner/na me"} {
		if _, err := unnamed.Deployable(next); err == nil {
			t.Errorf("a build naming %q deployed", unnamed)
		}
	}
}

func TestADeployVerifiesPullsAndHandsOverToTheRelease(t *testing.T) {
	f := newFakeHost(t)
	d, exec := newDeployer(f)

	if err := d.Deploy(context.Background(), next, strings.NewReader("deployer\nsecret-token\n")); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	f.before(t, "/verifier/cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp "+source.Signer()+" "+next, "docker pull "+next)
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
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != next {
		t.Errorf("the release is %q", data)
	}
	f.before(t, "systemctl stop", "docker create --pull never "+imageID)
	// The new tree and its web unit are in place before HQ restarts, so HQ
	// comes up as the release being deployed defines it.
	f.before(t, "docker create --pull never "+imageID, "systemctl restart "+WebUnit)
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "hq", "config", "synced-release")); err != nil {
		t.Error("the tree is not the image's")
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, WebUnit)); err != nil {
		t.Error("the release's web unit was not installed")
	}
	f.before(t, "systemctl restart "+WebUnit, filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")+" install")
	f.before(t, filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host")+" install", "systemctl start severino-hq-controller.path")
	// The release just installed is verified before the deploy ends.
	f.before(t, "systemctl start severino-hq-controller.path", "systemctl start "+DriftUnit)
	if leftovers, _ := filepath.Glob(filepath.Join(f.machine.Sbin, "severino-hq-*")); len(leftovers) != 0 {
		t.Errorf("a program was installed outside the tree: %v", leftovers)
	}
}

// Drift found after a healthy release fails the deploy and leaves the release
// standing: rolling back would not mend the host.
// A deploy waits out a glance pass as it does a controller pass: each is a
// container of the running image that the new one would fail under.
func TestTheHoldWaitsOutEveryPassUnderWay(t *testing.T) {
	for _, unit := range []string{ControllerUnit, GlanceUnit, SecretsUnit} {
		t.Run(unit, func(t *testing.T) {
			f := newFakeHost(t)
			f.active = []string{unit}
			d, _ := newDeployer(f)

			if _, err := d.hold(context.Background()); err != nil {
				t.Fatal(err)
			}

			if !strings.Contains(f.out.String(), "still going") {
				t.Errorf("the hold did not wait for %s", unit)
			}
		})
	}
}

func TestDriftAfterTheReleaseFailsTheDeployAndKeepsTheRelease(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	f.fail = []string{"systemctl start " + DriftUnit}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("a deploy that left the host off its release passed")
	}

	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != next {
		t.Errorf("the release was rolled back to %q", data)
	}
	if !strings.Contains(f.out.String(), "journalctl -u "+DriftUnit) {
		t.Errorf("the deploy does not say where verify's findings are:\n%s", f.out.String())
	}
}

func TestAnUnhealthyReleaseIsRolledBack(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{"severino-hq-secrets.timer"}
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(f.machine.Lib, "previous-release")
	if err := os.WriteFile(marker, []byte("kept"), 0o644); err != nil {
		t.Fatal(err)
	}
	previousUnit := filepath.Join(f.machine.Systemd, "severino-hq-controller.service")
	if err := os.WriteFile(previousUnit, []byte("previous\n"), 0o644); err != nil {
		t.Fatal(err)
	}
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
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "hq", "config", "synced-release")); err == nil {
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
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(f.machine.Lib, "previous-release")
	if err := os.WriteFile(marker, []byte("kept"), 0o644); err != nil {
		t.Fatal(err)
	}
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
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "hq", "config", "synced-release")); err == nil {
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

// data is the fake data volume, where the database and its kept copy live.
func (f *fakeHost) data() string {
	dir := filepath.Join(f.root, "volumes", "severino-hq_severino_data")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		f.t.Fatal(err)
	}
	return dir
}

func TestAReleaseThatMigratedAndFailedPutsTheDatabaseBack(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(f.data(), Database)
	if err := os.WriteFile(database, []byte("previous schema"), 0o600); err != nil {
		t.Fatal(err)
	}
	// The new image's boot keeps the database, migrates it, then never
	// becomes healthy. The previous image's boot finds nothing to migrate.
	booted := false
	f.boot = func() {
		if booted {
			return
		}
		booted = true
		data, _ := os.ReadFile(database)
		if err := os.WriteFile(filepath.Join(f.data(), SchemaSnapshot), data, 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(database, []byte("migrated schema"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(database+"-wal", []byte("migrated log"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	f.fail = []string{"systemctl restart " + WebUnit}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("an unhealthy release deployed")
	}

	if data, _ := os.ReadFile(database); string(data) != "previous schema" {
		t.Errorf("the previous image was left on %q", data)
	}
	for _, gone := range []string{database + "-wal", filepath.Join(f.data(), SchemaSnapshot)} {
		if _, err := os.Stat(gone); err == nil {
			t.Errorf("%s was left behind", filepath.Base(gone))
		}
	}
	// Stopped before the database moved, started again only after.
	calls := strings.Join(f.calls(), "\n")
	stop := strings.LastIndex(calls, "systemctl stop "+WebUnit)
	restart := strings.LastIndex(calls, "systemctl restart "+WebUnit)
	if stop < 0 || restart < stop {
		t.Errorf("HQ was not stopped while its database was restored:\n%s", calls)
	}
}

func TestAReleaseThatFailedWithoutMigratingLeavesTheDatabase(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(f.data(), Database)
	if err := os.WriteFile(database, []byte("current"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Kept by an earlier release: not the database this one found.
	if err := os.WriteFile(filepath.Join(f.data(), SchemaSnapshot), []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}
	f.fail = []string{"systemctl restart " + WebUnit}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err == nil {
		t.Fatal("an unhealthy release deployed")
	}

	if data, _ := os.ReadFile(database); string(data) != "current" {
		t.Errorf("the database became %q", data)
	}
	if f.called("systemctl stop " + WebUnit) {
		t.Error("HQ was stopped with no database to restore")
	}
}

func TestAReleaseThatStandsDropsTheDatabaseItKept(t *testing.T) {
	f := newFakeHost(t)
	if err := f.machine.Record(running); err != nil {
		t.Fatal(err)
	}
	kept := filepath.Join(f.data(), SchemaSnapshot)
	f.boot = func() {
		if err := os.WriteFile(kept, []byte("before"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	d, _ := newDeployer(f)

	if err := d.Release(context.Background(), next); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	if _, err := os.Stat(kept); err == nil {
		t.Error("the database kept before migrating outlived the release")
	}
}
