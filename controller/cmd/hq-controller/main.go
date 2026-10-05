// Command hq-controller is the one-shot native controller: apply what is
// queued, sweep when HQ says to, and report. HQ stays the owner of every
// decision and record; this reaches it only through the bridge socket.
//
// "hq-controller job NAME" is the other thing a timer asks of HQ: one piece of
// scheduled work, done by the running process. It reads no connection.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/joeseverino/severino-hq/controller/providers"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := 1
	if len(os.Args) > 1 && os.Args[1] == "job" {
		code = job(ctx, os.Args[2:], os.Getenv(BridgeSocket), os.Stdout)
	} else if env, err := runtime.LoadEnvironment(os.Environ()); err != nil {
		json.NewEncoder(os.Stdout).Encode(failure{Message: err.Error()})
	} else {
		code = run(ctx, os.Args[1:], env, os.Stdout, os.Stderr)
	}
	stop()
	os.Exit(code)
}

// BridgeSocket names the variable holding the path of HQ's bridge socket,
// which the launcher mounts. There is no other way to reach HQ: unset, or not
// a socket this account can trust, the pass fails and the next one tries again.
const BridgeSocket = "SEVERINO_BRIDGE_SOCKET"

// failure is the one line a pass that could not run prints.
type failure struct {
	OK      bool   `json:"ok"`
	Message string `json:"message"`
}

// job asks HQ for one piece of scheduled work and prints how it ended. Work
// that failed or was lost is exit 1, so the unit that asked fails with it;
// work already running is not a failure.
func job(ctx context.Context, args []string, socket string, stdout io.Writer) int {
	fail := func(message string) int {
		json.NewEncoder(stdout).Encode(failure{Message: message})
		return 1
	}
	if len(args) != 1 || args[0] == "" {
		fail("usage: hq-controller job NAME")
		return 2
	}
	bridge, err := runtime.NewSocketBridge(strings.TrimSpace(socket))
	if err != nil {
		return fail(err.Error())
	}
	bridge.Timeout = runtime.JobTimeout
	outcome, err := bridge.Job(ctx, args[0])
	if err != nil {
		return fail(err.Error())
	}
	json.NewEncoder(stdout).Encode(outcome)
	switch outcome.State {
	case runtime.JobStateSucceeded, runtime.JobStateRunning:
		return 0
	}
	return 1
}

func run(ctx context.Context, args []string, env runtime.Environment, stdout, stderr io.Writer) int {
	options := flag.NewFlagSet("hq-controller", flag.ContinueOnError)
	options.SetOutput(stderr)
	controllerID := options.String("controller-id", env.ControllerID(), "the controller this pass reports as")
	apply := options.Bool("apply", false, "claim and execute queued operations; omitted means a preflight-only plan")
	if err := options.Parse(args); err != nil {
		return 2
	}
	log := slog.New(slog.NewTextHandler(stderr, nil))
	slog.SetDefault(log)

	fail := func(err error) int {
		json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	// The bridge carries no credential in either direction: HQ persists, it
	// does not reach providers, and the socket itself is the authorization.
	bridge, err := runtime.NewSocketBridge(strings.TrimSpace(env[BridgeSocket]))
	if err != nil {
		return fail(err)
	}
	declared, err := bridge.Registry(ctx)
	if err != nil {
		if *apply {
			bridge.Steps(ctx, *controllerID, []runtime.StepFailure{})
		}
		return fail(err)
	}
	transport, err := runtime.NewHTTPClient(strings.TrimSpace(env["HQ_CONTROLLER_CA_FILE"]))
	if err != nil {
		return fail(err)
	}
	registry := providers.New(env, transport)
	registry.ControllerID = *controllerID
	registry.Commands.Log = log
	controller := providers.NewController(registry, declared)
	controller.Log = log
	if uncovered := controller.Uncovered(); len(uncovered) > 0 {
		log.Info("actions left for another controller", slog.String("event", "controller.uncovered"),
			slog.String("actions", strings.Join(uncovered, ", ")))
	}
	worker := runtime.Worker{ID: *controllerID, Bridge: bridge, Providers: controller, Output: stdout, Log: log}
	code, err := worker.Run(ctx, *apply)
	if err != nil {
		return fail(err)
	}
	return code
}
