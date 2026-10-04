package providers

import (
	"context"
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Every kind this controller registers is one the contract names, so a kind
// spelled differently here than in HQ's registry cannot register.
func TestRegisteredKindsAreContractKinds(t *testing.T) {
	coverage := New(runtime.Environment{}, &fakeHTTP{}).Coverage()
	kinds := append([]string{}, coverage.Readers...)
	for _, action := range coverage.Actions {
		kind, _, _ := strings.Cut(action, ":")
		kinds = append(kinds, kind)
	}
	if len(kinds) == 0 {
		t.Fatal("nothing registered")
	}
	for _, kind := range kinds {
		if !runtime.ResourceKind(kind).Valid() {
			t.Errorf("%q is registered but is not a ResourceKind in the contract", kind)
		}
	}
}

// The readers registered are exactly the contract's SweptKind, which Django
// holds to the kinds HQ expects a sweep to read.
func TestReadersAreTheContractsSweptKinds(t *testing.T) {
	data, err := os.ReadFile("../api/hq-controller.openapi.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract struct {
		Components struct {
			Schemas struct {
				SweptKind struct {
					Enum []string `json:"enum"`
				} `json:"SweptKind"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	swept := slices.Sorted(slices.Values(contract.Components.Schemas.SweptKind.Enum))
	readers := New(runtime.Environment{}, &fakeHTTP{}).Coverage().Readers
	if !slices.Equal(readers, swept) {
		t.Errorf("readers %v\nSweptKind %v", readers, swept)
	}
}

// runAction runs a registered action the way the controller dispatches it: the
// spec and observed payloads arrive opaque and decode at the boundary.
func (r *Registry) runAction(kind runtime.ResourceKind, action string, ctx context.Context, spec, observed Object, apply bool) (Result, error) {
	handler, ok := r.actions[actionKey{kind, action}]
	if !ok {
		return Result{}, &ProviderError{Message: "no action " + string(kind) + "/" + action}
	}
	return handler(ctx, spec, observed, apply)
}

func TestActionsDecodeOnceAtTheBoundary(t *testing.T) {
	r := New(runtime.Environment{}, &fakeHTTP{})
	ctx := t.Context()
	bad := Object{"consumers": "not a list"}
	if res, err := r.runAction(runtime.ResourceKindTLSCertificate, "renew", ctx, bad, nil, false); err != nil || !res.Changed {
		t.Fatalf("a renewal plan answers without reading the spec: %v %+v", err, res)
	}
	for _, action := range []struct {
		kind runtime.ResourceKind
		name string
	}{{runtime.ResourceKindTLSCertificate, "renew"}, {runtime.ResourceKindTLSCertificate, "reconcile"}, {runtime.ResourceKindTLSUploadedCertificate, "delete"}} {
		_, err := r.runAction(action.kind, action.name, ctx, bad, nil, true)
		if err == nil || err.Error() != certificateSpecInvalid {
			t.Errorf("%s/%s: %v", action.kind, action.name, err)
		}
	}
	if _, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "reconcile", ctx, Object{"domain": 5}, nil, true); err == nil {
		t.Error("an AdGuard spec that does not decode is refused with the decoder's error")
	}
	if _, err := decodePayload[NPMProxyHostSpec](Object{"connection_ref": 7}); err == nil {
		t.Error("a connection_ref that is not text is refused, never read as no connection")
	}
}
