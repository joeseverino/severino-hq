package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// call is one request the stub bridge received.
type call struct {
	Action  string
	Query   url.Values
	Payload []byte
}

// stubBridge plays HQ's bridge: a real HTTP server on a real Unix socket that
// records each call and answers each action from a canned, contract-valid
// response.
type stubBridge struct {
	Socket string
	mu     sync.Mutex
	calls  []call
}

func (s *stubBridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	action := strings.TrimPrefix(r.URL.Path, "/")
	payload, _ := io.ReadAll(r.Body)
	s.mu.Lock()
	s.calls = append(s.calls, call{action, r.URL.Query(), payload})
	s.mu.Unlock()
	answer, known := responses[action]
	if action == "job" {
		answer, known = jobs[r.URL.Query().Get("name")]
	}
	if r.Method != http.MethodPost || !known {
		http.Error(w, "no such action", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(answer)
}

func (s *stubBridge) Calls() []call {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.calls)
}

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
	"analytics":   map[string]any{"ok": true},
}

// jobs is what the stub answers for each piece of scheduled work asked of it.
var jobs = map[string]any{
	"audit.prune":      runtime.JobOutcome{Name: "audit.prune", State: runtime.JobStateSucceeded, Note: "", Job: "7b6c"},
	"content.sync":     runtime.JobOutcome{Name: "content.sync", State: runtime.JobStateFailed, Note: "The index did not answer."},
	"contacts.inbox":   runtime.JobOutcome{Name: "contacts.inbox", State: runtime.JobStateLost, Note: "The job did not finish."},
	"registry.refresh": runtime.JobOutcome{Name: "registry.refresh", State: runtime.JobStateRunning, Note: "Already running."},
}

func setUp(t *testing.T) (binary string, bridge *stubBridge) {
	t.Helper()
	binary = filepath.Join(t.TempDir(), "hq-controller")
	build := exec.Command("go", "build", "-o", binary, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
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
	}
	// A directory only this account can enter, short enough for a socket path.
	dir, err := os.MkdirTemp("", "hqb")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	bridge = &stubBridge{Socket: filepath.Join(dir, "bridge.sock")}
	listener, err := net.Listen("unix", bridge.Socket)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(bridge.Socket, 0o600); err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: bridge}
	go server.Serve(listener)
	t.Cleanup(func() { server.Close() })
	return binary, bridge
}

func runBinary(t *testing.T, binary string, bridge *stubBridge, args ...string) (int, []call, []string) {
	t.Helper()
	cmd := exec.Command(binary, args...)
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), BridgeSocket + "=" + bridge.Socket, "HQ_CONTROLLER_ID=e2e"}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	code := 0
	if exited, ok := err.(*exec.ExitError); ok {
		code = exited.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if code != 0 {
		t.Logf("stderr: %s", stderr.String())
	}
	return code, bridge.Calls(), lines
}

func actions(calls []call) []string {
	found := []string{}
	for _, call := range calls {
		found = append(found, call.Action)
	}
	return found
}

// TestOneApplyCycle runs the binary for one full apply pass against the stub bridge.
func TestOneApplyCycle(t *testing.T) {
	binary, bridge := setUp(t)
	code, calls, lines := runBinary(t, binary, bridge, "--apply")
	if code != 0 {
		t.Fatalf("exit %d, output %v", code, lines)
	}
	want := []string{"registry", "glance-plan", "claim", "sweep-due", "connections", "inventory", "analytics", "schedule", "claim", "steps"}
	if got := actions(calls); !slices.Equal(got, want) {
		t.Fatalf("calls %v, want %v", got, want)
	}
	claim := calls[2].Query
	if claim.Get("controller-id") != "e2e" || !slices.Contains(claim["capability"], "adguard.rewrite:reconcile") {
		t.Fatalf("claim %v", claim)
	}
	if strings.Contains(claim.Encode(), "pki.authority") {
		t.Fatalf("claimed an action this controller has no handler for: %v", claim)
	}
	var final map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &final); err != nil || final["ok"] != true || final["mode"] != "apply" || final["claimed"] != false {
		t.Fatalf("final line %v", lines)
	}
	payload := func(index int) []byte { return calls[index-1].Payload }
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
	binary, bridge := setUp(t)
	code, calls, lines := runBinary(t, binary, bridge)
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

// TestBridgeFailure reports a bridge that is not there as one JSON line and
// exit 1, saying which rule failed: there is no other way to reach HQ to fall
// back to.
func TestBridgeFailure(t *testing.T) {
	binary, bridge := setUp(t)
	// In this order: each case leaves the path as the next one finds it.
	for _, tc := range []struct {
		name    string
		arrange func()
		want    string
	}{
		{"HQ is not serving", func() { os.Remove(bridge.Socket) }, "no socket at its path"},
		{"something else is here", func() { os.WriteFile(bridge.Socket, nil, 0o600) }, "not a socket"},
		{"no socket is named", func() { bridge.Socket = "" }, "SEVERINO_BRIDGE_SOCKET"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.arrange()
			code, _, lines := runBinary(t, binary, bridge, "--apply")
			last := lines[len(lines)-1]
			if code != 1 || !strings.Contains(last, `"ok":false`) || !strings.Contains(last, tc.want) {
				t.Fatalf("exit %d, output %v", code, lines)
			}
		})
	}
}

// TestJob asks HQ for one piece of scheduled work and exits as it ended: work
// done or already under way is success, work that failed or was lost fails
// the unit that asked, and nothing but the bridge is called.
func TestJob(t *testing.T) {
	binary, bridge := setUp(t)
	for _, tc := range []struct {
		args []string
		code int
		want string
	}{
		{[]string{"job", "audit.prune"}, 0, `"state":"succeeded"`},
		{[]string{"job", "registry.refresh"}, 0, `"state":"running"`},
		{[]string{"job", "content.sync"}, 1, "The index did not answer."},
		{[]string{"job", "contacts.inbox"}, 1, `"state":"lost"`},
		{[]string{"job", "no.such"}, 1, `"ok":false`},
		{[]string{"job"}, 2, "usage"},
		{[]string{"job", "audit.prune", "extra"}, 2, "usage"},
	} {
		t.Run(strings.Join(tc.args, " "), func(t *testing.T) {
			before := len(bridge.Calls())
			code, calls, lines := runBinary(t, binary, bridge, tc.args...)
			if code != tc.code || len(lines) == 0 || !strings.Contains(lines[len(lines)-1], tc.want) {
				t.Fatalf("exit %d, output %v", code, lines)
			}
			for _, made := range calls[before:] {
				if made.Action != "job" || made.Query.Get("name") != tc.args[1] {
					t.Fatalf("called %s %v", made.Action, made.Query)
				}
			}
		})
	}
}
