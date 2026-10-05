package runtime

import (
	"context"
	"log/slog"
)

func (w *Worker) glance(ctx context.Context) {
	plan, err := w.Bridge.GlancePlan(ctx, w.ID)
	if err != nil {
		w.logger().Warn("glance plan unavailable", slog.Any("error", err))
		return
	}
	if len(plan.Panels) == 0 {
		return
	}
	result, err := w.Providers.Glance(ctx, plan)
	if err != nil {
		w.logger().Warn("glance read failed", slog.Any("error", err))
		return
	}
	w.post("glance", w.Bridge.Glance(ctx, w.ID, result))
}

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
	connections, err := w.Providers.Connections(ctx, verdict.Carry)
	if err != nil {
		w.logger().Warn("connections sweep skipped", slog.Any("error", err))
	} else {
		if connections == nil {
			connections = []ConnectionRecord{}
		}
		w.post("connections", w.Bridge.Connections(ctx, w.ID, connections))
	}
	inventory, err := w.Providers.Inventory(ctx, verdict.OnlyKinds)
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
