package host

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"
)

// unitSetting is one key of one shipped unit, as systemd would read its span.
func unitSetting(t *testing.T, unit, key string) time.Duration {
	t.Helper()
	file, err := os.Open(filepath.Join("..", "..", "deploy", "systemd", unit))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	lines := bufio.NewScanner(file)
	for lines.Scan() {
		value, ok := strings.CutPrefix(lines.Text(), key+"=")
		if !ok {
			continue
		}
		if seconds, err := strconv.Atoi(value); err == nil {
			return time.Duration(seconds) * time.Second
		}
		span, err := time.ParseDuration(strings.Replace(value, "min", "m", 1))
		if err != nil {
			t.Fatalf("%s %s=%s is not a span this test reads", unit, key, value)
		}
		return span
	}
	if err := lines.Err(); err != nil {
		t.Fatal(err)
	}
	t.Fatalf("%s sets no %s", unit, key)
	return 0
}

func TestTheUnitsKeepTheBoundsABootNests(t *testing.T) {
	stop, _ := strconv.Atoi(StopSeconds)
	for _, check := range []struct {
		what      string
		got, want time.Duration
		exact     bool
	}{
		{"the secrets unit's start timeout is one render", unitSetting(t, SecretsUnit, "TimeoutStartSec"), RenderBound, true},
		{"the web unit waits out a render and HQ's start", unitSetting(t, WebUnit, "TimeoutStartSec"), RenderBound + HealthStartPeriod, false},
		{"the web unit lets HQ's own stop finish", unitSetting(t, WebUnit, "TimeoutStopSec"), time.Duration(stop) * time.Second, false},
	} {
		if check.exact && check.got != check.want || !check.exact && check.got <= check.want {
			t.Errorf("%s: unit says %s, the bound is %s", check.what, check.got, check.want)
		}
	}
}

// The image makes each directory a volume mounts over, owned by the account
// HQ runs as, so a volume first mounted there takes that owner.
func TestTheImageMakesEveryVolumesDirectory(t *testing.T) {
	dockerfile, err := os.ReadFile(filepath.Join("..", "..", "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	var made string
	for line := range strings.SplitSeq(string(dockerfile), "\n") {
		if after, ok := strings.CutPrefix(line, "RUN mkdir -p "); ok {
			made = " " + after
		}
	}
	for _, volume := range Volumes {
		if !strings.Contains(made, " "+volume.Target+" ") {
			t.Errorf("the image does not make %s, where %s is mounted", volume.Target, volume.Name)
		}
	}
}

// The socket, its directory's owner and the program it starts are what the
// launcher and hq-host unit are built on, so the shipped files restate them.
// The glance's doorbell starts a glance pass, and still stands when that pass
// ends if it was rung during it: the pass removes it before reading, and a
// path unit looks again at a file that exists, never at a change, when what it
// started stops.
func TestARefreshDuringAGlanceStartsTheNext(t *testing.T) {
	systemd := filepath.Join("..", "..", "deploy", "systemd")
	path := filepath.Join(systemd, "severino-hq-glance.path")
	doorbell := RunDir + "/glance-doorbell"
	if got := unitValue(t, path, "PathExists"); got != doorbell {
		t.Errorf("the glance path waits on %q, not %s", got, doorbell)
	}
	if got := unitValue(t, path, "PathChanged"); got != "" {
		t.Errorf("the glance path waits on a change to %s, which a pass under way swallows", got)
	}
	if got := unitValue(t, path, "Unit"); got != GlanceUnit {
		t.Errorf("the glance path starts %q", got)
	}
	service := filepath.Join(systemd, GlanceUnit)
	if got := unitValue(t, service, "ExecStartPre"); got != "/bin/rm -f "+doorbell {
		t.Errorf("the glance pass does not remove its doorbell before it reads: %q", got)
	}
	if got := unitValue(t, service, "ExecStart"); got != HostProgram+" controller --glance" {
		t.Errorf("the glance unit runs %q", got)
	}
}

// The glance unit is given no more than the controller unit: each setting
// that confines the controller confines the glance the same way, and the
// glance writes nowhere the controller cannot.
func TestTheGlanceUnitIsGivenNoMoreThanTheControllers(t *testing.T) {
	systemd := filepath.Join("..", "..", "deploy", "systemd")
	controller, glance := filepath.Join(systemd, ControllerUnit), filepath.Join(systemd, GlanceUnit)
	for _, key := range []string{"User", "Group", "NoNewPrivileges", "PrivateTmp", "ProtectHome", "ProtectSystem", "WorkingDirectory"} {
		if want, got := unitValue(t, controller, key), unitValue(t, glance, key); got != want {
			t.Errorf("%s=%q for the glance, %q for the controller", key, got, want)
		}
	}
	writable := strings.Fields(unitValue(t, controller, "ReadWritePaths"))
	for path := range strings.FieldsSeq(unitValue(t, glance, "ReadWritePaths")) {
		if !slices.Contains(writable, path) {
			t.Errorf("the glance may write %s, which the controller may not", path)
		}
	}
	if got, bound := unitSetting(t, GlanceUnit, "TimeoutStartSec"), unitSetting(t, ControllerUnit, "TimeoutStartSec"); got >= bound {
		t.Errorf("a glance may run %s, as long as a sweep's %s", got, bound)
	}
}

func TestTheUnitSocketIsWhereTheLayoutSays(t *testing.T) {
	systemd := filepath.Join("..", "..", "deploy", "systemd")
	if got := unitValue(t, filepath.Join(systemd, "severino-hq-unit.socket"), "ListenStream"); got != UnitSocket {
		t.Errorf("the socket listens on %s, the controller is told %s", got, UnitSocket)
	}
	service := filepath.Join(systemd, "severino-hq-unit@.service")
	if got := unitValue(t, service, "ExecStart"); got != HostProgram+" unit --serve" {
		t.Errorf("a connection starts %q", got)
	}
	if got := unitValue(t, service, "StandardError"); got != "journal" {
		t.Errorf("hq-host's own errors go to %q, where they would reach the answer", got)
	}
	rules, err := os.ReadFile(filepath.Join("..", "..", "deploy", "tmpfiles", "severino-hq.conf"))
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf("d %s 0700 %d %d -", UnitSocketDir, WebUID, WebGID)
	if !slices.Contains(strings.Split(string(rules), "\n"), want) {
		t.Errorf("no tmpfiles rule %q makes the socket's directory the controller's alone", want)
	}
}

func TestTheRenovateUnitOutlastsEveryRunItStarts(t *testing.T) {
	// Every owner's run ends by its token's expiry, which the handoff holds
	// within one token's life, and then its container is removed.
	if got, bound := unitSetting(t, RenovateUnit, "TimeoutStartSec"), TokenLife+clockSkew+time.Minute; got <= bound {
		t.Errorf("the unit stops a run at %s, before its tokens' %s", got, bound)
	}
}
