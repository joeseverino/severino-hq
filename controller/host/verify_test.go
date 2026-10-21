package host

import (
	"context"
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
		InstallUnit(f.machine.UnitDir(), f.machine.Systemd, name)
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
	os.WriteFile(filepath.Join(f.machine.Lib, "deploy", "bin", "hq-host"), []byte("#!/bin/sh\necho edited\n"), 0o755)
	os.WriteFile(filepath.Join(f.machine.Lib, "deploy", "bin", "planted"), nil, 0o755)
	os.Remove(filepath.Join(f.machine.Lib, "hq", "config", "controller.json"))
	os.WriteFile(filepath.Join(f.machine.Systemd, "severino-hq-web.service"), []byte("edited\n"), 0o644)
	os.WriteFile(filepath.Join(f.machine.Systemd, "severino-hq-old.timer"), nil, 0o644)
	os.WriteFile(filepath.Join(f.machine.Sbin, "severino-hq-sync-scripts"), nil, 0o755)

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
	os.WriteFile(filepath.Join(f.machine.Lib, ".source"), []byte("image sha256:other\n"), 0o644)

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
