package upgrade

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const from = "ghcr.io/example/app:1.2.0"

var to = "ghcr.io/example/app@sha256:" + strings.Repeat("a", 64)

const composeFile = `services:
  app:
    image: "ghcr.io/example/app:1.2.0"
    volumes: ["app_data:/data", "./app.conf:/etc/app.conf"]
  sidecar:
    image: example/sidecar:3
`

const hardening = `# Hardening kept beside the stack.
services:
  app:
    mem_limit: 512m
    pids_limit: 200
    security_opt:
      - no-new-privileges:true
  sidecar:
    read_only: true
`

// drill is a machine with one stack, app, under a stand-in Docker and a
// stand-in tar that wraps the real one. Each field is how the stand-ins
// answer.
type drill struct {
	t                         *testing.T
	root, stack, override     string
	ps, running, trial, live  string
	extraMount, swapOnUp      string
	snapshotFails, diskFull   bool
	pullFails, rollbackUpFail bool
	termOnUp                  bool

	mu                    sync.Mutex
	calls                 []string
	ups, starts, restarts int
	trialLast             string
	now                   time.Time
	cancel                context.CancelFunc
	out                   bytes.Buffer
	code                  int
}

func newDrill(t *testing.T) *drill {
	t.Helper()
	// Its physical path: the upgrade refuses a data path reached through a
	// symlink, and a Mac's temporary directory sits behind one.
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	d := &drill{t: t, root: root, stack: filepath.Join(root, "stacks", "app"), ps: "one", running: from,
		trial: "healthy", live: "healthy", now: time.Unix(1_700_000_000, 0)}
	d.override = filepath.Join(d.stack, "compose.override.yaml")
	for _, dir := range []string{d.stack, filepath.Join(root, "volumes", "app_data")} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	d.write("volumes/app_data/state", "original\n")
	d.write("stacks/app/app.conf", "setting\n")
	d.write("stacks/app/compose.yaml", composeFile)
	return d
}

func (d *drill) write(path, body string) {
	d.t.Helper()
	if err := os.WriteFile(filepath.Join(d.root, path), []byte(body), 0o644); err != nil {
		d.t.Fatal(err)
	}
}

func (d *drill) read(path string) string {
	body, _ := os.ReadFile(filepath.Join(d.root, path))
	return string(body)
}

func (d *drill) upgrader() *Upgrader {
	return &Upgrader{
		StacksRoot: filepath.Join(d.root, "stacks"), StateRoot: filepath.Join(d.root, "state"),
		Poll: time.Second, Settle: 0, Command: d.command,
		Now: func() time.Time { d.mu.Lock(); defer d.mu.Unlock(); return d.now },
		Sleep: func(ctx context.Context, wait time.Duration) bool {
			d.mu.Lock()
			d.now = d.now.Add(wait)
			d.mu.Unlock()
			return ctx.Err() == nil
		},
		Out: &d.out, Err: &bytes.Buffer{},
	}
}

// upgrade runs one upgrade of app to the target, extra arguments after the
// usual ones winning.
func (d *drill) upgrade(extra ...string) map[string]any {
	d.t.Helper()
	return d.upgradeAs(d.upgrader(), extra...)
}

func (d *drill) upgradeAs(u *Upgrader, extra ...string) map[string]any {
	d.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	d.cancel = cancel
	d.out.Reset()
	args := []string{"--operation", "op-1", "--project-dir", d.stack, "--service", "app",
		"--from", from, "--to", to, "--tag", "1.2.1", "--wait", "20"}
	d.code = u.Upgrade(ctx, append(args, extra...))
	var result map[string]any
	if err := json.Unmarshal(d.out.Bytes(), &result); err != nil {
		d.t.Fatalf("the output is not one JSON object: %q", d.out.String())
	}
	return result
}

func (d *drill) said(want string) bool { return strings.Contains(d.out.String(), want) }

func (d *drill) called(want string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return slices.ContainsFunc(d.calls, func(call string) bool { return strings.Contains(call, want) })
}

func (d *drill) callIndex(prefix string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return slices.IndexFunc(d.calls, func(call string) bool { return strings.HasPrefix(call, prefix) })
}

func (d *drill) expect(code int, outcome string, result map[string]any) {
	d.t.Helper()
	if d.code != code || (outcome != "" && result["outcome"] != outcome) {
		d.t.Fatalf("exit %d, outcome %v; want %d, %s:\n%s", d.code, result["outcome"], code, outcome, d.out.String())
	}
}

// untouched is the stack and its data as the drill made them.
func (d *drill) untouched() {
	d.t.Helper()
	if d.read("stacks/app/compose.yaml") != composeFile {
		d.t.Error("the compose file changed")
	}
	if _, err := os.Stat(filepath.Join(d.root, "volumes/app_data/added")); err == nil {
		d.t.Error("a file the upgraded service added survived")
	}
	if d.read("volumes/app_data/state") != "original\n" {
		d.t.Error("the data is left changed")
	}
	if d.read("stacks/app/app.conf") != "setting\n" {
		d.t.Error("the mounted file is left changed")
	}
}

func (d *drill) noOverride() {
	d.t.Helper()
	if _, err := os.Lstat(d.override); err == nil {
		d.t.Error("an override was written")
	}
}

func answer(ctx context.Context, out string, code int) *exec.Cmd {
	return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
}

func (d *drill) command(ctx context.Context, name string, args ...string) *exec.Cmd {
	d.mu.Lock()
	d.calls = append(d.calls, strings.Join(append([]string{name}, args...), " "))
	d.mu.Unlock()
	switch name {
	case "tar":
		return d.tar(ctx, args)
	case "docker":
		if args[0] == "compose" {
			return d.compose(ctx, args[1:])
		}
		return d.docker(ctx, args)
	}
	d.t.Errorf("ran %s", name)
	return answer(ctx, "", 127)
}

func (d *drill) tar(ctx context.Context, args []string) *exec.Cmd {
	call := strings.Join(args, " ")
	switch {
	case strings.Contains(call, "-czf") && d.snapshotFails:
		return answer(ctx, "", 1)
	case strings.Contains(call, "-xzf") && !strings.Contains(call, "hq-trial-") && d.diskFull:
		return exec.CommandContext(ctx, "/bin/sh", "-c", `echo "tar: state: Cannot write: No space left on device" >&2; exit 2`)
	}
	return exec.CommandContext(ctx, "tar", args...)
}

func (d *drill) compose(ctx context.Context, args []string) *exec.Cmd {
	var files []string
	for len(args) > 1 && strings.HasPrefix(args[0], "-") {
		if args[0] == "-f" {
			files = append(files, args[1])
		}
		args = args[2:]
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	switch args[0] {
	case "config":
		return answer(ctx, resolved(files), 0)
	case "ps":
		return answer(ctx, map[string]string{"one": "live-1\n", "two": "live-1\nlive-2\n"}[d.ps], 0)
	case "start":
		d.starts++
	case "run":
		body, _ := os.ReadFile(files[len(files)-1])
		d.trialLast = string(body)
	case "up":
		d.ups++
		if d.ups > 1 {
			return answer(ctx, "", map[bool]int{true: 1}[d.rollbackUpFail])
		}
		if d.termOnUp {
			d.cancel()
		}
		// The upgraded service writes over its data before failing.
		if d.live != "healthy" {
			d.write("volumes/app_data/state", "overwritten\n")
			d.write("volumes/app_data/added", "new\n")
			d.write("stacks/app/app.conf", "overwritten\n")
		}
		// Something else writing in the stack swaps a data path for a link
		// to a file outside it, between the check and the rollback.
		if d.swapOnUp != "" {
			conf := filepath.Join(d.stack, "app.conf")
			os.Remove(conf)
			os.Symlink(d.swapOnUp, conf)
		}
	}
	return answer(ctx, "", 0)
}

// resolved is the image each file gives each service, the last file
// winning, as compose prints its configuration.
func resolved(files []string) string {
	images := map[string]map[string]string{}
	for _, file := range files {
		body, _ := os.ReadFile(file)
		within, name := false, ""
		for line := range strings.SplitSeq(string(body), "\n") {
			switch {
			case strings.HasPrefix(line, "services:"):
				within = true
			case line != "" && line[0] != ' ' && line[0] != '#':
				within = false
			case within && strings.HasPrefix(line, "  ") && len(line) > 2 && line[2] != ' ' && line[2] != '#':
				name = strings.Trim(strings.Fields(line)[0], `":`)
				if images[name] == nil {
					images[name] = map[string]string{}
				}
			case within && strings.HasPrefix(line, "    image:"):
				images[name]["image"] = strings.Trim(strings.Fields(line)[1], `"`)
			}
		}
	}
	out, _ := json.MarshalIndent(map[string]any{"services": images}, "", "  ")
	return string(out)
}

func (d *drill) docker(ctx context.Context, args []string) *exec.Cmd {
	d.mu.Lock()
	defer d.mu.Unlock()
	switch {
	case args[0] == "inspect" && strings.Contains(args[2], ".Mounts"):
		mounts := []map[string]any{
			{"Type": "volume", "Name": "app_data", "Source": filepath.Join(d.root, "volumes/app_data"), "Destination": "/data", "RW": true},
			{"Type": "bind", "Source": filepath.Join(d.stack, "app.conf"), "Destination": "/etc/app.conf", "RW": true},
			{"Type": "bind", "Source": "/etc/ssl/certs", "Destination": "/etc/ssl/certs", "RW": false},
		}
		if d.extraMount != "" {
			mounts = append(mounts, map[string]any{"Type": "bind", "Source": d.extraMount, "Destination": "/srv", "RW": true})
		}
		out, _ := json.Marshal(mounts)
		return answer(ctx, string(out)+"\n", 0)
	case args[0] == "inspect" && strings.Contains(args[2], ".Config.Image"):
		return answer(ctx, d.running+"\n", 0)
	case args[0] == "inspect" && strings.HasPrefix(args[3], "hq-trial-"):
		return answer(ctx, "running "+d.trial+" 0 t0\n", 0)
	case args[0] == "inspect" && d.live == "crashloop":
		d.restarts++
		return answer(ctx, fmt.Sprintf("running none %d t%d\n", d.restarts, d.restarts), 0)
	case args[0] == "inspect":
		return answer(ctx, "running "+d.live+" 0 t0\n", 0)
	case args[0] == "volume" && (args[1] == "inspect" || args[1] == "create"):
		path := filepath.Join(d.root, "volumes", args[len(args)-1])
		os.MkdirAll(path, 0o755)
		return answer(ctx, path+"\n", 0)
	case args[0] == "pull" && d.pullFails:
		return answer(ctx, "", 1)
	}
	return answer(ctx, "", 0)
}

func TestAProvenUpgradeIsKeptPinnedInANewOverride(t *testing.T) {
	d := newDrill(t)

	result := d.upgrade()

	d.expect(Kept, "kept", result)
	d.untouched()
	if got := d.read("stacks/app/compose.override.yaml"); got != "services:\n  \"app\":\n    image: "+to+"\n" {
		t.Errorf("the override is %q", got)
	}
	if !d.called("docker compose -p app --project-directory " + d.stack + " -f " + d.stack + "/compose.yaml -f " + d.override + " up -d --no-deps app") {
		t.Error("the service was not recreated with the stack's file set and project")
	}
	state := filepath.Join(d.root, "state", "op-1")
	if _, err := os.Stat(filepath.Join(state, "data-0.tgz")); err != nil {
		t.Error("the volume was not snapshotted")
	}
	if _, err := os.Stat(filepath.Join(state, "data-1.tgz")); err == nil {
		t.Error("a writable file mount was snapshotted as a directory")
	}
	if stop, snapshot := d.callIndex("docker compose -p app"), d.callIndex("tar -C"); stop < 0 || snapshot < 0 ||
		!slices.ContainsFunc(d.calls[stop:snapshot], func(call string) bool { return strings.HasSuffix(call, " stop app") }) {
		t.Error("the data was snapshotted while the service ran")
	}
	if !d.called(" run -d --no-deps --name hq-trial-op-1 app") || d.callIndex("docker run ") >= 0 {
		t.Error("the trial did not run from the service's own definition")
	}
	for _, line := range []string{"network_mode: none", "ports: !reset []", `target: "/data"`, `name: "hq-trial-op-1-0"`, "trial-1.file"} {
		if !strings.Contains(d.trialLast, line) {
			t.Errorf("the trial definition lacks %s:\n%s", line, d.trialLast)
		}
	}

	// The same operation again: its result, and no second run.
	first, calls := d.out.String(), len(d.calls)
	again := d.upgrade()
	if d.out.String() != first || d.code != Kept || again["outcome"] != "kept" {
		t.Errorf("asking again answered %d %s", d.code, d.out.String())
	}
	if len(d.calls) != calls {
		t.Error("asking again ran docker")
	}
}

func TestAnExistingOverrideKeepsEveryOtherLine(t *testing.T) {
	d := newDrill(t)
	d.write("stacks/app/compose.override.yaml", hardening)

	d.expect(Kept, "kept", d.upgrade())

	lines := strings.Split(d.read("stacks/app/compose.override.yaml"), "\n")
	if lines[7] != "    image: "+to {
		t.Errorf("the pin is not on the service's own block:\n%s", strings.Join(lines, "\n"))
	}
	if strings.Join(slices.Delete(lines, 7, 8), "\n") != hardening {
		t.Error("the pin changed the override beyond its image line")
	}
	if d.read("state/op-1/override.before") != hardening {
		t.Error("the override's copy is not byte-exact")
	}
}

func TestARollbackRestoresTheOverrideByteForByte(t *testing.T) {
	d := newDrill(t)
	d.write("stacks/app/compose.override.yaml", hardening)
	d.live = "unhealthy"

	d.expect(RolledBack, "rolled_back", d.upgrade())

	if d.read("stacks/app/compose.override.yaml") != hardening {
		t.Error("the rollback did not restore the override exactly")
	}
}

func TestAnUpgradeThatDoesNotProveItselfIsRolledBack(t *testing.T) {
	d := newDrill(t)
	d.live = "unhealthy"

	d.expect(RolledBack, "rolled_back", d.upgrade())

	d.untouched()
	d.noOverride()
	if d.ups != 2 {
		t.Errorf("recreated %d times; the rollback recreates once more", d.ups)
	}
}

func TestAFailedTrialOrSnapshotChangesNothing(t *testing.T) {
	for name, arrange := range map[string]func(*drill){
		"trial":    func(d *drill) { d.trial = "unhealthy" },
		"snapshot": func(d *drill) { d.snapshotFails = true },
	} {
		t.Run(name, func(t *testing.T) {
			d := newDrill(t)
			arrange(d)

			d.expect(Refused, "unchanged", d.upgrade())

			d.untouched()
			d.noOverride()
			if d.ups != 0 || d.starts != 1 {
				t.Errorf("recreated %d times, started %d; want 0 and 1", d.ups, d.starts)
			}
			if parts, _ := filepath.Glob(filepath.Join(d.root, "state", "op-1", "*.part")); len(parts) != 0 {
				t.Errorf("left %v", parts)
			}
		})
	}
	d := newDrill(t)
	d.snapshotFails = true
	d.upgrade()
	if !d.said("nothing changed") {
		t.Errorf("a failed snapshot did not say nothing changed: %s", d.out.String())
	}
}

func TestACrashLoopNeverVerifies(t *testing.T) {
	d := newDrill(t)
	d.live = "crashloop"

	d.expect(RolledBack, "rolled_back", d.upgrade("--wait", "2"))
}

func TestARunStoppedAfterThePinIsPutBackAndRecorded(t *testing.T) {
	d := newDrill(t)
	d.write("stacks/app/compose.override.yaml", hardening)
	d.termOnUp, d.live = true, "unhealthy"

	d.expect(RolledBack, "rolled_back", d.upgrade())

	if d.read("stacks/app/compose.override.yaml") != hardening {
		t.Error("a run stopped after the pin kept the pin")
	}
	d.untouched()
	if !strings.Contains(d.read("state/op-1/result.json"), "stopped part way") {
		t.Error("a stopped run recorded no result")
	}
}

func TestARollbackThatCannotFinishIsAFailure(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(*drill)
		said    string
	}{
		"a data path swapped for a link": {func(d *drill) {
			d.write("outside.conf", "outside\n")
			d.swapOnUp = filepath.Join(d.root, "outside.conf")
		}, "could not be restored"},
		"a full disk":           {func(d *drill) { d.diskFull = true }, "the disk filled"},
		"a service that is out": {func(d *drill) { d.rollbackUpFail = true }, "could not be started again"},
	} {
		t.Run(name, func(t *testing.T) {
			d := newDrill(t)
			d.live = "unhealthy"
			c.arrange(d)

			d.expect(Failed, "failed", d.upgrade())

			if !d.said(c.said) {
				t.Errorf("not reported as %q: %s", c.said, d.out.String())
			}
			d.noOverride()
			if body := d.read("outside.conf"); body != "" && body != "outside\n" {
				t.Error("the rollback wrote through a link out of the stack")
			}
		})
	}
}

func TestWhatTheMachineSaysIsRefusedBeforeAnythingStops(t *testing.T) {
	for name, c := range map[string]struct {
		arrange func(*drill)
		extra   []string
		reason  string
	}{
		"two containers for one service": {func(d *drill) { d.ps = "two" }, nil, "not exactly one running container"},
		"a container not running --from": {func(d *drill) { d.running = "ghcr.io/example/app:1.1.0" }, nil, "the running container is not"},
		"a writable directory outside":   {func(d *drill) { d.extraMount = d.root }, nil, "outside the stack is mounted at /srv"},
		"a failed pull":                  {func(d *drill) { d.pullFails = true }, nil, `"step":"pull","ok":false`},
		"a --from compose does not use":  {func(*drill) {}, []string{"--from", "ghcr.io/example/app:1.1.0"}, "compose does not resolve app to"},
		"an override compose does not load": {func(d *drill) {
			d.write("stacks/app/docker-compose.override.yml", "services: {}\n")
		}, nil, "is not the override compose loads"},
		"a flow-style override": {func(d *drill) {
			d.write("stacks/app/compose.override.yaml", "services:\n  app: {image: x}\n")
		}, nil, "not block style"},
		"a JSON override": {func(d *drill) {
			d.write("stacks/app/compose.override.yaml", "{\n  \"services\": {\n    \"app\": {\n      \"mem_limit\": \"512m\"\n    }\n  }\n}\n")
		}, nil, "not block style"},
	} {
		t.Run(name, func(t *testing.T) {
			d := newDrill(t)
			c.arrange(d)
			override := d.read("stacks/app/compose.override.yaml")

			d.expect(Refused, "refused", d.upgrade(c.extra...))

			if !d.said(c.reason) {
				t.Errorf("refused for another reason: %s", d.out.String())
			}
			if d.called(" stop ") {
				t.Error("the service was stopped")
			}
			d.untouched()
			if d.read("stacks/app/compose.override.yaml") != override {
				t.Error("the override changed")
			}
		})
	}
}

func TestWhatTheArgumentsAndFilesSayIsRefusedWithoutDocker(t *testing.T) {
	digest := "@sha256:" + strings.Repeat("a", 64)
	for name, c := range map[string]struct {
		arrange func(*drill) []string
		reason  string
	}{
		"a tag for --to":             {args("--to", "ghcr.io/example/app:1.2.1"), "to is not pinned by digest"},
		"a foreign --to repository":  {args("--to", "ghcr.io/example/other"+digest), "to is not the repository from names"},
		"a --to on another registry": {args("--to", "registry.example.com/example/app"+digest), "to is not the repository from names"},
		"caller-supplied data":       {args("--data", "app_data:/data"), "data is read from the running service"},
		"an unknown argument":        {args("--force", "yes"), "unknown argument"},
		"an argument with no value":  {args("--tag"), "an argument has no value"},
		"a service with a newline":   {args("--service", "app\nother"), "service is not a name"},
		"a stack outside the root": {func(d *drill) []string {
			os.MkdirAll(filepath.Join(d.root, "elsewhere", "app"), 0o755)
			d.write("elsewhere/app/compose.yaml", composeFile)
			return []string{"--project-dir", filepath.Join(d.root, "elsewhere", "app")}
		}, "not directly under"},
		"a stack below a stack": {func(d *drill) []string {
			return []string{"--project-dir", filepath.Join(d.stack, "nested")}
		}, "not directly under"},
		"a path that climbs out": {func(d *drill) []string {
			return []string{"--project-dir", filepath.Join(d.root, "stacks") + "/../elsewhere"}
		}, "climbs out"},
		"a symlinked stack directory": {func(d *drill) []string {
			os.Symlink(d.stack, filepath.Join(d.root, "stacks", "linked"))
			return []string{"--project-dir", filepath.Join(d.root, "stacks", "linked")}
		}, "the project directory is a symlink"},
		"a symlinked compose file": {func(d *drill) []string {
			os.MkdirAll(filepath.Join(d.root, "stacks", "other"), 0o755)
			os.Symlink(filepath.Join(d.stack, "compose.yaml"), filepath.Join(d.root, "stacks", "other", "compose.yaml"))
			return []string{"--project-dir", filepath.Join(d.root, "stacks", "other")}
		}, "the compose file is a symlink"},
		"a symlinked override": {func(d *drill) []string {
			os.Symlink(filepath.Join(d.stack, "compose.yaml"), d.override)
			return nil
		}, "the compose override is a symlink"},
		"a group-writable compose file": {func(d *drill) []string {
			os.Chmod(filepath.Join(d.stack, "compose.yaml"), 0o664)
			return nil
		}, "the compose file is writable by its group or others"},
		"a world-writable stack directory": {func(d *drill) []string {
			os.Chmod(d.stack, 0o757)
			return nil
		}, "the project directory is writable by its group or others"},
	} {
		t.Run(name, func(t *testing.T) {
			d := newDrill(t)
			extra := c.arrange(d)

			d.expect(Refused, "refused", d.upgrade(extra...))

			if !d.said(c.reason) {
				t.Errorf("refused for another reason: %s", d.out.String())
			}
			if len(d.calls) != 0 {
				t.Errorf("ran %v", d.calls)
			}
			d.untouched()
			if info, err := os.Lstat(d.override); err == nil && info.Mode().IsRegular() {
				t.Error("an override was written")
			}
		})
	}
}

func args(extra ...string) func(*drill) []string {
	return func(*drill) []string { return extra }
}

func TestOnlyRootUpgrades(t *testing.T) {
	d := newDrill(t)
	u := d.upgrader()
	u.EUID = 1000

	d.expect(Refused, "refused", d.upgradeAs(u))

	if !d.said("must run as root") || len(d.calls) != 0 {
		t.Errorf("a non-root run: %s %v", d.out.String(), d.calls)
	}
}

// Through sudo nothing the caller's environment says moves the stacks root,
// the state or the bounds, and docker gets root's own environment.
func TestThroughSudoTheMachineIsFixed(t *testing.T) {
	d := newDrill(t)
	env := map[string]string{
		"SUDO_UID": "1000", "PATH": "/home/example/bin", "HOME": "/home/example", "DOCKER_CONFIG": "/home/example/.docker",
		"SEVERINO_UPGRADE_STACKS": filepath.Join(d.root, "stacks"), "SEVERINO_UPGRADE_STATE": d.root,
		"SEVERINO_UPGRADE_POLL": "0", "SEVERINO_UPGRADE_SETTLE": "0",
	}

	u, err := FromEnvironment(func(name string) string { return env[name] }, 0, &d.out, &bytes.Buffer{})

	if err != nil {
		t.Fatal(err)
	}
	if u.StacksRoot != StacksRoot || u.StateRoot != StateRoot || u.Poll != Poll || u.Settle != Settle || u.Caller != "1000" {
		t.Errorf("moved through sudo: %+v", u)
	}
	command := u.Command(context.Background(), "docker")
	if !slices.Equal(command.Env, []string{"LC_ALL=C", "PATH=" + sudoPath, "HOME=/root"}) {
		t.Errorf("docker's environment through sudo: %v", command.Env)
	}
	d.expect(Refused, "refused", d.upgradeAs(u))
	if !d.said("not directly under /opt/apps") {
		t.Errorf("a run through sudo used the caller's stacks root: %s", d.out.String())
	}
}

func TestOutsideSudoTheDrillMovesTheMachine(t *testing.T) {
	env := map[string]string{"SEVERINO_UPGRADE_STACKS": "/srv/stacks", "SEVERINO_UPGRADE_POLL": "1", "SEVERINO_UPGRADE_SETTLE": "x"}

	_, err := FromEnvironment(func(name string) string { return env[name] }, 0, nil, nil)
	if err == nil || err.Error() != "poll and settle are not numbers" {
		t.Errorf("a settle that is not a number: %v", err)
	}
	env["SEVERINO_UPGRADE_SETTLE"] = "3"
	u, err := FromEnvironment(func(name string) string { return env[name] }, 0, nil, nil)
	if err != nil || u.StacksRoot != "/srv/stacks" || u.Poll != time.Second || u.Settle != 3*time.Second {
		t.Errorf("%+v %v", u, err)
	}
}

func TestThePinIsAddedWhereverTheOverrideLacksIt(t *testing.T) {
	for name, c := range map[string]struct{ override, want string }{
		"no services": {"x-note: kept\n", "x-note: kept\nservices:\n  \"app\":\n    image: IMAGE\n"},
		"another service only": {"services:\n  other:\n    read_only: true\n",
			"services:\n  other:\n    read_only: true\n  \"app\":\n    image: IMAGE\n"},
		"an image to replace": {"services:\n  app:\n    image: old\n    read_only: true\n",
			"services:\n  app:\n    image: IMAGE\n    read_only: true\n"},
		"a service with no keys, then another key": {"services:\n  app:\nvolumes:\n  data: {}\n",
			"services:\n  app:\n    image: IMAGE\nvolumes:\n  data: {}\n"},
		"a dotted name is literal": {"services:\n  appxv1:\n    read_only: true\n",
			"services:\n  appxv1:\n    read_only: true\n  \"app.v1\":\n    image: IMAGE\n"},
	} {
		t.Run(name, func(t *testing.T) {
			service := "app"
			if strings.Contains(name, "dotted") {
				service = "app.v1"
			}
			got, ok := pin(c.override, service, "IMAGE")
			if !ok || got != c.want {
				t.Errorf("got %v %q, want %q", ok, got, c.want)
			}
		})
	}
	if _, ok := pin("services: {app: {image: x}}\n", "app", "IMAGE"); ok {
		t.Error("a flow-style services was edited")
	}
}
