package providers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// workerRoutes answer as Cloudflare's API reference shows List Workers, List
// Deployments and Get Worker Version: two Workers, the first deployed at one
// version tagged by its deploy and binding HQ's answers dataset among its
// other bindings.
func workerRoutes(graphql, sql http.HandlerFunc) map[string]http.HandlerFunc {
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
			"bindings": []any{
				map[string]any{"type": "durable_object_namespace", "name": "HUB", "class_name": "Hub"},
				map[string]any{"type": "secret_text", "name": "GITHUB_WEBHOOK_SECRET"},
				map[string]any{"type": "analytics_engine", "name": "ANSWERS", "dataset": "hq_events"},
			},
		}),
		"GET /accounts/acct/workers/scripts/example-quiet/deployments": answer(map[string]any{"deployments": []any{}}),
		"POST /graphql": graphql,
		"POST /accounts/acct/analytics_engine/sql": sql,
	}
}

// uninvoked answers the Workers analytics query with no Worker invoked.
func uninvoked(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"data": map[string]any{"viewer": map[string]any{"accounts": []any{map[string]any{"workersInvocationsAdaptive": []any{}}}}}})
}

// answers answers an Analytics Engine query as its FORMAT JSON shape in
// Cloudflare's spec (analytics-engine_JsonFormatResponse), keeping the query.
func answers(asked *string, rows ...map[string]any) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		query, _ := io.ReadAll(r.Body)
		*asked = string(query)
		writeJSON(w, http.StatusOK, map[string]any{
			"meta": []any{
				map[string]any{"name": "route", "type": "String"}, map[string]any{"name": "status", "type": "Float64"},
				map[string]any{"name": "requests", "type": "UInt64"}, map[string]any{"name": "last_hour", "type": "UInt64"},
			},
			"data": rows, "rows": len(rows),
		})
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
	var query string
	r, _ := newCloudflare(t, workerRoutes(graphql, answers(&query)))
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

// A Worker whose serving version binds HQ's answers dataset carries its count
// of each route and status over the day and its last hour, weighed for
// sampling; one binding none was never counted, so it claims no answers. A
// UInt64 count arrives quoted, as production answers it, or bare, as the
// spec's example shows it.
func TestWorkersAnswersAreCountedForAVersionBindingTheDataset(t *testing.T) {
	var query string
	r, _ := newCloudflare(t, workerRoutes(uninvoked, answers(&query,
		map[string]any{"route": "/github", "status": 202, "requests": "118", "last_hour": "0"},
		map[string]any{"route": "/github", "status": 401, "requests": 9, "last_hour": 9},
	)))
	records, err := r.cloudflareWorkers(t.Context())
	if err != nil || len(records) != 2 {
		t.Fatalf("%v %+v", err, records)
	}
	events, quiet := records[0].(runtime.CloudflareWorkerRecord), records[1].(runtime.CloudflareWorkerRecord)
	want := []runtime.CloudflareWorkerWorkerAnswersRecord{
		{Route: "/github", Status: 202, Requests: 118},
		{Route: "/github", Status: 401, Requests: 9, LastHour: 9},
	}
	if events.Answers == nil || !slices.Equal(*events.Answers, want) || quiet.Answers != nil {
		t.Errorf("%+v %+v", events.Answers, quiet.Answers)
	}
	// The dataset, then the hour, the day and the minute read, as Unix seconds.
	for _, named := range []string{"FROM hq_events", "_sample_interval", "toDateTime(1768042800)", "toDateTime(1767960000)", "toDateTime(1768046400)"} {
		if !strings.Contains(query, named) {
			t.Errorf("%q lacks %q", query, named)
		}
	}
}

// Analytics refused is the invocations part refused: the Workers still read,
// and none of them claims a count.
func TestWorkersInvocationsRefusedIsARefusedPart(t *testing.T) {
	r, _ := newCloudflare(t, workerRoutes(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": nil, "errors": []any{map[string]any{"message": "Authentication error"}}})
	}, answers(new(string))))
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

// The SQL API refuses in plain text, 403 for any credential it will not take
// (analytics-engine_Forbidden): the answers part refused, its reason in its
// words, while the rest of the Worker still reads.
func TestWorkersAnswersRefusedIsARefusedPart(t *testing.T) {
	r, _ := newCloudflare(t, workerRoutes(uninvoked, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, "Authorization error")
	}))
	ledger := &refusals{}
	records, err := r.cloudflareWorkers(context.WithValue(t.Context(), refusalKey{}, ledger))
	if err != nil || len(records) != 2 {
		t.Fatalf("%v %+v", err, records)
	}
	if events := records[0].(runtime.CloudflareWorkerRecord); events.Answers != nil || events.Requests == nil || len(events.Versions) != 1 {
		t.Errorf("%+v", events)
	}
	if len(ledger.entries) != 1 || ledger.entries[0].Part != runtime.PartAnswers || ledger.entries[0].Refusal != runtime.FailureClassPermission || !strings.Contains(ledger.entries[0].Reason, "Authorization error") {
		t.Fatalf("%+v", ledger.entries)
	}
}
