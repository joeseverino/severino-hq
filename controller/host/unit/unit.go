// Package unit stops, starts or restarts the systemd unit that runs a
// container, for a container whose unit would undo the same verb asked of
// Docker, or would start it without what it needs.
//
//	hq-host unit --serve
//
// It runs as root on the container's machine and answers one request read
// from its standard input, the connection its carrier hands it: on the
// machine HQ runs on, severino-hq-unit.socket accepts the controller's
// connection and starts severino-hq-unit@.service with it. The request is
// one line, `VERB NAME`, and the answer one line of JSON (Result).
//
// The caller is not trusted: what decides whether root acts is read from the
// machine, never from the request.
//
//   - The grammar is closed: the verb is stop, start or restart and the name
//     a plain service unit (letters, digits, '_', '.' and '-', ending in
//     .service), one space between, nothing else. No template instance, no
//     path, no option.
//   - A socket's peer is root or the account HQ and its controller run as
//     (hostcore.WebUID); the socket's directory is that account's alone, and only
//     the controller's container mounts it.
//   - At least one container on this machine, running or stopped, carries
//     Label naming the unit, and for each of them the unit's own start
//     commands run docker on that container, read where docker reads a
//     container's name: the container `docker start`, `stop` or `restart`
//     names, the --name `docker run` or `create` gives, or a container
//     `docker compose up`, `create`, `start` or `restart` brings up, named
//     as compose names it from the project's own files (a service's
//     container_name, or project-service-N). No other label is read, and an
//     argument that spells the name anywhere else does not count. A label is set by whoever creates the container, and
//     Portainer's credential can create one; the unit's commands are root's,
//     so a label names no unit that does not already run docker on that
//     container.
//   - The unit is loaded under exactly that name, so an alias cannot stand in
//     for another unit.
//
// A second grammar acts on HQ's own units: the verb is start, restart,
// reset-failed or enable, and the name one of the unit files the installed
// release ships (host.Shipped), read from the tree on each request, never
// from the request. No container check applies, since the release is root's.
// It holds host.DeployLock shared while it acts and refuses when a deploy
// holds it exclusively, so a timer a deploy stopped on purpose is not started
// under it. A start is queued without waiting (--no-block), so the
// controller's own pass can ask for its unit without waiting on itself.
package unit

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Label is the container label naming the systemd unit that runs it. A
// compose file sets it beside the service; the controller reads it with the
// container and hq-host checks it against the unit before acting.
const Label = "severino-hq.unit"

// Name is a plain service unit's name, as the label and the argument must
// spell it.
var Name = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,240}\.service$`)

// Verbs are what may be asked of a unit, as systemctl spells them.
var Verbs = []string{"stop", "start", "restart"}

// ShippedName is the shape of a unit the release ships that may be asked
// for: a plain unit of a type that runs or starts something, no template.
var ShippedName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,240}\.(service|timer|path|socket|mount)$`)

// ShippedVerbs are what may be asked of a unit the release ships.
var ShippedVerbs = []string{"start", "restart", "reset-failed", "enable"}

// Outcomes a Result reports.
const (
	Done    = "done"
	Failed  = "failed"
	Refused = "refused"
)

// RequestLimit bounds one request line, its newline included.
const RequestLimit = 256

// Runner is how hq-host unit reaches docker and systemd. Every field is set
// so a test can stand in for both.
type Runner struct {
	EUID int
	// Command builds a program's command with the environment root's run
	// keeps.
	Command func(ctx context.Context, name string, args ...string) *exec.Cmd
	// Shipped lists the unit files the installed release ships; nil ships
	// none, so only the container grammar is read.
	Shipped func() ([]string, error)
	// DeployLock is the lock a deploy holds while it replaces the release;
	// "" holds none.
	DeployLock string
}

// refusal is a reason nothing was done.
type refusal string

func (r refusal) Error() string { return string(r) }

// Result is the one line a request is answered with.
type Result struct {
	Outcome string `json:"outcome"`
	Verb    string `json:"verb,omitempty"`
	Unit    string `json:"unit,omitempty"`
	Reason  string `json:"reason,omitempty"`
}

// Main serves `hq-host unit --serve`: one request on stdin, its answer on
// stdout. It exits 0 once it has answered, whatever the answer, so a refused
// request does not count as the socket's service failing; 2 when asked for
// anything but --serve.
func Main(ctx context.Context, args []string, euid int, stdin io.Reader, stdout io.Writer) int {
	if !slices.Equal(args, []string{"--serve"}) {
		write(stdout, Result{Outcome: Refused, Reason: "usage: hq-host unit --serve"})
		return 2
	}
	var result Result
	if err := admit(stdin); err != nil {
		result = Result{Outcome: Refused, Reason: err.Error()}
	} else {
		runner := &Runner{EUID: euid, Command: rootCommand, Shipped: func() ([]string, error) {
			return host.Shipped(filepath.Join(host.Lib, "deploy", "systemd"))
		}, DeployLock: host.DeployLock}
		result = runner.Serve(ctx, stdin)
	}
	write(stdout, result)
	return 0
}

// admit refuses a socket peer other than root or the account the controller
// runs as. A carrier that is not a socket, ssh's pipes or a terminal, has
// admitted its caller already.
func admit(stdin io.Reader) error {
	file, ok := stdin.(*os.File)
	if !ok {
		return nil
	}
	info, err := file.Stat()
	if err != nil {
		return refusal("the request's connection could not be read")
	}
	if info.Mode()&os.ModeSocket == 0 {
		return nil
	}
	// The descriptor is read in place: net.FileConn would dup it and set
	// O_NONBLOCK on the open file it shares with stdin, which systemd hands
	// over blocking, so stdin's next read would fail rather than wait.
	raw, err := file.SyscallConn()
	if err != nil {
		return refusal("the request's connection could not be read")
	}
	uid, err := runtime.PeerUIDOf(raw)
	if err != nil {
		return refusal("the request did not come over a local socket")
	}
	if uid != 0 && uid != hostcore.WebUID {
		return refusal("the request came from an account that may not ask")
	}
	return nil
}

// rootCommand gives a program root's own environment, never a caller's.
func rootCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	return hostcore.Process(ctx, hostcore.RootEnvironment(), name, args...)
}

// Serve reads one request line and answers it.
func (r *Runner) Serve(ctx context.Context, in io.Reader) Result {
	line, err := ReadRequest(in)
	if err != nil {
		return Result{Outcome: Refused, Reason: err.Error()}
	}
	return r.Run(ctx, line)
}

// ReadRequest is one request line without its newline, refusing one longer
// than RequestLimit.
func ReadRequest(in io.Reader) (string, error) {
	line, err := bufio.NewReaderSize(io.LimitReader(in, RequestLimit), RequestLimit).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", refusal("the request could not be read")
	}
	if !strings.HasSuffix(line, "\n") && len(line) >= RequestLimit {
		return "", refusal("the request is longer than one can be")
	}
	return strings.TrimSuffix(line, "\n"), nil
}

// Run checks one request against the machine and acts on it.
func (r *Runner) Run(ctx context.Context, request string) Result {
	verb, name, err := Parse(request)
	if err == nil && r.EUID != 0 {
		err = refusal("must run as root")
	}
	args := []string{verb, "--", name}
	if err == nil {
		var shipped bool
		if shipped, err = r.ships(verb, name); err == nil && shipped {
			var release func()
			if release, err = r.unheld(ctx); err == nil {
				defer release()
				err = r.loaded(ctx, name)
			}
			if verb == "start" || verb == "restart" {
				args = []string{verb, "--no-block", "--", name}
			}
		} else if err == nil && (!slices.Contains(Verbs, verb) || !Name.MatchString(name)) {
			err = refusal(verb + " " + name + " is not a unit the release ships, and a container's unit takes only stop, start or restart")
		} else if err == nil {
			err = r.Verify(ctx, name)
		}
	}
	if err != nil {
		return Result{Outcome: Refused, Reason: err.Error()}
	}
	if out, err := r.Command(ctx, "systemctl", args...).CombinedOutput(); err != nil {
		return Result{Outcome: Failed, Verb: verb, Unit: name, Reason: failure(string(out), err)}
	}
	return Result{Outcome: Done, Verb: verb, Unit: name}
}

func write(out io.Writer, result Result) {
	encoder := json.NewEncoder(out)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(result)
}

// Request is the line that asks for verb on name.
func Request(verb, name string) string { return verb + " " + name }

// Parse reads `VERB NAME` and nothing else, in either grammar's words.
func Parse(request string) (verb, name string, err error) {
	verb, name, ok := strings.Cut(request, " ")
	if !ok || strings.Contains(name, " ") {
		return "", "", refusal("a request is a verb and a unit, one space between")
	}
	if !slices.Contains(Verbs, verb) && !slices.Contains(ShippedVerbs, verb) {
		return "", "", refusal("the verb is not stop, start, restart, reset-failed or enable")
	}
	if !Name.MatchString(name) && !ShippedName.MatchString(name) {
		return "", "", refusal("the unit is not a plain unit name")
	}
	return verb, name, nil
}

// ships is whether the request is the second grammar's: a verb it takes on a
// unit the installed release ships. Anything else is the container grammar's,
// which refuses what is not its own.
func (r *Runner) ships(verb, name string) (bool, error) {
	if r.Shipped == nil || !slices.Contains(ShippedVerbs, verb) || !ShippedName.MatchString(name) {
		return false, nil
	}
	shipped, err := r.Shipped()
	if err != nil {
		return false, refusal("the release's units could not be listed")
	}
	return slices.Contains(shipped, name), nil
}

// unheld takes the deploy lock shared, refusing while a deploy holds it.
func (r *Runner) unheld(ctx context.Context) (func(), error) {
	if r.DeployLock == "" {
		return func() {}, nil
	}
	release, err := host.Lock{Path: r.DeployLock}.Shared(ctx)
	if held := (host.LockHeld{}); errors.As(err, &held) {
		return nil, refusal("a deploy is replacing the release; ask again once it ends")
	}
	if err != nil {
		return nil, refusal(err.Error())
	}
	return release, nil
}

// loaded refuses a unit systemd does not hold loaded under exactly that name.
func (r *Runner) loaded(ctx context.Context, name string) error {
	out, err := r.Command(ctx, "systemctl", "show", "--property=Id", "--property=LoadState", "--", name).Output()
	if err != nil {
		return refusal("systemd could not show " + name)
	}
	if !strings.Contains(string(out), "Id="+name+"\n") || !strings.Contains(string(out), "LoadState=loaded\n") {
		return refusal(name + " is not a loaded unit of that name")
	}
	return nil
}

// Verify refuses a unit no container on this machine names, or one whose
// start commands do not run every container that names it.
func (r *Runner) Verify(ctx context.Context, name string) error {
	containers, err := r.labeled(ctx, name)
	if err != nil {
		return err
	}
	if len(containers) == 0 {
		return refusal("no container on this machine names " + name + " in " + Label)
	}
	commands, err := r.startCommands(ctx, name)
	if err != nil {
		return err
	}
	starters := make([]starter, 0, len(commands))
	for _, argv := range commands {
		starters = append(starters, r.starter(ctx, argv))
	}
	for _, container := range containers {
		if !slices.ContainsFunc(starters, func(starts starter) bool { return starts(container) }) {
			return refusal(name + " does not start " + container)
		}
	}
	return nil
}

// labeled is the name of every container, running or stopped, whose label
// names the unit.
func (r *Runner) labeled(ctx context.Context, name string) ([]string, error) {
	out, err := r.Command(ctx, "docker", "container", "ls", "--all", "--no-trunc",
		"--filter", "label="+Label+"="+name, "--format", "{{json .Names}}").Output()
	if err != nil {
		return nil, refusal("docker could not list the containers")
	}
	found := []string{}
	for line := range strings.SplitSeq(strings.TrimSpace(string(out)), "\n") {
		if line == "" {
			continue
		}
		var names string
		if json.Unmarshal([]byte(line), &names) != nil {
			return nil, refusal("docker's container list did not read")
		}
		// A container with several names (a legacy link) is listed once
		// under each, comma-separated; its own is the one without a slash.
		for each := range strings.SplitSeq(names, ",") {
			if !strings.Contains(each, "/") {
				found = append(found, each)
				break
			}
		}
	}
	return found, nil
}

// startCommands are the argument lists of the unit's ExecStartPre and
// ExecStart, as systemd loaded them under exactly this name.
func (r *Runner) startCommands(ctx context.Context, name string) ([][]string, error) {
	out, err := r.Command(ctx, "systemctl", "show", "--property=Id", "--property=LoadState",
		"--property=ExecStartPre", "--property=ExecStart", "--", name).Output()
	if err != nil {
		return nil, refusal("systemd could not show " + name)
	}
	var id, load string
	commands := [][]string{}
	for line := range strings.SplitSeq(string(out), "\n") {
		property, value, _ := strings.Cut(line, "=")
		switch property {
		case "Id":
			id = value
		case "LoadState":
			load = value
		case "ExecStartPre", "ExecStart":
			if argv := execArgv(value); argv != nil {
				commands = append(commands, argv)
			}
		}
	}
	if id != name || load != "loaded" {
		return nil, refusal(name + " is not a loaded unit of that name")
	}
	return commands, nil
}

// execArgv is the argv systemctl show prints for one command:
// `{ path=/usr/bin/docker ; argv[]=/usr/bin/docker start hermes ; ... }`.
func execArgv(value string) []string {
	for field := range strings.SplitSeq(strings.Trim(value, "{} "), " ; ") {
		if argv, ok := strings.CutPrefix(strings.TrimSpace(field), "argv[]="); ok {
			return strings.Fields(argv)
		}
	}
	return nil
}

// failure is why systemctl failed: the last line it printed, else how it
// exited.
func failure(out string, err error) string {
	if line := runtime.LastLine(out); line != "" {
		return line
	}
	if exited, ok := errors.AsType[*exec.ExitError](err); ok {
		return fmt.Sprintf("systemctl exited %d", exited.ExitCode())
	}
	return "systemctl did not run"
}
