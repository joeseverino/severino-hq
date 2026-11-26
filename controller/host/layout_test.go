package host

import (
	"bufio"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"os"
	"path/filepath"
	"regexp"
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

// graceful is how long HQ's servers let a request in flight finish once asked
// to stop, as hq/config/serve.py declares it.
func graceful(t *testing.T) time.Duration {
	t.Helper()
	source, err := os.ReadFile(filepath.Join("..", "..", "hq", "config", "serve.py"))
	if err != nil {
		t.Fatal(err)
	}
	found := regexp.MustCompile(`(?m)^GRACEFUL_SHUTDOWN_SECONDS = (\d+)$`).FindSubmatch(source)
	if found == nil {
		t.Fatal("hq/config/serve.py declares no GRACEFUL_SHUTDOWN_SECONDS")
	}
	seconds, _ := strconv.Atoi(string(found[1]))
	return time.Duration(seconds) * time.Second
}

func TestTheUnitsKeepTheBoundsABootNests(t *testing.T) {
	for _, check := range []struct {
		what      string
		got, want time.Duration
		exact     bool
	}{
		{"the secrets unit's start timeout is one render", unitSetting(t, SecretsUnit, "TimeoutStartSec"), RenderBound, true},
		// A start first stops a container a killed run left behind.
		{"the web unit waits out a stop, a render and HQ's start", unitSetting(t, hostcore.WebUnit, "TimeoutStartSec"), StopBound + RenderBound + HealthStartPeriod, false},
		{"the web unit lets hq-host's own stop finish", unitSetting(t, hostcore.WebUnit, "TimeoutStopSec"), StopBound, false},
		// Its server's bound, then the bridge's within the lifespan, then a
		// request cut on a thread as the process exits.
		{"HQ is given its three graceful bounds before docker kills it", StopGrace, 3 * graceful(t), true},
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
	doorbell := hostcore.RunDir + "/glance-doorbell"
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

// The glance and listener units are given no more than the controller unit:
// each setting that confines the controller confines them the same way, and
// they write nowhere the controller cannot.
func TestThePartialPassUnitsAreGivenNoMoreThanTheControllers(t *testing.T) {
	systemd := filepath.Join("..", "..", "deploy", "systemd")
	for _, unit := range []string{GlanceUnit, ListenerUnit} {
		for _, key := range []string{"User", "Group", "NoNewPrivileges", "PrivateTmp", "ProtectHome", "ProtectSystem", "WorkingDirectory"} {
			want, _ := effective(t, systemd, ControllerUnit, key)
			if got, _ := effective(t, systemd, unit, key); got != want {
				t.Errorf("%s=%q for %s, %q for the controller", key, got, unit, want)
			}
		}
		// What it may write, hold or reach is what the controller may.
		for _, key := range []string{"ReadWritePaths", "CapabilityBoundingSet", "RestrictAddressFamilies", "SystemCallFilter"} {
			_, wide := effective(t, systemd, ControllerUnit, key)
			_, given := effective(t, systemd, unit, key)
			for _, word := range given {
				if !slices.Contains(wide, word) {
					t.Errorf("%s's %s holds %s, which the controller's does not", unit, key, word)
				}
			}
		}
		// Its container dials out through dockerd's network, never the unit's.
		if net, _ := effective(t, systemd, unit, "PrivateNetwork"); !systemdTrue(net) {
			t.Errorf("%s reaches the host's network (PrivateNetwork=%q)", unit, net)
		}
	}
	if got, bound := unitSetting(t, GlanceUnit, "TimeoutStartSec"), unitSetting(t, ControllerUnit, "TimeoutStartSec"); got >= bound {
		t.Errorf("a glance may run %s, as long as a sweep's %s", got, bound)
	}
}

// The listener holds the socket for as long as HQ runs: started again
// whenever it ends, and with HQ, so it runs the image HQ runs.
func TestTheListenerUnitHoldsTheSocketWhileHQRuns(t *testing.T) {
	service := filepath.Join("..", "..", "deploy", "systemd", ListenerUnit)
	if got := unitValue(t, service, "ExecStart"); got != HostProgram+" controller --events" {
		t.Errorf("the listener unit runs %q", got)
	}
	if got := unitValue(t, service, "Restart"); got != "always" {
		t.Errorf("the listener is started again only on %q", got)
	}
	if got := unitValue(t, service, "PartOf"); got != hostcore.WebUnit {
		t.Errorf("the listener is part of %q, not HQ's unit", got)
	}
}

func TestTheControllerUnitMayWriteWhatAPassPrepares(t *testing.T) {
	writable := strings.Fields(unitValue(t, filepath.Join("..", "..", "deploy", "systemd", ControllerUnit), "ReadWritePaths"))
	for _, path := range []string{ACMEDir, SealDir, filepath.Dir(SealPublicKey)} {
		if !slices.Contains(writable, path) {
			t.Errorf("a pass prepares %s, which its unit may not write", path)
		}
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
	want := fmt.Sprintf("d %s 0700 %d %d -", UnitSocketDir, hostcore.WebUID, hostcore.WebGID)
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
