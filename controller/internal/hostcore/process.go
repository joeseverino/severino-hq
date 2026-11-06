package hostcore

import (
	"context"
	"os"
	"os/exec"
	"time"
)

// ProcessWaitDelay bounds how long a killed child's pipes are waited on, so a
// grandchild holding them open cannot outlive the deadline.
const ProcessWaitDelay = 5 * time.Second

// RootPath is where root's own programs are, the PATH a command run for root
// gets whatever its caller's was.
const RootPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

// RootEnvironment is root's own environment, never a caller's: a docker CLI
// run with a caller's $HOME or $DOCKER_CONFIG would load that caller's
// plugins and credential helpers as root.
func RootEnvironment() []string {
	return []string{"LC_ALL=C", "PATH=" + RootPath, "HOME=/root"}
}

// Process is the one way the controller's binaries start a program: fixed
// arguments, a bounded wait for its pipes, and env as its whole environment.
// An exec.Cmd whose Env is nil inherits every variable of this process, so
// an empty or nil env here gives the program none.
func Process(ctx context.Context, env []string, name string, args ...string) *exec.Cmd {
	command := exec.CommandContext(ctx, name, args...)
	command.WaitDelay = ProcessWaitDelay
	command.Env = append([]string{}, env...)
	return command
}

// Command runs a program with fixed arguments and an environment that holds
// nothing of this process's but where to find programs and docker.
func Command(ctx context.Context, name string, args ...string) *exec.Cmd {
	env := []string{}
	for _, variable := range []string{"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG"} {
		if value, ok := os.LookupEnv(variable); ok {
			env = append(env, variable+"="+value)
		}
	}
	return Process(ctx, env, name, args...)
}
