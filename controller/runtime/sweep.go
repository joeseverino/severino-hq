package runtime

import (
	"context"
	"log/slog"
	"slices"
)

func (w *Worker) sweep(ctx context.Context) error {
	verdict, err := w.Bridge.SweepDue(ctx, w.ID)
	if err != nil {
		w.logger().Warn("sweep policy unavailable", slog.Any("error", err))
		verdict.Carry, verdict.OnlyKinds = nil, nil
	} else if !verdict.Due {
		return nil
	}
	closeSnapshot := w.Providers.BeginSnapshot()
	defer closeSnapshot()
	// A sweep of named kinds, as an event asks, probes no connection unless a
	// forced read names one.
	if len(verdict.OnlyKinds) == 0 || slices.ContainsFunc(verdict.Forced, func(read ForcedRead) bool { return read.ConnectionRef != "" }) {
		w.connections(ctx, verdict.Carry)
	}
	if len(verdict.OnlyKinds) != 0 {
		ctx = Telling(ctx, verdict.Forced)
	}
	inventory, err := w.Providers.Inventory(ctx, verdict.OnlyKinds, verdict.CarryKinds)
	if err != nil {
		return err
	}
	w.post("inventory", w.Bridge.Inventory(ctx, w.ID, inventory))
	if len(verdict.OnlyKinds) != 0 {
		return nil
	}
	sites, err := w.Providers.AnalyticsSites(ctx)
	if err != nil {
		w.logger().Warn("analytics sweep skipped", slog.String("phase", "sites"), slog.Any("error", err))
		return nil
	}
	readings, err := w.Providers.Analytics(ctx, sites, w.analyticsWindows(ctx, sites))
	if err != nil {
		w.logger().Warn("analytics sweep skipped", slog.String("phase", "analytics"), slog.Any("error", err))
		return nil
	}
	w.post("analytics", w.Bridge.Analytics(ctx, w.ID, readings))
	return nil
}

// toldKey carries the forced reads of a pass that reads only what they ask.
type toldKey struct{}

// Telling is ctx for a pass that reads only what forced asks.
func Telling(ctx context.Context, forced []ForcedRead) context.Context {
	return context.WithValue(ctx, toldKey{}, forced)
}

// Told is what of kind the pass reading ctx was asked for, as its provider
// names each (a repository), or nil for all of it: a pass due on its own
// clock, or one a read of the whole kind forces.
func Told(ctx context.Context, kind ResourceKind) []string {
	forced, _ := ctx.Value(toldKey{}).([]ForcedRead)
	var told []string
	for _, read := range forced {
		if read.Kinds != nil && !slices.Contains(read.Kinds, kind) {
			continue
		}
		if read.Subject == "" {
			return nil
		}
		told = append(told, read.Subject)
	}
	return told
}

// connections probes every connection, but the carried, and tells HQ.
func (w *Worker) connections(ctx context.Context, carry []string) {
	connections, err := w.Providers.Connections(ctx, carry)
	if err != nil {
		w.logger().Warn("connections sweep skipped", slog.Any("error", err))
		return
	}
	if connections == nil {
		connections = []ConnectionRecord{}
	}
	w.post("connections", w.Bridge.Connections(ctx, w.ID, connections))
}

func (w *Worker) analyticsWindows(ctx context.Context, sites []AnalyticsSiteIdentity) []AnalyticsWindow {
	if len(sites) == 0 {
		return []AnalyticsWindow{}
	}
	plan, err := w.Bridge.AnalyticsPlan(ctx, sites)
	if err != nil {
		w.logger().Warn("analytics plan unavailable", slog.Any("error", err))
		return []AnalyticsWindow{}
	}
	return plan.Windows
}
