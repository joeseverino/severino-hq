package providers

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What `systemctl show --timestamp=unix` prints for a failed service, a
// waiting timer and a unit that is not installed, with a property the launcher
// never asks for.
const hostUnitsShown = "Id=severino-hq-backup.service\n" +
	"LoadState=loaded\nUnitFileState=static\nActiveState=failed\nSubState=failed\nResult=exit-code\n" +
	"ExecMainCode=1\nExecMainStatus=3\n" +
	"InactiveExitTimestamp=@1767323100\nInactiveEnterTimestamp=@1767323160\n" +
	"ConditionResult=yes\nConditionTimestamp=@1767323100\n" +
	"Environment=EXAMPLE_TOKEN=sentinel-value\n" +
	"\n" +
	"Id=severino-hq-backup.timer\n" +
	"LoadState=loaded\nUnitFileState=enabled\nActiveState=active\nSubState=waiting\nResult=success\n" +
	"LastTriggerUSec=Fri 2026-01-02 03:05:00 UTC\nNextElapseUSecRealtime=@1767409500\n" +
	"InactiveExitTimestamp=n/a\nUnit=severino-hq-backup.service\n" +
	"\n" +
	"Id=severino-hq-job@audit.prune.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n"

func writeHostUnits(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "units")
	if err := os.WriteFile(path, []byte(body), 0o400); err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 1, 2, 4, 0, 0, 0, time.UTC)
	if err := os.Chtimes(path, at, at); err != nil {
		t.Fatal(err)
	}
	return path
}

func hostUnits(t *testing.T, path string) ([]any, error) {
	t.Helper()
	return New(runtime.Environment{hostUnitsEnv: path}, &fakeHTTP{}).hostUnits(t.Context())
}

func TestHostUnitsAreWhatSystemdSaysOfEachUnit(t *testing.T) {
	records, err := hostUnits(t, writeHostUnits(t, hostUnitsShown))
	if err != nil {
		t.Fatal(err)
	}
	const readAt = "2026-01-02T04:00:00Z"
	want := []any{
		runtime.HostUnitRecord{
			Unit: "severino-hq-backup.service", Load: "loaded", FileState: "static", Active: "failed", Sub: "failed",
			Result: "exit-code", MainCode: 1, MainStatus: 3,
			StartedAt: "2026-01-02T03:05:00Z", EndedAt: "2026-01-02T03:06:00Z",
			Condition: "yes", ConditionAt: "2026-01-02T03:05:00Z", ReadAt: readAt,
		},
		runtime.HostUnitRecord{
			Unit: "severino-hq-backup.timer", Load: "loaded", FileState: "enabled", Active: "active", Sub: "waiting",
			Result: "success", LastTriggerAt: "2026-01-02T03:05:00Z", NextElapseAt: "2026-01-03T03:05:00Z",
			Activates: "severino-hq-backup.service", ReadAt: readAt,
		},
		runtime.HostUnitRecord{
			Unit: "severino-hq-job@audit.prune.service", Load: "not-found", Active: "inactive", Sub: "dead", ReadAt: readAt,
		},
	}
	if !slices.Equal(records, want) {
		t.Fatalf("records\n%+v\nwant\n%+v", records, want)
	}
}

// The record is the allowlisted properties and nothing else: a property the
// file holds beyond them never reaches HQ.
func TestHostUnitsCarryNoPropertyTheReadingDoesNotName(t *testing.T) {
	records, err := hostUnits(t, writeHostUnits(t, hostUnitsShown))
	if err != nil {
		t.Fatal(err)
	}
	sent, err := json.Marshal(records)
	if err != nil {
		t.Fatal(err)
	}
	for _, absent := range []string{"sentinel-value", "EXAMPLE_TOKEN", "Environment"} {
		if strings.Contains(string(sent), absent) {
			t.Errorf("the reading carries %q: %s", absent, sent)
		}
	}
}

// The launcher asks systemd for exactly the properties the reading keeps.
func TestTheLauncherAsksForThePropertiesTheReadingKeeps(t *testing.T) {
	library, err := os.ReadFile("../../scripts/lib/systemd-units.sh")
	if err != nil {
		t.Fatal(err)
	}
	declared := regexp.MustCompile(`(?m)^readonly units_properties='([A-Za-z,]+)'$`).FindSubmatch(library)
	if declared == nil {
		t.Fatal("scripts/lib/systemd-units.sh declares no units_properties")
	}
	asked := strings.Split(string(declared[1]), ",")
	slices.Sort(asked)
	kept := []string{}
	for property := range hostUnitProperties {
		kept = append(kept, property)
	}
	slices.Sort(kept)
	if !slices.Equal(asked, kept) {
		t.Errorf("asked %v\nkept  %v", asked, kept)
	}
}

func TestHostUnitsRefuseWhatSystemctlDoesNotPrint(t *testing.T) {
	unit := "Id=example.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
	cases := map[string]string{
		"nothing":                       "",
		"only blank lines":              "\n\n\n",
		"a line that is no property":    unit + "systemd is not running\n",
		"a unit with no name":           "LoadState=loaded\nActiveState=active\nSubState=running\n",
		"a unit with no state":          "Id=example.service\nLoadState=loaded\n",
		"a name that is not a unit":     strings.Replace(unit, "example.service", "../etc/passwd", 1),
		"a state that is not a word":    strings.Replace(unit, "running", "running; rm -rf", 1),
		"a sentence where a word goes":  unit + "Result=the unit failed badly\n",
		"an exit status not a number":   unit + "ExecMainStatus=three\n",
		"a negative exit status":        unit + "ExecMainStatus=-1\n",
		"an instant in a local zone":    unit + "InactiveEnterTimestamp=Fri 2026-01-02 03:05:00 CST\n",
		"an instant that is not one":    unit + "InactiveEnterTimestamp=yesterday\n",
		"a unit systemd answered twice": unit + "\n" + unit,
		"a unit it starts with no name": unit + "Unit=/bin/sh\n",
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			path := writeHostUnits(t, body)
			records, err := hostUnits(t, path)
			if err == nil || records != nil {
				t.Fatalf("accepted: %+v", records)
			}
			if err.Error() != hostUnitsRefused || strings.Contains(err.Error(), path) {
				t.Fatalf("the refusal says more than that it refused: %v", err)
			}
		})
	}
}

func TestHostUnitsSayWhenTheLauncherMountedNoAnswer(t *testing.T) {
	absent := filepath.Join(t.TempDir(), "units")
	for _, path := range []string{"", absent} {
		records, err := hostUnits(t, path)
		if err == nil || records != nil {
			t.Fatalf("path %q: %+v", path, records)
		}
		if strings.Contains(err.Error(), absent) {
			t.Errorf("the refusal names a path: %v", err)
		}
	}
	if _, err := hostUnits(t, writeHostUnits(t, strings.Repeat("\n", hostUnitsMaxBytes+1))); err == nil {
		t.Error("more than a unit state holds was read")
	}
}

func TestHostUnitsAreReadOnlyWhereTheLauncherNamesTheReading(t *testing.T) {
	kind := runtime.ResourceKindHostUnit
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	shown := writeHostUnits(t, hostUnitsShown)
	for path, want := range map[string]int{"": -1, shown: 3} {
		controller := &Controller{Registry: New(runtime.Environment{hostUnitsEnv: path}, &fakeHTTP{}), Declared: declared}
		inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
		if err != nil {
			t.Fatal(err)
		}
		report := inventory[string(kind)]
		connected := report.Connected == nil || *report.Connected
		if connected != (want >= 0) || !report.OK {
			t.Errorf("path %q: %+v", path, report)
		}
		if want >= 0 && len(report.Records) != want {
			t.Errorf("path %q: %d records, want %d", path, len(report.Records), want)
		}
	}
}

// TestHostUnitsAsSystemdPrintsThem holds the parser to what `systemctl show
// --timestamp=unix` prints on systemd 259: a timer on a monotonic schedule has
// no next elapse, a unit not started since boot has empty instants and
// ConditionResult=no, and a unit systemd has no file for is not-found.
func TestHostUnitsAsSystemdPrintsThem(t *testing.T) {
	const printed = `Id=example-backup.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=static
InactiveExitTimestamp=@1791084218
InactiveEnterTimestamp=@1791084221
ConditionResult=yes
ConditionTimestamp=@1791084218
Result=success
ExecMainCode=1
ExecMainStatus=0

Id=example-controller.timer
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
InactiveExitTimestamp=@1791140198
InactiveEnterTimestamp=@1791140132
ConditionResult=yes
ConditionTimestamp=@1791140198
Unit=example-controller.service
NextElapseUSecRealtime=
LastTriggerUSec=@1791159472
Result=success

Id=example-job@audit.prune.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=
InactiveExitTimestamp=
InactiveEnterTimestamp=
ConditionResult=no
ConditionTimestamp=
Result=success
ExecMainCode=0
ExecMainStatus=0
`
	records, err := parseHostUnits(printed, "2026-10-04T00:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 3 {
		t.Fatalf("records %v", records)
	}
	timer, unstarted := records[1], records[2]
	if timer.NextElapseAt != "" || timer.LastTriggerAt == "" || timer.Activates != "example-controller.service" {
		t.Fatalf("timer %+v", timer)
	}
	if unstarted.Load != "not-found" || unstarted.Condition != "no" || unstarted.ConditionAt != "" || unstarted.FileState != "" {
		t.Fatalf("unstarted %+v", unstarted)
	}
}
