package host

import (
	"bufio"
	"os"
	"path/filepath"
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
	for _, line := range strings.Split(string(dockerfile), "\n") {
		if strings.HasPrefix(line, "RUN mkdir -p ") {
			made = " " + strings.TrimPrefix(line, "RUN mkdir -p ")
		}
	}
	for _, volume := range Volumes {
		if !strings.Contains(made, " "+volume.Target+" ") {
			t.Errorf("the image does not make %s, where %s is mounted", volume.Target, volume.Name)
		}
	}
}
