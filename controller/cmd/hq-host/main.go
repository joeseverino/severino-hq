// Command hq-host is the host's side of HQ: what systemd and the deploy run
// to keep HQ running on this machine.
//
//	hq-host run-web          the web container (severino-hq-web.service)
//	hq-host controller       one controller pass, planning; --apply to apply
//	hq-host install          install the release in the root-owned tree
//	hq-host deploy IMAGE     verify and pull IMAGE, then hand over to its hq-host
//	hq-host release IMAGE    switch HQ to IMAGE, install it, or roll back
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
)

const usage = "usage: hq-host run-web | controller [--apply] | install | deploy IMAGE | release [--stage DIR] IMAGE"

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
	if euid != 0 {
		fmt.Fprintln(stderr, "hq-host must run as root.")
		return 1
	}
	var err error
	switch args[0] {
	case "run-web":
		err = runWeb(ctx, args[1:], getenv, stdout, stderr, log)
	case "controller":
		apply := len(args) == 2 && args[1] == "--apply"
		if len(args) > 2 || (len(args) == 2 && !apply) {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		err = controller(stdout, stderr).Run(ctx, apply)
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
	runner := &host.WebRunner{
		Release:   host.ReleaseFile,
		SecretDir: host.WebSecretDir,
		RootUID:   0, WebUID: host.WebUID,
		Docker: func(ctx context.Context, args ...string) *exec.Cmd { return host.Command(ctx, "docker", args...) },
		Notify: func(state string) error { return host.Notify(getenv("NOTIFY_SOCKET"), state) },
		Poll:   2 * time.Second,
		// The renderer's own bound on a run, so a slow boot render is waited
		// out and a failed one retried when the unit restarts.
		Await:  4 * time.Minute,
		Stdout: stdout, Stderr: stderr, Log: log,
	}
	return runner.Run(ctx)
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

func machine(stdout io.Writer) *host.Machine {
	return &host.Machine{
		Lib:      "/usr/local/lib/severino-hq",
		Systemd:  "/etc/systemd/system",
		Tmpfiles: "/etc/tmpfiles.d",
		LogDir:   "/var/log/severino-hq",
		State:    host.StateDir,
		Sbin:     "/usr/local/sbin",
		Runtime:  "/run",
		Command:  host.Command,
		Out:      stdout,
	}
}

func controller(stdout, stderr io.Writer) *host.Controller {
	return &host.Controller{
		Machine:     machine(stdout),
		SecretDir:   host.SecretDir,
		ACME:        host.ACMEDir,
		CertStore:   "/usr/local/share/ca-certificates",
		Tailscaled:  "/var/run/tailscale/tailscaled.sock",
		Connections: requireConnections,
		Lock:        host.SharedLock(filepath.Join(host.SecretDir, "ssh.lock"), time.Minute),
		Owner:       func(root *os.Root, name string) error { return root.Lchown(name, host.WebUID, host.WebGID) },
		LookPath:    exec.LookPath,
		Now:         time.Now,
		Stdout:      stdout, Stderr: stderr,
	}
}

func requireConnections() error {
	return install.RequireConnections(host.SecretDir, install.SystemMounts)
}

func deployer(stdout io.Writer) *host.Deployer {
	return &host.Deployer{
		Machine:   machine(stdout),
		Cosign:    "/usr/local/lib/severino-hq-verifier/cosign",
		Exec:      syscall.Exec,
		Drain:     3 * time.Minute,
		Poll:      5 * time.Second,
		FreeBytes: 512 << 20,
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
