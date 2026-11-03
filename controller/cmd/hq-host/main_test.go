package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/host"
)

func TestARunStagesWhereItsUnitMayWrite(t *testing.T) {
	for _, c := range []struct{ given, want string }{
		{"", "/run"},
		{"/run/severino-hq-verify", "/run/severino-hq-verify"},
		{"/run/first:/run/second", "/run/first"},
	} {
		t.Setenv("RUNTIME_DIRECTORY", c.given)
		if got := runtimeDir(); got != c.want {
			t.Errorf("RUNTIME_DIRECTORY=%q: staged in %q, want %q", c.given, got, c.want)
		}
	}
}

// The deploy rule admits any arguments after `deploy`, so the count is held
// here, before anything runs as root.
func TestADeployTakesExactlyOneImage(t *testing.T) {
	for _, args := range [][]string{{"deploy"}, {"deploy", "a", "--stage", "/tmp"}} {
		var stderr strings.Builder
		if code := run(context.Background(), args, func(string) string { return "" }, 0, strings.NewReader(""), io.Discard, &stderr); code != 2 {
			t.Errorf("%q exited %d, want 2", args, code)
		}
	}
}

// A unit request is answered in JSON before the root check every other
// command makes, so its caller reads a refusal the same way from any account.
func TestAUnitRequestIsAnsweredInJSONAsAnyAccount(t *testing.T) {
	var stdout strings.Builder
	code := run(context.Background(), []string{"unit", "--serve"}, func(string) string { return "" }, 1000, strings.NewReader("stop example.service\n"), &stdout, io.Discard)
	if code != 0 || !strings.Contains(stdout.String(), `"outcome":"refused"`) {
		t.Errorf("exited %d with %q", code, stdout.String())
	}
}

type ended struct {
	error
	outcome host.DeployOutcome
}

func (e ended) Outcome() host.DeployOutcome { return e.outcome }

// The Deploy workflow tells what production runs by this status alone, so a
// deploy's error exits with the status its outcome declares however it is
// wrapped, and every other failure exits 1.
func TestADeployExitsWithItsOutcomesStatus(t *testing.T) {
	for _, outcome := range []host.DeployOutcome{host.DeployLiveCheckFailed, host.DeployRollbackFailed} {
		err := fmt.Errorf("deploy: %w", ended{errors.New("a check failed"), outcome})
		if got := exitStatus(err); got != outcome.Exit || got == 1 || got == 2 {
			t.Errorf("%s exited %d, want a status of its own", outcome.Name, got)
		}
	}
	if got := exitStatus(errors.New("the new image did not become healthy")); got != 1 {
		t.Errorf("a release that did not go live exited %d, want 1", got)
	}
}
