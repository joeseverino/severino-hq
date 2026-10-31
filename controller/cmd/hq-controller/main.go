// Command hq-controller is the one-shot native controller: apply what is
// queued, sweep when HQ says to, and report. HQ stays the owner of every
// decision and record; this reaches it only through the bridge socket.
//
// "hq-controller --glance" is a pass of its own beside that one: it reads
// only the dashboard panels HQ is waiting on, so a refresh never waits for a
// sweep.
//
// "hq-controller job NAME" is the other thing a timer asks of HQ: one piece of
// scheduled work, done by the running process. It reads no connection.
//
// "hq-controller ready URL" is the image's healthcheck: exit 0 when URL
// answers 200, with no interpreter started per probe.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := 1
	mode := ""
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	switch mode {
	case "job":
		code = job(ctx, os.Args[2:], os.Getenv(BridgeSocket), os.Stdout)
	case "ready":
		code = ready(ctx, os.Args[2:], os.Stdout)
	default:
		if env, supplied, err := load(os.Environ()); err != nil {
			_ = json.NewEncoder(os.Stdout).Encode(failure{Message: err.Error()})
		} else {
			code = run(ctx, os.Args[1:], env, supplied, os.Stdout, os.Stderr)
		}
	}
	stop()
	os.Exit(code)
}

// load is what a pass is told: the facts in its environment, and the
// connections in the document the environment names.
func load(entries []string) (runtime.Environment, runtime.Connections, error) {
	env, err := runtime.ReadEnvironment(entries)
	if err != nil {
		return runtime.Environment{}, runtime.Connections{}, err
	}
	supplied, err := runtime.LoadConnections(env)
	return env, supplied, err
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
		_ = json.NewEncoder(stdout).Encode(failure{Message: message})
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
	_ = json.NewEncoder(stdout).Encode(outcome)
	switch outcome.State {
	case runtime.JobStateSucceeded, runtime.JobStateRunning:
		return 0
	}
	return 1
}

// readyTimeout is under the healthcheck's own timeout, so a slow answer is
// reported here as not ready rather than killed by Docker.
const readyTimeout = 3 * time.Second

// ready is exit 0 when the address answers 200 itself. A redirect is not
// followed, no proxy is consulted, and the body is not read.
func ready(ctx context.Context, args []string, stdout io.Writer) int {
	if len(args) != 1 || args[0] == "" {
		_ = json.NewEncoder(stdout).Encode(failure{Message: "usage: hq-controller ready URL"})
		return 2
	}
	ctx, cancel := context.WithTimeout(ctx, readyTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, args[0], http.NoBody)
	if err != nil {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 2
	}
	client := &http.Client{
		Transport:     &http.Transport{Proxy: nil, DisableKeepAlives: true},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	response, err := client.Do(request)
	if err != nil {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		_ = json.NewEncoder(stdout).Encode(failure{Message: response.Status})
		return 1
	}
	return 0
}

func run(ctx context.Context, args []string, env runtime.Environment, supplied runtime.Connections, stdout, stderr io.Writer) int {
	options := flag.NewFlagSet("hq-controller", flag.ContinueOnError)
	options.SetOutput(stderr)
	controllerID := options.String("controller-id", env.ControllerID(), "the controller this pass reports as")
	apply := options.Bool("apply", false, "claim and execute queued operations; omitted means a preflight-only plan")
	glance := options.Bool("glance", false, "read only the dashboard panels HQ is waiting on")
	if err := options.Parse(args); err != nil {
		return 2
	}
	if *apply && *glance {
		fmt.Fprintln(stderr, "--apply and --glance are separate passes")
		return 2
	}
	log := slog.New(slog.NewTextHandler(stderr, nil))
	slog.SetDefault(log)

	fail := func(err error) int {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	// The bridge carries no credential in either direction: HQ persists, it
	// does not reach providers, and the socket itself is the authorization.
	bridge, err := runtime.NewSocketBridge(env.BridgeSocket)
	if err != nil {
		return fail(err)
	}
	declared, err := bridge.Registry(ctx)
	if err != nil {
		if *apply {
			_ = bridge.Steps(ctx, *controllerID, []runtime.StepFailure{})
		}
		return fail(err)
	}
	transport, err := runtime.NewHTTPClient(env.CAFile)
	if err != nil {
		return fail(err)
	}
	registry := providers.New(env, supplied, transport)
	registry.ControllerID = *controllerID
	registry.Commands.Log = log
	controller := providers.NewController(registry, declared)
	controller.Log = log
	if uncovered := controller.Uncovered(); len(uncovered) > 0 && !*glance {
		log.Info("actions left for another controller", slog.String("event", "controller.uncovered"),
			slog.String("actions", strings.Join(uncovered, ", ")))
	}
	worker := runtime.Worker{ID: *controllerID, Bridge: bridge, Providers: controller, Output: stdout, Log: log}
	var code int
	if *glance {
		code, err = worker.Glances(ctx)
	} else {
		code, err = worker.Run(ctx, *apply)
	}
	if err != nil {
		return fail(err)
	}
	return code
}
