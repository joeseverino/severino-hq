package providers

import (
	"net/http"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The history answers as the example of List History in Cloudflare's spec
// shows it: each alert's type, policy and time are kept, and neither its body
// nor the address it went to.
func TestAlertsReadTheHistorySinceTheWindow(t *testing.T) {
	var query map[string]string
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{
		"GET /accounts": answer([]any{map[string]any{"id": "acct"}}),
		"GET /accounts/acct/alerting/v3/history": func(w http.ResponseWriter, request *http.Request) {
			values := request.URL.Query()
			query = map[string]string{"since": values.Get("since"), "per_page": values.Get("per_page"), "page": values.Get("page")}
			answer([]any{map[string]any{
				"alert_body": map[string]any{
					"data":     map[string]any{"id": "11111111111", "method": "txt", "validation_records": []any{map[string]any{"txt_name": "_acme-challenge.example.com"}}},
					"metadata": map[string]any{"event": map[string]any{"type": "ssl.certificate.validation.failed"}, "zone": map[string]any{"id": "11111111111"}},
				},
				"alert_type":     "universal_ssl_event_type",
				"description":    "Universal Certificate validation status, issuance, renewal, and expiration notices.",
				"id":             "f174e90a-fafe-4643-bbbc-4a0ed4fc8415",
				"mechanism":      "test@example.com",
				"mechanism_type": "email",
				"name":           "SSL Notification Event Policy",
				"policy_id":      "35040955-3102-4710-938c-0f4eaf736e25",
				"sent":           "2021-10-08T17:52:17.571336Z",
			}})(w, request)
		},
	})
	records, err := r.cloudflareAlerts(t.Context())
	if err != nil || len(records) != 1 {
		t.Fatalf("%v %+v", err, records)
	}
	if query["since"] != "2025-12-11T12:00:00Z" || query["per_page"] != "1000" || query["page"] != "1" {
		t.Errorf("query %v", query)
	}
	want := runtime.CloudflareAlertRecord{
		ConnectionRef: "example-api", AccountID: "acct", ID: "f174e90a-fafe-4643-bbbc-4a0ed4fc8415", AlertType: "universal_ssl_event_type",
		Name: "SSL Notification Event Policy", PolicyID: "35040955-3102-4710-938c-0f4eaf736e25",
		Description: "Universal Certificate validation status, issuance, renewal, and expiration notices.", MechanismType: "email", Sent: "2021-10-08T17:52:17.571336Z",
	}
	if records[0] != want {
		t.Errorf("%+v", records[0])
	}
}
