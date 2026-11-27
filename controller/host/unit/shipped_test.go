package unit

import (
	"context"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/host"
)

// release is a machine whose installed release ships a timer and a service,
// beside the container estate, acting through a fake that records each call.
func release(t *testing.T) (*Runner, *[]string) {
	m := estate(t)
	m.units["example-job.timer"] = "Id=example-job.timer\nLoadState=loaded\n"
	m.units["example-job.service"] = "Id=example-job.service\nLoadState=loaded\n"
	calls := []string{}
	runner := &Runner{
		EUID: 0,
		Command: func(ctx context.Context, name string, args ...string) *exec.Cmd {
			calls = append(calls, strings.Join(append([]string{name}, args...), " "))
			if name == "systemctl" && args[0] == "show" {
				return m.command(ctx, name, args...)
			}
			return answer(ctx, "", 0)
		},
		Shipped: func() ([]string, error) {
			return []string{"example-job.service", "example-job.service.d/limits.conf", "example-job.timer"}, nil
		},
		DeployLock: filepath.Join(t.TempDir(), "deploy.lock"),
	}
	return runner, &calls
}

func TestEachShippedVerbActsOnAUnitTheReleaseShips(t *testing.T) {
	for _, line := range []string{"start example-job.timer", "restart example-job.service", "reset-failed example-job.service", "enable example-job.timer"} {
		runner, calls := release(t)
		if result := runner.Run(context.Background(), line); result.Outcome != Done {
			t.Fatalf("%q: %+v", line, result)
		}
		verb, name, _ := strings.Cut(line, " ")
		acted := (*calls)[len(*calls)-1]
		if !strings.HasPrefix(acted, "systemctl "+verb+" ") || !strings.HasSuffix(acted, " -- "+name) {
			t.Errorf("%q ran %v", line, *calls)
		}
		if slices.ContainsFunc(*calls, func(call string) bool { return strings.HasPrefix(call, "docker") }) {
			t.Errorf("%q read docker: %v", line, *calls)
		}
	}
}

func TestAStartIsQueuedWithoutWaiting(t *testing.T) {
	runner, calls := release(t)
	runner.Run(context.Background(), "start example-job.service")
	if (*calls)[len(*calls)-1] != "systemctl start --no-block -- example-job.service" {
		t.Fatalf("ran %v", *calls)
	}
}

func TestOnlyAUnitTheReleaseShipsTakesTheSecondGrammar(t *testing.T) {
	for _, line := range []string{
		// Not shipped: the container grammar, which takes neither.
		"enable ssh.service",
		"reset-failed hermes.service",
		"start other.timer",
		// Shipped, but a drop-in or a template is no unit to ask for.
		"enable example-job.service.d/limits.conf",
		"start example-job@.service",
		// Shipped, but a verb the second grammar does not take.
		"stop example-job.timer",
	} {
		runner, calls := release(t)
		result := runner.Run(context.Background(), line)
		if result.Outcome != Refused || slices.ContainsFunc(*calls, func(call string) bool {
			return strings.HasPrefix(call, "systemctl ") && !strings.HasPrefix(call, "systemctl show")
		}) {
			t.Errorf("%q: %+v, calls %v", line, result, *calls)
		}
	}
}

func TestAShippedUnitIsRefusedWhileADeployHoldsTheRelease(t *testing.T) {
	runner, calls := release(t)
	release, err := host.HoldDeploys(runner.DeployLock)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	result := runner.Run(context.Background(), "start example-job.timer")
	if result.Outcome != Refused || !strings.Contains(result.Reason, "deploy") || len(*calls) != 0 {
		t.Fatalf("%+v, calls %v", result, *calls)
	}
}
