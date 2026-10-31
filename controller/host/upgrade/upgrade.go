// Package upgrade moves one compose service to an image pinned by digest, the
// way HQ's upgrade plan describes it, and puts everything back if the new
// image does not prove itself.
//
//	hq-host upgrade --operation ID --project-dir STACKS_ROOT/NAME \
//	    --service NAME --from IMAGE --to REPOSITORY@sha256:DIGEST \
//	    [--tag TAG] [--wait SECONDS]
//
// It runs as root through one sudo rule, by the controller on the machine the
// service runs on, for an operation HQ queued and a person approved. The
// caller is not trusted: everything that decides what root does is read from
// the machine, not from the arguments.
//
//   - The stack is a directory directly under StacksRoot, not a symlink, and
//     it, its compose file, any override and any .env are owned by root or by
//     the owner of StacksRoot, never by the account that called sudo, and are
//     writable by nobody else. Compose runs with the file set it would use by
//     default and -p named from that directory.
//   - --from must be the image compose resolves for the service, and what its
//     one running container runs. --to must be the same repository, by digest.
//   - The data is the running container's writable mounts, from Docker.
//
// The steps:
//  0. check what runs, and that the override takes the pin and compose reads
//     it from there, before anything is stopped;
//  1. pull the target by digest;
//  2. stop the service and snapshot its data, so the snapshot is consistent;
//  3. run the target from the service's own compose definition, as
//     `compose run --no-deps`, with no network and no published ports,
//     against a copy of the snapshot, until it proves itself;
//  4. pin the digest in the stack's compose override, keeping everything
//     else in it and a byte-exact copy of it; the compose file itself is
//     never edited;
//  5. recreate that one service and verify it the way the trial was verified;
//  6. keep it, or restore the override and the data and recreate again.
//
// The service is down from step 2 until step 5, or until it is started again
// unchanged when the trial fails. An error or a signal at any point after
// step 2 puts it back the same way, and still records a result.
//
// Idempotent by operation: the result is kept under the state directory, and
// asking again for the same operation prints it instead of running twice.
//
// Needs Docker Compose 2.24.4 or later, for `!reset` in the trial's overlay.
//
// Prints one JSON object. Exits 0 when the upgrade was kept, 3 when it was
// rolled back, 2 when it was refused or stopped before anything changed, 1
// when it failed part way and could not be put back.
package upgrade

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Fixed for anyone reaching this through sudo, which passes none of the
// variables that move them outside it (env_reset, and the rule grants no
// SETENV).
const (
	StacksRoot = "/opt/apps"
	StateRoot  = "/var/lib/severino-hq/upgrades"
	// Poll is how often a container is read while it proves itself.
	Poll = 2 * time.Second
	// Settle is how long a container must keep running, without a restart,
	// to count.
	Settle = 10 * time.Second
	// sudoPath is the PATH a sudo caller's run gets, whatever sudo passed.
	sudoPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

// Exit codes.
const (
	Kept       = 0
	Failed     = 1
	Refused    = 2
	RolledBack = 3
)

// patterns are what each argument must match whole. The sudo rule HQ renders
// admits any arguments, so these, checked as root, are the boundary.
var patterns = map[string]*regexp.Regexp{
	"operation": regexp.MustCompile(`^[A-Za-z0-9-]{1,64}$`),
	"service":   regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$`),
	"from":      regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}$`),
	"to":        regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_./:-]{0,200}@sha256:[0-9a-f]{64}$`),
	"tag":       regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`),
	"wait":      regexp.MustCompile(`^\d{1,4}$`),
	"name":      regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`),
}

var (
	absolute = regexp.MustCompile(`^/[A-Za-z0-9_./-]+$`)
	project  = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	seconds  = map[string]*regexp.Regexp{
		"poll":   regexp.MustCompile(`^\d{1,3}$`),
		"settle": regexp.MustCompile(`^\d{1,4}$`),
	}
)

// Upgrader is where an upgrade acts and how it runs a program. Every field is
// set so a test can relocate the whole machine.
type Upgrader struct {
	StacksRoot, StateRoot string
	Poll, Settle          time.Duration
	// Caller is the uid that asked through sudo; empty for root itself.
	Caller string
	EUID   int
	// Command builds a program's command with the environment root's run
	// keeps.
	Command func(ctx context.Context, name string, args ...string) *exec.Cmd
	Now     func() time.Time
	// Sleep waits d, or reports false when ctx ends first.
	Sleep    func(ctx context.Context, d time.Duration) bool
	Out, Err io.Writer
}

// Request is one upgrade, as asked.
type Request struct {
	Operation, ProjectDir, Service, From, To, Tag string
	Wait                                          time.Duration
}

// refusal is a reason nothing was done, before any state was kept.
type refusal string

func (r refusal) Error() string { return string(r) }

// Main runs one upgrade as `hq-host upgrade` and returns its exit code.
func Main(ctx context.Context, args []string, getenv func(string) string, euid int, stdout, stderr io.Writer) int {
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGHUP, syscall.SIGINT, syscall.SIGTERM)
	// Ignored, not reset, once caught: a second signal must not stop a
	// restore half done.
	defer stop()
	syscall.Umask(0o077)
	upgrader, err := FromEnvironment(getenv, euid, stdout, stderr)
	if err != nil {
		return refuse(stdout, err)
	}
	return upgrader.Upgrade(ctx, args)
}

// FromEnvironment is the upgrader for this process. Through sudo every path
// and bound is fixed and the docker CLI gets root's own environment: a sudo
// that kept the caller's $HOME or $DOCKER_CONFIG would run the caller's
// plugins and credential helpers as root. Outside sudo, as root already or
// in a drill, SEVERINO_UPGRADE_{STACKS,STATE,POLL,SETTLE} move them.
func FromEnvironment(getenv func(string) string, euid int, stdout, stderr io.Writer) (*Upgrader, error) {
	u := &Upgrader{
		StacksRoot: StacksRoot, StateRoot: StateRoot, Poll: Poll, Settle: Settle,
		EUID: euid, Now: time.Now, Sleep: sleep, Out: stdout, Err: stderr,
	}
	env := []string{"LC_ALL=C"}
	if caller := getenv("SUDO_UID"); caller != "" {
		if caller != "0" {
			u.Caller = caller
		}
		env = append(env, "PATH="+sudoPath, "HOME=/root")
	} else {
		for _, name := range []string{"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG"} {
			if value := getenv(name); value != "" {
				env = append(env, name+"="+value)
			}
		}
		u.StacksRoot = cmp.Or(getenv("SEVERINO_UPGRADE_STACKS"), u.StacksRoot)
		u.StateRoot = cmp.Or(getenv("SEVERINO_UPGRADE_STATE"), u.StateRoot)
		for name, bound := range map[string]*time.Duration{"poll": &u.Poll, "settle": &u.Settle} {
			value := getenv("SEVERINO_UPGRADE_" + strings.ToUpper(name))
			if value == "" {
				continue
			}
			if !seconds[name].MatchString(value) {
				return nil, refusal("poll and settle are not numbers")
			}
			count, _ := strconv.Atoi(value)
			*bound = time.Duration(count) * time.Second
		}
	}
	u.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		command := exec.CommandContext(ctx, name, args...)
		command.Env = env
		return command
	}
	return u, nil
}

func sleep(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// Upgrade runs one upgrade and returns its exit code, having printed its
// result.
func (u *Upgrader) Upgrade(ctx context.Context, args []string) int {
	request, err := Parse(args)
	if err != nil {
		return refuse(u.Out, err)
	}
	r, err := u.prepare(request)
	if err != nil {
		return refuse(u.Out, err)
	}
	if code, done := r.recorded(); done {
		return code
	}
	if err := os.MkdirAll(u.StateRoot, 0o700); err != nil {
		return refuse(u.Out, refusal("the state directory could not be made"))
	}
	if err := os.Mkdir(r.state, 0o700); err != nil {
		return refuse(u.Out, refusal("the operation is running, or stopped without a result"))
	}
	r.ctx = ctx
	return r.execute()
}

func refuse(out io.Writer, err error) int {
	var reason refusal
	if !errors.As(err, &reason) {
		reason = refusal(err.Error())
	}
	write(out, struct {
		Outcome string `json:"outcome"`
		Reason  string `json:"reason"`
	}{"refused", string(reason)})
	return Refused
}

func write(out io.Writer, value any) {
	encoder := json.NewEncoder(out)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
}

// Parse reads the arguments: each flag once with a value, nothing else.
func Parse(args []string) (Request, error) {
	options := flag.NewFlagSet("upgrade", flag.ContinueOnError)
	options.SetOutput(io.Discard)
	var request Request
	var wait, data string
	options.StringVar(&request.Operation, "operation", "", "")
	options.StringVar(&request.ProjectDir, "project-dir", "", "")
	options.StringVar(&request.Service, "service", "", "")
	options.StringVar(&request.From, "from", "", "")
	options.StringVar(&request.To, "to", "", "")
	options.StringVar(&request.Tag, "tag", "", "")
	options.StringVar(&wait, "wait", "120", "")
	options.StringVar(&data, "data", "", "")
	if err := options.Parse(args); err != nil {
		if strings.Contains(err.Error(), "needs an argument") {
			return Request{}, refusal("an argument has no value")
		}
		return Request{}, refusal("unknown argument")
	}
	given := false
	options.Visit(func(f *flag.Flag) { given = given || f.Name == "data" })
	switch {
	case given:
		return Request{}, refusal("data is read from the running service, not given")
	case options.NArg() != 0:
		return Request{}, refusal("unknown argument")
	}
	for _, check := range []struct{ name, value, reason string }{
		{"operation", request.Operation, "operation is not an id"},
		{"service", request.Service, "service is not a name"},
		{"from", request.From, "from is not an image reference"},
		{"to", request.To, "to is not pinned by digest"},
		{"wait", wait, "wait is not a number of seconds"},
	} {
		if !patterns[check.name].MatchString(check.value) {
			return Request{}, refusal(check.reason)
		}
	}
	if request.Tag != "" && !patterns["tag"].MatchString(request.Tag) {
		return Request{}, refusal("tag is not a tag")
	}
	if repository(request.To) != repository(request.From) {
		return Request{}, refusal("to is not the repository from names")
	}
	count, _ := strconv.Atoi(wait)
	request.Wait = time.Duration(count) * time.Second
	return request, nil
}

// repository is a reference without its digest, and without a tag after its
// last slash: a registry's port is not a tag.
func repository(ref string) string {
	ref, _, _ = strings.Cut(ref, "@")
	slash := strings.LastIndex(ref, "/")
	if colon := strings.LastIndex(ref, ":"); colon > slash {
		ref = ref[:colon]
	}
	return ref
}

// prepare reads the stack the request names, refusing anything outside it.
func (u *Upgrader) prepare(request Request) (*run, error) {
	dir := request.ProjectDir
	if !absolute.MatchString(u.StacksRoot) {
		return nil, refusal("the stacks root is not an absolute path")
	}
	if canonical := u.StacksRoot + " " + dir; strings.Contains(canonical, "..") || strings.Contains(canonical, "//") ||
		strings.HasSuffix(u.StacksRoot, "/") || strings.HasSuffix(dir, "/") {
		return nil, refusal("a path climbs out or is not canonical")
	}
	if !absolute.MatchString(dir) {
		return nil, refusal("project directory is not an absolute path")
	}
	if filepath.Dir(dir) != u.StacksRoot {
		return nil, refusal("project directory is not directly under " + u.StacksRoot)
	}
	name := filepath.Base(dir)
	if !patterns["name"].MatchString(name) {
		return nil, refusal("project directory is not a stack name")
	}
	projectName := strings.Map(func(c rune) rune {
		if c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_' || c == '-' {
			return c
		}
		return -1
	}, strings.ToLower(name))
	if !project.MatchString(projectName) {
		return nil, refusal("no compose project name follows from the directory")
	}
	if u.EUID != 0 {
		return nil, refusal("must run as root")
	}
	r := &run{u: u, req: request, project: projectName, dir: dir}
	if err := r.guardStack(); err != nil {
		return nil, err
	}
	if !absolute.MatchString(u.StateRoot) {
		return nil, refusal("the state directory is not an absolute path")
	}
	r.state = filepath.Join(u.StateRoot, request.Operation)
	r.trial = "hq-trial-" + request.Operation
	return r, nil
}

// guardStack checks the stacks root, the stack and the file set compose
// uses by default: the first compose file, and the override named after it.
func (r *run) guardStack() error {
	root := r.u.StacksRoot
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() {
		return refusal("the stacks root is not a directory")
	}
	r.trusted = owner(info)
	if r.trusted == r.u.Caller {
		return refusal("the stacks root belongs to the account asking")
	}
	if othersWrite(root, info) {
		return refusal("the stacks root is writable by its group or others")
	}
	if !present(r.dir) {
		return refusal("project directory does not exist")
	}
	if err := r.guarded(r.dir, "the project directory", true); err != nil {
		return err
	}
	for _, candidate := range []string{"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"} {
		if path := filepath.Join(r.dir, candidate); present(path) {
			r.composeFile = path
			break
		}
	}
	if r.composeFile == "" {
		return refusal("no compose file in the project directory")
	}
	if err := r.guarded(r.composeFile, "the compose file", false); err != nil {
		return err
	}
	// Compose loads only the override named after its compose file
	// (compose.yaml, compose.override.yaml). A pin in any other would hold
	// for these calls and vanish on the next plain `docker compose up`.
	base := filepath.Base(r.composeFile)
	extension := filepath.Ext(base)
	r.pinFile = filepath.Join(r.dir, strings.TrimSuffix(base, extension)+".override"+extension)
	for _, candidate := range []string{"compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml"} {
		if path := filepath.Join(r.dir, candidate); path != r.pinFile && present(path) {
			return refusal(candidate + " is not the override compose loads beside " + base)
		}
	}
	if present(r.pinFile) {
		r.override = r.pinFile
		if err := r.guarded(r.override, "the compose override", false); err != nil {
			return err
		}
	}
	if env := filepath.Join(r.dir, ".env"); present(env) {
		return r.guarded(env, "the stack's .env", false)
	}
	return nil
}

// guarded holds a path to root's own or the stacks root owner's, never the
// caller's, and writable by no one else.
func (r *run) guarded(path, what string, directory bool) error {
	info, err := os.Lstat(path)
	if err == nil && info.Mode()&os.ModeSymlink != 0 {
		return refusal(what + " is a symlink")
	}
	if err != nil || info.IsDir() != directory || (!directory && !info.Mode().IsRegular()) {
		return refusal(fmt.Sprintf("%s is not a %s", what, map[bool]string{true: "directory", false: "file"}[directory]))
	}
	if id := owner(info); (id != "0" && id != r.trusted) || id == r.u.Caller {
		return refusal(what + " is not owned by root or the owner of " + r.u.StacksRoot)
	}
	if othersWrite(path, info) {
		return refusal(what + " is writable by its group or others")
	}
	return nil
}

// present is whether anything, a dangling link included, is at path.
func present(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}
