// Command hq-secrets is the root renderer: one run reads the host's 1Password
// vault through the Connect server on this machine and installs the
// application environment, the controller's connections document, and the
// SSH identities and signing keys. systemd runs it at boot and hourly
// (deploy/systemd/severino-hq-secrets.service). It holds no provider code.
package main

import (
	"context"
	"flag"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/clock"
	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/secrets"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
)

// exitCodes gives each failure class its own status, so a unit's result says
// which kind of failure it was without anyone reading a message.
var exitCodes = map[secrets.Failure]int{
	secrets.RenderFailureInternal: 1, secrets.RenderFailureConfig: 2, secrets.RenderFailureHost: 3,
	secrets.RenderFailureBusy: 4, secrets.RenderFailureConnectUnavailable: 5, secrets.RenderFailureConnectDenied: 6,
	secrets.RenderFailureConnectResponse: 7, secrets.RenderFailureContent: 8, secrets.RenderFailureWebUnhealthy: 9,
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	code := run(ctx, os.Args[1:], os.Getenv, os.Geteuid(), install.UnprivilegedPortStart, os.Stderr)
	stop()
	os.Exit(code)
}

var containerName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

// fullEvery is the longest an unchanged vault goes without a full read, as
// HQ's registry declares it beside the findings that judge it.
var fullEvery = time.Duration(api.MustLimit("RenderFullReadSeconds", "default")) * time.Second

type configError string

func (e configError) Error() string { return string(e) }
func (configError) Unwrap() error   { return secrets.ErrConfig }

// configure reads the unit's environment. Nothing here is a secret: the token
// arrives as a credential file, never as a variable.
func configure(args []string, getenv func(string) string, stderr io.Writer) (secrets.Config, string, error) {
	options := flag.NewFlagSet("hq-secrets", flag.ContinueOnError)
	options.SetOutput(stderr)
	executable, _ := os.Executable()
	registry := options.String("registry", filepath.Join(filepath.Dir(executable), "..", "..", "hq", "config", "controller-connections.json"),
		"the connection registry, in the root-owned tree")
	timeout := options.Duration("connect-timeout", 75*time.Second, "bound on Connect readiness and the whole read of the vault")
	if err := options.Parse(args); err != nil || options.NArg() != 0 {
		return secrets.Config{}, "", configError("usage: hq-secrets [-registry FILE] [-connect-timeout DURATION]")
	}
	// A token in the environment is a token in /proc and in every child.
	for _, name := range []string{"OP_CONNECT_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN"} {
		if getenv(name) != "" {
			return secrets.Config{}, "", configError("Remove " + name + " from the environment; the token is read from the unit's credentials.")
		}
	}
	value := func(name, fallback string) string {
		if found := getenv(name); found != "" {
			return found
		}
		return fallback
	}
	runtime := value("SEVERINO_CONTROLLER_SECRET_DIR", hostcore.SecretDir)
	container := value("HQ_CONTAINER", hostcore.Container)
	if !containerName.MatchString(container) {
		return secrets.Config{}, "", configError("HQ_CONTAINER is not a container name.")
	}
	return secrets.Config{
		Vault:             getenv("SEVERINO_SECRETS_VAULT"),
		EnvItem:           getenv("SEVERINO_ENV_ITEM"),
		Endpoint:          getenv("OP_CONNECT_HOST"),
		CredentialsDir:    getenv("CREDENTIALS_DIRECTORY"),
		ConnectCredential: getenv("SEVERINO_CONNECT_CREDENTIAL"),
		Layout: install.Layout{
			SecretDir:  value("SEVERINO_HQ_SECRET_DIR", "/opt/apps/severino-hq/secrets"),
			RuntimeDir: runtime,
			WebDir:     value("SEVERINO_HQ_WEB_SECRET_DIR", runtime+"/web"),
			RootUID:    0, RootGID: 0, WebUID: hostcore.WebUID, WebGID: hostcore.WebGID,
		},
		RegistryPath:   *registry,
		ConnectTimeout: *timeout,
		FullEvery:      fullEvery,
		HealthAttempts: 12,
		HealthInterval: 5 * time.Second,
	}, container, nil
}

func run(ctx context.Context, args []string, getenv func(string) string, euid int, portFloor func() (int, error), stderr io.Writer) int {
	log := mask.Logger(stderr)
	fail := func(err error) int {
		class := secrets.Class(err)
		// The message is the renderer's own words: no value, no token, no URL.
		log.Error(err.Error(), slog.String("event", "secrets.render.failed"), slog.String("class", string(class)))
		return exitCodes[class]
	}
	// A dry run makes the host reads a render begins with and nothing else:
	// the rehearsal runs it in the unit's sandbox.
	if len(args) == 1 && args[0] == hostcore.DryRun {
		if _, err := portFloor(); err != nil {
			return fail(err)
		}
		if err := install.SystemMounts(hostcore.SecretDir); err != nil {
			return fail(err)
		}
		return 0
	}
	config, container, err := configure(args, getenv, stderr)
	if err != nil {
		return fail(err)
	}
	if euid != 0 {
		return fail(configError("hq-secrets must run as root."))
	}
	runner := &secrets.Runner{
		Config: config, Mounts: install.SystemMounts, PortFloor: portFloor, Web: secrets.UnitWeb{Unit: hostcore.WebUnit, Container: container},
		Log: log, Now: time.Now, Sleep: clock.Sleep,
	}
	if _, err := runner.Run(ctx); err != nil {
		return fail(err)
	}
	return 0
}
