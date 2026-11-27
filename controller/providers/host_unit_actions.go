package providers

import (
	"context"
	"fmt"

	"github.com/joeseverino/severino-hq/controller/host/unit"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// HostUnitSpec is a unit HQ's release installs on the machine this
// controller runs on, by name (provider_adapters/host.py).
type HostUnitSpec struct {
	Unit string `json:"unit"`
}

// admitHostUnits registers each verb HQ may ask of its own units. Root's
// socket checks the name against the installed set and refuses while a
// deploy holds the units, so nothing here decides which unit is HQ's.
func (r *Registry) admitHostUnits() {
	for _, verb := range unit.ShippedVerbs {
		act(r, runtime.ResourceKindHostUnit, verb, r.hostUnitActor(verb))
	}
	if r.Units == nil {
		r.Units = r.unitControl()
	}
}

// hostUnitActor asks the unit socket for verb on one unit. The next unit
// reading says where it stands, so a plan only checks the socket is there.
func (r *Registry) hostUnitActor(verb string) func(context.Context, HostUnitSpec, struct{}, bool) (Result, error) {
	return func(ctx context.Context, spec HostUnitSpec, _ struct{}, apply bool) (Result, error) {
		if r.Units == nil {
			return Result{}, &ProviderError{Message: "HQ cannot act on units on this machine: no unit socket is mounted"}
		}
		if apply {
			if err := r.Units.Act(ctx, r.controllerID(), verb, spec.Unit); err != nil {
				return Result{}, fmt.Errorf("%s %s: %w", verb, spec.Unit, err)
			}
		}
		said := fmt.Sprintf("Asked systemd to %s %s.", verb, spec.Unit)
		return result(apply, spec, "Asked", said, said), nil
	}
}
