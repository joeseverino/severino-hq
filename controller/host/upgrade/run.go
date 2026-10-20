package upgrade

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
)

// phase is how far a run got, which decides how it is put back.
type phase int

const (
	checking phase = iota // nothing is changed
	stopped               // the service is stopped; nothing is pinned
	pinned                // the override holds the pin
)

// Step is one line of the result.
type Step struct {
	Step   string `json:"step"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail"`
}

// Result is what a run prints and keeps.
type Result struct {
	Operation string `json:"operation"`
	Outcome   string `json:"outcome"`
	Service   string `json:"service"`
	Image     string `json:"image"`
	Steps     []Step `json:"steps"`
}

// mount is one writable mount the run snapshots: a volume, or a directory or
// file inside the stack.
type mount struct {
	index              int
	kind, ref, mounted string
}

func (m mount) snapshot(state string) string {
	if m.kind == "file" {
		return filepath.Join(state, fmt.Sprintf("data-%d.file", m.index))
	}
	return filepath.Join(state, fmt.Sprintf("data-%d.tgz", m.index))
}

type run struct {
	u   *Upgrader
	ctx context.Context
	req Request
	// trusted is the stacks root's owner.
	trusted, project, dir, composeFile string
	// override is the override compose loads now, if any; pinFile is where
	// the pin goes, whether or not it exists yet.
	override, pinFile, trialFile string
	state, trial                 string
	data                         []mount
	steps                        []Step
	phase                        phase
}

var containerID = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

var (
	mountPath  = regexp.MustCompile(`^/[A-Za-z0-9_./@+-]*$`)
	volumeName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$`)
)

func (r *run) step(name string, ok bool, detail string) {
	r.steps = append(r.steps, Step{name, ok, detail})
}

// recorded prints the result of an operation that already ran.
func (r *run) recorded() (int, bool) {
	result, err := os.ReadFile(filepath.Join(r.state, "result.json"))
	if err != nil {
		return 0, false
	}
	r.u.Out.Write(result)
	code, err := os.ReadFile(filepath.Join(r.state, "exit"))
	if exit, parseErr := strconv.Atoi(strings.TrimSpace(string(code))); err == nil && parseErr == nil {
		return exit, true
	}
	return Failed, true
}

// command runs to completion even after a signal: a signal is acted on
// between steps, as a shell's trap is, so nothing is cut off half done.
func (r *run) command(name string, args ...string) *exec.Cmd {
	return r.u.Command(context.WithoutCancel(r.ctx), name, args...)
}

// compose is docker compose with the stack's file set, and the trial's on
// top when one is set.
func (r *run) compose(override string, args ...string) *exec.Cmd {
	files := []string{"compose", "-p", r.project, "--project-directory", r.dir, "-f", r.composeFile}
	if override != "" {
		files = append(files, "-f", override)
	}
	if r.trialFile != "" {
		files = append(files, "-f", r.trialFile)
	}
	return r.command("docker", append(files, args...)...)
}

func quiet(command *exec.Cmd) bool { return command.Run() == nil }

// loud runs a command whose errors are worth the operator's log.
func (r *run) loud(command *exec.Cmd) bool {
	command.Stderr = r.u.Err
	return command.Run() == nil
}

func (r *run) interrupted() bool { return r.ctx.Err() != nil }

// serviceImage is the image compose resolves for the service with override
// standing in for the stack's own.
func (r *run) serviceImage(override string) string {
	out, err := r.compose(override, "config", "--format", "json").Output()
	if err != nil {
		return ""
	}
	var config struct {
		Services map[string]struct {
			Image string `json:"image"`
		} `json:"services"`
	}
	if json.Unmarshal(out, &config) != nil {
		return ""
	}
	return config.Services[r.req.Service].Image
}

// onlyContainer is the one container compose runs for the service; more
// than one is refused.
func (r *run) onlyContainer() (string, bool) {
	out, err := r.compose(r.override, "ps", "-q", r.req.Service).Output()
	id := strings.TrimRight(string(out), "\n")
	if err != nil || !containerID.MatchString(id) {
		return "", false
	}
	return id, true
}

// dataPath is where one mount lives on this machine.
func (r *run) dataPath(kind, ref string) string {
	if kind != "volume" {
		return ref
	}
	out, err := r.command("docker", "volume", "inspect", "-f", "{{.Mountpoint}}", ref).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func (r *run) tar(args ...string) error {
	var refusal bytes.Buffer
	command := r.command("tar", args...)
	command.Stderr = &refusal
	if err := command.Run(); err != nil {
		if strings.Contains(strings.ToLower(refusal.String()), "no space left") {
			return fmt.Errorf("%w: %s", syscall.ENOSPC, refusal.String())
		}
		return fmt.Errorf("%w: %s", err, refusal.String())
	}
	return nil
}

// proves is whether a container keeps running, healthy or with no health
// check and not restarted, for Settle across at least two readings, within
// the wait. A restart starts the window again, so a crash loop never
// finishes it.
func (r *run) proves(container string) bool {
	deadline := r.u.Now().Add(r.req.Wait)
	var since, last = r.u.Now(), ""
	settling := false
	for {
		out, err := r.command("docker", "inspect", "-f",
			"{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.StartedAt}}",
			container).Output()
		seen := strings.TrimSpace(string(out))
		if err != nil {
			seen = "gone gone"
		}
		now := r.u.Now()
		status, rest, _ := strings.Cut(seen, " ")
		health, started, _ := strings.Cut(rest, " ")
		switch {
		case status == "running" && (health == "healthy" || health == "none"):
			if settling && started == last {
				if now.Sub(since) >= r.u.Settle {
					return true
				}
			} else {
				since, last, settling = now, started, true
			}
		case status == "running" && health == "starting", status == "restarting", status == "created":
			settling = false
		default:
			return false
		}
		if !now.Before(deadline) || !r.u.Sleep(r.ctx, r.u.Poll) {
			return false
		}
	}
}

// readData records the container's writable mounts: volumes, and
// directories and files inside the stack. A socket is a channel, not data.
// Anything writable outside the stack is refused: nothing here may snapshot
// or restore it.
func (r *run) readData(container string) bool {
	out, err := r.command("docker", "inspect", "-f", "{{json .Mounts}}", container).Output()
	var mounts []struct {
		Type, Name, Source, Destination string
		RW                              bool
	}
	if err != nil || json.Unmarshal(out, &mounts) != nil {
		r.step("check", false, "the container's mounts could not be read")
		return false
	}
	for _, m := range mounts {
		if m.Type == "" || !m.RW || m.Type == "tmpfs" {
			continue
		}
		if !mountPath.MatchString(m.Destination) {
			r.step("check", false, "a mount has an unreadable destination")
			return false
		}
		next := mount{index: len(r.data), mounted: m.Destination}
		switch m.Type {
		case "volume":
			if !volumeName.MatchString(m.Name) {
				r.step("check", false, "a volume has an unreadable name")
				return false
			}
			next.kind, next.ref = "volume", m.Name
		case "bind":
			if !mountPath.MatchString(m.Source) {
				r.step("check", false, "a mount has an unreadable source")
				return false
			}
			if !unmoved(m.Source) {
				r.step("check", false, "a mount at "+m.Destination+" is reached through a symlink")
				return false
			}
			info, err := os.Stat(m.Source)
			switch {
			case err == nil && info.Mode().IsRegular():
				next.kind = "file"
			case err == nil && info.IsDir():
				next.kind = "bind"
			default:
				continue
			}
			inside, ok := strings.CutPrefix(m.Source+"/", r.dir+"/")
			switch {
			case strings.Contains(m.Source, ".."):
				r.step("check", false, "a mount source climbs out")
				return false
			case !ok || inside == "":
				r.step("check", false, "a writable path outside the stack is mounted at "+m.Destination)
				return false
			}
			next.ref = m.Source
		default:
			r.step("check", false, "a mount of an unknown kind")
			return false
		}
		r.data = append(r.data, next)
	}
	return true
}

// execute is steps 0 to 6.
func (r *run) execute() int {
	// 0. What runs today, from the machine.
	if r.serviceImage(r.override) != r.req.From {
		r.step("check", false, "compose does not resolve "+r.req.Service+" to "+r.req.From)
		return r.finish("refused", Refused)
	}
	live, ok := r.onlyContainer()
	if !ok {
		r.step("check", false, r.req.Service+" is not exactly one running container")
		return r.finish("refused", Refused)
	}
	if out, _ := r.command("docker", "inspect", "-f", "{{.Config.Image}}", live).Output(); strings.TrimSpace(string(out)) != r.req.From {
		r.step("check", false, "the running container is not "+r.req.From)
		return r.finish("refused", Refused)
	}
	if !r.readData(live) {
		return r.finish("refused", Refused)
	}
	// The pin is written and proved here, while the service still runs: an
	// override this cannot edit, or one compose does not read the pin from,
	// is refused with nothing stopped.
	pinnedFile, err := r.writePinned()
	if err != nil {
		return r.fault(err)
	}
	if pinnedFile == "" {
		r.step("check", false, "the compose override is not block style YAML this can edit; nothing changed")
		return r.finish("refused", Refused)
	}
	if r.serviceImage(pinnedFile) != r.req.To {
		r.step("check", false, "compose would not resolve the pin from the override; nothing changed")
		return r.finish("refused", Refused)
	}
	r.step("check", true, fmt.Sprintf("%s runs %s, with %d data mount(s)", r.req.Service, r.req.From, len(r.data)))
	if r.interrupted() {
		return r.stopped()
	}

	// 1. Pull by digest.
	pull := r.command("docker", "pull", "-q", r.req.To)
	if !r.loud(pull) {
		r.step("pull", false, r.req.To)
		return r.finish("refused", Refused)
	}
	r.step("pull", true, r.req.To)
	if r.interrupted() {
		return r.stopped()
	}

	// 2. Stop, and snapshot each mount to a temporary name, renamed only when
	// whole.
	r.phase = stopped
	if !r.loud(r.compose(r.override, "stop", r.req.Service)) {
		r.step("stop", false, "the service did not stop")
		return r.unchanged()
	}
	for _, m := range r.data {
		if !r.snapshot(m) {
			r.step("snapshot", false, fmt.Sprintf("data-%d could not be snapshotted; nothing changed", m.index))
			return r.unchanged()
		}
	}
	r.step("snapshot", true, fmt.Sprintf("%d data mount(s)", len(r.data)))
	if r.interrupted() {
		return r.stopped()
	}

	// 3. Trial: the service's own definition, the target image, copies of
	// the data, no network and no published ports.
	if code, failed := r.runTrial(); failed {
		return code
	}
	if r.interrupted() {
		return r.stopped()
	}

	// 4. Pin in the override, keeping a byte-exact copy and every other line.
	if r.override != "" {
		if err := copyFile(r.override, filepath.Join(r.state, "override.before")); err != nil {
			return r.fault(err)
		}
	}
	staged := r.pinFile + ".hq-pin"
	if err := copyFile(pinnedFile, staged); err != nil {
		os.Remove(staged)
		return r.fault(err)
	}
	r.phase = pinned
	if err := os.Rename(staged, r.pinFile); err != nil {
		return r.fault(err)
	}
	r.override = r.pinFile
	if r.serviceImage(r.override) != r.req.To {
		r.step("pin", false, "compose does not resolve the pin")
		return r.rollBack()
	}
	detail := filepath.Base(r.pinFile)
	if r.req.Tag != "" {
		detail += ", " + r.req.Tag
	}
	r.step("pin", true, detail)
	if r.interrupted() {
		return r.stopped()
	}

	// 5. Recreate, and verify.
	up := quiet(r.compose(r.override, "up", "-d", "--no-deps", r.req.Service))
	if r.interrupted() {
		return r.stopped()
	}
	if up {
		if upgraded, ok := r.onlyContainer(); ok && r.proves(upgraded) {
			r.step("verify", true, "running and proven")
			return r.finish("kept", Kept)
		}
	}
	if r.interrupted() {
		return r.stopped()
	}
	r.step("verify", false, "the upgraded service did not prove itself")

	// 6. Roll back.
	return r.rollBack()
}

// writePinned writes the override as it would be pinned into the state
// directory, with the override's own mode; "" when it cannot be edited.
func (r *run) writePinned() (string, error) {
	path := filepath.Join(r.state, "override.pinned")
	current, mode := "", fs.FileMode(0o644)
	if r.override != "" {
		body, err := os.ReadFile(r.override)
		if err != nil {
			return "", err
		}
		info, err := os.Stat(r.override)
		if err != nil {
			return "", err
		}
		current, mode = string(body), info.Mode().Perm()
	}
	edited, ok := pin(current, r.req.Service, r.req.To)
	if !ok {
		return "", nil
	}
	if err := os.WriteFile(path, []byte(edited), mode); err != nil {
		return "", err
	}
	return path, os.Chmod(path, mode)
}

// snapshot copies one mount into the state directory, whole or not at all.
func (r *run) snapshot(m mount) bool {
	source := r.dataPath(m.kind, m.ref)
	if source == "" || source == "/" || (m.kind != "volume" && !unmoved(source)) {
		return false
	}
	part := filepath.Join(r.state, fmt.Sprintf("data-%d.part", m.index))
	var err error
	if m.kind == "file" {
		err = copyFile(source, part)
	} else {
		err = r.tar("-C", source, "-czf", part, ".")
	}
	if err == nil {
		err = os.Rename(part, m.snapshot(r.state))
	}
	if err != nil {
		os.Remove(part)
		return false
	}
	return true
}

// runTrial runs the target against copies of the data; failed when the
// service was put back unchanged.
func (r *run) runTrial() (int, bool) {
	trialFile := filepath.Join(r.state, "trial.yaml")
	if err := os.WriteFile(trialFile, []byte(r.trialDefinition()), 0o600); err != nil {
		return r.fault(err), true
	}
	r.trialFile = trialFile
	for _, m := range r.data {
		if !r.copyForTrial(m) {
			r.step("trial", false, fmt.Sprintf("data-%d could not be copied for the trial", m.index))
			return r.unchanged(), true
		}
	}
	if !quiet(r.compose(r.override, "run", "-d", "--no-deps", "--name", r.trial, r.req.Service)) || !r.proves(r.trial) {
		if r.interrupted() {
			return r.stopped(), true
		}
		r.step("trial", false, "the target did not prove itself as the service would run it")
		return r.unchanged(), true
	}
	r.step("trial", true, "proven on a copy of the data, with no network")
	r.cleanupTrial()
	return 0, false
}

// trialDefinition is the overlay that runs the target with no network, no
// ports and the copies of the data.
func (r *run) trialDefinition() string {
	var b strings.Builder
	fmt.Fprintf(&b, "services:\n  %q:\n    image: %q\n    network_mode: none\n", r.req.Service, r.req.To)
	b.WriteString("    networks: !reset {}\n    ports: !reset []\n    volumes:\n")
	volumes := false
	for _, m := range r.data {
		if m.kind == "file" {
			// Its own copy, so the trial never writes the live file.
			fmt.Fprintf(&b, "      - type: bind\n        source: %q\n        target: %q\n",
				filepath.Join(r.state, fmt.Sprintf("trial-%d.file", m.index)), m.mounted)
			continue
		}
		volumes = true
		fmt.Fprintf(&b, "      - type: volume\n        source: hq_trial_%d\n        target: %q\n", m.index, m.mounted)
	}
	if volumes {
		b.WriteString("volumes:\n")
		for _, m := range r.data {
			if m.kind != "file" {
				fmt.Fprintf(&b, "  hq_trial_%d:\n    external: true\n    name: \"%s-%d\"\n", m.index, r.trial, m.index)
			}
		}
	}
	return b.String()
}

func (r *run) copyForTrial(m mount) bool {
	if m.kind == "file" {
		return copyFile(m.snapshot(r.state), filepath.Join(r.state, fmt.Sprintf("trial-%d.file", m.index))) == nil
	}
	volume := fmt.Sprintf("%s-%d", r.trial, m.index)
	if !quiet(r.command("docker", "volume", "create", volume)) {
		return false
	}
	copied := r.dataPath("volume", volume)
	return copied != "" && r.tar("-C", copied, "-xzf", m.snapshot(r.state)) == nil
}

func (r *run) cleanupTrial() {
	quiet(r.command("docker", "rm", "-f", r.trial))
	for _, m := range r.data {
		quiet(r.command("docker", "volume", "rm", "-f", fmt.Sprintf("%s-%d", r.trial, m.index)))
	}
	r.trialFile = ""
}

// restoreData puts the data back from its snapshots, reporting a full disk
// as such.
func (r *run) restoreData() bool {
	for _, m := range r.data {
		target := r.dataPath(m.kind, m.ref)
		if target == "" || target == "/" {
			r.step("restore", false, fmt.Sprintf("data-%d has no mountpoint", m.index))
			return false
		}
		if m.kind != "volume" && !unmoved(target) {
			r.step("restore", false, fmt.Sprintf("data-%d could not be restored: its path now leads through a symlink; its snapshot is kept in the state directory", m.index))
			return false
		}
		var err error
		if m.kind == "file" {
			err = copyFile(m.snapshot(r.state), target)
		} else if err = empty(target); err == nil {
			err = r.tar("-C", target, "-xzf", m.snapshot(r.state))
		}
		if errors.Is(err, syscall.ENOSPC) {
			r.step("restore", false, fmt.Sprintf("the disk filled restoring data-%d; its snapshot is kept in the state directory", m.index))
			return false
		}
		if err != nil {
			r.step("restore", false, fmt.Sprintf("data-%d could not be restored; its snapshot is kept in the state directory", m.index))
			return false
		}
	}
	r.step("restore", true, "the data is back")
	return true
}

// empty removes everything in dir, following no link out of it.
func empty(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if err := os.RemoveAll(filepath.Join(dir, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

// rollBack undoes the pin: the override exactly as it was, then the data,
// then recreate.
func (r *run) rollBack() int {
	before := filepath.Join(r.state, "override.before")
	var err error
	if present(before) {
		restored := r.pinFile + ".hq-restore"
		if err = copyFile(before, restored); err == nil {
			err = os.Rename(restored, r.pinFile)
		}
	} else {
		if err = os.Remove(r.pinFile); errors.Is(err, fs.ErrNotExist) {
			err = nil
		}
		r.override = ""
	}
	if err != nil {
		r.step("rollback", false, "the compose override could not be restored")
		return r.finish("failed", Failed)
	}
	quiet(r.compose(r.override, "stop", r.req.Service))
	if !r.restoreData() {
		return r.finish("failed", Failed)
	}
	if quiet(r.compose(r.override, "up", "-d", "--no-deps", r.req.Service)) {
		r.step("rollback", true, "the previous image and data are back")
		return r.finish("rolled_back", RolledBack)
	}
	r.step("rollback", false, "the previous image could not be started again")
	return r.finish("failed", Failed)
}

// unchanged starts the stopped service again as it was, having changed
// nothing.
func (r *run) unchanged() int {
	r.cleanupTrial()
	if quiet(r.compose(r.override, "start", r.req.Service)) {
		r.step("restart", true, "the service is running again, unchanged")
		return r.finish("unchanged", Refused)
	}
	r.step("restart", false, "the service could not be started again")
	return r.finish("failed", Failed)
}

// stopped puts back what a signal interrupted, by how far the run got.
func (r *run) stopped() int {
	switch r.phase {
	case checking:
		r.step("stopped", false, "stopped before anything changed")
		return r.finish("refused", Refused)
	case stopped:
		r.step("stopped", false, "stopped part way, before the pin")
		return r.unchanged()
	default:
		r.step("stopped", false, "stopped part way, after the pin")
		return r.rollBack()
	}
}

// fault puts back what an error interrupted, the way a signal would be.
func (r *run) fault(err error) int {
	r.step("error", false, err.Error())
	switch r.phase {
	case checking:
		return r.finish("refused", Refused)
	case stopped:
		return r.unchanged()
	default:
		return r.rollBack()
	}
}

// finish records the result and prints it.
func (r *run) finish(outcome string, code int) int {
	r.cleanupTrial()
	var body bytes.Buffer
	steps := r.steps
	if steps == nil {
		steps = []Step{}
	}
	write(&body, Result{r.req.Operation, outcome, r.req.Service, r.req.To, steps})
	part := filepath.Join(r.state, "result.part")
	err := os.WriteFile(filepath.Join(r.state, "exit"), []byte(strconv.Itoa(code)+"\n"), 0o600)
	if err == nil {
		err = os.WriteFile(part, body.Bytes(), 0o600)
	}
	if err == nil {
		err = os.Rename(part, filepath.Join(r.state, "result.json"))
	}
	if err != nil {
		fmt.Fprintln(r.u.Err, "hq-host upgrade: the result could not be recorded in", r.state)
	}
	io.Copy(r.u.Out, &body)
	return code
}
