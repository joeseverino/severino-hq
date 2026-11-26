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

// A service outside the reported set is asked about again when systemd
// restarted it on its own or holds it as failed, as a oneshot instance whose
// run failed is: it was never restarted, so only its state says so.
func TestTroubledIsEachServiceSystemdRestartedOrFailedBeyondTheReportedSet(t *testing.T) {
	shown := []byte("Id=example-upload.service\nNRestarts=4127\nActiveState=active\n\n" +
		"Id=example-steady.service\nNRestarts=0\nActiveState=active\n\n" +
		"Id=severino-hq-web.service\nNRestarts=2\nActiveState=failed\n\n" +
		"Id=example-pull@7ad6909.service\nNRestarts=0\nActiveState=failed\n\n" +
		"Id=example-oneshot.service\nNRestarts=\nActiveState=inactive\n\n" +
		"Id=example-a\\x2db.service\nNRestarts=1\nActiveState=active\n")

	troubled := Troubled(shown, []string{"severino-hq-web.service"})

	if want := []string{"example-a\\x2db.service", "example-pull@7ad6909.service", "example-upload.service"}; !slices.Equal(troubled, want) {
		t.Errorf("troubled %v", troubled)
	}
}

// The services asked about again are the ones the first answer named, and
// with nothing restarting or failed the answer is empty rather than absent, so HQ can
// tell "none" from "systemd did not say".
func TestForeignAsksSystemdForTheStateOfEachTroubledService(t *testing.T) {
	var asked [][]string
	counts := "Id=example-upload.service\nNRestarts=12\nActiveState=active\n"
	machine := &Machine{Command: func(ctx context.Context, _ string, args ...string) *exec.Cmd {
		asked = append(asked, args)
		if slices.Contains(args, "*.service") {
			return exec.CommandContext(ctx, "printf", "%s", counts)
		}
		return exec.CommandContext(ctx, "printf", "%s", "Id=example-upload.service\n")
	}}

	state, err := machine.Foreign(t.Context(), []string{"severino-hq-web.service"})

	if err != nil || string(state) != "Id=example-upload.service\n" {
		t.Fatalf("state %q, %v", state, err)
	}
	if len(asked) != 2 || !slices.Contains(asked[1], "--property="+strings.Join(UnitProperties, ",")) || asked[1][len(asked[1])-1] != "example-upload.service" {
		t.Errorf("asked %v", asked)
	}

	counts = "Id=example-upload.service\nNRestarts=0\nActiveState=inactive\n"
	state, err = machine.Foreign(t.Context(), nil)
	if err != nil || state == nil || len(state) != 0 {
		t.Errorf("nothing troubled answered %q, %v", state, err)
	}
}
