package providers

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// workerRoutes answer as Cloudflare's API reference shows List Workers, List
// Deployments and Get Worker Version: two Workers, the first deployed at one
// version tagged by its deploy.
func workerRoutes(graphql http.HandlerFunc) map[string]http.HandlerFunc {
	return map[string]http.HandlerFunc{
		"GET /accounts": answer([]any{map[string]any{"id": "acct"}}),
		"GET /accounts/acct/workers/scripts": answer([]any{
			map[string]any{"id": "example-events", "created_on": "2026-01-01T00:00:00Z", "handlers": []string{"fetch"}},
			map[string]any{"id": "example-quiet"},
		}),
		"GET /accounts/acct/workers/scripts/example-events/deployments": answer(map[string]any{"deployments": []any{
			map[string]any{
				"id": "dep-new", "created_on": "2026-01-09T18:30:00.000000Z", "source": "wrangler", "strategy": "percentage",
				"versions":    []any{map[string]any{"version_id": "ver-7", "percentage": 100}},
				"annotations": map[string]any{"workers/triggered_by": "deployment"},
			},
			map[string]any{"id": "dep-old", "created_on": "2026-01-02T00:00:00Z", "source": "wrangler", "strategy": "percentage", "versions": []any{map[string]any{"version_id": "ver-6", "percentage": 100}}},
		}}),
		"GET /accounts/acct/workers/workers/example-events/versions/ver-7": answer(map[string]any{
			"id": "ver-7", "number": 7, "created_on": "2026-01-09T18:29:00Z", "urls": []any{},
			"annotations": map[string]any{"workers/tag": "0123456789abcdef0123456789abcdef", "workers/triggered_by": "upload"},
		}),
		"GET /accounts/acct/workers/scripts/example-quiet/deployments": answer(map[string]any{"deployments": []any{}}),
		"POST /graphql": graphql,
	}
}

// The serving deployment is the list's first, with each of its versions'
// number and tag, and a Worker's last day of invocations is its row of one
// query over the account; a Worker with no row was not invoked.
func TestWorkersReadDeploymentsAndInvocations(t *testing.T) {
	var window map[string]string
	graphql := func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Variables map[string]string `json:"variables"`
		}
		_ = json.NewDecoder(r.Body).Decode(&request)
		window = request.Variables
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]any{"viewer": map[string]any{"accounts": []any{map[string]any{
			"workersInvocationsAdaptive": []any{map[string]any{
				"sum": map[string]any{"requests": 120, "errors": 3, "subrequests": 0}, "quantiles": map[string]any{"cpuTimeP50": 206, "cpuTimeP99": 1800.5},
				"dimensions": map[string]any{"scriptName": "example-events"},
			}},
		}}}}})
	}
	r, _ := newCloudflare(t, workerRoutes(graphql))
	records, err := r.cloudflareWorkers(t.Context())
	if err != nil || len(records) != 2 {
		t.Fatalf("%v %+v", err, records)
	}
	if window["account"] != "acct" || window["since"] != "2026-01-09T12:00:00Z" || window["until"] != "2026-01-10T12:00:00Z" {
		t.Errorf("window %v", window)
	}
	events := records[0].(runtime.CloudflareWorkerRecord)
	version := runtime.CloudflareWorkerWorkerVersionRecord{ID: "ver-7", Number: 7, Percentage: 100, Tag: "0123456789abcdef0123456789abcdef"}
	if events.Name != "example-events" || events.DeploymentID != "dep-new" || events.DeployedAt != "2026-01-09T18:30:00.000000Z" || len(events.Versions) != 1 || events.Versions[0] != version {
		t.Errorf("%+v", events)
	}
	if *events.Requests != 120 || *events.Errors != 3 || *events.CPUTimeP50Us != 206 || *events.CPUTimeP99Us != 1800.5 {
		t.Errorf("invocations %+v", events)
	}
	quiet := records[1].(runtime.CloudflareWorkerRecord)
	if quiet.DeploymentID != "" || *quiet.Requests != 0 || *quiet.Errors != 0 || quiet.CPUTimeP50Us != nil {
		t.Errorf("%+v", quiet)
	}
}

// Analytics refused is the invocations part refused: the Workers still read,
// and none of them claims a count.
func TestWorkersInvocationsRefusedIsARefusedPart(t *testing.T) {
	r, _ := newCloudflare(t, workerRoutes(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": nil, "errors": []any{map[string]any{"message": "Authentication error"}}})
	}))
	ledger := &refusals{}
	records, err := r.cloudflareWorkers(context.WithValue(t.Context(), refusalKey{}, ledger))
	if err != nil || len(records) != 2 {
		t.Fatalf("%v %+v", err, records)
	}
	if events := records[0].(runtime.CloudflareWorkerRecord); events.Requests != nil || events.Errors != nil || events.Versions[0].Tag == "" {
		t.Errorf("%+v", events)
	}
	if len(ledger.entries) != 1 || ledger.entries[0].Part != runtime.PartInvocations || ledger.entries[0].Refusal != runtime.FailureClassPermission {
		t.Fatalf("%+v", ledger.entries)
	}
}
