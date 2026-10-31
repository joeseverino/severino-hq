// Command hq-host is the host's side of HQ: what systemd and the deploy run
// to keep HQ running on this machine.
//
//	hq-host run-web          the web container (severino-hq-web.service)
//	hq-host controller       one controller pass, planning; --apply to apply,
//	                         --glance to read only the dashboard panels HQ waits on
//	hq-host renovate         run the dependency handoff a pass spooled
//	hq-host install          install the release in the root-owned tree
//	hq-host deploy IMAGE     verify and pull IMAGE, then hand over to its hq-host
//	hq-host release IMAGE    switch HQ to IMAGE, install it, or roll back
//	hq-host sync IMAGE       replace the root-owned tree with IMAGE's
//	hq-host verify           report where the host differs from its release
//	hq-host bootstrap        write the host's own configuration from host.json
//	hq-host backup           back up HQ's volumes: database, uploads and exports
//	hq-host upgrade ARGS     move one compose service to a pinned image (package upgrade)
//	hq-host unit --serve     stop, start or restart the unit a container names (package unit)
//	hq-host check            the deploy host's half of a preflight, as any account
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/host/unit"
	"github.com/joeseverino/severino-hq/controller/host/upgrade"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
)

const usage = "usage: hq-host run-web | controller [--apply | --glance] | renovate | install | deploy IMAGE | release [--stage DIR] IMAGE | sync IMAGE | verify | bootstrap | backup | upgrade ARGS | unit --serve | check --release DIR --main DIR"

// passes are the controller passes `hq-host controller` launches, by flag.
var passes = map[string]host.Pass{"": host.Plan, "--apply": host.Apply, "--glance": host.Glance}

// hostConfig is the host's own configuration, which bootstrap reads.
const hostConfig = "/etc/severino-hq/host.json"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Getenv, os.Geteuid(), os.Stdin, os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

func run(ctx context.Context, args []string, getenv func(string) string, euid int, stdin io.Reader, stdout, stderr io.Writer) int {
	log := slog.New(slog.NewTextHandler(stderr, nil))
	if len(args) == 0 {
		fmt.Fprintln(stderr, usage)
		return 2
	}
	// An upgrade or a unit answers in JSON whatever happens, a refusal to run as
	// anyone but root included.
	if args[0] == "upgrade" {
		return upgrade.Main(ctx, args[1:], getenv, euid, stdout, stderr)
	}
	if args[0] == "unit" {
		return unit.Main(ctx, args[1:], euid, stdin, stdout)
	}
	// A preflight runs its check as the operator's own account.
	if args[0] == "check" {
		return check(ctx, args[1:], stdout, stderr)
	}
	if euid != 0 {
		fmt.Fprintln(stderr, "hq-host must run as root.")
		return 1
	}
	var err error
	switch args[0] {
	case "run-web":
		err = runWeb(ctx, args[1:], getenv, stdout, stderr, log)
	case "controller":
		pass, known := passes[strings.Join(args[1:], " ")]
		if !known {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		err = controller(stdout, stderr).Run(ctx, pass)
	case "renovate":
		if len(args) != 1 {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		err = renovator(stdout).Run(ctx)
	case "install":
		err = machine(stdout).Install(ctx, requireConnections)
	case "deploy":
		if len(args) != 2 {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		err = deployer(stdout).Deploy(ctx, args[1], piped(stdin))
	case "release":
		err = release(ctx, args[1:], stdout)
	case "sync":
		if len(args) != 2 {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		err = machine(stdout).Sync(ctx, args[1])
	case "verify":
		var drift []string
		if drift, err = machine(stdout).Verify(ctx); err == nil && len(drift) > 0 {
			for _, line := range drift {
				fmt.Fprintln(stderr, "drift:", line)
			}
			return 1
		}
		if err == nil {
			fmt.Fprintln(stdout, "The host is its release: the tree, the units, the runner's sudo rules and nothing else.")
		}
	case "backup":
		err = machine(stdout).Backup(ctx, getenv)
	case "bootstrap":
		var data []byte
		if data, err = os.ReadFile(hostConfig); err == nil {
			var config host.Config
			if config, err = host.ReadConfig(data); err == nil {
				err = machine(stdout).Bootstrap(ctx, config)
			}
		}
	default:
		fmt.Fprintln(stderr, usage)
		return 2
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

func runWeb(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer, log *slog.Logger) error {
	if len(args) != 0 {
		return errors.New(usage)
	}
	period, err := host.StartPeriod(getenv(host.StartPeriodEnv))
	if err != nil {
		return err
	}
	runner := &host.WebRunner{
		Release:   host.ReleaseFile,
		SecretDir: host.WebSecretDir,
		RootUID:   0, WebUID: host.WebUID,
		Docker: func(ctx context.Context, args ...string) *exec.Cmd { return host.Command(ctx, "docker", args...) },
		Notify: func(state string) error { return host.Notify(getenv("NOTIFY_SOCKET"), state) },
		Poll:   2 * time.Second,
		// The renderer's own bound on a run, so a slow boot render is waited
		// out and a failed one retried when the unit restarts.
		Await:       host.RenderBound,
		StartPeriod: period,
		Stdout:      stdout, Stderr: stderr, Log: log,
	}
	return runner.Run(ctx)
}

// check is the deploy host's half of a preflight: --release and --main are
// the unit directories this release and main ship.
func check(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	options := flag.NewFlagSet("check", flag.ContinueOnError)
	options.SetOutput(stderr)
	release := options.String("release", "", "the unit directory this release ships")
	main := options.String("main", "", "the unit directory main ships")
	if err := options.Parse(args); err != nil || options.NArg() != 0 || *release == "" || *main == "" {
		fmt.Fprintln(stderr, usage)
		return 2
	}
	checker := &host.Checker{
		Lib: host.Lib, Systemd: "/etc/systemd/system", RootUID: 0,
		Release: *release, Main: *main,
		FreeBytes: host.DeployFreeBytes, Free: host.Free,
		Command: host.Command, Out: stdout,
	}
	return checker.Check(ctx)
}

func release(ctx context.Context, args []string, stdout io.Writer) error {
	options := flag.NewFlagSet("release", flag.ContinueOnError)
	stage := options.String("stage", "", "the directory deploy copied this program into, removed when done")
	if err := options.Parse(args); err != nil || options.NArg() != 1 {
		return errors.New(usage)
	}
	if *stage != "" {
		defer os.RemoveAll(*stage)
	}
	return deployer(stdout).Release(ctx, options.Arg(0))
}

// runtimeDir is where this run stages what it must not leave behind: the
// directory systemd gives a unit that asks for one (RuntimeDirectory=), which
// a sandboxed unit can write, else /run.
func runtimeDir() string {
	if dir, _, _ := strings.Cut(os.Getenv("RUNTIME_DIRECTORY"), ":"); dir != "" {
		return dir
	}
	return "/run"
}

func machine(stdout io.Writer) *host.Machine {
	return &host.Machine{
		Lib:      host.Lib,
		Systemd:  "/etc/systemd/system",
		Tmpfiles: "/etc/tmpfiles.d",
		LogDir:   "/var/log/severino-hq",
		State:    host.StateDir,
		Sbin:     "/usr/local/sbin",
		Runtime:  runtimeDir(),
		Sudoers:  "/etc/sudoers.d",
		Command:  host.Command,
		Own:      func(path string) error { return os.Lchown(path, 0, 0) },
		Verifier: func(ctx context.Context) error {
			client := &http.Client{Timeout: 3 * time.Minute}
			return host.InstallVerifier(ctx, client, host.CosignURL, cosign)
		},
		Out: stdout,
	}
}

// cosign is root's own verifier, outside the tree a deploy replaces.
const cosign = "/usr/local/lib/severino-hq-verifier/cosign"

func controller(stdout, stderr io.Writer) *host.Controller {
	return &host.Controller{
		Machine:       machine(stdout),
		SecretDir:     host.SecretDir,
		ACME:          host.ACMEDir,
		CertStore:     "/usr/local/share/ca-certificates",
		Tailscaled:    "/var/run/tailscale/tailscaled.sock",
		UnitSocket:    host.UnitSocket,
		Spool:         host.RenovateSpool,
		DependencyRun: host.DependencyRunFile,
		Connections:   requireConnections,
		Lock:          host.SharedLock(filepath.Join(host.SecretDir, "ssh.lock"), time.Minute),
		Owner:         func(root *os.Root, name string) error { return root.Lchown(name, host.WebUID, host.WebGID) },
		LookPath:      exec.LookPath,
		Now:           time.Now,
		Stdout:        stdout, Stderr: stderr,
	}
}

func renovator(stdout io.Writer) *host.Renovator {
	return &host.Renovator{
		Machine:    machine(stdout),
		Spool:      host.RenovateSpool,
		Outcome:    host.DependencyRunFile,
		OwnerBound: host.RenovateOwnerBound,
		Now:        time.Now,
	}
}

func requireConnections() error {
	return install.RequireConnections(host.SecretDir, install.SystemMounts)
}

func deployer(stdout io.Writer) *host.Deployer {
	return &host.Deployer{
		Machine:   machine(stdout),
		Source:    host.Source(host.BuildSource),
		Cosign:    cosign,
		Exec:      syscall.Exec,
		Drain:     3 * time.Minute,
		Poll:      5 * time.Second,
		FreeBytes: host.DeployFreeBytes,
		FreeOn:    "/",
		Free:      host.Free,
	}
}

// piped is stdin when something was piped to it: the deploy job writes the
// registry credential there, and a person at a terminal has none to give.
func piped(stdin io.Reader) io.Reader {
	if file, ok := stdin.(*os.File); ok {
		if info, err := file.Stat(); err == nil && info.Mode()&os.ModeCharDevice != 0 {
			return nil
		}
	}
	return stdin
}
