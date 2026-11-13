package host

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// An instance name with no unit type starts no template, and reading it
// leaves the rest of the report intact.
func TestReportedPassesOverAnInstanceWithNoUnitType(t *testing.T) {
	dir := t.TempDir()
	files := map[string]string{
		"job@.service": "",
		"a.timer":      "[Timer]\nUnit=job@two\n",
		"b.timer":      "[Timer]\nUnit=job@one.service\n",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	shipped, _ := Shipped(dir)

	reported, err := Reported(dir, shipped)

	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"a.timer", "b.timer", "job@one.service"}; !slices.Equal(reported, want) {
		t.Errorf("reported %v", reported)
	}
}

// systemd answers a glob with every loaded service, each with its count of
// restarts it made on its own; only a service it restarted, and that the
// reported set does not already ask about, is asked about again.
func TestRestartedIsEachServiceSystemdRestartedBeyondTheReportedSet(t *testing.T) {
	shown := []byte("Id=example-upload.service\nNRestarts=4127\n\n" +
		"Id=example-steady.service\nNRestarts=0\n\n" +
		"Id=severino-hq-web.service\nNRestarts=2\n\n" +
		"Id=example-oneshot.service\nNRestarts=\n\n" +
		"Id=example-a\\x2db.service\nNRestarts=1\n")

	restarted := Restarted(shown, []string{"severino-hq-web.service"})

	if want := []string{"example-a\\x2db.service", "example-upload.service"}; !slices.Equal(restarted, want) {
		t.Errorf("restarted %v", restarted)
	}
}

// The services asked about again are the ones the first answer named, and
// with nothing restarting the answer is empty rather than absent, so HQ can
// tell "none" from "systemd did not say".
func TestRestartingAsksSystemdForTheStateOfEachRestartedService(t *testing.T) {
	var asked [][]string
	counts := "Id=example-upload.service\nNRestarts=12\n"
	machine := &Machine{Command: func(ctx context.Context, _ string, args ...string) *exec.Cmd {
		asked = append(asked, args)
		if slices.Contains(args, "*.service") {
			return exec.CommandContext(ctx, "printf", "%s", counts)
		}
		return exec.CommandContext(ctx, "printf", "%s", "Id=example-upload.service\n")
	}}

	state, err := machine.Restarting(t.Context(), []string{"severino-hq-web.service"})

	if err != nil || string(state) != "Id=example-upload.service\n" {
		t.Fatalf("state %q, %v", state, err)
	}
	if len(asked) != 2 || !slices.Contains(asked[1], "--property="+strings.Join(UnitProperties, ",")) || asked[1][len(asked[1])-1] != "example-upload.service" {
		t.Errorf("asked %v", asked)
	}

	counts = "Id=example-upload.service\nNRestarts=0\n"
	state, err = machine.Restarting(t.Context(), nil)
	if err != nil || state == nil || len(state) != 0 {
		t.Errorf("nothing restarting answered %q, %v", state, err)
	}
}
