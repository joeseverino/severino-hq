package runtime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"reflect"
	"sort"
	"strings"
	"testing"
)

type bridgeCall struct {
	Args    []string
	Payload any
}
type fakeBridge struct {
	Calls     []bridgeCall
	Responses map[string]any
	Fail      map[string]error
	Pending   []Pending
}

func (b *fakeBridge) Call(_ context.Context, args []string, payload, into any) error {
	b.Calls = append(b.Calls, bridgeCall{append([]string{}, args...), payload})
	if err := b.Fail[args[0]]; err != nil {
		return err
	}
	result := b.Responses[args[0]]
	if args[0] == "claim" && len(b.Pending) > 0 {
		result = b.Pending[0]
		b.Pending = b.Pending[1:]
	}
	if result == nil && args[0] == "sweep-due" {
		result = SweepVerdict{OK: true, Due: true, Carry: []string{}, Forced: []ForcedRead{}, OnlyKinds: []ResourceKind{}, Reason: "Nothing has been swept yet."}
	}
	if result == nil {
		result = Object{}
	}
	if into == nil {
		return nil
	}
	return decodeObject(result, into)
}
func (b *fakeBridge) actions() []string {
	a := []string{}
	for _, c := range b.Calls {
		a = append(a, c.Args[0])
	}
	return a
}

type fakeProviders struct {
	Rounds         [][]ConnectionRecord
	ProbeCalls     int
	Apply          []bool
	ExecErr        error
	Carried        []string
	Only           []ResourceKind
	Sites          []AnalyticsSiteIdentity
	AnalyticsCalls int
	SnapshotOpen   bool
	Material       bool
	Executed       []Resource
}

func (*fakeProviders) Capabilities() []Capability        { return []Capability{{"example.kind", "reconcile"}} }
func (p *fakeProviders) NeedsMaterial(ResourceKind) bool { return p.Material }
func (p *fakeProviders) Execute(_ context.Context, resource Resource, _ string, apply bool) (Result, error) {
	p.Apply = append(p.Apply, apply)
	p.Executed = append(p.Executed, resource)
	return Result{Changed: true, Status: Object{"ready": true}, Message: "Current."}, p.ExecErr
}
func (p *fakeProviders) Connections(_ context.Context, carry []string) ([]ConnectionRecord, error) {
	p.Carried = carry
	p.ProbeCalls++
	if len(p.Rounds) == 0 {
		return []ConnectionRecord{}, nil
	}
	i := min(p.ProbeCalls-1, len(p.Rounds)-1)
	return append([]ConnectionRecord{}, p.Rounds[i]...), nil
}
func (p *fakeProviders) Inventory(_ context.Context, only []ResourceKind) (Inventory, error) {
	p.Only = only
	return Inventory{}, nil
}
func (p *fakeProviders) AnalyticsSites(context.Context) ([]AnalyticsSiteIdentity, error) {
	return p.Sites, nil
}
func (p *fakeProviders) Analytics(context.Context, []AnalyticsSiteIdentity, []AnalyticsWindow) (AnalyticsReadings, error) {
	p.AnalyticsCalls++
	return AnalyticsReadings{}, nil
}
func (*fakeProviders) Glance(context.Context, GlancePlan) (GlanceObservations, error) {
	return GlanceObservations{}, nil
}
func (*fakeProviders) StepFailures() []StepFailure { return []StepFailure{} }
func (p *fakeProviders) BeginSnapshot() func() {
	p.SnapshotOpen = true
	return func() { p.SnapshotOpen = false }
}

func newWorker() (*Worker, *fakeBridge, *fakeProviders, *bytes.Buffer) {
	b := &fakeBridge{Responses: map[string]any{}, Fail: map[string]error{}}
	p := &fakeProviders{}
	out := &bytes.Buffer{}
	w := &Worker{ID: "example-controller", Bridge: b, Providers: p, Output: out, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return w, b, p, out
}
func pending(id string) Pending {
	return Pending{Operation: &Operation{ID: id, Action: "reconcile"}, Resource: Resource{Key: "example", Kind: "example.kind", Generation: 7, Spec: Object{}}}
}

func TestPlanDoesNotClaimReportScheduleOrLoadMaterial(t *testing.T) {
	w, b, p, _ := newWorker()
	b.Responses["peek"] = pending("operation")
	p.Material = true
	code, err := w.Run(context.Background(), false)
	if err != nil || code != 0 {
		t.Fatalf("%d %v", code, err)
	}
	if !reflect.DeepEqual(b.actions(), []string{"peek"}) || !reflect.DeepEqual(p.Apply, []bool{false}) {
		t.Fatalf("actions %v apply %v", b.actions(), p.Apply)
	}
}
func TestIdleApplyOrder(t *testing.T) {
	w, b, p, _ := newWorker()
	code, err := w.Run(context.Background(), true)
	want := []string{"glance-plan", "claim", "sweep-due", "connections", "inventory", "analytics", "schedule", "claim", "steps"}
	if code != 0 || err != nil || !reflect.DeepEqual(b.actions(), want) {
		t.Fatalf("%d %v %v", code, err, b.actions())
	}
	if p.SnapshotOpen {
		t.Fatal("snapshot leaked beyond sweep")
	}
}
func TestTargetedSweepDoesNotReadAnalytics(t *testing.T) {
	w, b, p, _ := newWorker()
	b.Responses["sweep-due"] = Object{"due": true, "carry": []string{"example-ssh"}, "only_kinds": []string{"example.kind"}}
	_, err := w.Run(context.Background(), true)
	if err != nil || p.AnalyticsCalls != 0 || !reflect.DeepEqual(p.Only, []ResourceKind{"example.kind"}) || !reflect.DeepEqual(p.Carried, []string{"example-ssh"}) {
		t.Fatalf("%v %#v", err, p)
	}
}
func TestProviderFailureReportsGenerationAndStillSweeps(t *testing.T) {
	w, b, p, _ := newWorker()
	b.Pending = []Pending{pending("first"), pending("second")}
	p.ExecErr = &ProviderError{Message: "Provider refused.", Status: Object{"partial": true}}
	code, err := w.Run(context.Background(), true)
	if err != nil || code != 1 || len(p.Apply) != 1 {
		t.Fatalf("%d %v %#v", code, err, p.Apply)
	}
	for _, call := range b.Calls {
		if call.Args[0] == "report" {
			report, ok := call.Payload.(ControllerReport)
			if !ok || report.Success != false || report.ObservedGeneration != int64(7) {
				t.Fatalf("%#v", call.Payload)
			}
		}
	}
	if !reflect.DeepEqual(b.actions(), []string{"glance-plan", "claim", "report", "sweep-due", "connections", "inventory", "analytics", "schedule", "steps"}) {
		t.Fatal(b.actions())
	}
}
func TestFailedReportDoesNotPreventSweepOrBecomeSuccess(t *testing.T) {
	w, b, _, _ := newWorker()
	b.Pending = []Pending{pending("first")}
	want := errors.New("report unavailable")
	b.Fail["report"] = want
	code, err := w.Run(context.Background(), true)
	if code != 1 || !errors.Is(err, want) {
		t.Fatalf("%d %v", code, err)
	}
	if !reflect.DeepEqual(b.actions(), []string{"glance-plan", "claim", "report", "sweep-due", "connections", "inventory", "analytics", "schedule", "steps"}) {
		t.Fatal(b.actions())
	}
}
func TestQueueIsBoundedBeforeAndAfterSweep(t *testing.T) {
	w, b, p, _ := newWorker()
	for range 15 {
		b.Pending = append(b.Pending, pending("operation"))
	}
	code, err := w.Run(context.Background(), true)
	if code != 0 || err != nil || len(p.Apply) != 2*ApplyLimit {
		t.Fatalf("%d %v calls=%d", code, err, len(p.Apply))
	}
}
func TestNetworkFailureIsRetriedAndOnlyPartialOutageForgiven(t *testing.T) {
	bad := ConnectionRecord{ConnectionRef: "a", OK: false, Failure: "network", Detail: "no answer", Probed: true}
	good := ConnectionRecord{ConnectionRef: "b", OK: true, Probed: true}
	refused := ConnectionRecord{ConnectionRef: "c", OK: false, Failure: "credential", Probed: true}
	for _, tc := range []struct {
		name        string
		rounds      [][]ConnectionRecord
		code, calls int
	}{
		{"recovered", [][]ConnectionRecord{{bad}, {good}}, 0, 2},
		{"partial", [][]ConnectionRecord{{bad, good}, {bad, good}}, 0, 2},
		{"total", [][]ConnectionRecord{{bad}, {bad}}, 1, 2},
		{"credential", [][]ConnectionRecord{{refused, good}}, 1, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w, _, p, _ := newWorker()
			if tc.name == "recovered" {
				tc.rounds[1] = []ConnectionRecord{{ConnectionRef: "a", OK: true, Probed: true}}
			}
			p.Rounds = tc.rounds
			code, err := w.Run(context.Background(), false)
			if err != nil || code != tc.code || p.ProbeCalls != tc.calls {
				t.Fatalf("%d %v probes=%d", code, err, p.ProbeCalls)
			}
		})
	}
}
func TestAnalyticsPlanContainsNoProviderAccountIdentity(t *testing.T) {
	w, b, p, _ := newWorker()
	p.Sites = []AnalyticsSiteIdentity{{ConnectionRef: "example", SiteTag: "site"}}
	_, err := w.Run(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	for _, call := range b.Calls {
		if call.Args[0] == "analytics-plan" {
			want := []AnalyticsSiteIdentity{{ConnectionRef: "example", SiteTag: "site"}}
			if !reflect.DeepEqual(call.Payload, want) {
				t.Fatalf("%#v", call.Payload)
			}
		}
	}
}

// The passes print the lines the Python worker prints, key for key.
func TestPassOutputKeysMatchThePythonWorker(t *testing.T) {
	keys := func(value any) []string {
		raw, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(raw, &fields)
		found := []string{}
		for key := range fields {
			found = append(found, key)
		}
		sort.Strings(found)
		return found
	}
	for name, tc := range map[string]struct {
		value any
		want  []string
	}{
		"idle":    {IdlePassOutput{OK: true, Mode: PassModeApply}, []string{"claimed", "mode", "ok"}},
		"plan":    {PlanPassOutput{Connections: []ConnectionRecord{}, Warnings: []string{}}, []string{"claimed", "connections", "mode", "ok", "plan", "warnings"}},
		"applied": {AppliedOperationOutput{}, []string{"changed", "ok", "operation", "resource"}},
		"refused": {RefusedOperationOutput{}, []string{"message", "ok", "operation", "resource"}},
	} {
		if got := keys(tc.value); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: %v, want %v", name, got, tc.want)
		}
	}
	raw, _ := json.Marshal(PlanPassOutput{})
	if !strings.Contains(string(raw), `"plan":null`) {
		t.Errorf("an empty plan must print null: %s", raw)
	}
}
