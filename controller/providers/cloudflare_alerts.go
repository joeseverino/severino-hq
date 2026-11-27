package providers

import (
	"context"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// alertsWindow is how far back the alert history is read: as far as
// Cloudflare keeps it on every plan but Enterprise's (List History).
const alertsWindow = 30 * 24 * time.Hour

// alertsPerPage is the largest page List History answers.
const alertsPerPage = 1000

// cloudflareAlerts is each notification Cloudflare sent for the account in
// the window, by its type, policy and time; never its body or the address
// it went to.
func (r *Registry) cloudflareAlerts(ctx context.Context) ([]any, error) {
	found := []any{}
	since := r.Now().UTC().Add(-alertsWindow).Format(time.RFC3339)
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		alerts, err := cloudflareAccountItems[cfapi.AaaHistory](ctx, r, ref, "alert", alertsPerPage, func(client *cfapi.ClientWithResponses, account string, page int) (vendorReply, error) {
			return client.NotificationHistoryListHistoryWithResponse(ctx, account, &cfapi.NotificationHistoryListHistoryParams{Page: new(float32(page)), PerPage: new(float32(alertsPerPage)), Since: &since})
		})
		if err != nil {
			return nil, err
		}
		for _, alert := range alerts {
			found = append(found, runtime.CloudflareAlertRecord{
				ConnectionRef: ref, AccountID: account, ID: alert.ID, AlertType: alert.AlertType, Name: alert.Name,
				PolicyID: alert.PolicyID, Description: alert.Description, MechanismType: string(alert.MechanismType), Sent: alert.Sent,
			})
		}
	}
	return found, nil
}
