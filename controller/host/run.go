package host

import (
	"context"
	"errors"
	"fmt"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// WebRunner is the web unit's main process. It waits for the rendered
// environment, runs the container in the foreground, and tells the service
// manager HQ is ready only once HQ answers its health check: a start or a
// restart of the unit succeeds when HQ is healthy and fails when it is not.
type WebRunner struct {
	// Release names the image to run (ReleaseFile on a host).
	Release string
	// SecretDir holds the rendered environment (WebSecretDir on a host).
	SecretDir       string
	RootUID, WebUID int
	// Docker builds a docker command; a test substitutes its own.
	Docker func(ctx context.Context, args ...string) *exec.Cmd
	// Notify sends one sd_notify state.
	Notify func(state string) error
	// Poll is how often the environment and the health are looked at; Await
	// bounds the wait for the environment, after which the start fails and
	// the unit tries again.
	Poll, Await time.Duration
	// StartPeriod is the container's health start period (Web.StartPeriod).
	StartPeriod time.Duration
	// SealPublic is where a controller pass writes the public sealing key
	// (SealPublicKey on a host), given to HQ when it is there.
	SealPublic     string
	Stdout, Stderr io.Writer
	Log            *slog.Logger
}

// ErrNotRendered is a start that found no environment to run on.
var ErrNotRendered = errors.New("the application environment was not rendered")

// Run returns when the container has stopped, or a stop came while it
// waited: nil after a stop the service manager asked for, an error for
// anything else.
func (r *WebRunner) Run(ctx context.Context) error {
	image, err := r.image()
	if err != nil {
		return err
	}
	if err := r.awaitRendered(ctx); err != nil {
		if ctx.Err() != nil {
			return nil
		}
		return err
	}
	// A container left by a crash, or by the compose deployment this
	// replaces, holds the name. It is stopped as a deploy would stop it,
	// then removed. Nothing else may run as HQ.
	_ = r.Docker(ctx, "stop", "--time", StopSeconds, hostcore.Container).Run()
	_ = r.Docker(ctx, "rm", "--force", hostcore.Container).Run()

	// Not ctx: cancelling it would kill the client; the stop below ends HQ.
	web := Web{Image: image, StartPeriod: r.StartPeriod}
	if r.sealPublic() {
		web.SealPublic = r.SealPublic
	}
	container := r.Docker(context.WithoutCancel(ctx), web.Args()...)
	container.Stdout, container.Stderr = r.Stdout, r.Stderr
	if err := container.Start(); err != nil {
		return fmt.Errorf("docker could not start the container: %w", err)
	}
	exited := make(chan error, 1)
	go func() { exited <- container.Wait() }()
	r.Log.Info("HQ is starting.", slog.String("event", "host.web.starting"), slog.String("image", image))
	return r.supervise(ctx, exited)
}

func (r *WebRunner) supervise(ctx context.Context, exited <-chan error) error {
	ticker := time.NewTicker(r.Poll)
	defer ticker.Stop()
	ready := false
	for {
		select {
		case err := <-exited:
			if err == nil {
				err = errors.New("exited")
			}
			return fmt.Errorf("the container stopped on its own: %w", err)
		case <-ctx.Done():
			_ = r.Notify("STOPPING=1")
			_ = r.Docker(context.WithoutCancel(ctx), "stop", "--time", StopSeconds, hostcore.Container).Run()
			<-exited //nolint:errcheck // stopped on purpose, so how it exited says nothing
			return nil
		case <-ticker.C:
			if ready {
				continue
			}
			switch r.health(ctx) {
			case "healthy":
				ready = true
				if err := r.Notify("READY=1\nSTATUS=HQ is healthy."); err != nil {
					return fmt.Errorf("the service manager was not told HQ is ready: %w", err)
				}
				r.Log.Info("HQ is healthy.", slog.String("event", "host.web.ready"))
			case "unhealthy":
				// Past its start period and failing: the start fails now,
				// so a deploy of an image that cannot serve rolls back.
				_ = r.Docker(context.WithoutCancel(ctx), "stop", "--time", StopSeconds, hostcore.Container).Run()
				<-exited //nolint:errcheck // stopped on purpose, so how it exited says nothing
				return errors.New("HQ did not become healthy")
			}
		}
	}
}

// sealPublic is whether HQ is given the public sealing key: only root's file,
// in root's directory, that no other account can write, so the account HQ
// shares with the controller cannot choose the key HQ seals to. Anything
// else, a host whose first pass has not written it included, leaves HQ
// holding no sealed secret rather than not starting.
func (r *WebRunner) sealPublic() bool {
	for _, path := range []string{filepath.Dir(r.SealPublic), r.SealPublic} {
		info, err := os.Lstat(path)
		if err != nil {
			return false
		}
		uid, _, ok := hostcore.Ownership(info)
		if !ok || uid != r.RootUID || info.Mode().Perm()&0o022 != 0 {
			return false
		}
	}
	info, err := os.Lstat(r.SealPublic)
	return err == nil && info.Mode().IsRegular()
}

func (r *WebRunner) image() (string, error) {
	data, err := os.ReadFile(r.Release)
	if err != nil {
		return "", fmt.Errorf("no release is recorded at %s; a deploy writes it", r.Release)
	}
	return Pinned(strings.TrimSpace(string(data)))
}

func (r *WebRunner) awaitRendered(ctx context.Context) error {
	deadline := time.Now().Add(r.Await)
	said := false
	for {
		err := Rendered(r.SecretDir, r.RootUID, r.WebUID)
		if err == nil {
			return nil
		}
		if !said {
			said = true
			_ = r.Notify("STATUS=Waiting for the application environment.")
			r.Log.Info("Waiting for the application environment.", slog.String("event", "host.web.awaiting"), slog.String("reason", err.Error()))
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("%w: %w", ErrNotRendered, err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(r.Poll):
		}
	}
}

func (r *WebRunner) health(ctx context.Context) string {
	out, err := r.Docker(ctx, "inspect", "--format", "{{.State.Health.Status}}", hostcore.Container).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
