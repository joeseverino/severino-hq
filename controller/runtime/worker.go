package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
)

const ApplyLimit = 5

type Worker struct {
	ID        string
	Bridge    Bridge
	Providers Providers
	Output    io.Writer
	Log       *slog.Logger
}

func (w *Worker) logger() *slog.Logger {
	if w.Log != nil {
		return w.Log
	}
	return slog.Default()
}

func (w *Worker) emit(value any) error {
	if w.Output == nil {
		return errors.New("controller output is not configured")
	}
	return json.NewEncoder(w.Output).Encode(value)
}

func (w *Worker) call(ctx context.Context, action string, payload any, result any, args ...string) error {
	return w.Bridge.Call(ctx, append([]string{action}, args...), payload, result)
}

func (w *Worker) post(ctx context.Context, action string, payload any) {
	if err := w.call(ctx, action, payload, nil, "--controller-id", w.ID); err != nil {
		w.logger().Warn("controller report skipped", slog.String("action", action), slog.Any("error", err))
	}
}

func (w *Worker) claimArgs(action string) []string {
	args := []string{action}
	if action == "claim" {
		args = append(args, "--controller-id", w.ID)
	}
	for _, capability := range w.Providers.Capabilities() {
		args = append(args, "--capability", string(capability.Kind)+":"+capability.Action)
	}
	return args
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
		w.post(ctx, "steps", failures)
	}()
	w.glance(ctx)
	applied, failed, queueErr := w.applyQueued(ctx)
	sweepErr := w.sweep(ctx)
	if sweepErr == nil {
		sweepErr = w.call(ctx, "schedule", nil, nil, "--controller-id", w.ID)
	}
	if queueErr != nil {
		return 1, queueErr
	}
	if sweepErr != nil {
		return 1, sweepErr
	}
	if !failed {
		var next int
		var err error
		next, failed, err = w.applyQueued(ctx)
		applied += next
		if err != nil {
			return 1, err
		}
	}
	if applied == 0 && !failed {
		if err := w.emit(IdlePassOutput{OK: true, Mode: PassModeApply}); err != nil {
			return 1, err
		}
	}
	if failed {
		return 1, nil
	}
	return 0, nil
}

func (w *Worker) plan(ctx context.Context) (int, error) {
	var pending Pending
	if err := w.Bridge.Call(ctx, w.claimArgs("peek"), nil, &pending); err != nil {
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

func (w *Worker) applyQueued(ctx context.Context) (int, bool, error) {
	applied := 0
	for range ApplyLimit {
		claimed, success, err := w.applyOne(ctx)
		if err != nil {
			return applied, false, err
		}
		if !claimed {
			return applied, false, nil
		}
		if !success {
			return applied, true, nil
		}
		applied++
	}
	return applied, false, nil
}

func (w *Worker) applyOne(ctx context.Context) (bool, bool, error) {
	var pending Pending
	if err := w.Bridge.Call(ctx, w.claimArgs("claim"), nil, &pending); err != nil {
		return false, false, err
	}
	if pending.Operation == nil {
		return false, true, nil
	}
	resource := pending.Resource
	if w.Providers.NeedsMaterial(resource.Kind) {
		var material Material
		if err := w.call(ctx, "material", nil, &material, "--resource", resource.Key); err != nil {
			return true, false, err
		}
		if resource.Spec == nil {
			resource.Spec = Object{}
		}
		resource.Spec["material"] = material
	}
	result, err := w.Providers.Execute(WithVerification(ctx, pending.Verification), resource, pending.Operation.Action, true)
	var refusal *ProviderError
	if err != nil && !errors.As(err, &refusal) {
		return true, false, err
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
	if err := w.call(ctx, "report", report, nil, "--controller-id", w.ID, "--operation", pending.Operation.ID); err != nil {
		return true, false, err
	}
	var output any = AppliedOperationOutput{OK: true, Operation: pending.Operation.ID, Resource: resource.Key, Changed: result.Changed}
	if refusal != nil {
		output = RefusedOperationOutput{OK: false, Operation: pending.Operation.ID, Resource: resource.Key, Message: ReportText(err.Error())}
	}
	return true, refusal == nil, w.emit(output)
}
