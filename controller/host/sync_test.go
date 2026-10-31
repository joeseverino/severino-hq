package host

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheTreeIsTheImagesSealed(t *testing.T) {
	f := newFakeHost(t)
	if err := os.WriteFile(filepath.Join(f.machine.Lib, "previous-release"), nil, 0o644); err != nil {
		t.Fatal(err)
	}

	if err := f.machine.Sync(context.Background(), next); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(filepath.Join(f.machine.Lib, "previous-release")); err == nil {
		t.Error("the previous tree's file survived the sync")
	}
	for path, mode := range map[string]os.FileMode{
		"deploy/bin/hq-host":        0o755,
		"hq/config/controller.json": 0o644,
		"deploy/systemd":            0o755,
	} {
		info, err := os.Stat(filepath.Join(f.machine.Lib, path))
		if err != nil || info.Mode().Perm() != mode {
			t.Errorf("%s: %v %v, want %v", path, info.Mode().Perm(), err, mode)
		}
	}
	source, _ := os.ReadFile(filepath.Join(f.machine.Lib, ".source"))
	if string(source) != "image "+imageID+"\n" {
		t.Errorf("provenance %q", source)
	}
	if entries, _ := filepath.Glob(filepath.Join(filepath.Dir(f.machine.Lib), ".severino-hq-sync.*")); len(entries) != 0 {
		t.Errorf("staging left behind: %v", entries)
	}
}

func TestATreeWithALinkIsRefused(t *testing.T) {
	f := newFakeHost(t)
	if err := os.Symlink("/etc/shadow", filepath.Join(f.image, "deploy", "bin", "planted")); err != nil {
		t.Fatal(err)
	}

	err := f.machine.Sync(context.Background(), next)

	if err == nil || !strings.Contains(err.Error(), "link") {
		t.Fatalf("got %v", err)
	}
	if _, err := os.Stat(filepath.Join(f.machine.Lib, "hq", "config", "synced-release")); err == nil {
		t.Error("the refused tree was swapped in")
	}
}

func TestAUnitNamingWhatTheTreeLacksIsRefused(t *testing.T) {
	f := newFakeHost(t)
	if err := os.WriteFile(filepath.Join(f.image, "deploy", "systemd", "severino-hq-orphan.service"),
		[]byte("[Service]\nExecStart=/usr/local/lib/severino-hq/deploy/bin/absent\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	err := f.machine.Sync(context.Background(), next)

	if err == nil || !strings.Contains(err.Error(), "deploy/bin/absent") {
		t.Fatalf("got %v", err)
	}
}

func TestAnImageThatIsNotHereIsNotSynced(t *testing.T) {
	f := newFakeHost(t)
	f.fail = []string{"docker image inspect"}

	if err := f.machine.Sync(context.Background(), next); err == nil {
		t.Fatal("synced an image that is not on the host")
	}
	if f.called("docker create") {
		t.Error("an image was opened after it was not found")
	}
}
