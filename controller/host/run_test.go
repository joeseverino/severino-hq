package host

import (
	"context"
	"errors"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
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
	// inspects counts the health reads, so a scenario can change HQ's
	// health after the first.
	inspects int
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
			h.inspects++
			later := h.inspects > 1
			health := "starting"
			switch {
			case scenario == "healthy", scenario == "unhealthy":
				health = scenario
			case scenario == "sickens":
				health = "healthy"
				if later {
					health = "unhealthy"
				}
			case scenario == "unreadable" && later:
				return exec.CommandContext(ctx, "/bin/sh", "-c", "exit 1")
			case scenario == "unreadable":
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
				_ = h.run.Process.Signal(syscall.SIGTERM)
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
	if err := os.WriteFile(release, []byte("ghcr.io/example/hq@"+digest+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	secrets := filepath.Join(root, "web")
	if err := os.Mkdir(secrets, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(secrets, hostcore.AppEnvName), []byte("A=1\n"), 0o400); err != nil {
		t.Fatal(err)
	}

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

// pings is how many watchdog keep-alives were sent, and how many of them
// came before READY=1.
func (h *harness) pings() (all, early int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	ready := false
	for _, note := range h.notes {
		ready = ready || strings.HasPrefix(note, "READY=1")
		if note == "WATCHDOG=1" {
			all++
			if !ready {
				early++
			}
		}
	}
	return all, early
}

func (h *harness) healthReads() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.inspects
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
	if !strings.HasPrefix(calls, "stop --time 30 "+hostcore.Container+"\nrm --force "+hostcore.Container+"\n") {
		t.Errorf("a stale container is not stopped and removed first:\n%s", calls)
	}
	if !strings.Contains(calls, "stop --time 30 "+hostcore.Container) {
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
	os.Remove(filepath.Join(h.runner.SecretDir, hostcore.AppEnvName))

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
	file := filepath.Join(h.runner.SecretDir, hostcore.AppEnvName)
	os.Remove(file)
	h.runner.Await = 5 * time.Second
	go func() {
		time.Sleep(100 * time.Millisecond)
		if err := os.WriteFile(file, []byte("A=1\n"), 0o400); err != nil {
			t.Error(err)
		}
	}()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for !h.ready() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("a cancelled run is a clean stop, got %v", err)
	}
	if !h.ready() {
		t.Fatal("HQ did not start once the environment was rendered")
	}
}

func TestNoReleaseNoContainer(t *testing.T) {
	h := newHarness(t, "healthy")
	if err := os.WriteFile(h.runner.Release, []byte("ghcr.io/example/hq:latest\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := h.runner.Run(context.Background()); err == nil {
		t.Fatal("a tag-only release was run")
	}
	if h.calls() != "" {
		t.Errorf("docker was called:\n%s", h.calls())
	}
}

func TestAStopWhileWaitingIsNotAFailure(t *testing.T) {
	h := newHarness(t, "healthy")
	os.Remove(filepath.Join(h.runner.SecretDir, hostcore.AppEnvName))
	h.runner.Await = time.Minute
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	if err := h.runner.Run(ctx); err != nil {
		t.Fatalf("a stop while waiting failed the unit: %v", err)
	}
}

func TestAnUnhealthyContainerFailsTheStart(t *testing.T) {
	h := newHarness(t, "unhealthy")

	err := h.runner.Run(context.Background())

	if err == nil || !strings.Contains(err.Error(), "did not become healthy") {
		t.Fatalf("got %v", err)
	}
	if h.ready() {
		t.Error("an unhealthy container was reported ready")
	}
	if !strings.Contains(h.calls(), "stop --time 30 "+hostcore.Container) {
		t.Errorf("the unhealthy container was not stopped:\n%s", h.calls())
	}
}

func TestAHealthyHQPingsTheWatchdogOnEachPoll(t *testing.T) {
	h := newHarness(t, "healthy")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for all, _ := h.pings(); all < 3 && time.Now().Before(deadline); all, _ = h.pings() {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("a requested stop is not a failure: %v", err)
	}
	all, early := h.pings()
	if all < 3 {
		t.Fatalf("a healthy HQ sent %d watchdog pings; it pings on every poll", all)
	}
	if early != 0 {
		t.Errorf("%d watchdog pings came before HQ was ready", early)
	}
}

func TestHQTurningUnhealthyStopsItAndFailsTheUnit(t *testing.T) {
	h := newHarness(t, "sickens")
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(context.Background()) }()

	var err error
	select {
	case err = <-done:
	case <-time.After(5 * time.Second):
		// Stop the run that never ended, so the test leaves nothing behind.
		_ = h.runner.Docker(context.Background(), "stop").Run()
		t.Fatal("HQ turned unhealthy after it was ready and the unit kept running")
	}
	if err == nil {
		t.Fatal("HQ turning unhealthy did not fail the unit, so systemd would not restart it")
	}
	if !h.ready() {
		t.Fatal("HQ was never ready, so this is the start path, not a later failure")
	}
	calls := strings.Split(strings.TrimSpace(h.calls()), "\n")
	if last := calls[len(calls)-1]; last != "stop --time 30 "+hostcore.Container {
		t.Errorf("the unhealthy container was not stopped before the unit failed:\n%s", h.calls())
	}
}

func TestAnUnreadableHealthSendsNoPing(t *testing.T) {
	h := newHarness(t, "unreadable")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.runner.Run(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for h.healthReads() < 4 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("a requested stop is not a failure: %v", err)
	}
	if h.healthReads() < 4 {
		t.Fatal("the health stopped being read once it could not be")
	}
	if !h.ready() {
		t.Fatal("HQ was never ready")
	}
	if all, _ := h.pings(); all != 0 {
		t.Errorf("%d watchdog pings were sent while the health could not be read", all)
	}
}

func TestTheWebUnitsWatchdogOutlastsTwoPolls(t *testing.T) {
	value := unitValue(t, filepath.Join(shippedUnits, "severino-hq-web.service"), "WatchdogSec")
	limit, err := time.ParseDuration(value)
	if err != nil {
		t.Fatalf("WatchdogSec=%q is not a span this test reads: %v", value, err)
	}
	// sd_watchdog_enabled(3): ping at least every half of the limit.
	if WebPoll > limit/2 {
		t.Errorf("the web runner polls every %s, more than half of WatchdogSec=%s, so a healthy HQ can be killed", WebPoll, value)
	}
}
