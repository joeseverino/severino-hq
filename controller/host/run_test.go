package host

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type harness struct {
	runner *WebRunner
	log    []string
	notes  []string
	mu     sync.Mutex
	run    *exec.Cmd
}

// docker is the CLI as the runner tests see it: each call recorded, and the
// container a shell that runs until it is sent SIGTERM.
func (h *harness) docker(scenario string) func(context.Context, ...string) *exec.Cmd {
	return func(ctx context.Context, args ...string) *exec.Cmd {
		h.mu.Lock()
		defer h.mu.Unlock()
		h.log = append(h.log, strings.Join(args, " "))
		switch args[0] {
		case "inspect":
			health := "starting"
			if scenario == "healthy" {
				health = "healthy"
			}
			return exec.CommandContext(ctx, "/bin/sh", "-c", "echo "+health)
		case "run":
			script := `trap 'kill $!; exit 0' TERM; sleep 10 & wait`
			if scenario == "crash" {
				script = "exit 3"
			}
			h.run = exec.CommandContext(ctx, "/bin/sh", "-c", script)
			return h.run
		case "stop":
			if h.run != nil && h.run.Process != nil {
				h.run.Process.Signal(syscall.SIGTERM)
			}
		}
		return exec.CommandContext(ctx, "/bin/sh", "-c", "exit 0")
	}
}

func newHarness(t *testing.T, scenario string) *harness {
	t.Helper()
	root := t.TempDir()
	h := &harness{}
	release := filepath.Join(root, "release")
	os.WriteFile(release, []byte("ghcr.io/example/hq@"+digest+"\n"), 0o644)
	secrets := filepath.Join(root, "web")
	os.Mkdir(secrets, 0o700)
	os.WriteFile(filepath.Join(secrets, AppEnvName), []byte("A=1\n"), 0o400)

	h.runner = &WebRunner{
		Release: release, SecretDir: secrets, RootUID: os.Getuid(), WebUID: os.Getuid(),
		Docker: h.docker(scenario),
		Notify: func(state string) error {
			h.mu.Lock()
			defer h.mu.Unlock()
			h.notes = append(h.notes, state)
			return nil
		},
		Poll: 20 * time.Millisecond, Await: 200 * time.Millisecond,
		Stdout: io.Discard, Stderr: io.Discard,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	return h
}

func (h *harness) calls() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.log) == 0 {
		return ""
	}
	return strings.Join(h.log, "\n") + "\n"
}

func (h *harness) ready() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, note := range h.notes {
		if strings.HasPrefix(note, "READY=1") {
			return true
		}
	}
	return false
}

func TestHQIsReadyOnlyWhenHealthyAndStopsCleanly(t *testing.T) {
	h := newHarness(t, "healthy")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for !h.ready() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !h.ready() {
		t.Fatal("never ready on a healthy container")
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("a requested stop is not a failure: %v", err)
	}
	calls := h.calls()
	if !strings.HasPrefix(calls, "stop --time 30 "+Container+"\nrm --force "+Container+"\n") {
		t.Errorf("a stale container is not stopped and removed first:\n%s", calls)
	}
	if !strings.Contains(calls, "stop --time 30 "+Container) {
		t.Errorf("HQ was not asked to stop:\n%s", calls)
	}
}

func TestAStartingContainerIsNotReady(t *testing.T) {
	h := newHarness(t, "starting")
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	if err := h.runner.Run(ctx); err != nil {
		t.Fatalf("stop: %v", err)
	}
	if h.ready() {
		t.Error("ready before HQ was healthy")
	}
}

func TestAContainerThatDiesFailsTheUnit(t *testing.T) {
	h := newHarness(t, "crash")

	err := h.runner.Run(context.Background())

	if err == nil || !strings.Contains(err.Error(), "stopped on its own") {
		t.Fatalf("a crash was not a failure: %v", err)
	}
}

func TestNoEnvironmentNoContainer(t *testing.T) {
	h := newHarness(t, "healthy")
	os.Remove(filepath.Join(h.runner.SecretDir, AppEnvName))

	err := h.runner.Run(context.Background())

	if !errors.Is(err, ErrNotRendered) {
		t.Fatalf("got %v", err)
	}
	if strings.Contains(h.calls(), "run ") {
		t.Errorf("a container was started without its environment:\n%s", h.calls())
	}
}

func TestTheEnvironmentIsWaitedFor(t *testing.T) {
	h := newHarness(t, "healthy")
	file := filepath.Join(h.runner.SecretDir, AppEnvName)
	os.Remove(file)
	h.runner.Await = 5 * time.Second
	go func() {
		time.Sleep(100 * time.Millisecond)
		os.WriteFile(file, []byte("A=1\n"), 0o400)
	}()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for !h.ready() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done
	if !h.ready() {
		t.Fatal("HQ did not start once the environment was rendered")
	}
}

func TestNoReleaseNoContainer(t *testing.T) {
	h := newHarness(t, "healthy")
	os.WriteFile(h.runner.Release, []byte("ghcr.io/example/hq:latest\n"), 0o644)

	if err := h.runner.Run(context.Background()); err == nil {
		t.Fatal("a tag-only release was run")
	}
	if h.calls() != "" {
		t.Errorf("docker was called:\n%s", h.calls())
	}
}

func TestAStopWhileWaitingIsNotAFailure(t *testing.T) {
	h := newHarness(t, "healthy")
	os.Remove(filepath.Join(h.runner.SecretDir, AppEnvName))
	h.runner.Await = time.Minute
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	if err := h.runner.Run(ctx); err != nil {
		t.Fatalf("a stop while waiting failed the unit: %v", err)
	}
}
