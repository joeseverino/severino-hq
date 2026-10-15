package secrets

import (
	"context"
	"strings"

	"github.com/joeseverino/severino-hq/controller/host"
)

// Web is the web container: the one consumer that has to be restarted to load
// a changed application environment.
type Web interface {
	// Installed reports whether the container exists on this host.
	Installed(ctx context.Context) bool
	Restart(ctx context.Context) error
	// Health is the container's health status, or empty when it cannot be read.
	Health(ctx context.Context) string
}

// UnitWeb drives HQ through its unit, which is the only thing that runs the
// container, and reads its health from docker.
type UnitWeb struct{ Unit, Container string }

// Installed is whether HQ is running or starting, so may have read an
// environment this run replaces. A unit that is neither reads the file when
// it next starts.
func (u UnitWeb) Installed(ctx context.Context) bool {
	out, err := host.Command(ctx, "systemctl", "show", "--property", "ActiveState", "--value", u.Unit).Output()
	if err != nil {
		return false
	}
	switch strings.TrimSpace(string(out)) {
	case "active", "activating", "reloading":
		return true
	}
	return false
}

// Restart returns once HQ is healthy on the new environment, or failed to
// be: the unit is ready only when HQ answers its health check.
func (u UnitWeb) Restart(ctx context.Context) error {
	return host.Command(ctx, "systemctl", "restart", u.Unit).Run()
}

func (u UnitWeb) Health(ctx context.Context) string {
	out, err := host.Command(ctx, "docker", "inspect", "--format", "{{.State.Health.Status}}", u.Container).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
