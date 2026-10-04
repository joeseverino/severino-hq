// Command hq-controller is the one-shot native controller: apply what is
// queued, sweep when HQ says to, and report. HQ stays the owner of every
// decision and record; this reaches it only through the bridge command.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"

	"github.com/joeseverino/severino-hq/controller/providers"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], runtime.ParseEnvironment(os.Environ()), os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

// bridgeCommand is how this controller reaches HQ: the bridge in HQ's container
// through docker exec, or, with HQ_IN_PROCESS=1, a manage.py on this machine.
func bridgeCommand(env runtime.Environment) ([]string, error) {
	if env["HQ_IN_PROCESS"] == "1" {
		manage := strings.TrimSpace(env["HQ_MANAGE_PY"])
		if manage == "" {
			return nil, &runtime.BridgeError{Message: "HQ_MANAGE_PY must name manage.py when HQ_IN_PROCESS is set"}
		}
		python := strings.TrimSpace(env["HQ_PYTHON"])
		if python == "" {
			python = "python3"
		}
		return []string{python, manage, "infrastructure_controller"}, nil
	}
	docker := strings.TrimSpace(env["HQ_DOCKER_BIN"])
	if docker == "" {
		found, err := exec.LookPath("docker")
		if err != nil {
			return nil, &runtime.BridgeError{Message: "docker CLI not found"}
		}
		docker = found
	}
	container := strings.TrimSpace(env["HQ_CONTAINER"])
	if container == "" {
		container = "severino-hq"
	}
	return []string{docker, "exec", "-i", container, "python", "manage.py", "infrastructure_controller"}, nil
}

// failure is the one line a pass that could not run prints.
type failure struct {
	OK      bool   `json:"ok"`
	Message string `json:"message"`
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
	prefix, err := bridgeCommand(env)
	if err != nil {
		return fail(err)
	}
	// HQ's process gets no connection's credentials: it persists, it does not reach providers.
	bridge := runtime.CommandBridge{Prefix: prefix, Env: env.WithoutConnections()}
	var declared runtime.ControllerRegistry
	if err := bridge.Call(ctx, []string{"registry"}, nil, &declared); err != nil {
		if *apply {
			bridge.Call(ctx, []string{"steps", "--controller-id", *controllerID}, []runtime.StepFailure{}, nil)
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
