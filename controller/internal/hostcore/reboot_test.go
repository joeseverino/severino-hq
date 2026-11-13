package hostcore

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"
)

// busctl's answers for logind's ScheduledShutdown, as systemd 259 prints them
// with --json=short: nothing scheduled, and `shutdown -r 03:00`.
const (
	nothingScheduled = `{"type":"(st)","data":["",18446744073709551615]}`
	rebootScheduled  = `{"type":"(st)","data":["reboot",1893466800000000]}`
)

var (
	written = time.Date(2029, 12, 31, 18, 4, 5, 0, time.UTC)
	readNow = time.Date(2030, 1, 1, 6, 0, 0, 0, time.FixedZone("example", 3600))
)

func bornAt(at time.Time) func(string) (time.Time, error) {
	return func(string) (time.Time, error) { return at, nil }
}

// A flag as update-notifier leaves one after a kernel and a library each asked
// for a restart, the kernel twice.
func pendingFlag(t *testing.T) string {
	t.Helper()
	flag := filepath.Join(t.TempDir(), "reboot-required")
	if err := os.WriteFile(flag, []byte("*** System restart required ***\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(flag+".pkgs", []byte("linux-image-example\nlibc6\nlinux-image-example\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return flag
}

func readReboot(t *testing.T, flag, scheduled string) RebootReading {
	t.Helper()
	data, err := ReadReboot(flag, bornAt(written), []byte(scheduled), readNow)
	if err != nil {
		t.Fatal(err)
	}
	var reading RebootReading
	if err := json.Unmarshal(data, &reading); err != nil {
		t.Fatal(err)
	}
	return reading
}

func TestAFlagSaysSinceWhenAndForWhichPackages(t *testing.T) {
	reading := readReboot(t, pendingFlag(t), nothingScheduled)

	want := RebootReading{
		ReadAt: "2030-01-01T05:00:00Z", Pending: true, Since: "2029-12-31T18:04:05Z",
		Packages: []string{"linux-image-example", "libc6"},
	}
	if !slices.Equal(reading.Packages, want.Packages) || reading.ReadAt != want.ReadAt || !reading.Pending ||
		reading.Since != want.Since || reading.ScheduledAction != "" || reading.ScheduledAt != "" {
		t.Errorf("reading = %+v, want %+v", reading, want)
	}
}

func TestNoFlagIsNothingWaiting(t *testing.T) {
	reading := readReboot(t, filepath.Join(t.TempDir(), "reboot-required"), nothingScheduled)

	if reading.Pending || reading.Since != "" || len(reading.Packages) != 0 || reading.ReadAt == "" {
		t.Errorf("reading = %+v", reading)
	}
}

func TestARestartLogindHasScheduledIsSaid(t *testing.T) {
	reading := readReboot(t, pendingFlag(t), rebootScheduled)

	if reading.ScheduledAction != "reboot" || reading.ScheduledAt != "2030-01-01T03:00:00Z" {
		t.Errorf("reading = %+v", reading)
	}
}

func TestADryRunOrNoTimeIsNothingScheduled(t *testing.T) {
	for _, answer := range []string{
		nothingScheduled,
		`{"type":"(st)","data":["dry-reboot",1893466800000000]}`,
		`{"type":"(st)","data":["reboot",0]}`,
	} {
		action, at, err := ParseScheduledShutdown([]byte(answer))
		if err != nil || action != "" || !at.IsZero() {
			t.Errorf("%s reads as %q at %v (%v)", answer, action, at, err)
		}
	}
}

// A flag root could not tell is there, or logind's silence, is no reading:
// an empty one would say nothing waits.
func TestWhatCouldNotBeAskedIsNoReading(t *testing.T) {
	flag := pendingFlag(t)
	for name, answer := range map[string]string{
		"logind did not answer": "",
		"not the property":      `{"type":"s","data":"reboot"}`,
	} {
		t.Run(name, func(t *testing.T) {
			if data, err := ReadReboot(flag, bornAt(written), []byte(answer), readNow); err == nil {
				t.Errorf("read as %s", data)
			}
		})
	}
	directory := t.TempDir()
	if data, err := ReadReboot(directory, bornAt(written), []byte(nothingScheduled), readNow); err == nil {
		t.Errorf("a directory at the flag reads as %s", data)
	}
}

func TestABirthNotToldLeavesSinceEmpty(t *testing.T) {
	unknown := func(string) (time.Time, error) { return time.Time{}, errors.New("no birth time") }

	data, err := ReadReboot(pendingFlag(t), unknown, []byte(nothingScheduled), readNow)
	if err != nil {
		t.Fatal(err)
	}
	var reading RebootReading
	if err := json.Unmarshal(data, &reading); err != nil {
		t.Fatal(err)
	}
	if !reading.Pending || reading.Since != "" {
		t.Errorf("reading = %+v", reading)
	}
}

func TestBornIsWhenTheFileWasMade(t *testing.T) {
	path := filepath.Join(t.TempDir(), "flag")
	before := time.Now().Add(-time.Second)
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	at, err := Born(path)
	if err != nil {
		t.Fatal(err)
	}
	if at.Before(before) || at.After(time.Now().Add(time.Second)) {
		t.Errorf("born %v, written after %v", at, before)
	}
}
