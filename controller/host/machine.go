package host

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
)

// Machine is where an install or a deploy acts, and how it runs a command
// there. Every path is a field so a test can relocate the whole host.
type Machine struct {
	// Lib is the root-owned tree root's units execute from.
	Lib string
	// Systemd and Tmpfiles are where units and tmpfiles rules are installed.
	Systemd, Tmpfiles string
	// LogDir holds each step's full output, readable by root alone: the
	// deploy's own output is a public Actions log.
	LogDir string
	// State holds the release record.
	State string
	// Sbin held programs earlier releases installed; none is installed now.
	Sbin string
	// Runtime is /run, where the deploy stages what it must not leave behind.
	Runtime string
	// Sudoers is where the runner's sudo rule is installed.
	Sudoers string
	// Verifier installs the signature verifier a deploy uses.
	Verifier func(ctx context.Context) error
	// Command builds a command; a test substitutes its own.
	Command func(ctx context.Context, name string, args ...string) *exec.Cmd
	// Own makes a path in the tree root's; a test, run unprivileged, does not.
	Own func(path string) error
	// Out is the public log: one line per step, nothing a host is made of.
	Out io.Writer
}

// UnitDir is the shipped unit directory in the tree.
func (m *Machine) UnitDir() string { return filepath.Join(m.Lib, "deploy", "systemd") }

// ReleasePath is the release record's path.
func (m *Machine) ReleasePath() string { return filepath.Join(m.State, "release") }

// run runs a command whose output is safe for the public log: systemctl and
// docker verbs that print nothing of the host's.
func (m *Machine) run(ctx context.Context, name string, args ...string) error {
	command := m.Command(ctx, name, args...)
	if output, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("%s %v: %w: %s", name, args, err, output)
	}
	return nil
}

// private runs a command with its output in LogDir/log.log and only whether
// it passed in the public log.
func (m *Machine) private(ctx context.Context, label, log, name string, args ...string) error {
	if err := os.MkdirAll(m.LogDir, 0o700); err != nil {
		return err
	}
	file, err := os.CreateTemp(m.LogDir, "."+log+".")
	if err != nil {
		return err
	}
	command := m.Command(ctx, name, args...)
	command.Stdout, command.Stderr = file, file
	runErr := command.Run()
	file.Close()
	if err := os.Rename(file.Name(), filepath.Join(m.LogDir, log+".log")); err != nil {
		os.Remove(file.Name())
		return err
	}
	if runErr != nil {
		fmt.Fprintf(m.Out, "%s failed; inspect the private host log.\n", label)
		return fmt.Errorf("%s failed: %w", label, runErr)
	}
	fmt.Fprintf(m.Out, "%s passed.\n", label)
	return nil
}

// Command runs a program with fixed arguments and an environment that holds
// nothing of this process's but where to find programs and docker.
func Command(ctx context.Context, name string, args ...string) *exec.Cmd {
	command := exec.CommandContext(ctx, name, args...)
	command.Env = []string{}
	for _, variable := range []string{"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG"} {
		if value, ok := os.LookupEnv(variable); ok {
			command.Env = append(command.Env, variable+"="+value)
		}
	}
	return command
}

// active reports whether systemd says unit is active.
func (m *Machine) active(ctx context.Context, unit string) bool {
	return m.Command(ctx, "systemctl", "is-active", "--quiet", unit).Run() == nil
}
