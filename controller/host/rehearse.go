package host

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Rehearsal is where a release rehearses itself before anything live is
// touched: the image's web process boots on a copy of production's
// database, and the image's controller plans on copies of the host's
// connections, identities and certificate state.
//
// Its security properties:
//   - It runs inside `hq-host release`, so as root only through the runner's
//     one deploy rule, after the deployment was approved, and only on the
//     digest Deploy verified with cosign and pulled: every container is run
//     with --pull never, so nothing else can be fetched in its place.
//   - The database copy, the secret copies and the doorbell live in a
//     root-only scratch directory on /run and in volumes of the rehearsal's
//     own, all removed however it ends, and cleared first in case a killed
//     rehearsal left them.
//   - The web process has no network, no capabilities and no new privileges.
//     It reads the live application environment, and the copy is written as
//     HQ's account, so nothing it makes beside the live database is root's.
//   - The controller runs in plan mode, which reads providers and writes to
//     none, and reaches only the rehearsal's own bridge, never live HQ's.
//   - It never renders secrets: the renderer restarts the live web unit.
//   - It is lent no sealing key (SealDir), and its web no public key, so
//     nothing in it seals a secret or opens one.
//   - Only the verdict line reaches the public log; each step's output is
//     the private host log's rehearsal.log.
type Rehearsal struct {
	// SecretDir is the renderer's tmpfs (SecretDir on a host), and ACME the
	// controller's certificate state (ACMEDir).
	SecretDir, ACME string
	// LookPath finds a host program to lend the controller, as a pass has.
	LookPath func(name string) (string, error)
	// Poll is how often the web process is asked whether it is ready.
	Poll time.Duration
}

const (
	// The rehearsal's container and volumes have fixed names, so one a
	// killed rehearsal left is removed by the next.
	rehearsalName   = "severino-hq-rehearsal"
	rehearsalData   = rehearsalName + "-data"
	rehearsalBridge = rehearsalName + "-bridge"
	// rehearsalReady bounds the wait for the web process to answer ready:
	// twice what the live start is given, since the copy boots beside it.
	rehearsalReady = 2 * HealthStartPeriod
	// rehearsalBound bounds the whole rehearsal, the controller's plan in it;
	// the Deploy job's timeout allows for it.
	rehearsalBound    = 10 * time.Minute
	rehearsalReadyURL = "http://127.0.0.1:8000/health/ready/"
)

// The checks the rehearsal runs in the web process, as python one-liners.
const (
	backupScript = `import sqlite3
source = sqlite3.connect("file:/src/` + Database + `?mode=ro", uri=True)
copy = sqlite3.connect("/data/` + Database + `")
source.backup(copy)
copy.close()
source.close()`
	socketScript = `import os, stat, sys
info = os.stat("` + BridgeSocket + `")
sys.exit(0 if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() else 1)`
	integrityScript = `import sqlite3
print(sqlite3.connect("file:/data/` + Database + `?mode=ro", uri=True).execute("pragma integrity_check").fetchone()[0])`
)

// verdict is the rehearsal's one line: each check, "-" for one not reached.
type verdict struct{ ready, socket, integrity, jobs, memory, plan, live string }

func (v verdict) passed() bool {
	for _, value := range []string{v.ready, v.socket, v.integrity, v.jobs, v.memory, v.plan} {
		if value != "yes" && value != "ok" {
			return false
		}
	}
	return v.live == "untouched"
}

func (v verdict) String() string {
	result := "fail"
	if v.passed() {
		result = "pass"
	}
	return fmt.Sprintf("result=%s ready=%s socket=%s integrity=%s jobs=%s memory=%s plan=%s live=%s",
		result, v.ready, v.socket, v.integrity, v.jobs, v.memory, v.plan, v.live)
}

// rehearse refuses image unless it passes its rehearsal.
func (d *Deployer) rehearse(ctx context.Context, image string) error {
	data := volumeAt("/data")
	mountpoint, err := d.mountpoint(ctx, data)
	if err != nil {
		return err
	}
	// The copy, and room for its migrations to rewrite it, above the floor
	// the deploy keeps.
	size := fileSize(filepath.Join(mountpoint, Database)) + fileSize(filepath.Join(mountpoint, Database+"-wal"))
	if free, err := d.Free(d.FreeOn); err == nil && free < d.FreeBytes+2*size {
		return fmt.Errorf("a rehearsal needs %d MiB free for its copy of the database; %d MiB are",
			(d.FreeBytes+2*size)>>20, free>>20)
	}
	if err := os.MkdirAll(d.LogDir, 0o700); err != nil {
		return err
	}
	log, err := os.CreateTemp(d.LogDir, ".rehearsal.")
	if err != nil {
		return err
	}
	fmt.Fprintln(d.Out, "Rehearsing the release on a copy of production's data.")
	r := &rehearsing{Deployer: d, image: image, data: data.Name, log: log,
		scratch: filepath.Join(d.Runtime, rehearsalName)}
	said := r.run(ctx)
	log.Close()
	if err := os.Rename(log.Name(), filepath.Join(d.LogDir, "rehearsal.log")); err != nil {
		os.Remove(log.Name())
	}
	fmt.Fprintln(d.Out, "Rehearsal: "+said.String())
	if !said.passed() {
		fmt.Fprintln(d.Out, "The rehearsal failed; inspect the private host log. Nothing live was touched.")
		return fmt.Errorf("refusing to deploy %s: it failed its rehearsal on a copy of production's data", image)
	}
	return nil
}

type rehearsing struct {
	*Deployer
	image, data, scratch string
	log                  io.Writer
}

func (r *rehearsing) run(ctx context.Context) (said verdict) {
	said = verdict{ready: "no", socket: "-", integrity: "-", jobs: "-", memory: "-", plan: "-", live: "-"}
	ctx, cancel := context.WithTimeout(ctx, rehearsalBound)
	defer cancel()
	r.clear()
	defer r.clear()
	before := r.liveMarks(ctx)
	defer func() {
		if after := r.liveMarks(context.WithoutCancel(ctx)); after == before {
			said.live = "untouched"
		} else {
			fmt.Fprintf(r.log, "The live web restarted: %s -> %s\n", before, after)
			said.live = "restarted"
		}
	}()

	if !r.copyDatabase(ctx) {
		return said
	}
	if !r.startWeb(ctx) {
		return said
	}
	said.ready = "yes"

	said.socket = "missing"
	if _, err := r.exec(ctx, "docker", "exec", rehearsalName, "python", "-c", socketScript); err == nil {
		said.socket = "ok"
	}
	said.integrity = "damaged"
	if answer, err := r.exec(ctx, "docker", "exec", rehearsalName, "python", "-c", integrityScript); err == nil && strings.TrimSpace(answer) == "ok" {
		said.integrity = "ok"
	}
	said.jobs = "ok"
	for _, name := range []string{"audit.prune", "sessions.clear"} {
		if _, err := r.exec(ctx, "docker", "exec", rehearsalName, "hq-controller", "job", name); err != nil {
			said.jobs = "failed"
		}
	}
	// A job HQ does not declare is refused, not run.
	if _, err := r.exec(ctx, "docker", "exec", rehearsalName, "hq-controller", "job", "no.such.job"); err == nil {
		said.jobs = "failed"
	}
	_, _ = r.exec(ctx, "docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", rehearsalName)
	said.memory = "killed"
	if killed, err := r.exec(ctx, "docker", "inspect", "--format", "{{.State.OOMKilled}}", rehearsalName); err == nil && strings.TrimSpace(killed) == "false" {
		said.memory = "ok"
	}
	said.plan = r.plan(ctx)
	return said
}

// copyDatabase copies production's database into the rehearsal's volume,
// read-only and as HQ's account, so a shared-memory file SQLite makes beside
// the live database is one HQ owns.
func (r *rehearsing) copyDatabase(ctx context.Context) bool {
	user := fmt.Sprintf("%d:%d", WebUID, WebGID)
	for _, dir := range []string{r.scratch, filepath.Join(r.scratch, "bell"), filepath.Join(r.scratch, "plan", "ssh"), filepath.Join(r.scratch, "plan", "acme")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			fmt.Fprintln(r.log, err)
			return false
		}
	}
	for _, step := range [][]string{
		{"docker", "volume", "create", rehearsalData},
		{"docker", "volume", "create", rehearsalBridge},
		{"docker", "run", "--rm", "--pull", "never", "--user", "0:0", "--network", "none", "--entrypoint", "sh",
			"--mount", "type=volume,source=" + rehearsalData + ",target=/data", r.image,
			"-c", "mkdir -p /data/tmp && chown -R " + user + " /data"},
		{"docker", "run", "--rm", "--pull", "never", "--user", user, "--network", "none", "--entrypoint", "python",
			"--mount", "type=volume,source=" + r.data + ",target=/src",
			"--mount", "type=volume,source=" + rehearsalData + ",target=/data", r.image, "-c", backupScript},
		{"chown", user, filepath.Join(r.scratch, "bell")},
	} {
		if _, err := r.exec(ctx, step[0], step[1:]...); err != nil {
			return false
		}
	}
	return true
}

// startWeb starts the image's web process on the copy with no network, as
// the web unit runs it otherwise, and waits for it to answer ready.
func (r *rehearsing) startWeb(ctx context.Context) bool {
	appEnv := "/run/secrets/" + AppEnvName
	if _, err := r.exec(ctx, "docker", "run", "-d", "--pull", "never", "--name", rehearsalName,
		"--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
		"--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
		"--tmpfs", fmt.Sprintf("/media:uid=%d,gid=%d", WebUID, WebGID),
		"--tmpfs", fmt.Sprintf("/exports:uid=%d,gid=%d", WebUID, WebGID),
		"--pids-limit", "512", "--memory", "1g",
		"--env", "TMPDIR=/data/tmp",
		"--env", "SEVERINO_CONTROLLER_DOORBELL="+RunDir+"/controller-doorbell",
		"--env", "SEVERINO_BRIDGE_SOCKET="+BridgeSocket,
		"--env", "SEVERINO_APP_ENV_PATH="+appEnv,
		"--mount", "type=volume,source="+rehearsalData+",target=/data",
		"--mount", "type=volume,source="+rehearsalBridge+",target="+filepath.Dir(BridgeSocket),
		"--mount", "type=bind,source="+filepath.Join(r.scratch, "bell")+",target="+RunDir,
		"--mount", readOnly(filepath.Join(r.Rehearsal.SecretDir, "web", AppEnvName), appEnv),
		r.image); err != nil {
		return false
	}
	ready := false
	for deadline := time.Now().Add(rehearsalReady); time.Now().Before(deadline); {
		if _, err := r.exec(ctx, "docker", "exec", rehearsalName, "hq-controller", "ready", rehearsalReadyURL); err == nil {
			ready = true
			break
		}
		if running, _ := r.exec(ctx, "docker", "inspect", "--format", "{{.State.Running}}", rehearsalName); strings.TrimSpace(running) != "true" {
			break
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(r.Rehearsal.Poll):
		}
	}
	_, _ = r.exec(ctx, "docker", "logs", "--tail", "200", rehearsalName)
	return ready
}

// plan runs the image's controller in plan mode on copies of what the host
// rendered for a pass. The host's launcher is not used: it runs the live
// image.
func (r *rehearsing) plan(ctx context.Context) string {
	plan := filepath.Join(r.scratch, "plan")
	defer os.RemoveAll(plan)
	copies := [][]string{{"cp", filepath.Join(r.Rehearsal.SecretDir, "controller-connections.json"), filepath.Join(plan, "connections.json")}}
	for _, dir := range [][2]string{{filepath.Join(r.Rehearsal.SecretDir, "ssh"), "ssh"}, {r.Rehearsal.ACME, "acme"}} {
		if info, err := os.Stat(dir[0]); err == nil && info.IsDir() {
			copies = append(copies, []string{"cp", "-R", dir[0] + "/.", filepath.Join(plan, dir[1]) + "/"})
		}
	}
	copies = append(copies, []string{"chown", "-R", fmt.Sprintf("%d:%d", WebUID, WebGID), plan})
	for _, step := range copies {
		if _, err := r.exec(ctx, step[0], step[1:]...); err != nil {
			return "uncopied"
		}
	}
	args := []string{"run", "--rm", "--pull", "never", "--network", "host",
		"--user", fmt.Sprintf("%d:%d", WebUID, WebGID), "--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--tmpfs", "/tmp:size=64m,noexec,nosuid,nodev", "--entrypoint", "/usr/local/bin/hq-controller",
		"--mount", readOnly(filepath.Join(plan, "connections.json"), controllerConnections),
		"--mount", readOnly(filepath.Join(plan, "ssh"), controllerSSH),
		"--mount", "type=bind,source=" + filepath.Join(plan, "acme") + ",target=" + controllerACME,
		"--mount", "type=volume,source=" + rehearsalBridge + ",target=" + filepath.Dir(BridgeSocket) + ",readonly",
	}
	if op, err := r.Rehearsal.LookPath("op"); err == nil {
		args = append(args, "--mount", readOnly(op, "/usr/bin/op"))
	}
	args = append(args,
		"--env", "HQ_CONTROLLER_CONNECTIONS="+controllerConnections,
		"--env", "HQ_CONTROLLER_SSH_DIR="+controllerSSH,
		"--env", "HQ_ACME_DIR="+controllerACME,
		"--env", "HQ_CONTROLLER_IMAGE="+r.image,
		"--env", "SEVERINO_BRIDGE_SOCKET="+BridgeSocket,
		// No --apply: a plan reads providers and writes to none.
		r.image)
	_, err := r.exec(ctx, "docker", args...)
	var exit *exec.ExitError
	switch {
	case err == nil:
		return "ok"
	case ctx.Err() != nil:
		return "timeout"
	case errors.As(err, &exit):
		return fmt.Sprintf("exit-%d", exit.ExitCode())
	default:
		return "unrun"
	}
}

// liveMarks is when the live web unit and its container last started.
func (r *rehearsing) liveMarks(ctx context.Context) string {
	unit, _ := r.exec(ctx, "systemctl", "show", WebUnit, "-p", "ActiveEnterTimestamp", "--value")
	container, _ := r.exec(ctx, "docker", "inspect", "--format", "{{.State.StartedAt}}", Container)
	return strings.TrimSpace(unit) + " " + strings.TrimSpace(container)
}

// clear removes everything the rehearsal makes, whether or not it is there.
func (r *rehearsing) clear() {
	ctx := context.Background()
	_, _ = r.exec(ctx, "docker", "rm", "--force", rehearsalName)
	_, _ = r.exec(ctx, "docker", "volume", "rm", "--force", rehearsalData, rehearsalBridge)
	if err := os.RemoveAll(r.scratch); err != nil {
		fmt.Fprintln(r.log, err)
	}
}

// exec runs one step with its output in the private log, and returns it.
func (r *rehearsing) exec(ctx context.Context, name string, args ...string) (string, error) {
	output, err := r.Command(ctx, name, args...).CombinedOutput()
	fmt.Fprintf(r.log, "$ %s %s\n%s", name, strings.Join(args, " "), output)
	if err != nil {
		fmt.Fprintf(r.log, "(%v)\n", err)
	}
	return string(output), err
}

// fileSize is a file's size, or nothing for one that is not there.
func fileSize(path string) uint64 {
	if info, err := os.Stat(path); err == nil {
		return uint64(info.Size())
	}
	return 0
}
