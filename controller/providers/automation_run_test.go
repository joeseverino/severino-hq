package providers

import (
	"slices"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A run of an automation procedure is the automation worker's to claim. Even
// declared to this controller, it is no action it has a handler for, so the
// kind:action pairs it claims by (Worker.claimable) never name it.
func TestTheControllerNeverClaimsAnAutomationRun(t *testing.T) {
	run := runtime.Capability{Kind: runtime.ResourceKindAutomationOperation, Action: "run"}
	handled := runtime.Capability{Kind: runtime.ResourceKindAdGuardRewrite, Action: "reconcile"}
	controller := NewController(New(runtime.Environment{}, supplied(), &fakeHTTP{}), runtime.ControllerRegistry{
		Capabilities: []runtime.Capability{handled, run},
	})

	claimable := controller.Capabilities()

	if slices.Contains(claimable, run) {
		t.Fatalf("the controller would claim %s:%s: %v", run.Kind, run.Action, claimable)
	}
	if !slices.Contains(claimable, handled) {
		t.Fatalf("the controller lost an action it handles: %v", claimable)
	}
	if !slices.Contains(controller.Uncovered(), string(run.Kind)+":"+run.Action) {
		t.Fatalf("a run is not left for another claimant: %v", controller.Uncovered())
	}
}
