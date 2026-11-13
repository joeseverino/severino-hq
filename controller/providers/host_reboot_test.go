package providers

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func rebootInventory(t *testing.T, path string) runtime.KindReport {
	t.Helper()
	kind := runtime.ResourceKindHostReboot
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	controller := &Controller{Registry: New(runtime.Environment{HostReboot: path}, supplied(), &fakeHTTP{}), Declared: declared}
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)]
}

func writeReboot(t *testing.T, body []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "reboot.json")
	if err := os.WriteFile(path, body, 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

// What root reads on the host is what HQ is sent: the launcher's own document,
// from a flag, its package list and logind's answer, read back as the record.
func TestTheRestartTheLauncherReadIsTheRecord(t *testing.T) {
	flag := filepath.Join(t.TempDir(), "reboot-required")
	if err := os.WriteFile(flag, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	// A name dpkg would never write, between two it would.
	if err := os.WriteFile(flag+".pkgs", []byte("linux-image-example\nNot|A package\nlibc6\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	born := func(string) (time.Time, error) { return time.Date(2029, 12, 31, 18, 0, 0, 0, time.UTC), nil }
	scheduled := []byte(`{"type":"(st)","data":["reboot",1893466800000000]}`)
	document, err := hostcore.ReadReboot(flag, born, scheduled, time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}

	report := rebootInventory(t, writeReboot(t, document))

	if !report.OK {
		t.Fatalf("%+v", report)
	}
	want := []any{runtime.HostRebootRecord{
		Pending: true, Since: "2029-12-31T18:00:00Z", Packages: []string{"linux-image-example", "libc6"},
		ScheduledAction: "reboot", ScheduledAt: "2030-01-01T03:00:00Z", ReadAt: "2030-01-01T00:00:00Z",
	}}
	if !reflect.DeepEqual(report.Records, want) {
		t.Errorf("records %+v, want %+v", report.Records, want)
	}
}

func TestThePackageListStopsAtTheContractsCount(t *testing.T) {
	var names []string
	for i := range hostRebootPackages + 5 {
		names = append(names, fmt.Sprintf("%q", fmt.Sprintf("example-%d", i)))
	}
	body := `{"read_at":"2030-01-01T00:00:00Z","pending":true,"since":"","packages":[` + strings.Join(names, ",") +
		`],"scheduled_action":"","scheduled_at":""}`

	report := rebootInventory(t, writeReboot(t, []byte(body)))

	if !report.OK || len(report.Records) != 1 {
		t.Fatalf("%+v", report)
	}
	if got := report.Records[0].(runtime.HostRebootRecord).Packages; len(got) != hostRebootPackages {
		t.Errorf("%d packages kept, the contract admits %d", len(got), hostRebootPackages)
	}
}

// A run the launcher named the reading for but could not take it fails the
// read, so HQ keeps the last one beside the reason; a run that named nothing
// has no source for the kind. Neither is a reading that nothing waits.
func TestARestartReadingNotTakenIsNeverAnEmptyOne(t *testing.T) {
	for name, path := range map[string]string{
		"missing":          filepath.Join(t.TempDir(), "reboot.json"),
		"not a reading":    writeReboot(t, []byte(`{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":"","path":"/run"}`)),
		"no time":          writeReboot(t, []byte(`{"read_at":"","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":""}`)),
		"an action, no at": writeReboot(t, []byte(`{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"reboot","scheduled_at":""}`)),
	} {
		t.Run(name, func(t *testing.T) {
			report := rebootInventory(t, path)
			if report.OK || len(report.Records) != 0 || report.Error == "" {
				t.Errorf("%+v", report)
			}
		})
	}
	unnamed := rebootInventory(t, "")
	if unnamed.Connected == nil || *unnamed.Connected || len(unnamed.Records) != 0 {
		t.Errorf("a run handed no restart reading: %+v", unnamed)
	}
}
