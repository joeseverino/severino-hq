package host

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const exampleImage = "ghcr.io/renovatebot/renovate:1.2.3@sha256:" + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

type renovation struct {
	r     *Renovator
	mu    sync.Mutex
	calls [][]string
	// envs is each run's environment file as docker would read it, and its mode.
	envs  []string
	modes []os.FileMode
	// docker is what the fake's `docker run` does: a shell command.
	docker string
	// network is what `docker network inspect` prints, empty while there is
	// no network; `docker network create` makes it renovateNetworkJSON.
	network string
	// loaded is the file the fake's nft writes what it is given to; nftFails
	// makes it refuse.
	loaded   string
	nftFails bool
}

// renovateNetworkJSON is `docker network inspect --format '{{json .}}'` of the
// network as `hq-host renovate` makes it.
const renovateNetworkJSON = `{"Name":"severino-hq-renovate","Id":"0123abcd","Scope":"local","Driver":"bridge","EnableIPv4":true,"EnableIPv6":false,` +
	`"IPAM":{"Driver":"default","Options":null,"Config":[{"Subnet":"10.231.0.0/24","Gateway":"10.231.0.1"}]},"Internal":false,"Attachable":false,` +
	`"Containers":{},"Options":{"com.docker.network.bridge.name":"hq-renovate0"},"Labels":{}}`

func newRenovation(t *testing.T) *renovation {
	t.Helper()
	root := t.TempDir()
	lib := filepath.Join(root, "lib")
	if err := os.MkdirAll(filepath.Join(lib, "deploy", "renovate"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(lib, "deploy", "renovate", "image"), []byte(exampleImage+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(lib, "deploy", "renovate", "config.json"), []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	v := &renovation{docker: "exit 0", network: renovateNetworkJSON, loaded: filepath.Join(root, "loaded.nft")}
	v.r = &Renovator{
		Machine:    &Machine{Lib: lib, LogDir: filepath.Join(root, "log")},
		Spool:      filepath.Join(root, "spool"),
		Outcome:    filepath.Join(root, "state", "renovate", "dependency-run.json"),
		OwnerBound: time.Hour,
		Now:        time.Now,
	}
	v.r.Command = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		v.mu.Lock()
		v.calls = append(v.calls, append([]string{name}, args...))
		v.mu.Unlock()
		if name == "docker" && args[0] == "run" {
			env := args[slices.Index(args, "--env-file")+1]
			data, _ := os.ReadFile(env)
			info, _ := os.Lstat(env)
			v.envs = append(v.envs, string(data))
			v.modes = append(v.modes, info.Mode().Perm())
			return exec.CommandContext(ctx, "/bin/sh", "-c", v.docker)
		}
		answer := func(out string, code int) *exec.Cmd {
			return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"; exit "$2"`, "sh", out, strconv.Itoa(code))
		}
		switch {
		case name == "docker" && slices.Equal(args[:2], []string{"network", "inspect"}):
			if v.network == "" {
				return answer("Error response from daemon: network severino-hq-renovate not found", 1)
			}
			return answer(v.network, 0)
		case name == "docker" && slices.Equal(args[:2], []string{"network", "create"}):
			v.network = renovateNetworkJSON
		case name == "nft":
			code := 0
			if v.nftFails {
				code = 1
			}
			return exec.CommandContext(ctx, "/bin/sh", "-c", `cat > "$1"; exit "$2"`, "sh", v.loaded, strconv.Itoa(code))
		}
		return answer("", 0)
	}
	return v
}

func (v *renovation) spool(t *testing.T, runs ...HandoffRun) {
	t.Helper()
	if err := spool(v.r.Spool, Handoff{Version: 1, Runs: runs}); err != nil {
		t.Fatal(err)
	}
}

func exampleRun(owner string) HandoffRun {
	return HandoffRun{Owner: owner, Repositories: []string{owner + "/app"}, Token: exampleToken, ExpiresAt: time.Now().Add(50 * time.Minute)}
}

func (v *renovation) outcome(t *testing.T) DependencyRun {
	t.Helper()
	data, err := os.ReadFile(v.r.Outcome)
	if err != nil {
		t.Fatal("no outcome was recorded")
	}
	var outcome DependencyRun
	if err := strictJSON(data, &outcome); err != nil {
		t.Fatal(err)
	}
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(data, &keys); err != nil {
		t.Fatal(err)
	}
	if err := exactKeys(keys, "version", "started_at", "finished_at", "runs"); err != nil {
		t.Error(err)
	}
	if outcome.Version != 1 || outcome.StartedAt.IsZero() || outcome.FinishedAt.Before(outcome.StartedAt) {
		t.Errorf("outcome %s", data)
	}
	return outcome
}

// nothingLeft fails when the spool holds anything: the handoff, a claimed
// copy, or an environment file.
func (v *renovation) nothingLeft(t *testing.T) {
	t.Helper()
	var left []string
	if err := filepath.WalkDir(v.r.Spool, func(path string, _ os.DirEntry, _ error) error {
		if path != v.r.Spool {
			left = append(left, path)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(left) != 0 {
		t.Errorf("left %v", left)
	}
}

func TestTheRenovateContainerReachesNothingOfTheHosts(t *testing.T) {
	args := RenovateArgs("severino-hq-renovate-x", exampleImage, "/run/severino-hq-renovate/run.1/0.env", "/usr/local/lib/severino-hq/deploy/renovate/config.json", "Example")

	want := []string{
		"run", "--rm", "--name", "severino-hq-renovate-x",
		"--user", "12021",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--read-only",
		"--network", "severino-hq-renovate",
		"--dns", "1.1.1.1",
		"--dns", "8.8.8.8",
		"--pids-limit", "4096",
		"--tmpfs", "/tmp:size=4g,exec,nosuid,nodev",
		"--mount", "type=volume,target=/opt/containerbase",
		"--mount", "type=volume,target=/home/ubuntu",
		"--mount", "type=volume,source=severino-hq-renovate-cache-example,target=/runner/cache",
		"--mount", "type=bind,source=/usr/local/lib/severino-hq/deploy/renovate/config.json,target=/run/renovate/config.json,readonly",
		"--env-file", "/run/severino-hq-renovate/run.1/0.env",
		"--env", "RENOVATE_CONFIG_FILE=/run/renovate/config.json",
		"--env", "RENOVATE_CACHE_DIR=/runner/cache",
		exampleImage,
	}
	if !slices.Equal(args, want) {
		t.Errorf("args\n%q\nwant\n%q", args, want)
	}
	joined := strings.Join(args, " ")
	for _, absent := range []string{"--network bridge", "--network host", "--network=host", "docker.sock", "--privileged", "--cap-add", BridgeSocket, SecretDir, RunDir + ","} {
		if strings.Contains(joined, absent) {
			t.Errorf("the container is given %q", absent)
		}
	}
	for i, arg := range args {
		if arg == "--mount" && strings.Contains(args[i+1], "type=bind") && !strings.HasSuffix(args[i+1], ",readonly") {
			t.Errorf("a host path is mounted writable: %s", args[i+1])
		}
	}
}

func TestEachOwnerRunsWithItsOwnTokenAndNothingIsLeft(t *testing.T) {
	v := newRenovation(t)
	v.spool(t, exampleRun("example"), exampleRun("example-org"))

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	if len(v.envs) != 2 {
		t.Fatalf("ran %d times", len(v.envs))
	}
	for i, owner := range []string{"example", "example-org"} {
		if v.envs[i] != "RENOVATE_TOKEN="+exampleToken+"\nRENOVATE_REPOSITORIES="+owner+"/app\n" || v.modes[i] != 0o400 {
			t.Errorf("%s's environment file is %q, %v", owner, v.envs[i], v.modes[i])
		}
	}
	for _, call := range v.calls {
		if strings.Contains(strings.Join(call, " "), exampleToken) {
			t.Error("the token is on a command line")
		}
	}
	outcome := v.outcome(t)
	if len(outcome.Runs) != 2 || outcome.Runs[1].Owner != "example-org" || outcome.Runs[0].Outcome != "ok" || outcome.Runs[0].ExitCode != 0 || !slices.Equal(outcome.Runs[0].Repositories, []string{"example/app"}) {
		t.Errorf("outcome %+v", outcome)
	}
	v.nothingLeft(t)
	if log, err := os.ReadFile(filepath.Join(v.r.LogDir, "renovate.log")); err != nil || !strings.Contains(string(log), "example-org") || strings.Contains(string(log), exampleToken) {
		t.Errorf("log %q", log)
	}
}

func TestAFailedRunIsRecordedAndLeavesNothing(t *testing.T) {
	v := newRenovation(t)
	v.docker = "echo renovate failed >&2; exit 3"
	v.spool(t, exampleRun("example"))

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	if run := v.outcome(t).Runs[0]; run.Outcome != "failed" || run.ExitCode != 3 {
		t.Errorf("recorded %+v", run)
	}
	v.nothingLeft(t)
	if log, _ := os.ReadFile(filepath.Join(v.r.LogDir, "renovate.log")); !strings.Contains(string(log), "renovate failed") {
		t.Error("Renovate's own output is not in the host's log")
	}
}

func TestARunPastItsBoundIsStoppedAndItsContainerRemoved(t *testing.T) {
	v := newRenovation(t)
	v.docker = "exec sleep 60"
	v.r.OwnerBound = 50 * time.Millisecond
	v.spool(t, exampleRun("example"))

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	if run := v.outcome(t).Runs[0]; run.Outcome != "timeout" {
		t.Errorf("recorded %+v", run)
	}
	run := v.calls[slices.IndexFunc(v.calls, runsContainer)]
	name := run[slices.Index(run, "--name")+1]
	if !slices.ContainsFunc(v.calls, func(call []string) bool {
		return slices.Equal(call, []string{"docker", "rm", "--force", "--volumes", name})
	}) {
		t.Errorf("the container was not removed: %v", v.calls)
	}
	v.nothingLeft(t)
}

func TestATokenExpiredBeforeItsTurnIsNeverRun(t *testing.T) {
	v := newRenovation(t)
	run := exampleRun("example")
	run.ExpiresAt = time.Now().Add(-time.Second)
	v.spool(t, run)

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	if len(v.envs) != 0 || v.outcome(t).Runs[0].Outcome != "timeout" {
		t.Errorf("ran %d times; recorded %+v", len(v.envs), v.outcome(t).Runs)
	}
	v.nothingLeft(t)
}

func TestNothingSpooledIsNothingToDo(t *testing.T) {
	v := newRenovation(t)

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	if len(v.calls) != 0 {
		t.Errorf("ran %v", v.calls)
	}
	if _, err := os.Lstat(v.r.Outcome); err == nil {
		t.Error("recorded an outcome of no run")
	}
	v.nothingLeft(t)
}

func TestWhatAnEarlierRunLeftIsRemoved(t *testing.T) {
	v := newRenovation(t)
	left := filepath.Join(v.r.Spool, "run.killed")
	if err := os.MkdirAll(left, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(left, "0.env"), []byte("RENOVATE_TOKEN="+exampleToken+"\n"), 0o400); err != nil {
		t.Fatal(err)
	}

	if err := v.r.Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	v.nothingLeft(t)
}

func TestAnUnpinnedImageIsNeverRun(t *testing.T) {
	v := newRenovation(t)
	if err := os.WriteFile(filepath.Join(v.r.Lib, "deploy", "renovate", "image"), []byte("ghcr.io/renovatebot/renovate:latest\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	v.spool(t, exampleRun("example"))

	if err := v.r.Run(context.Background()); err == nil {
		t.Fatal("ran an image a tag could move")
	}
	if len(v.calls) != 0 {
		t.Errorf("ran %v", v.calls)
	}
	v.nothingLeft(t)
}

// The shipped pin and configuration are what the run reads: a digest-pinned
// image, and a configuration that allows every command HQ's own repository
// configuration runs and names no repository of its own.
func TestTheShippedRenovateFilesAreWhatARunNeeds(t *testing.T) {
	deploy := filepath.Join("..", "..", "deploy", "renovate")
	pin, err := os.ReadFile(filepath.Join(deploy, "image"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Pinned(strings.TrimSpace(string(pin))); err != nil {
		t.Error(err)
	}
	var global struct {
		AllowedCommands []string `json:"allowedCommands"`
		Repositories    any      `json:"repositories"`
		Autodiscover    any      `json:"autodiscover"`
	}
	data, err := os.ReadFile(filepath.Join(deploy, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &global); err != nil {
		t.Fatal(err)
	}
	if global.Repositories != nil || global.Autodiscover != nil {
		t.Error("the global configuration chooses repositories; the handoff does")
	}
	var repository struct {
		PackageRules []struct {
			PostUpgradeTasks struct {
				Commands []string `json:"commands"`
			} `json:"postUpgradeTasks"`
		} `json:"packageRules"`
	}
	data, err = os.ReadFile(filepath.Join("..", "..", ".github", "renovate.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &repository); err != nil {
		t.Fatal(err)
	}
	for _, rule := range repository.PackageRules {
		for _, command := range rule.PostUpgradeTasks.Commands {
			if !slices.ContainsFunc(global.AllowedCommands, func(pattern string) bool { return regexp.MustCompile(pattern).MatchString(command) }) {
				t.Errorf("%q is a command the repository runs and the global configuration does not allow", command)
			}
		}
	}
}
