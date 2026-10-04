package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"unicode/utf8"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// dispatchController is a Controller over a bare registry, so these tests hold
// the dispatch rules and none of any provider's behavior.
func dispatchController(env runtime.Environment, declared runtime.ControllerRegistry) *Controller {
	r := &Registry{
		Env: env, actions: map[actionKey]Action{}, readers: map[string]Reader{}, probes: map[runtime.ConnectionProvider]Probe{},
		zoneIDs: map[string]string{},
	}
	return NewController(r, declared)
}

const dispatchKind = runtime.ResourceKindAdGuardRewrite

func TestExecuteRefusesWritesThatNoManagingConnectionApproves(t *testing.T) {
	adguard := runtime.Environment{"ADGUARD_CONNECTION_REF": "dns", "ADGUARD_URL": "https://adguard.example.invalid"}
	with := func(env runtime.Environment, extra map[string]string) runtime.Environment {
		merged := runtime.Environment{}
		for _, source := range []map[string]string{env, extra} {
			maps.Copy(merged, source)
		}
		return merged
	}
	edge := map[string]string{"EDGE_CONNECTION_REF": "edge", "EDGE_HOST": "edge.example.invalid", "EDGE_USER": "deploy", "EDGE_MANAGES": "true"}
	declared := runtime.ControllerRegistry{ConnectionProviders: map[string][]runtime.ConnectionProvider{string(dispatchKind): {"adguard"}}}
	cases := []struct {
		name     string
		env      runtime.Environment
		declared runtime.ControllerRegistry
		spec     Object
		apply    bool
		want     error // nil means the handler ran
	}{
		{"plan skips the gate", adguard, declared, nil, false, nil},
		{"observe-only connection", adguard, declared, nil, true, ErrObserveOnly},
		{"managing connection", with(adguard, map[string]string{"ADGUARD_MANAGES": "true"}), declared, nil, true, nil},
		{"undeclared kind fails closed", with(adguard, map[string]string{"ADGUARD_MANAGES": "true"}), runtime.ControllerRegistry{}, nil, true, ErrUndeclared},
		{"undeclared kind still plans", adguard, runtime.ControllerRegistry{}, nil, false, nil},
		// Regression (Stage 1 M3): a named connection of another provider passed
		// the gate because it managed something.
		{"named connection of another provider", with(with(adguard, edge), map[string]string{"ADGUARD_MANAGES": "true"}), declared, Object{"connection_ref": "edge"}, true, ErrForeignConnection},
		{"named connection nobody supplied", with(adguard, map[string]string{"ADGUARD_MANAGES": "true"}), declared, Object{"connection_ref": "nowhere"}, true, ErrForeignConnection},
		{"two items sharing a ref name neither", with(adguard, map[string]string{"ADGUARD_MANAGES": "true", "DNS2_CONNECTION_REF": "dns", "DNS2_PROVIDER": "adguard", "DNS2_MANAGES": "true"}), declared, nil, true, ErrNoManager},
		{"named own connection that manages", with(adguard, map[string]string{"ADGUARD_MANAGES": "true"}), declared, Object{"connection_ref": "dns"}, true, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			controller := dispatchController(c.env, c.declared)
			ran := false
			controller.action(dispatchKind, "reconcile", func(context.Context, Object, Object, bool) (Result, error) {
				ran = true
				return Result{Changed: true}, nil
			})
			_, err := controller.Execute(t.Context(), runtime.Resource{Kind: dispatchKind, Spec: c.spec}, "reconcile", c.apply)
			if c.want == nil {
				if err != nil || !ran {
					t.Fatalf("handler did not run: %v", err)
				}
				return
			}
			if !errors.Is(err, c.want) || ran {
				t.Fatalf("got %v (ran %v), want %v", err, ran, c.want)
			}
		})
	}
}

func TestExecuteRefusesLockedAndUnsupportedActions(t *testing.T) {
	declared := runtime.ControllerRegistry{Locked: []runtime.LockedAction{{Kind: runtime.ResourceKindMachine, Action: "reconcile", Reason: "Nothing to observe."}}}
	controller := dispatchController(runtime.Environment{}, declared)
	controller.action(runtime.ResourceKindMachine, "reconcile", func(context.Context, Object, Object, bool) (Result, error) {
		t.Fatal("a locked action ran")
		return Result{}, nil
	})
	if _, err := controller.Execute(t.Context(), runtime.Resource{Kind: runtime.ResourceKindMachine}, "reconcile", true); err == nil || err.Error() != "Nothing to observe." {
		t.Fatalf("locked: %v", err)
	}
	if _, err := controller.Execute(t.Context(), runtime.Resource{Kind: "unknown.kind"}, "reconcile", false); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("unsupported: %v", err)
	}
}

func TestCapabilitiesAreTheDeclaredActionsWithAHandler(t *testing.T) {
	declared := runtime.ControllerRegistry{Capabilities: []runtime.Capability{
		{Kind: dispatchKind, Action: "reconcile"}, {Kind: runtime.ResourceKindPKIAuthority, Action: "reconcile"},
	}}
	controller := dispatchController(runtime.Environment{}, declared)
	controller.action(dispatchKind, "reconcile", func(context.Context, Object, Object, bool) (Result, error) { return Result{}, nil })
	if got := controller.Capabilities(); len(got) != 1 || got[0].Kind != dispatchKind {
		t.Fatalf("capabilities %+v", got)
	}
	if got := controller.Uncovered(); len(got) != 1 || got[0] != "pki.authority:reconcile" {
		t.Fatalf("uncovered %v", got)
	}
}

// inventoryController reads kinds of a fake vendor whose connection is
// supplied, so each kind has a source.
func inventoryController(kinds ...string) *Controller {
	env := runtime.Environment{}
	declared := runtime.ControllerRegistry{Observations: map[string]string{}}
	for _, kind := range kinds {
		vendor, _, _ := strings.Cut(kind, ".")
		prefix := strings.ToUpper(vendor)
		env[prefix+"_CONNECTION_REF"] = vendor
		declared.Observations[kind] = vendor
		declared.ConnectionCredentials = append(declared.ConnectionCredentials, runtime.ConnectionProvider(vendor))
	}
	return dispatchController(env, declared)
}

// Regression: connected:false was dropped by omitempty, so an unconnected kind
// read as a successful empty read.
func TestUnconnectedKindSaysConnectedFalseOnTheWire(t *testing.T) {
	controller := dispatchController(runtime.Environment{}, runtime.ControllerRegistry{
		Observations: map[string]string{"alpha.thing": "alpha"}, ConnectionCredentials: []runtime.ConnectionProvider{"alpha"},
	})
	controller.readers["alpha.thing"] = func(context.Context) ([]any, error) {
		t.Fatal("an unconnected kind was read")
		return nil, nil
	}
	found, err := controller.Inventory(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(found["alpha.thing"])
	if !strings.Contains(string(data), `"connected":false`) {
		t.Fatalf("report %s", data)
	}
	read := inventoryController("beta.thing")
	read.readers["beta.thing"] = func(context.Context) ([]any, error) { return []any{}, nil }
	found, _ = read.Inventory(t.Context(), nil)
	if data, _ := json.Marshal(found["beta.thing"]); strings.Contains(string(data), "connected") {
		t.Fatalf("a read kind carries connected: %s", data)
	}
}

func TestReadFailuresBecomeReportFields(t *testing.T) {
	long := strings.Repeat("é", 300) + strings.Repeat("x", 300)
	cases := []struct {
		name    string
		err     error
		refusal runtime.Refusal
		text    string
	}{
		{"credential refusal says the provider's reason", fmt.Errorf("list zones: %w", &ProviderError{Message: "token refused", Failure: runtime.FailureClassCredential, Reason: "Invalid API Token"}), runtime.RefusalCredential, "Invalid API Token"},
		{"permission refusal keeps the chain", fmt.Errorf("list zones: %w", runtime.HTTPRefusal(403)), runtime.RefusalPermission, "list zones: provider answered 403"},
		{"unclassified failure", errors.New("boom"), runtime.RefusalUnclassified, "boom"},
		{"text is clipped by characters", errors.New(long), runtime.RefusalUnclassified, string([]rune(long)[:runtime.ReportTextLimit])},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			controller := inventoryController("alpha.thing")
			controller.readers["alpha.thing"] = func(context.Context) ([]any, error) { return nil, c.err }
			found, _ := controller.Inventory(t.Context(), nil)
			report := found["alpha.thing"]
			if report.OK || report.Refusal != c.refusal || report.Error != c.text || !utf8.ValidString(report.Error) {
				t.Fatalf("%+v", report)
			}
		})
	}
}

// Provider groups are read at once; one provider's kinds in turn. Run under
// -race: groups share the registry's snapshot, its ledger and the report map.
func TestInventoryReadsProviderGroupsConcurrentlyWithoutRacing(t *testing.T) {
	kinds := []string{"alpha.one", "alpha.two", "beta.one", "gamma.one", "delta.one", "delta.two"}
	controller := inventoryController(kinds...)
	var loads atomic.Int32
	var inFlight sync.WaitGroup
	inFlight.Add(4) // one reader per group meets the others before loading
	for _, kind := range kinds {
		controller.readers[kind] = func(ctx context.Context) ([]any, error) {
			if strings.HasSuffix(kind, ".one") {
				inFlight.Done()
				inFlight.Wait()
			}
			raw, err := controller.cached(ctx, "shared", func() (json.RawMessage, error) {
				loads.Add(1)
				return json.RawMessage(`"value"`), nil
			})
			if err != nil {
				return nil, err
			}
			refuse(ctx, "part", "", kind, &ProviderError{Message: "withheld", Failure: runtime.FailureClassPermission})
			return []any{kind, string(raw)}, nil
		}
	}
	done := controller.BeginSnapshot()
	found, err := controller.Inventory(t.Context(), nil)
	done()
	if err != nil || len(found) != len(kinds) {
		t.Fatalf("%v %d", err, len(found))
	}
	for _, kind := range kinds {
		report := found[kind]
		if !report.OK || len(report.Records) != 2 || len(report.RefusedParts) != 1 || report.RefusedParts[0].Refusal != runtime.FailureClassPermission {
			t.Errorf("%s: %+v", kind, report)
		}
	}
	if loads.Load() != 1 {
		t.Errorf("the shared value loaded %d times in one sweep", loads.Load())
	}
}

func TestOnlyReadsTheKindsAsked(t *testing.T) {
	controller := inventoryController("alpha.one", "beta.one")
	for _, kind := range []string{"alpha.one", "beta.one"} {
		controller.readers[kind] = func(context.Context) ([]any, error) { return []any{kind}, nil }
	}
	found, _ := controller.Inventory(t.Context(), []runtime.ResourceKind{"beta.one"})
	if _, read := found["alpha.one"]; read || len(found) != 1 {
		t.Fatalf("%+v", found)
	}
}
