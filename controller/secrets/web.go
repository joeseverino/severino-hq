package secrets

import (
	"context"
	"os"
	"os/exec"
	"strings"
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

// DockerWeb drives the container through the docker CLI, with fixed arguments
// and an environment that holds nothing of the renderer's.
type DockerWeb struct{ Container string }

func (d DockerWeb) command(ctx context.Context, args ...string) *exec.Cmd {
	command := exec.CommandContext(ctx, "docker", args...)
	command.Env = []string{}
	for _, name := range []string{"PATH", "HOME", "DOCKER_HOST"} {
		if value, ok := os.LookupEnv(name); ok {
			command.Env = append(command.Env, name+"="+value)
		}
	}
	return command
}

func (d DockerWeb) Installed(ctx context.Context) bool {
	return d.command(ctx, "inspect", "--type", "container", d.Container).Run() == nil
}

func (d DockerWeb) Restart(ctx context.Context) error {
	return d.command(ctx, "restart", d.Container).Run()
}

func (d DockerWeb) Health(ctx context.Context) string {
	out, err := d.command(ctx, "inspect", "--format", "{{.State.Health.Status}}", d.Container).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
