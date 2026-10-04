package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// stubBridge plays HQ's bridge command: it logs each call, keeps each payload,
// and answers each action from a canned, contract-valid response.
const stubBridge = `#!/bin/sh
dir="$(dirname "$0")"
shift 6
action="$1"
echo "$*" >> "$dir/calls.log"
calls="$(wc -l < "$dir/calls.log" | tr -d ' ')"
case " $* " in *" --payload - "*) cat > "$dir/payload-$calls.json" ;; esac
cat "$dir/responses/$action.json"
`

// strict decodes one contract value, refusing unknown fields.
func strict(t *testing.T, data []byte, into any) {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		t.Fatalf("not contract-valid %T: %v\n%s", into, err, data)
	}
}

var responses = map[string]any{
	"registry": runtime.ControllerRegistry{
		OK: true,
		Capabilities: []runtime.Capability{
			{Kind: "adguard.rewrite", Action: "reconcile"},
			{Kind: "pki.authority", Action: "reconcile"},
		},
		Locked:                []runtime.LockedAction{{Kind: "portainer.container", Action: "reconcile", Reason: "Defined outside HQ."}},
		MaterialKinds:         []runtime.ResourceKind{"tls.uploaded_certificate"},
		ConnectionProviders:   map[string][]runtime.ConnectionProvider{"adguard.rewrite": {"adguard"}},
		Observations:          map[string]string{"host.firewall": "local"},
		ConnectionCredentials: []runtime.ConnectionProvider{"adguard", "ssh"},
	},
	"glance-plan": runtime.GlancePlan{OK: true, Panels: []runtime.GlancePanelID{},
		Targets: runtime.GlancePlanTargets{Infrastructure: []runtime.GlanceMachineTarget{}}},
	"claim":       map[string]any{"ok": true, "operation": nil},
	"peek":        map[string]any{"ok": true, "operation": nil},
	"sweep-due":   runtime.SweepVerdict{OK: true, Due: true, Carry: []string{}, Forced: []runtime.ForcedRead{}, OnlyKinds: []runtime.ResourceKind{}, IntervalSeconds: 60, Reason: "due"},
	"schedule":    map[string]any{"ok": true},
	"connections": map[string]any{"ok": true},
	"inventory":   map[string]any{"ok": true},
	"steps":       map[string]any{"ok": true},
	"glance":      map[string]any{"ok": true},
}

func setUp(t *testing.T) (binary, dir string) {
	t.Helper()
	dir = t.TempDir()
	binary = filepath.Join(dir, "hq-controller")
	build := exec.Command("go", "build", "-o", binary, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	if err := os.MkdirAll(filepath.Join(dir, "responses"), 0o755); err != nil {
		t.Fatal(err)
	}
	for action, value := range responses {
		data, _ := json.Marshal(value)
		switch action {
		case "registry":
			strict(t, data, new(runtime.ControllerRegistry))
		case "glance-plan":
			strict(t, data, new(runtime.GlancePlan))
		case "claim", "peek":
			strict(t, data, new(runtime.Pending))
		case "sweep-due":
			strict(t, data, new(runtime.SweepVerdict))
		default:
			strict(t, data, new(runtime.Acknowledgement))
		}
		os.WriteFile(filepath.Join(dir, "responses", action+".json"), data, 0o644)
	}
	if err := os.WriteFile(filepath.Join(dir, "docker"), []byte(stubBridge), 0o755); err != nil {
		t.Fatal(err)
	}
	return binary, dir
}

func runBinary(t *testing.T, binary, dir string, args ...string) (int, []string, []string) {
	t.Helper()
	cmd := exec.Command(binary, args...)
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HQ_DOCKER_BIN=" + filepath.Join(dir, "docker"), "HQ_CONTROLLER_ID=e2e"}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	code := 0
	if exited, ok := err.(*exec.ExitError); ok {
		code = exited.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	log, _ := os.ReadFile(filepath.Join(dir, "calls.log"))
	calls := strings.Split(strings.TrimSpace(string(log)), "\n")
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if code != 0 {
		t.Logf("stderr: %s", stderr.String())
	}
	return code, calls, lines
}

func actions(calls []string) []string {
	found := []string{}
	for _, call := range calls {
		found = append(found, strings.Fields(call)[0])
	}
	return found
}

// TestOneApplyCycle runs the binary for one full apply pass against the stub bridge.
func TestOneApplyCycle(t *testing.T) {
	binary, dir := setUp(t)
	code, calls, lines := runBinary(t, binary, dir, "--apply")
	if code != 0 {
		t.Fatalf("exit %d, output %v", code, lines)
	}
	want := []string{"registry", "glance-plan", "claim", "sweep-due", "connections", "inventory", "analytics", "schedule", "claim", "steps"}
	if got := actions(calls); !slices.Equal(got, want) {
		t.Fatalf("calls %v, want %v", got, want)
	}
	claim := calls[2]
	if !strings.Contains(claim, "--controller-id e2e") || !strings.Contains(claim, "--capability adguard.rewrite:reconcile") {
		t.Fatalf("claim %q", claim)
	}
	if strings.Contains(claim, "pki.authority") {
		t.Fatalf("claimed an action this controller has no handler for: %q", claim)
	}
	var final map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &final); err != nil || final["ok"] != true || final["mode"] != "apply" || final["claimed"] != false {
		t.Fatalf("final line %v", lines)
	}
	payload := func(index int) []byte {
		data, err := os.ReadFile(filepath.Join(dir, "payload-"+strconv.Itoa(index)+".json"))
		if err != nil {
			t.Fatalf("payload %d: %v", index, err)
		}
		return data
	}
	var connections []runtime.ConnectionRecord
	strict(t, payload(5), &connections)
	var inventory runtime.Inventory
	strict(t, payload(6), &inventory)
	if report, ok := inventory["adguard.rewrite"]; !ok || !report.OK || report.Connected == nil || *report.Connected || len(report.Records) != 0 {
		t.Fatalf("adguard.rewrite with no connection should be reported unconnected: %+v", inventory["adguard.rewrite"])
	}
	var analytics runtime.AnalyticsReadings
	strict(t, payload(7), &analytics)
	var steps []runtime.StepFailure
	strict(t, payload(10), &steps)
	if len(steps) != 0 {
		t.Fatalf("steps %+v", steps)
	}
}

// TestPlanMode is a preflight: it peeks, probes, and writes nothing to HQ.
func TestPlanMode(t *testing.T) {
	binary, dir := setUp(t)
	code, calls, lines := runBinary(t, binary, dir)
	if code != 0 {
		t.Fatalf("exit %d, output %v", code, lines)
	}
	if got := actions(calls); !slices.Equal(got, []string{"registry", "peek"}) {
		t.Fatalf("calls %v", got)
	}
	var plan map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &plan); err != nil || plan["ok"] != true || plan["mode"] != "plan" {
		t.Fatalf("plan %v", lines)
	}
}

// TestBridgeFailure reports a bridge it cannot start as one JSON line and exit 1.
func TestBridgeFailure(t *testing.T) {
	binary, dir := setUp(t)
	os.Remove(filepath.Join(dir, "docker"))
	code, _, lines := runBinary(t, binary, dir, "--apply")
	if code != 1 || !strings.Contains(lines[len(lines)-1], `"ok":false`) {
		t.Fatalf("exit %d, output %v", code, lines)
	}
}
