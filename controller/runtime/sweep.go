package runtime

import (
	"context"
	"log/slog"
)

func (w *Worker) glance(ctx context.Context) {
	var plan GlancePlan
	if err := w.call(ctx, "glance-plan", nil, &plan, "--controller-id", w.ID); err != nil {
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
	w.post(ctx, "glance", result)
}

func (w *Worker) sweep(ctx context.Context) error {
	var verdict SweepVerdict
	if err := w.call(ctx, "sweep-due", nil, &verdict, "--controller-id", w.ID); err != nil {
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
		w.post(ctx, "connections", connections)
	}
	inventory, err := w.Providers.Inventory(ctx, verdict.OnlyKinds)
	if err != nil {
		return err
	}
	w.post(ctx, "inventory", inventory)
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
	w.post(ctx, "analytics", readings)
	return nil
}

func (w *Worker) analyticsWindows(ctx context.Context, sites []AnalyticsSiteIdentity) []AnalyticsWindow {
	if len(sites) == 0 {
		return []AnalyticsWindow{}
	}
	var plan AnalyticsPlan
	if err := w.call(ctx, "analytics-plan", sites, &plan); err != nil {
		w.logger().Warn("analytics plan unavailable", slog.Any("error", err))
		return []AnalyticsWindow{}
	}
	return plan.Windows
}
