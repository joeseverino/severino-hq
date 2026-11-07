package runtime

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"maps"
	"slices"
)

const ApplyLimit = 5

type Worker struct {
	ID        string
	Bridge    Bridge
	Providers Providers
	// Opener opens the sealed secrets an action's material carries; nil
	// opens none.
	Opener Unsealer
	Output io.Writer
	Log    *slog.Logger
}

func (w *Worker) logger() *slog.Logger {
	return cmp.Or(w.Log, slog.Default())
}

func (w *Worker) emit(value any) error {
	if w.Output == nil {
		return errors.New("controller output is not configured")
	}
	return json.NewEncoder(w.Output).Encode(value)
}

// post reports to HQ. A report HQ did not take is logged and the pass goes on:
// the next pass reports again.
func (w *Worker) post(action string, err error) {
	if err != nil {
		w.logger().Warn("controller report skipped", slog.String("action", action), slog.Any("error", err))
	}
}

// claimable is every kind:action this controller has a handler for.
func (w *Worker) claimable() []string {
	capabilities := []string{}
	for _, capability := range w.Providers.Capabilities() {
		capabilities = append(capabilities, string(capability.Kind)+":"+capability.Action)
	}
	return capabilities
}

// Run preserves the host's two bounded queue passes and sweep ordering. A failed
// operation stops its queue, but does not prevent observing the estate.
func (w *Worker) Run(ctx context.Context, apply bool) (int, error) {
	if !apply {
		return w.plan(ctx)
	}
	defer func() {
		failures := w.Providers.StepFailures()
		if failures == nil {
			failures = []StepFailure{}
		}
		w.post("steps", w.Bridge.Steps(ctx, w.ID, failures))
	}()
	w.Glance(ctx)
	applied, stopped, failed, queueErr := w.applyQueued(ctx)
	sweepErr := w.sweep(ctx)
	if sweepErr == nil {
		sweepErr = w.Bridge.Schedule(ctx, w.ID)
	}
	if queueErr != nil {
		return 1, queueErr
	}
	if sweepErr != nil {
		return 1, sweepErr
	}
	if !stopped {
		var next int
		var err error
		next, stopped, failed, err = w.applyQueued(ctx)
		applied += next
		if err != nil {
			return 1, err
		}
	}
	if applied == 0 && !stopped {
		if err := w.emit(IdlePassOutput{OK: true, Mode: PassModeApply}); err != nil {
			return 1, err
		}
	}
	if failed {
		return 1, nil
	}
	return 0, nil
}

// GlanceLimit bounds the readings one glance pass takes.
const GlanceLimit = 3

// Glance reads the dashboard panels HQ is waiting on and reports them. Each
// observation carries the plan's as_of, so HQ answers only the requests the
// plan saw: one made while this reads waits for a reading begun after it.
func (w *Worker) Glance(ctx context.Context) {
	_, _ = w.glance(ctx)
}

// Glances is a glance pass, the whole of one: it reads again while HQ is
// still waiting on a panel, which after a reading it reported is a request
// made during that reading, up to GlanceLimit readings. It fails only when
// HQ gave it no plan at all.
func (w *Worker) Glances(ctx context.Context) (int, error) {
	for range GlanceLimit {
		again, err := w.glance(ctx)
		if err != nil {
			return 1, err
		}
		if !again {
			break
		}
	}
	return 0, nil
}

// glance takes one reading; whether it read and HQ took the report, so a
// pass may read again, and the error when HQ gave no plan.
func (w *Worker) glance(ctx context.Context) (bool, error) {
	plan, err := w.Bridge.GlancePlan(ctx, w.ID)
	if err != nil {
		w.logger().Warn("glance plan unavailable", slog.Any("error", err))
		return false, err
	}
	if len(plan.Panels) == 0 {
		return false, nil
	}
	result, err := w.Providers.Glance(ctx, plan)
	if err != nil {
		w.logger().Warn("glance read failed", slog.Any("error", err))
		return false, nil
	}
	for _, observation := range result {
		observation["as_of"] = plan.AsOf
	}
	err = w.Bridge.Glance(ctx, w.ID, result)
	w.post("glance", err)
	return err == nil, nil
}

func (w *Worker) plan(ctx context.Context) (int, error) {
	pending, err := w.Bridge.Peek(ctx, w.claimable())
	if err != nil {
		return 1, err
	}
	connections, err := w.Providers.Connections(ctx, nil)
	if err != nil {
		return 1, err
	}
	if connections == nil {
		connections = []ConnectionRecord{}
	}
	var plan *PlannedOperation
	if pending.Operation != nil {
		result, err := w.Providers.Execute(ctx, pending.Resource, pending.Operation.Action, false)
		if err != nil {
			return 1, err
		}
		plan = &PlannedOperation{Operation: pending.Operation.ID, Resource: pending.Resource.Key,
			Action: pending.Operation.Action, WouldChange: result.Changed, Message: result.Message}
	}
	connections, err = w.retryNetwork(ctx, connections)
	if err != nil {
		return 1, err
	}
	healthy, warnings := planHealth(connections)
	err = w.emit(PlanPassOutput{OK: healthy, Mode: PassModePlan, Connections: connections, Warnings: warnings, Plan: plan})
	if err != nil {
		return 1, err
	}
	if !healthy {
		return 1, nil
	}
	return 0, nil
}

func (w *Worker) retryNetwork(ctx context.Context, connections []ConnectionRecord) ([]ConnectionRecord, error) {
	needed := false
	for _, c := range connections {
		needed = needed || isNetworkFailure(c)
	}
	if !needed {
		return connections, nil
	}
	again, err := w.Providers.Connections(ctx, nil)
	if err != nil {
		return nil, err
	}
	byRef := map[string]ConnectionRecord{}
	for _, c := range again {
		byRef[c.ConnectionRef] = c
	}
	for i, c := range connections {
		if replacement, ok := byRef[c.ConnectionRef]; ok && isNetworkFailure(c) {
			connections[i] = replacement
		}
	}
	return connections, nil
}

// applyQueued applies up to ApplyLimit operations; how many applied, whether
// a refused one stopped the queue, and whether that refusal is a failure. A
// refusal for an allowance spent stops the queue without failing the pass:
// HQ is told the operation did not apply and the next pass tries again.
func (w *Worker) applyQueued(ctx context.Context) (applied int, stopped, failed bool, err error) {
	for range ApplyLimit {
		claimed, refusal, err := w.applyOne(ctx)
		if err != nil {
			return applied, false, false, err
		}
		if !claimed {
			return applied, false, false, nil
		}
		if refusal != nil {
			return applied, true, !refusal.Limited, nil
		}
		applied++
	}
	return applied, false, false, nil
}

// applyOne claims and applies one operation: whether one was claimed, and the
// provider's refusal of it, which HQ has been told.
func (w *Worker) applyOne(ctx context.Context) (bool, *ProviderError, error) {
	pending, err := w.Bridge.Claim(ctx, w.ID, w.claimable())
	if err != nil {
		return false, nil, err
	}
	if pending.Operation == nil {
		return false, nil, nil
	}
	resource := pending.Resource
	result, err := w.execute(ctx, pending, &resource)
	var refusal *ProviderError
	if err != nil && !errors.As(err, &refusal) {
		return true, nil, err
	}
	if refusal != nil {
		message := ReportText(err.Error())
		result = Result{Status: refusal.Status, Message: message, Conditions: []Condition{
			{Type: ConditionDegraded, Status: true, Reason: "ProviderError", Message: message},
		}}
	}
	if result.Status == nil {
		result.Status = Object{}
	}
	if result.Conditions == nil {
		result.Conditions = []Condition{}
	}
	report := ControllerReport{
		Success:            refusal == nil,
		ObservedGeneration: resource.Generation,
		Status:             result.Status,
		Conditions:         result.Conditions,
		Message:            result.Message,
	}
	if err := w.Bridge.Report(ctx, w.ID, pending.Operation.ID, report); err != nil {
		return true, nil, err
	}
	var output any = AppliedOperationOutput{OK: true, Operation: pending.Operation.ID, Resource: resource.Key, Changed: result.Changed}
	if refusal != nil {
		output = RefusedOperationOutput{OK: false, Operation: pending.Operation.ID, Resource: resource.Key, Message: ReportText(err.Error())}
	}
	return true, refusal, w.emit(output)
}

// execute applies a claimed action, first opening the material its kind
// needs. A sealed value that does not open refuses this action alone.
func (w *Worker) execute(ctx context.Context, pending Pending, resource *Resource) (Result, error) {
	if w.Providers.NeedsMaterial(resource.Kind) {
		material, err := w.Bridge.Material(ctx, resource.Key)
		if err != nil {
			return Result{}, err
		}
		opened, err := w.open(resource.Key, material)
		if err != nil {
			return Result{}, err
		}
		if resource.Spec == nil {
			resource.Spec = Object{}
		}
		resource.Spec["material"] = opened
	}
	return w.Providers.Execute(WithVerification(ctx, pending.Verification), *resource, pending.Operation.Action, true)
}

// open is material with every sealed slot opened for resourceKey.
func (w *Worker) open(resourceKey string, material Material) (OpenedMaterial, error) {
	opener := w.Opener
	if opener == nil {
		opener = Unopenable{Err: errors.New("no sealing keys were lent to this run")}
	}
	opened := OpenedMaterial{Fullchain: material.Fullchain, Domains: material.Domains, Secrets: map[string]string{}}
	for _, slot := range slices.Sorted(maps.Keys(material.Slots)) {
		plaintext, err := opener.Open(material.Slots[slot], resourceKey, slot)
		if err != nil {
			return OpenedMaterial{}, &ProviderError{Message: "HQ's sealed " + slot + " does not open on this machine", Err: err}
		}
		opened.Secrets[slot] = string(plaintext)
	}
	return opened, nil
}
