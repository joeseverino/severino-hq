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
// "hq-controller events [--reconnected]" tells HQ what its host was told
// happened: one event per line of its input. Input with no events says the
// teller is listening.
//
// "hq-controller waiting PROFILE..." answers which of the playbook worker's
// profiles HQ holds queued work for, asked on the worker's own bridge, so the
// automation host starts only those workers.
//
// "hq-controller pass-due" answers whether an applying pass would find
// anything to do, so the host starts a pass's container only when one would.
//
// "hq-controller listen" holds the inbound-events Worker's socket open, the
// one events connection it is given, and tells HQ each event it hears, until
// it is stopped. It reaches HQ only through the events bridge, which serves
// the events action alone.
//
// "hq-controller ready [unix:PATH] URL" is the image's healthcheck: exit 0
// when URL answers 200, asked on the socket at PATH when one is given, with
// no interpreter started per probe.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
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
	// What a pass prints is its launcher's account of it, so it is struck of
	// every learned secret as its log is.
	stdout := mask.Writer(os.Stdout)
	switch mode {
	case "job":
		code = job(ctx, os.Args[2:], os.Getenv(BridgeSocket), stdout)
	case "events":
		code = events(ctx, os.Args[2:], os.Getenv(EventsBridgeSocket), os.Stdin, stdout, os.Stderr)
	case "waiting":
		code = waiting(ctx, os.Args[2:], os.Getenv(AutomationBridgeSocket), stdout)
	case "pass-due":
		code = passDue(ctx, os.Args[2:], os.Getenv(BridgeSocket), stdout)
	case "listen":
		code = listen(ctx, os.Args[2:], stdout)
	case "ready":
		code = ready(ctx, os.Args[2:], stdout)
	default:
		if env, supplied, err := load(os.Environ()); err != nil {
			_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		} else {
			code = run(ctx, os.Args[1:], env, supplied, stdout, os.Stderr)
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

// EventsBridgeSocket names the variable holding the path of the bridge socket
// that serves the events action alone, which `events` and `listen` reach HQ
// through.
const EventsBridgeSocket = "SEVERINO_EVENTS_BRIDGE_SOCKET"

// AutomationBridgeSocket names the variable holding the path of the playbook
// worker's bridge socket, which `waiting` reaches HQ through.
const AutomationBridgeSocket = "SEVERINO_AUTOMATION_BRIDGE_SOCKET"

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

// waiting prints which of the profiles named HQ holds queued work for, as the
// bridge's Waiting answer.
func waiting(ctx context.Context, args []string, socket string, stdout io.Writer) int {
	if len(args) == 0 || slices.Contains(args, "") {
		_ = json.NewEncoder(stdout).Encode(failure{Message: "usage: hq-controller waiting PROFILE..."})
		return 2
	}
	return asked(ctx, socket, stdout, func(ctx context.Context, bridge *runtime.SocketBridge) (any, error) {
		profiles, err := bridge.Waiting(ctx, args)
		return runtime.Waiting{OK: true, Profiles: profiles}, err
	})
}

// passDue prints whether a controller pass is due, as the bridge's PassDue answer.
func passDue(ctx context.Context, args []string, socket string, stdout io.Writer) int {
	if len(args) != 0 {
		_ = json.NewEncoder(stdout).Encode(failure{Message: "usage: hq-controller pass-due"})
		return 2
	}
	return asked(ctx, socket, stdout, func(ctx context.Context, bridge *runtime.SocketBridge) (any, error) {
		return bridge.PassDue(ctx)
	})
}

// asked prints the answer of one call on the bridge at socket, or why there
// was none, exit 1.
func asked(ctx context.Context, socket string, stdout io.Writer, call func(context.Context, *runtime.SocketBridge) (any, error)) int {
	bridge, err := runtime.NewSocketBridge(strings.TrimSpace(socket))
	var answer any
	if err == nil {
		answer, err = call(ctx, bridge)
	}
	if err != nil {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	_ = json.NewEncoder(stdout).Encode(answer)
	return 0
}

// events posts the events of its input, one InboundArrival per line, as HQ's
// host was told of them, in batches of the contract's bound. A line the
// contract refuses is dropped alone, with a line on stderr. --reconnected
// says the teller has just begun reading, so HQ reads again what it may have
// missed; input with no events says it is listening. A batch HQ refuses ends
// it with exit 1.
func events(ctx context.Context, args []string, socket string, stdin io.Reader, stdout, stderr io.Writer) int {
	fail := func(message string) int {
		_ = json.NewEncoder(stdout).Encode(failure{Message: message})
		return 1
	}
	options := flag.NewFlagSet("events", flag.ContinueOnError)
	options.SetOutput(io.Discard)
	reconnected := options.Bool("reconnected", false, "the teller has just begun reading")
	if options.Parse(args) != nil || options.NArg() != 0 {
		fail("usage: hq-controller events [--reconnected]")
		return 2
	}
	bridge, err := runtime.NewSocketBridge(strings.TrimSpace(socket))
	if err != nil {
		return fail(err.Error())
	}
	log := mask.Logger(stderr)
	read := []runtime.InboundArrival{}
	reader := bufio.NewReader(stdin)
	for line := 1; ; line++ {
		text, readErr := reader.ReadBytes('\n')
		if text = bytes.TrimSpace(text); len(text) > 0 {
			if event, err := runtime.ReadArrival(text); err != nil {
				log.Warn("event dropped", slog.String("event", "events.unreadable"), slog.Int("line", line), slog.Any("error", err))
			} else {
				read = append(read, event)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return fail(readErr.Error())
		}
	}
	batches := slices.Collect(slices.Chunk(read, runtime.MaxInboundEvents))
	if len(batches) == 0 {
		batches = [][]runtime.InboundArrival{read}
	}
	for i, batch := range batches {
		if err := bridge.Events(ctx, runtime.InboundBatch{Source: runtime.InboundSourceHost, Events: batch, Reconnected: *reconnected && i == 0}); err != nil {
			return fail(err.Error())
		}
	}
	_ = json.NewEncoder(stdout).Encode(struct {
		OK     bool `json:"ok"`
		Posted int  `json:"posted"`
	}{true, len(read)})
	return 0
}

// listen runs the listener on the events connection the launcher lent, until
// ctx ends. One that cannot start is exit 1, so its unit starts it again.
func listen(ctx context.Context, args []string, stdout io.Writer) int {
	fail := func(err error) int {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	if len(args) != 0 {
		fail(errors.New("usage: hq-controller listen"))
		return 2
	}
	env, supplied, err := load(os.Environ())
	if err != nil {
		return fail(err)
	}
	connection, err := supplied.For(runtime.ConnectionProviderCloudflareEvents, "")
	if err != nil {
		return fail(err)
	}
	bridge, err := runtime.NewSocketBridge(env.EventsBridgeSocket)
	if err != nil {
		return fail(err)
	}
	transport, err := runtime.NewHTTPClient(env.CAFile)
	if err != nil {
		return fail(err)
	}
	client := &http.Client{Transport: transport.Transport, Timeout: transport.Timeout}
	listener, err := runtime.NewListener(connection, client, bridge.Events, mask.Logger(os.Stderr))
	if err != nil {
		return fail(err)
	}
	listener.Run(ctx)
	return 0
}

// readyTimeout is under the healthcheck's own timeout, so a slow answer is
// reported here as not ready rather than killed by Docker.
const readyTimeout = 3 * time.Second

// ready is exit 0 when the address answers 200 itself. A redirect is not
// followed, no proxy is consulted, and the body is not read. Given a
// unix:PATH first, the request goes to the socket at PATH, whatever host the
// URL names.
func ready(ctx context.Context, args []string, stdout io.Writer) int {
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true}
	if len(args) == 2 {
		socket, ok := strings.CutPrefix(args[0], "unix:")
		if !ok || socket == "" {
			args = nil
		} else {
			args = args[1:]
			transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", socket)
			}
		}
	}
	if len(args) != 1 || args[0] == "" {
		_ = json.NewEncoder(stdout).Encode(failure{Message: "usage: hq-controller ready [unix:PATH] URL"})
		return 2
	}
	ctx, cancel := context.WithTimeout(ctx, readyTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, args[0], http.NoBody) //nolint:gosec // G704: the URL is the image's own HEALTHCHECK command line, a loopback address the Dockerfile fixes
	if err != nil {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 2
	}
	client := &http.Client{
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	response, err := client.Do(request) //nolint:gosec // G704: the request built above, to the healthcheck's own loopback URL
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
	log := mask.Logger(stderr)
	slog.SetDefault(log)

	fail := func(err error) int {
		_ = json.NewEncoder(stdout).Encode(failure{Message: err.Error()})
		return 1
	}
	// The bridge carries no credential in either direction, and a secret
	// only sealed to this controller: HQ persists, it does not reach
	// providers, and the socket itself is the authorization.
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
	// Keys that cannot be read refuse each action that needs one, with the
	// reason, and leave the rest of the pass to run.
	if opener, err := runtime.NewOpener(env.SealKeyDir); err != nil {
		log.Warn("sealing keys unreadable", slog.String("event", "controller.seal_keys"), slog.Any("error", err))
		worker.Opener = runtime.Unopenable{Err: err}
	} else {
		worker.Opener = opener
	}
	var code int
	if *glance {
		code, err = worker.Glances(ctx)
	} else {
		if *apply {
			// Before the queue and the sweep, so a token root is waiting on is
			// not held up by them; a grant that fails is a step of this pass.
			registry.HandOffGrants(ctx)
		}
		code, err = worker.Run(ctx, *apply)
	}
	if err != nil {
		return fail(err)
	}
	return code
}
