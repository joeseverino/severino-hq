package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// cloudflareFake is Cloudflare's API as an httptest server: routes answer by
// method and path, every request is recorded.
type cloudflareFake struct {
	mu       sync.Mutex
	requests []*http.Request
	routes   map[string]http.HandlerFunc
}

func (f *cloudflareFake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/graphql" {
		recordVendorCall("cloudflare", r.Method, r.URL.Path)
	}
	f.mu.Lock()
	f.requests = append(f.requests, r)
	handler := f.routes[r.Method+" "+r.URL.Path]
	f.mu.Unlock()
	if handler == nil {
		http.Error(w, `{"success":false,"errors":[{"code":7003,"message":"no route"}]}`, http.StatusNotFound)
		return
	}
	handler(w, r)
}

// count is how many requests reached a path.
func (f *cloudflareFake) count(method, path string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, r := range f.requests {
		if r.Method == method && r.URL.Path == path {
			n++
		}
	}
	return n
}

// newCloudflare is a Registry whose two Cloudflare connections reach the fake
// through the real HTTP client.
func newCloudflare(t *testing.T, routes map[string]http.HandlerFunc) (*Registry, *cloudflareFake) {
	t.Helper()
	fake := &cloudflareFake{routes: routes}
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	env := runtime.Environment{
		"CLOUDFLARE_DNS_CONNECTION_REF": "example-dns", "CLOUDFLARE_DNS_API_TOKEN": "synthetic", "CLOUDFLARE_DNS_URL": server.URL,
		"CLOUDFLARE_API_CONNECTION_REF": "example-api", "CLOUDFLARE_API_API_TOKEN": "synthetic", "CLOUDFLARE_API_URL": server.URL,
	}
	r := New(env, client)
	r.Now = func() time.Time { return time.Date(2026, 1, 10, 12, 0, 0, 0, time.UTC) }
	return r, fake
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// answer is a successful envelope.
func answer(result any) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "errors": []any{}, "result": result})
	}
}

// refusing answers with status and one Cloudflare error.
func refusing(status, code int, message string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, status, map[string]any{"success": false, "errors": []any{map[string]any{"code": code, "message": message}}, "result": nil})
	}
}

// paged serves total items a page at a time, at most pageCap per page whatever
// was asked; withTotal says whether result_info carries total_pages.
func paged(total, pageCap int, withTotal bool, item func(int) any) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		perPage, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		size := min(perPage, pageCap)
		result := []any{}
		for i := (page - 1) * size; i < min(page*size, total); i++ {
			result = append(result, item(i))
		}
		info := map[string]any{"page": page, "per_page": size}
		if withTotal {
			info["total_pages"] = (total + size - 1) / size
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "errors": []any{}, "result": result, "result_info": info})
	}
}

func zone(i int) any {
	return map[string]any{"id": fmt.Sprintf("zone%03d", i), "name": fmt.Sprintf("z%03d.example", i), "account": map[string]any{"id": "acct"}, "status": "active"}
}

// Regression: the DNS probe read /zones as one page of 50, so zones past the
// first page went unnamed. Every page is read on both surfaces.
func TestZonesReadPastFiftyIsNotTruncated(t *testing.T) {
	cases := []struct {
		name      string
		total     int
		pageCap   int
		withTotal bool
		read      func(context.Context, *Registry) (int, error)
	}{
		{"dns probe, short page ends", 2*cloudflarePerPage + 3, cloudflarePerPage, false, func(ctx context.Context, r *Registry) (int, error) {
			probe, err := r.cloudflareDNSProbe(ctx, "example-dns")
			return len(probe.Reaches), err
		}},
		{"dns zone list", 2*cloudflarePerPage + 3, cloudflarePerPage, false, func(ctx context.Context, r *Registry) (int, error) {
			zones, err := r.cloudflareZones(ctx)
			return len(zones), err
		}},
		{"account zones, total_pages decides", 3 * cloudflareAccountPerPage, cloudflareAccountPerPage, true, func(ctx context.Context, r *Registry) (int, error) {
			zones, err := r.cloudflareAPIZones(ctx, "example-api")
			return len(zones), err
		}},
		{"capped page is not the last when total_pages says more", 70, 20, true, func(ctx context.Context, r *Registry) (int, error) {
			zones, err := r.cloudflareAPIZones(ctx, "example-api")
			return len(zones), err
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r, _ := newCloudflare(t, map[string]http.HandlerFunc{
				"GET /user/tokens/verify": answer(map[string]any{"status": "active", "expires_on": "2027-01-01T00:00:00Z"}),
				"GET /zones":              paged(c.total, c.pageCap, c.withTotal, zone),
			})
			got, err := c.read(t.Context(), r)
			if err != nil || got != c.total {
				t.Fatalf("read %d of %d zones: %v", got, c.total, err)
			}
		})
	}
}

// Regression: a refused credential is recorded once per sweep; every later
// call that sweep is refused without reaching Cloudflare, so repeated failures
// cannot lock the token out. The next sweep asks again.
func TestRefusedCredentialIsNotRetriedThisSweep(t *testing.T) {
	r, fake := newCloudflare(t, map[string]http.HandlerFunc{
		"GET /accounts": refusing(http.StatusForbidden, 9109, "Cannot use the access token from location: 192.0.2.1"),
	})
	end := r.BeginSnapshot()
	ctx := t.Context()
	for _, read := range []Reader{r.cloudflarePagesProjects, r.cloudflareD1Databases, r.cloudflareTunnels} {
		_, err := read(ctx)
		failure, refusal, reason := runtime.Classify(err)
		if failure != runtime.FailureClassCredential || refusal != runtime.RefusalCredential || !strings.Contains(reason, "from location") {
			t.Fatalf("%v: %q %q %q", err, failure, refusal, reason)
		}
	}
	if n := fake.count("GET", "/accounts"); n != 1 {
		t.Fatalf("the refused credential was used %d times in one sweep", n)
	}
	end()
	end = r.BeginSnapshot()
	defer end()
	if _, err := r.cloudflarePagesProjects(ctx); err == nil || fake.count("GET", "/accounts") != 2 {
		t.Fatalf("the next sweep asks again: %v", err)
	}
}

// A refusal's class comes from Cloudflare's code words and status; under 401
// a credential that still verifies is missing a permission, not dead.
func TestCloudflareRefusalClassification(t *testing.T) {
	verifies := answer(map[string]any{"status": "active"})
	cases := []struct {
		name    string
		answer  http.HandlerFunc
		verify  http.HandlerFunc
		failure runtime.FailureClass
		reason  string
	}{
		{"9109 refuses the credential", refusing(http.StatusForbidden, 9109, "Cannot use the access token from location: 192.0.2.1"), verifies, runtime.FailureClassCredential, "Cannot use the access token from location: 192.0.2.1"},
		{"10000 is a missing permission", refusing(http.StatusForbidden, 10000, "Authentication error"), verifies, runtime.FailureClassPermission, ""},
		{"401 with a credential that verifies", refusing(http.StatusUnauthorized, 10000, "Authentication error"), verifies, runtime.FailureClassPermission, ""},
		{"401 with a credential that does not", refusing(http.StatusUnauthorized, 10000, "Authentication error"), refusing(http.StatusUnauthorized, 1000, "Invalid API Token"), runtime.FailureClassCredential, "Authentication error"},
		{"expired token", refusing(http.StatusBadRequest, 6003, "Expired token"), verifies, runtime.FailureClassCredential, "Expired token"},
		{"200 with success false is refused, unclassified", refusing(http.StatusOK, 81057, "Record already exists"), verifies, runtime.FailureClassUnclassified, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r, _ := newCloudflare(t, map[string]http.HandlerFunc{"GET /zones": c.answer, "GET /user/tokens/verify": c.verify})
			_, err := r.cloudflareZones(t.Context())
			if err == nil {
				t.Fatal("refused, yet no error")
			}
			failure, _, reason := runtime.Classify(err)
			if failure != c.failure || reason != c.reason {
				t.Errorf("%v: %q %q", err, failure, reason)
			}
		})
	}
}

func TestCloudflareRefusalWords(t *testing.T) {
	cases := []struct {
		detail   string
		status   int
		verified bool
		want     runtime.FailureClass
	}{
		{"Invalid API Token", 400, false, runtime.FailureClassCredential},
		{"Authentication error", 403, false, runtime.FailureClassPermission},
		{"Authentication error", 401, true, runtime.FailureClassPermission},
		{"Authentication error", 401, false, runtime.FailureClassCredential},
		{"no reason given", 401, true, runtime.FailureClassCredential},
		{"no reason given", 403, false, runtime.FailureClassPermission},
		{"Record already exists", 400, false, runtime.FailureClassUnclassified},
	}
	for _, c := range cases {
		if got := cloudflareRefusal(c.detail, c.status, func() bool { return c.verified }); got != c.want {
			t.Errorf("cloudflareRefusal(%q, %d) = %q, want %q", c.detail, c.status, got, c.want)
		}
	}
}

// Malformed answers are errors, never coerced or skipped.
func TestMalformedCloudflareAnswersAreErrors(t *testing.T) {
	cases := []struct {
		name   string
		routes map[string]http.HandlerFunc
		read   func(context.Context, *Registry) error
	}{
		{"a zone that is not an object", map[string]http.HandlerFunc{"GET /zones": answer([]any{zone(0), "not a zone"})}, func(ctx context.Context, r *Registry) error {
			_, err := r.cloudflareZones(ctx)
			return err
		}},
		{"a record ttl that is text", map[string]http.HandlerFunc{
			"GET /zones":                     answer([]any{zone(0)}),
			"GET /zones/zone000/dns_records": answer([]any{map[string]any{"id": "r1", "type": "A", "name": "a.example", "content": "192.0.2.1", "ttl": "auto"}}),
		}, func(ctx context.Context, r *Registry) error {
			_, err := r.cloudflareRecordInventory(ctx)
			return err
		}},
		{"a list result that is not a list", map[string]http.HandlerFunc{"GET /zones": answer(map[string]any{"id": "x"})}, func(ctx context.Context, r *Registry) error {
			_, err := r.cloudflareZones(ctx)
			return err
		}},
		{"success that is not a bool", map[string]http.HandlerFunc{"GET /zones": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(w, http.StatusOK, map[string]any{"success": "yes", "result": []any{}})
		}}, func(ctx context.Context, r *Registry) error {
			_, err := r.cloudflareZones(ctx)
			return err
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r, _ := newCloudflare(t, c.routes)
			if err := c.read(t.Context(), r); err == nil {
				t.Fatal("a malformed answer read as data")
			}
		})
	}
}

func TestDNSRecordReconcile(t *testing.T) {
	live := map[string]any{"id": "r1", "type": "A", "name": "app.example", "content": "192.0.2.1", "ttl": 1, "proxied": false}
	cases := []struct {
		name    string
		records []any
		spec    CloudflareDNSRecordSpec
		apply   bool
		changed bool
		writes  string
		err     bool
	}{
		{"absent is created", []any{}, CloudflareDNSRecordSpec{Zone: "z000.example", Name: "app.example", RecordType: "A", Content: "192.0.2.1"}, true, true, "POST /zones/zone000/dns_records", false},
		{"a plan writes nothing", []any{}, CloudflareDNSRecordSpec{Zone: "z000.example", Name: "app.example", RecordType: "A", Content: "192.0.2.1"}, false, true, "", false},
		{"current is unchanged", []any{live}, CloudflareDNSRecordSpec{Zone: "z000.example", Name: "App.Example.", RecordType: "a", Content: "192.0.2.1", TTL: 1}, true, false, "", false},
		{"proxied differs is updated", []any{live}, CloudflareDNSRecordSpec{Zone: "z000.example", Name: "app.example", RecordType: "A", Content: "192.0.2.1", Proxied: true}, true, true, "PUT /zones/zone000/dns_records/r1", false},
		{"a CAA value compares by its parts", []any{map[string]any{"id": "c1", "type": "CAA", "name": "z000.example", "ttl": 1, "content": `0 issue "letsencrypt.org"`, "data": map[string]any{"flags": 0, "tag": "issue", "value": "letsencrypt.org"}}},
			CloudflareDNSRecordSpec{Zone: "z000.example", Name: "z000.example", RecordType: "CAA", Content: `0  issue "letsencrypt.org"`}, true, false, "", false},
		{"an MX record needs a priority", []any{}, CloudflareDNSRecordSpec{Zone: "z000.example", Name: "z000.example", RecordType: "MX", Content: "mx.example"}, true, false, "", true},
		{"an unknown zone is refused", []any{}, CloudflareDNSRecordSpec{Zone: "elsewhere.example", Name: "a.elsewhere.example", RecordType: "A", Content: "192.0.2.1"}, true, false, "", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			written := answer(live)
			r, fake := newCloudflare(t, map[string]http.HandlerFunc{
				"GET /zones":                        answer([]any{zone(0)}),
				"GET /zones/zone000/dns_records":    answer(c.records),
				"POST /zones/zone000/dns_records":   written,
				"PUT /zones/zone000/dns_records/r1": written,
			})
			res, err := r.cloudflareRecordReconcile(t.Context(), c.spec, CloudflareDNSRecordObserved{}, c.apply)
			if (err != nil) != c.err || res.Changed != c.changed {
				t.Fatalf("%v %+v", err, res)
			}
			for _, write := range []string{"POST /zones/zone000/dns_records", "PUT /zones/zone000/dns_records/r1"} {
				method, path, _ := strings.Cut(write, " ")
				if want := btoi(write == c.writes); fake.count(method, path) != want {
					t.Errorf("%s sent %d times, want %d", write, fake.count(method, path), want)
				}
			}
		})
	}
}

func btoi(b bool) int {
	if b {
		return 1
	}
	return 0
}

func TestNormalizedRecordContent(t *testing.T) {
	cases := map[[2]string]string{
		{"TXT", "v=spf1 -all"}:                   `"v=spf1 -all"`,
		{"TXT", `"quoted"`}:                      `"quoted"`,
		{"CNAME", " Target.Example.COM. "}:       "target.example.com",
		{"CAA", `0   issue   "letsencrypt.org"`}: `0 issue "letsencrypt.org"`,
		{"A", " 192.0.2.1 "}:                     "192.0.2.1",
	}
	for input, want := range cases {
		if got := normalizedRecordContent(input[0], input[1]); got != want {
			t.Errorf("normalizedRecordContent(%q, %q) = %q, want %q", input[0], input[1], got, want)
		}
	}
	if _, _, _, ok := caaParts("0 issue letsencrypt.org"); ok {
		t.Error("an unquoted CAA value parsed")
	}
}

func TestMilliseconds(t *testing.T) {
	value := func(f float64) *float64 { return &f }
	cases := []struct {
		name   string
		micros *float64
		want   *int
	}{
		{"absent", nil, nil},
		{"-1 means no samples", value(-1), nil},
		{"zero", value(0), new(int)},
		{"microseconds become milliseconds", value(2888000), func() *int { n := 2888; return &n }()},
		{"rounded to the nearest millisecond", value(2500), func() *int { n := 3; return &n }()},
	}
	for _, c := range cases {
		got := milliseconds(c.micros)
		if (got == nil) != (c.want == nil) || (got != nil && *got != *c.want) {
			t.Errorf("%s: got %v, want %v", c.name, got, c.want)
		}
	}
}

// The breakdowns HQ stores are the contract's AnalyticsRow dimensions, each
// asked for once in the query.
func TestAnalyticsDimensionsAreTheContracts(t *testing.T) {
	data, err := os.ReadFile("../api/hq-controller.openapi.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract struct {
		Components struct {
			Schemas struct {
				AnalyticsRow struct {
					Properties struct {
						Dimension struct {
							Enum []string `json:"enum"`
						} `json:"dimension"`
					} `json:"properties"`
				} `json:"AnalyticsRow"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	names := []string{}
	query := analyticsQuery()
	for _, dimension := range analyticsDimensions {
		names = append(names, string(dimension.name))
		if strings.Count(query, string(dimension.name)+": rumPageloadEventsAdaptiveGroups") != 1 || !strings.Contains(query, "date "+dimension.field+" }") {
			t.Errorf("%s is not asked for once", dimension.name)
		}
	}
	if want := slices.Sorted(slices.Values(contract.Components.Schemas.AnalyticsRow.Properties.Dimension.Enum)); !slices.Equal(slices.Sorted(slices.Values(names)), want) {
		t.Errorf("dimensions %v, contract %v", names, want)
	}
}

func TestAnalyticsReadsRowsAndVitals(t *testing.T) {
	graphql := func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Variables struct {
				Account string            `json:"account"`
				Filter  map[string]string `json:"filter"`
			} `json:"variables"`
		}
		_ = json.NewDecoder(r.Body).Decode(&request)
		if request.Variables.Account != "acct" || request.Variables.Filter["date_geq"] != "2026-01-09" || request.Variables.Filter["date_leq"] != "2026-01-09" {
			writeJSON(w, http.StatusOK, map[string]any{"errors": []any{map[string]any{"message": "unexpected window"}}})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]any{"viewer": map[string]any{"accounts": []any{map[string]any{
			"path": []any{
				map[string]any{"count": 12, "sum": map[string]any{"visits": 9}, "avg": map[string]any{"sampleInterval": 1.0}, "dimensions": map[string]any{"date": "2026-01-09", "requestPath": "/"}},
				map[string]any{"count": 1, "sum": map[string]any{"visits": 1}, "avg": map[string]any{"sampleInterval": 0}, "dimensions": map[string]any{"date": "2026-01-09", "requestPath": "  "}},
			},
			"vitals": []any{map[string]any{
				"count": 3, "avg": map[string]any{"sampleInterval": 10.4},
				"quantiles": map[string]any{"largestContentfulPaintP75": 1800000, "interactionToNextPaintP75": -1, "firstContentfulPaintP75": nil, "timeToFirstByteP75": 120000, "cumulativeLayoutShiftP75": 0.05},
				"sum":       map[string]any{"lcpGood": 2, "lcpPoor": 1}, "dimensions": map[string]any{"date": "2026-01-09"},
			}},
		}}}}})
	}
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{"POST /graphql": graphql})
	readings, err := r.cloudflareAnalytics(t.Context(), []CloudflareAnalyticsSite{{SiteTag: "site", Host: "example.com", Account: "acct", ConnectionRef: "example-api"}}, nil, 1)
	if err != nil || len(readings.Sites) != 1 {
		t.Fatalf("%v %+v", err, readings)
	}
	site := readings.Sites[0]
	date := "2026-01-09"
	wantRows := []runtime.AnalyticsRow{{Dimension: runtime.Path, Value: "/", Date: &date, Pageviews: 12, Visits: 9, SampleInterval: 1}}
	if !reflect.DeepEqual(site.Rows, wantRows) {
		t.Errorf("rows %+v", site.Rows)
	}
	if len(site.Vitals) != 1 {
		t.Fatalf("vitals %+v", site.Vitals)
	}
	vitals := site.Vitals[0]
	if *vitals.LargestContentfulPaintMs != 1800 || vitals.InteractionToNextPaintMs != nil || vitals.FirstContentfulPaintMs != nil ||
		*vitals.TimeToFirstByteMs != 120 || *vitals.CumulativeLayoutShift != 0.05 || vitals.SampleInterval != 10 || vitals.LcpGood != 2 || vitals.LcpPoor != 1 {
		t.Errorf("vitals %+v", vitals)
	}
}

// A GraphQL answer with errors is a refusal, not a site nobody visited.
func TestAnalyticsQueryErrorsAreRefusals(t *testing.T) {
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{"POST /graphql": func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": nil, "errors": []any{map[string]any{"message": "Authentication error"}}})
	}})
	_, err := r.cloudflareAnalytics(t.Context(), []CloudflareAnalyticsSite{{SiteTag: "site", Account: "acct", ConnectionRef: "example-api"}}, nil, 1)
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassPermission {
		t.Fatalf("%v: %q", err, failure)
	}
}

func TestRedirectHosts(t *testing.T) {
	got := expressionHosts(`(http.host eq "WWW.Example.com" or http.host in {"a.example" "*.b.example"}) and http.request.full_uri wildcard r"https://c.example/*"`)
	want := []string{"www.example.com", "a.example", "*.b.example", "c.example"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("expressionHosts = %v, want %v", got, want)
	}
	if host := targetHost("https://User@Example.com:8443/path"); host != "example.com" {
		t.Errorf("targetHost = %q", host)
	}
	if host := targetHost(`concat("https://dest.example", http.request.uri.path)`); host != "dest.example" {
		t.Errorf("targetHost of an expression = %q", host)
	}
}

func TestRedirectsReadRulesAndPageRules(t *testing.T) {
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{
		"GET /zones": answer([]any{zone(0)}),
		"GET /zones/zone000/rulesets": answer([]any{
			map[string]any{"id": "rs1", "phase": redirectPhase},
			map[string]any{"id": "rs2", "phase": "http_request_firewall_custom"},
		}),
		"GET /zones/zone000/rulesets/rs1": answer(map[string]any{"id": "rs1", "phase": redirectPhase, "rules": []any{
			map[string]any{"id": "rule1", "action": "redirect", "description": "www", "expression": `http.host eq "www.z000.example"`,
				"action_parameters": map[string]any{"from_value": map[string]any{"status_code": 301, "preserve_query_string": true, "target_url": map[string]any{"value": "https://z000.example/"}}}},
			map[string]any{"id": "rule2", "action": "redirect", "enabled": false, "expression": `http.host eq "old.z000.example"`,
				"action_parameters": map[string]any{"from_value": map[string]any{"target_url": map[string]any{"expression": `concat("https://new.example", http.request.uri.path)`}}}},
			map[string]any{"id": "rule3", "action": "block", "action_parameters": map[string]any{"response": map[string]any{"status_code": "not a number"}}},
		}}),
		"GET /zones/zone000/pagerules": answer([]any{
			map[string]any{"id": "pr1", "status": "disabled",
				"targets": []any{map[string]any{"target": "url", "constraint": map[string]any{"operator": "matches", "value": "*.legacy.z000.example/*"}}},
				"actions": []any{map[string]any{"id": "forwarding_url", "value": map[string]any{"url": "https://z000.example/$2", "status_code": 302}}}},
			map[string]any{"id": "pr2", "actions": []any{map[string]any{"id": "cache_level", "value": "bypass"}}},
		}),
	})
	ledger := &refusals{}
	records, err := r.cloudflareRedirects(context.WithValue(t.Context(), refusalKey{}, ledger))
	if err != nil || len(ledger.entries) != 0 {
		t.Fatalf("%v %+v", err, ledger.entries)
	}
	text := func(s string) *string { return &s }
	yes, no := true, false
	code := func(n int) *int { return &n }
	want := []any{
		CloudflareRedirectRecord{ConnectionRef: "example-api", AccountID: "acct", Zone: "z000.example", Source: "rule", ID: "rule1", Description: text("www"),
			Hostnames: []string{"www.z000.example"}, Target: "https://z000.example/", TargetHost: "z000.example", StatusCode: code(301), PreserveQueryString: &yes, Enabled: true},
		CloudflareRedirectRecord{ConnectionRef: "example-api", AccountID: "acct", Zone: "z000.example", Source: "rule", ID: "rule2", Description: text(""),
			Hostnames: []string{"old.z000.example"}, Target: `concat("https://new.example", http.request.uri.path)`, TargetHost: "new.example", PreserveQueryString: &no, Enabled: false},
		CloudflareRedirectRecord{ConnectionRef: "example-api", AccountID: "acct", Zone: "z000.example", Source: "page_rule", ID: "pr1",
			Hostnames: []string{"*.legacy.z000.example"}, Target: "https://z000.example/$2", TargetHost: "z000.example", StatusCode: code(302), Enabled: false},
	}
	if !reflect.DeepEqual(records, want) {
		got, _ := json.MarshalIndent(records, "", " ")
		t.Errorf("records:\n%s", got)
	}
}

// One zone refusing one part is a refused part; every part refused on every
// zone is a refused read that keeps the refusal's class.
func TestRedirectsRefusedEverywhereIsARefusedRead(t *testing.T) {
	denied := refusing(http.StatusForbidden, 10000, "Authentication error")
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{
		"GET /zones":                   answer([]any{zone(0)}),
		"GET /zones/zone000/rulesets":  denied,
		"GET /zones/zone000/pagerules": denied,
		"GET /user/tokens/verify":      answer(map[string]any{"status": "active"}),
	})
	ledger := &refusals{}
	_, err := r.cloudflareRedirects(context.WithValue(t.Context(), refusalKey{}, ledger))
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassPermission || !errors.As(err, new(*ProviderError)) {
		t.Fatalf("%v: %q", err, failure)
	}
	if len(ledger.entries) != 2 || ledger.entries[0].Part != "rules" || ledger.entries[1].Part != "page_rules" || ledger.entries[0].Refusal != runtime.FailureClassPermission {
		t.Fatalf("%+v", ledger.entries)
	}
}

func TestAccessAppsAndServiceTokens(t *testing.T) {
	app := map[string]any{
		"id": "app1", "name": "Admin", "type": "self_hosted", "domain": "admin.example", "session_duration": "24h",
		"destinations": []any{
			map[string]any{"type": "public", "uri": "https://Admin.Example/path"},
			map[string]any{"type": "private", "hostname": "intra.example", "cidr": "10.0.0.0/8"},
			map[string]any{"type": "public", "uri": "admin.example/other"},
		},
		"policies": []any{map[string]any{"id": "p1", "name": "Tokens", "include": []any{
			map[string]any{"email": map[string]any{"email": "a@example.test"}},
			map[string]any{"service_token": map[string]any{"token_id": "tok1"}},
		}}},
	}
	r, _ := newCloudflare(t, map[string]http.HandlerFunc{
		"GET /accounts":                            answer([]any{map[string]any{"id": "acct", "name": "Example"}}),
		"GET /accounts/acct/access/apps":           answer([]any{app}),
		"GET /accounts/acct/access/service_tokens": answer([]any{map[string]any{"id": "tok1", "name": "ci", "expires_at": "2027-01-01T00:00:00Z", "client_id": "never read"}, map[string]any{"id": "tok2", "name": "other"}}),
	})
	ctx := t.Context()
	apps, err := r.cloudflareAccessApps(ctx)
	if err != nil || len(apps) != 1 {
		t.Fatalf("%v %+v", err, apps)
	}
	if got := apps[0].(CloudflareAccessAppRecord); !reflect.DeepEqual(got.Destinations, []string{"admin.example", "intra.example"}) || got.Policies[0] != (CloudflareNamedRef{ID: "p1", Name: "Tokens"}) {
		t.Errorf("%+v", got)
	}
	tokens, err := r.cloudflareServiceTokens(ctx)
	if err != nil || len(tokens) != 2 {
		t.Fatalf("%v %+v", err, tokens)
	}
	first, second := tokens[0].(CloudflareServiceTokenRecord), tokens[1].(CloudflareServiceTokenRecord)
	if len(*first.Apps) != 1 || (*first.Apps)[0].ID != "app1" || len(*second.Apps) != 0 {
		t.Errorf("%+v %+v", first, second)
	}
}

func TestCloudflareAccountModelsAndDetailCalls(t *testing.T) {
	routes := map[string]http.HandlerFunc{
		"GET /accounts":                                       answer([]any{map[string]any{"id": "acct"}}),
		"GET /accounts/acct/pages/projects":                   answer([]any{map[string]any{"name": "site", "subdomain": "site.example.com", "canonical_deployment": map[string]any{"id": "deploy", "deployment_trigger": map[string]any{"metadata": map[string]any{"commit_hash": "abcdef123"}}}}}),
		"GET /accounts/acct/d1/database":                      answer([]any{map[string]any{"uuid": "db", "name": "database"}}),
		"GET /accounts/acct/d1/database/db":                   answer(map[string]any{"file_size": 42}),
		"GET /accounts/acct/cfd_tunnel":                       answer([]any{map[string]any{"id": "tunnel", "name": "ingress"}}),
		"GET /accounts/acct/cfd_tunnel/tunnel/configurations": answer(map[string]any{"source": "cloudflare", "config": map[string]any{"ingress": []any{map[string]any{"hostname": "app.example.com", "service": "http://localhost:8080"}}}}),
		"GET /accounts/acct/cfd_tunnel/tunnel/connections":    answer([]any{map[string]any{"version": "1", "conns": []any{map[string]any{"colo_name": "ORD", "origin_ip": "192.0.2.1"}}}}),
		"GET /accounts/acct/rum/site_info/list":               answer([]any{map[string]any{"site_tag": "tag", "ruleset": map[string]any{"zone_name": "example.com"}}}),
		"GET /accounts/acct/registrar/registrations":          answer([]any{map[string]any{"domain_name": "example.com"}}),
		"GET /zones": answer([]any{zone(0)}),
		"GET /zones/zone000/ssl/certificate_packs": answer([]any{map[string]any{"id": "cert", "hosts": []string{"example.com"}}}),
	}
	r, _ := newCloudflare(t, routes)
	ctx := t.Context()
	for _, test := range []struct {
		name string
		read Reader
	}{
		{"pages", r.cloudflarePagesProjects}, {"databases", r.cloudflareD1Databases}, {"tunnels", r.cloudflareTunnels}, {"certificates", r.cloudflareEdgeCertificates},
	} {
		t.Run(test.name, func(t *testing.T) {
			records, err := test.read(ctx)
			if err != nil || len(records) != 1 {
				t.Fatalf("records %v: %v", records, err)
			}
		})
	}
	sites, err := r.cloudflareAccountSites(ctx, "acct", "example-api")
	if err != nil || len(sites) != 1 || sites[0].Host != "example.com" {
		t.Fatalf("sites %v: %v", sites, err)
	}
	domains, err := r.cloudflareRegistrations(ctx)
	if err != nil || len(domains) != 1 {
		t.Fatalf("domains %v: %v", domains, err)
	}
	for _, setting := range zonePostureSettings {
		routes["GET /zones/zone000/settings/"+setting] = answer(map[string]any{"value": "strict"})
	}
	posture := r.cloudflareZonePosture(ctx, "zone000", "example.com")
	if posture["ssl"] != "strict" {
		t.Fatalf("posture %v", posture)
	}
}

func TestCloudflareDeleteAppliesOnlyWhenRequested(t *testing.T) {
	for _, apply := range []bool{false, true} {
		t.Run(strconv.FormatBool(apply), func(t *testing.T) {
			r, f := newCloudflare(t, map[string]http.HandlerFunc{
				"GET /zones":                               answer([]any{zone(0)}),
				"GET /zones/zone000/dns_records":           answer([]any{map[string]any{"id": "record", "name": "app.example.com", "type": "A", "content": "192.0.2.1"}}),
				"DELETE /zones/zone000/dns_records/record": answer(map[string]any{"id": "record"}),
			})
			result, err := r.cloudflareRecordDelete(t.Context(), CloudflareDNSRecordSpec{Zone: "z000.example", Name: "app.example.com", RecordType: "A"}, CloudflareDNSRecordObserved{RecordID: "record"}, apply)
			if err != nil || !result.Changed {
				t.Fatalf("result %+v: %v", result, err)
			}
			want := 0
			if apply {
				want = 1
			}
			if got := f.count("DELETE", "/zones/zone000/dns_records/record"); got != want {
				t.Fatalf("delete calls %d want %d", got, want)
			}
		})
	}
}
