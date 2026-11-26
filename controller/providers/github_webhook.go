package providers

import (
	"context"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The App's webhook as GitHub reports it: the events the App is subscribed
// to and how its newest deliveries went, both read as the App itself. HQ is
// never dialed, so GitHub's delivery log is the one place a delivery the
// Worker refused, or never received, shows.

// githubWebhook is the App's webhook: its subscribed events, and its delivery
// log's newest page, newest first as GitHub orders it, down to how many of
// the newest failed in a row.
func (r *Registry) githubWebhook(ctx context.Context) ([]any, error) {
	c, err := r.githubConnection("")
	if err != nil {
		return nil, err
	}
	api := r.githubAsApp(ctx, c)
	app, err := json200(api.AppsGetAuthenticatedWithResponse(ctx))
	if err != nil {
		return nil, err
	}
	deliveries, err := json200(api.AppsListWebhookDeliveriesWithResponse(ctx, &githubapi.AppsListWebhookDeliveriesParams{PerPage: new(githubListPage)}))
	if err != nil {
		return nil, err
	}
	record := runtime.GitHubWebhookRecord{ConnectionRef: c.ref, App: app.Slug, Events: app.Events, Deliveries: len(deliveries)}
	if owner, err := app.Owner.AsSimpleUser(); err == nil {
		record.Owner, record.Organization = owner.Login, owner.Type == "Organization"
	}
	if len(deliveries) > 0 {
		newest := deliveries[0]
		record.Newest = runtime.GitHubWebhookDelivery{Event: newest.Event, Action: newest.Action, Status: newest.Status, StatusCode: newest.StatusCode, DeliveredAt: newest.DeliveredAt}
	}
	// GitHub counts an answer from 200 to 399 a success, as its log's status
	// filter does.
	for _, delivery := range deliveries {
		if delivery.StatusCode >= 200 && delivery.StatusCode < 400 {
			break
		}
		record.Failing++
	}
	return []any{record}, nil
}
