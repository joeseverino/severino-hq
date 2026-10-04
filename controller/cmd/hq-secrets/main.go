// Command hq-secrets is the root renderer: one run reads the host's 1Password
// vault through the Connect server on this machine and installs the
// application environment, the controller's connections document, and the
// SSH identities and signing keys. systemd runs it at boot and hourly
// (deploy/systemd/severino-hq-secrets.service). It holds no provider code.
package main

import (
	"context"
	"flag"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
)

// The web container's account, which owns the application environment.
const webUID, webGID = 10001, 10001

// exitCodes gives each failure class its own status, so a unit's result says
// which kind of failure it was without anyone reading a message.
var exitCodes = map[string]int{
	"internal": 1, "config": 2, "host": 3, "busy": 4, "connect_unavailable": 5,
	"connect_denied": 6, "connect_response": 7, "content": 8, "web_unhealthy": 9,
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	code := run(ctx, os.Args[1:], os.Getenv, os.Geteuid(), install.UnprivilegedPortStart, os.Stderr)
	stop()
	os.Exit(code)
}

var containerName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

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
	// The renderer reads through Connect and nothing else. A host still
	// configured for another backend is refused, not quietly switched.
	if backend := getenv("SEVERINO_SECRETS_BACKEND"); backend != "" && backend != "connect" {
		return secrets.Config{}, "", configError("SEVERINO_SECRETS_BACKEND names a backend that no longer exists; the renderer reads through Connect.")
	}
	if getenv("SEVERINO_CONTROLLER_ENV") != "" {
		return secrets.Config{}, "", configError("Remove SEVERINO_CONTROLLER_ENV; configure SEVERINO_CONTROLLER_SECRET_DIR consistently instead.")
	}
	value := func(name, fallback string) string {
		if found := getenv(name); found != "" {
			return found
		}
		return fallback
	}
	runtime := value("SEVERINO_CONTROLLER_SECRET_DIR", "/run/severino-hq-secrets")
	container := value("HQ_CONTAINER", "severino-hq")
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
			RootUID:    0, RootGID: 0, WebUID: webUID, WebGID: webGID,
		},
		RegistryPath:    *registry,
		MinAppVariables: 15,
		ConnectTimeout:  *timeout,
		FullEvery:       24 * time.Hour,
		HealthAttempts:  12,
		HealthInterval:  5 * time.Second,
	}, container, nil
}

func run(ctx context.Context, args []string, getenv func(string) string, euid int, portFloor func() (int, error), stderr io.Writer) int {
	log := slog.New(slog.NewTextHandler(stderr, nil))
	fail := func(err error) int {
		class := secrets.Class(err)
		// The message is the renderer's own words: no value, no token, no URL.
		log.Error(err.Error(), slog.String("event", "secrets.render.failed"), slog.String("class", class))
		return exitCodes[class]
	}
	config, container, err := configure(args, getenv, stderr)
	if err != nil {
		return fail(err)
	}
	if euid != 0 {
		return fail(configError("hq-secrets must run as root."))
	}
	runner := &secrets.Runner{
		Config: config, Mounts: install.SystemMounts, PortFloor: portFloor, Web: secrets.DockerWeb{Container: container},
		Log: log, Now: time.Now,
		Sleep: func(ctx context.Context, d time.Duration) error {
			timer := time.NewTimer(d)
			defer timer.Stop()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-timer.C:
				return nil
			}
		},
	}
	if _, err := runner.Run(ctx); err != nil {
		return fail(err)
	}
	return 0
}
