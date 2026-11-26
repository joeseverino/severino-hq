package providers

import (
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubDelivery is one item of GitHub's delivery log, in the shape its
// description gives hook-delivery-item.
func githubDelivery(id, code int, status string) string {
	return `{"id":` + strconv.Itoa(id) + `,"guid":"0b989ba4-242f-11e5-81e1-c7b6966d2516","delivered_at":"2030-01-01T00:00:0` + strconv.Itoa(id%10) + `Z","redelivery":false,"duration":0.27,"status":"` + status + `","status_code":` + strconv.Itoa(code) + `,"event":"push","action":"completed","installation_id":7,"repository_id":42,"throttled_at":null}`
}

// The webhook's reading is the App's subscribed events and owner, and how
// many of its newest deliveries failed in a row, both read under the App's
// JWT and neither under an installation token.
func TestTheWebhookReadingSaysHowTheNewestDeliveriesWent(t *testing.T) {
	for _, test := range []struct {
		name       string
		deliveries []string
		failing    int
	}{
		{"the newest two failed", []string{githubDelivery(3, 502, "failed to connect"), githubDelivery(2, 401, "Unauthorized"), githubDelivery(1, 200, "OK")}, 2},
		{"the newest succeeded", []string{githubDelivery(3, 202, "Accepted"), githubDelivery(2, 500, "Internal Server Error")}, 0},
		{"none yet", nil, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, map[string]string{
				githubFakeAPI + " /app":                              `{"slug":"hq-example","owner":{"login":"example","type":"Organization"},"events":["push","workflow_run"]}`,
				githubFakeAPI + " /app/hook/deliveries?per_page=100": `[` + strings.Join(test.deliveries, ",") + `]`,
			}, runtime.Environment{})
			controller := NewController(h.r, runtime.ControllerRegistry{})
			report := controller.readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubWebhook)])
			if !report.OK || len(report.Records) != 1 {
				t.Fatalf("report %+v", report)
			}
			record := report.Records[0].(runtime.GitHubWebhookRecord)
			if record.App != "hq-example" || record.Owner != "example" || !record.Organization || !slices.Equal(record.Events, []string{"push", "workflow_run"}) {
				t.Errorf("record %+v", record)
			}
			if record.Failing != test.failing || record.Deliveries != len(test.deliveries) {
				t.Errorf("failing %d of %d", record.Failing, record.Deliveries)
			}
			if len(test.deliveries) > 0 && record.Newest.DeliveredAt != "2030-01-01T00:00:03Z" {
				t.Errorf("newest %+v", record.Newest)
			}
			if h.minted() != 0 {
				t.Errorf("minted %d installation tokens", h.minted())
			}
		})
	}
}
