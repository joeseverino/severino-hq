package host

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const running = "ghcr.io/joeseverino/severino-hq/composition@" + digest

func connected() error { return nil }

func TestTheRenderGoesFirstAndHQIsEnabledLast(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{WebUnit}

	if err := f.machine.Install(context.Background(), connected); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	f.before(t, "systemctl start "+SecretsUnit, "docker exec "+Container+" hq-controller job content.sync")
	f.before(t, "docker exec "+Container, "systemctl enable --now")
	if !f.called("systemctl enable --now severino-hq-controller.path severino-hq-secrets.timer severino-hq-web.service") {
		t.Errorf("not enabled:\n%s", strings.Join(f.calls(), "\n"))
	}
	for _, name := range []string{"severino-hq-web.service", "severino-hq-secrets.service.d/10-root-owned-exec.conf"} {
		if _, err := os.Stat(filepath.Join(f.machine.Systemd, name)); err != nil {
			t.Errorf("%s not installed", name)
		}
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, "severino-hq-connect.conf.example")); err == nil {
		t.Error("a template was installed")
	}
	if !f.called("systemd-tmpfiles --create " + filepath.Join(f.machine.Tmpfiles, "severino-hq.conf")) {
		t.Error("the doorbell directory's rule was not applied")
	}
}

func TestAFailedRenderPutsTheRenderersUnitsBack(t *testing.T) {
	f := newFakeHost(t)
	previous := filepath.Join(f.machine.Systemd, SecretsUnit)
	os.WriteFile(previous, []byte("previous\n"), 0o644)
	f.fail = []string{"systemctl start " + SecretsUnit}

	err := f.machine.Install(context.Background(), connected)

	if err == nil {
		t.Fatal("a failed render installed")
	}
	if data, _ := os.ReadFile(previous); string(data) != "previous\n" {
		t.Errorf("the renderer's unit was not restored: %q", data)
	}
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, "severino-hq-secrets.service.d/10-root-owned-exec.conf")); err == nil {
		t.Error("a drop-in the previous release did not have was left behind")
	}
	if f.called("systemctl enable") {
		t.Error("units were enabled after a failed render")
	}
}

func TestConnectionsThatAreNotTheRenderersStopTheInstall(t *testing.T) {
	f := newFakeHost(t)

	err := f.machine.Install(context.Background(), func() error { return errors.New("not on tmpfs") })

	if err == nil || !strings.Contains(err.Error(), "not on tmpfs") {
		t.Fatalf("got %v", err)
	}
	if f.called("docker exec") {
		t.Error("a preflight ran on connections that were refused")
	}
}

func TestAComposeHostIsTakenOverWithItsRunningImage(t *testing.T) {
	f := newFakeHost(t)
	f.running = running

	if err := f.machine.Install(context.Background(), connected); err != nil {
		t.Fatalf("%v\n%s", err, f.out.String())
	}

	data, err := os.ReadFile(f.machine.ReleasePath())
	if err != nil || strings.TrimSpace(string(data)) != running {
		t.Fatalf("the release is %q, %v", data, err)
	}
	f.before(t, "systemctl enable --now "+WebUnit, "systemctl enable --now severino-hq-controller.path")
}

func TestATakeOverThatFailsLeavesTheUnitDisabled(t *testing.T) {
	f := newFakeHost(t)
	f.running = running
	f.fail = []string{"systemctl enable --now " + WebUnit}

	err := f.machine.Install(context.Background(), connected)

	if err == nil {
		t.Fatal("a failed take-over installed")
	}
	if !f.called("systemctl disable --now " + WebUnit) {
		t.Errorf("the unit was left enabled:\n%s", strings.Join(f.calls(), "\n"))
	}
}

func TestAnUnpinnedRunningImageIsNotRecorded(t *testing.T) {
	f := newFakeHost(t)
	f.running = "ghcr.io/joeseverino/severino-hq/composition:latest"

	if err := f.machine.Install(context.Background(), connected); err == nil {
		t.Fatal("a tag was recorded as the release")
	}
	if _, err := os.Stat(f.machine.ReleasePath()); err == nil {
		t.Error("a release was written")
	}
}

func TestPreflightOutputStaysInThePrivateLog(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{WebUnit}

	if err := f.machine.Install(context.Background(), connected); err != nil {
		t.Fatal(err)
	}

	if !strings.Contains(f.out.String(), "Content index preflight passed.") {
		t.Errorf("public log: %s", f.out.String())
	}
	info, err := os.Stat(filepath.Join(f.machine.LogDir, "content-index-preflight.log"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("the private log is %v", info.Mode().Perm())
	}
}

func TestATakeOverRecordsWhatRunsOverARecordLeftBehind(t *testing.T) {
	f := newFakeHost(t)
	f.machine.Record(next)
	f.running = running

	if err := f.machine.Install(context.Background(), connected); err != nil {
		t.Fatal(err)
	}

	data, _ := os.ReadFile(f.machine.ReleasePath())
	if strings.TrimSpace(string(data)) != running {
		t.Errorf("the release is %q, not the image running", data)
	}
}

func TestAFailedTakeOverFreesHQsName(t *testing.T) {
	f := newFakeHost(t)
	f.running = running
	f.fail = []string{"systemctl enable --now " + WebUnit}

	if err := f.machine.Install(context.Background(), connected); err == nil {
		t.Fatal("a failed take-over installed")
	}

	f.before(t, "systemctl disable --now "+WebUnit, "docker rm --force "+Container)
	if _, err := os.Stat(filepath.Join(f.machine.Systemd, WebUnit)); err == nil {
		t.Error("the web unit was left installed")
	}
}

func TestWhatNoReleaseShipsIsRemoved(t *testing.T) {
	f := newFakeHost(t)
	f.active = []string{WebUnit}
	os.Remove(filepath.Join(f.machine.UnitDir(), SecretsUnit+".d", "10-root-owned-exec.conf"))
	leftovers := []string{
		filepath.Join(f.machine.Systemd, ControllerUnit+".d", "10-root-owned-exec.conf"),
		filepath.Join(f.machine.Systemd, SecretsUnit+".d", "10-root-owned-exec.conf"),
		filepath.Join(f.machine.Sbin, "severino-hq-sync-scripts"),
	}
	for _, leftover := range leftovers {
		os.MkdirAll(filepath.Dir(leftover), 0o755)
		os.WriteFile(leftover, nil, 0o644)
	}

	if err := f.machine.Install(context.Background(), connected); err != nil {
		t.Fatal(err)
	}

	for _, leftover := range leftovers {
		if _, err := os.Stat(leftover); err == nil {
			t.Errorf("%s was left", leftover)
		}
	}
}
