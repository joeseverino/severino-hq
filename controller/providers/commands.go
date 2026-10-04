package providers

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Exec runs one process. A start failure is err; a non-zero exit is exit.
type Exec func(ctx context.Context, argv []string, stdin []byte, env []string) (stdout, stderr []byte, exit int, err error)

// Commands runs local tools and SSH operations for one pass. A failure is
// recorded as a step HQ can see; the tool's own output never reaches a result.
type Commands struct {
	Env  runtime.Environment
	Exec Exec
	Log  *slog.Logger

	mu       sync.Mutex
	failures []runtime.StepFailure
}

// CommandOutputLimit bounds what one local tool or SSH call may print, per
// stream. More is a failed step, not a controller run out of memory.
const CommandOutputLimit = 16 << 20

// errOutputLimit is a child that printed more than CommandOutputLimit.
var errOutputLimit = errors.New("output over limit")

func execProcess(ctx context.Context, argv []string, stdin []byte, env []string) ([]byte, []byte, int, error) {
	ctx, cancel := context.WithTimeout(ctx, runtime.CommandTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = bytes.NewReader(stdin)
	cmd.Env = env
	cmd.WaitDelay = runtime.ProcessWaitDelay
	stdout, stderr := runtime.BoundedBuffer{Limit: CommandOutputLimit}, runtime.BoundedBuffer{Limit: CommandOutputLimit}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		return nil, nil, 0, context.DeadlineExceeded
	}
	if stdout.Overflow || stderr.Overflow {
		return nil, nil, 0, errOutputLimit
	}
	var exited *exec.ExitError
	if errors.As(err, &exited) {
		return stdout.Bytes(), stderr.Bytes(), exited.ExitCode(), nil
	}
	return stdout.Bytes(), stderr.Bytes(), 0, err
}

func (c *Commands) logger() *slog.Logger {
	if c.Log != nil {
		return c.Log
	}
	return slog.Default()
}

// StepFailures is what this pass could not finish, in the order it failed.
func (c *Commands) StepFailures() []runtime.StepFailure {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]runtime.StepFailure{}, c.failures...)
}

func (c *Commands) record(step, subject, reason string) {
	c.mu.Lock()
	c.failures = append(c.failures, runtime.StepFailure{Step: step, Subject: subject, Reason: reason})
	c.mu.Unlock()
}

// environment is what a child gets: the allowlisted variables plus this call's
// overrides. Connection credentials are never inherited.
func (c *Commands) environment(overrides map[string]string) []string {
	return c.Env.ChildEnvironment(overrides)
}

func redacted(text string, overrides map[string]string) string {
	for _, value := range overrides {
		if value != "" {
			text = strings.ReplaceAll(text, value, "[redacted]")
		}
	}
	return text
}

// startFailure names a process that never ran as Python's subprocess would.
func startFailure(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return "TimeoutExpired"
	case errors.Is(err, exec.ErrNotFound), errors.Is(err, fs.ErrNotExist):
		return "FileNotFoundError"
	case errors.Is(err, fs.ErrPermission):
		return "PermissionError"
	}
	return "OSError"
}

func lastLine(stderr string) string {
	lines := []string{}
	for _, line := range strings.Split(stderr, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			lines = append(lines, line)
		}
	}
	if len(lines) == 0 {
		return ""
	}
	line := lines[len(lines)-1]
	if clipped := runtime.Clip(line, runtime.SaidLimit); clipped == line {
		return line
	}
	return runtime.Clip(line, runtime.SaidLimit-1) + "…"
}

// Run runs a local command. step names what failed; env is added for this one
// call and struck out of anything logged.
func (c *Commands) Run(ctx context.Context, argv []string, input []byte, step, subject string, env map[string]string) ([]byte, error) {
	run := c.Exec
	if run == nil {
		run = execProcess
	}
	stdout, stderr, exit, err := run(ctx, argv, input, c.environment(env))
	if errors.Is(err, errOutputLimit) {
		c.logger().Warn(fmt.Sprintf("controller step failed: %s (output over limit)", step),
			slog.String("event", "controller.step.failed"), slog.String("step", step))
		c.record(step, subject, "output over limit")
		return nil, &ProviderError{Message: step + " failed."}
	}
	if err != nil {
		kind := startFailure(err)
		c.logger().Warn(fmt.Sprintf("controller step failed: %s (%s)", step, kind),
			slog.String("event", "controller.step.failed"), slog.String("step", step), slog.String("exception", kind))
		c.record(step, subject, kind)
		return nil, &ProviderError{Message: step + " could not complete."}
	}
	if exit != 0 {
		said := lastLine(redacted(string(stderr), env))
		suffix := ""
		if said != "" {
			suffix = ": " + said
		}
		c.logger().Warn(fmt.Sprintf("controller step failed: %s (exit %d)%s", step, exit, suffix),
			slog.String("event", "controller.step.failed"), slog.String("step", step), slog.Int("exit_code", exit))
		c.record(step, subject, "exit "+strconv.Itoa(exit))
		return nil, &ProviderError{Message: step + " failed."}
	}
	return stdout, nil
}

// SSH runs one forced operation on a connection's host, with host keys pinned
// and nothing read from an SSH config.
func (c *Commands) SSH(ctx context.Context, ref, operation string, payload []byte) ([]byte, error) {
	target, err := c.Env.SSH(ref)
	if err != nil {
		return nil, err
	}
	dir, err := c.Env.Required("HQ_CONTROLLER", "SSH_DIR")
	if err != nil {
		return nil, err
	}
	argv := []string{
		"ssh", "-F", "/dev/null",
		"-o", "BatchMode=yes",
		"-o", "IdentitiesOnly=yes",
		"-o", "StrictHostKeyChecking=yes",
		"-o", "UserKnownHostsFile=" + filepath.Join(dir, "known_hosts"),
		"-o", "GlobalKnownHostsFile=/dev/null",
		"-o", "ConnectTimeout=" + strconv.Itoa(runtime.SSHConnectTimeoutSeconds),
		"-i", filepath.Join(dir, ref),
		"-p", strconv.Itoa(target.Port),
		"--", target.User + "@" + target.Host,
		operation,
	}
	return c.Run(ctx, argv, payload, "SSH "+operation+" for "+ref, ref, nil)
}
