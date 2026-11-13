package providers

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func filesystemInventory(t *testing.T, path string) runtime.KindReport {
	t.Helper()
	kind := runtime.ResourceKindHostFilesystem
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	controller := &Controller{Registry: New(runtime.Environment{HostFilesystems: path}, supplied(), &fakeHTTP{}), Declared: declared}
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)]
}

func writeFilesystems(t *testing.T, body []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "filesystems.json")
	if err := os.WriteFile(path, body, 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

// What root reads on the host is what HQ is sent: the launcher's own document,
// from a mount table and statfs's answers, read back as records.
func TestEachFilesystemTheLauncherReadIsARecord(t *testing.T) {
	table := "/dev/sda2 / ext4 rw 0 0\n/dev/sda1 /boot xfs rw 0 0\n/dev/sdb1 /srv/example|data ext4 rw 0 0\n"
	space := map[string][2]uint64{"/": {59 << 20, 29 << 30}, "/boot": {800 << 20, 1 << 30}, "/srv/example|data": {1, 2}}
	statfs := func(path string) (free, total uint64, err error) { return space[path][0], space[path][1], nil }
	document, err := hostcore.ReadFilesystems([]byte(table), statfs, time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}

	report := filesystemInventory(t, writeFilesystems(t, document))

	if !report.OK {
		t.Fatalf("%+v", report)
	}
	want := []any{
		runtime.HostFilesystemRecord{Mount: "/", FreeBytes: 59 << 20, TotalBytes: 29 << 30, ReadAt: "2030-01-01T00:00:00Z"},
		runtime.HostFilesystemRecord{Mount: "/boot", FreeBytes: 800 << 20, TotalBytes: 1 << 30, ReadAt: "2030-01-01T00:00:00Z"},
	}
	// The mount whose name holds the bar a fact row splits on is passed over.
	if !slices.Equal(report.Records, want) {
		t.Errorf("records %+v, want %+v", report.Records, want)
	}
}

// A run the launcher named the reading for but could not take it fails the
// read, so HQ keeps the last one beside the reason; a run that named nothing
// has no source for the kind. Neither is an empty reading.
func TestAReadingNotTakenIsNeverAnEmptyOne(t *testing.T) {
	for name, path := range map[string]string{
		"missing":       filepath.Join(t.TempDir(), "filesystems.json"),
		"not a reading": writeFilesystems(t, []byte(`{"read_at":"2030-01-01T00:00:00Z","filesystems":[],"device":"/dev/sda2"}`)),
		"no time":       writeFilesystems(t, []byte(`{"read_at":"","filesystems":[]}`)),
	} {
		t.Run(name, func(t *testing.T) {
			report := filesystemInventory(t, path)
			if report.OK || len(report.Records) != 0 || report.Error == "" {
				t.Errorf("%+v", report)
			}
		})
	}
	unnamed := filesystemInventory(t, "")
	if unnamed.Connected == nil || *unnamed.Connected || len(unnamed.Records) != 0 {
		t.Errorf("a run handed no filesystem reading: %+v", unnamed)
	}
}
