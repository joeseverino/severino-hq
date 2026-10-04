package providers

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/adguardapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// asJSON is v as the generic JSON value it marshals to: what leaves the controller.
func asJSON(v any) any {
	data, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(data, &out)
	return out
}

// asRecords is a reader's records as generic JSON objects.
func asRecords(v any) []map[string]any {
	out := []map[string]any{}
	for _, record := range asJSON(v).([]any) {
		out = append(out, record.(map[string]any))
	}
	return out
}

// encoded is a test answer as the transport returns it.
func encoded(v any) (json.RawMessage, error) { return json.Marshal(v) }

type request struct {
	path, method string
	payload      any
	ifMatch      string // the version a conditional write is held to
}
type fakeHTTP struct {
	mu        sync.Mutex // readers run concurrently
	requests  []request
	routes    map[string]any
	fail      map[string]error
	answers   map[string]any            // write answers, where they differ from the read
	writeFail map[string]error          // failures for writes only
	headers   map[string]map[string]any // response headers by path
}

func (h *fakeHTTP) RequestHeader(ctx context.Context, address string, headers map[string]string, name string) (json.RawMessage, string, error) {
	data, err := h.Request(ctx, address, "GET", headers, nil)
	if httpStatus(err) != 0 {
		return nil, "", err
	}
	path := strings.TrimPrefix(strings.TrimPrefix(address, "https://example.invalid"), "https://api.tailscale.com/api/v2")
	value, _ := h.headers[path][name].(string)
	return data, value, err
}

// Request answers from routes, encoded as the real transport would return it.
func (h *fakeHTTP) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	value, err := h.answer(ctx, address, method, headers, payload)
	var provider *ProviderError
	if _, multipart := payload.(runtime.Multipart); multipart && errors.As(err, &provider) {
		return nil, runtime.AsMultipartFailure(provider)
	}
	if err != nil || value == nil {
		return nil, err
	}
	// Raw fixture JSON keeps its key order; null is an empty body.
	data, err := json.Marshal(value)
	if string(data) == "null" {
		return nil, err
	}
	return data, err
}

func (h *fakeHTTP) answer(_ context.Context, address, method string, headers map[string]string, payload any) (any, error) {
	path := strings.TrimPrefix(address, "https://example.invalid")
	path = strings.TrimPrefix(path, "https://api.tailscale.com/api/v2")
	h.mu.Lock()
	h.requests = append(h.requests, request{path, method, payload, headers["If-Match"]})
	h.mu.Unlock()
	if method != "GET" {
		if err, ok := h.writeFail[path]; ok {
			return nil, err
		}
	}
	if err, ok := h.fail[path]; ok {
		return nil, err
	}
	if err, ok := h.fail[address]; ok {
		return nil, err
	}
	if value, ok := h.answers[path]; ok && method != "GET" {
		return value, nil
	}
	if value, ok := h.routes[path]; ok {
		return value, nil
	}
	if value, ok := h.routes[address]; ok {
		return value, nil
	}
	if method == "GET" {
		return nil, errors.New("unexpected read: " + path)
	}
	return nil, nil
}
func adguardFixture() *Registry {
	return New(runtime.Environment{"ADGUARD_CONNECTION_REF": "example", "ADGUARD_URL": "https://example.invalid", "ADGUARD_USERNAME": "user", "ADGUARD_PASSWORD": "synthetic"}, &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
}
func TestAdguardReconcileAndPlan(t *testing.T) {
	for _, tc := range []struct {
		name         string
		live         []Object
		observed     Object
		path, method string
		changed      bool
	}{
		{"create", nil, nil, "/control/rewrite/add", "POST", true},
		{"same", []Object{{"domain": "example.test", "answer": "192.0.2.1"}}, nil, "", "", false},
		{"update", []Object{{"domain": "example.test", "answer": "192.0.2.2"}}, nil, "/control/rewrite/update", "PUT", true},
		{"rename", []Object{{"domain": "old.test", "answer": "192.0.2.1"}}, Object{"domain": "old.test"}, "/control/rewrite/update", "PUT", true},
	} {
		for _, apply := range []bool{false, true} {
			t.Run(tc.name+map[bool]string{false: "-plan", true: "-apply"}[apply], func(t *testing.T) {
				r := adguardFixture()
				h := r.HTTP.(*fakeHTTP)
				h.routes["/control/rewrite/list"] = tc.live
				result, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "reconcile", context.Background(), Object{"domain": "example.test", "answer": "192.0.2.1"}, tc.observed, apply)
				if err != nil || result.Changed != tc.changed {
					t.Fatalf("%#v %v", result, err)
				}
				want := 1
				if apply && tc.changed {
					want = 2
				}
				if len(h.requests) != want {
					t.Fatalf("requests %#v", h.requests)
				}
				if want == 2 && (h.requests[1].path != tc.path || h.requests[1].method != tc.method) {
					t.Fatal(h.requests)
				}
			})
		}
	}
}
func TestAdguardRefusesDuplicateRewriteWithoutWriting(t *testing.T) {
	r := adguardFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/control/rewrite/list"] = []Object{{"domain": "example.test"}, {"domain": "example.test"}}
	_, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "reconcile", context.Background(), Object{"domain": "example.test", "answer": "192.0.2.1"}, nil, true)
	if err == nil || len(h.requests) != 1 {
		t.Fatalf("%v %#v", err, h.requests)
	}
}
func TestAdguardDeletePreservesUnrelatedRewrite(t *testing.T) {
	r := adguardFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/control/rewrite/list"] = []Object{{"domain": "example.test", "answer": "192.0.2.1"}, {"domain": "other.test", "answer": "192.0.2.2"}}
	result, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "delete", context.Background(), Object{"domain": "example.test"}, nil, true)
	if err != nil || !result.Changed || len(h.requests) != 2 {
		t.Fatalf("%#v %v", result, err)
	}
	if !reflect.DeepEqual(asJSON(h.requests[1].payload), map[string]any{"domain": "example.test", "answer": "192.0.2.1"}) {
		t.Fatal(h.requests)
	}
}
func TestAdguardDisabledRewriteIsDegraded(t *testing.T) {
	r := adguardFixture()
	r.HTTP.(*fakeHTTP).routes["/control/rewrite/list"] = []Object{{"domain": "example.test", "answer": "192.0.2.1", "enabled": false}}
	result, err := r.runAction(runtime.ResourceKindAdGuardRewrite, "reconcile", context.Background(), Object{"domain": "example.test", "answer": "192.0.2.1"}, nil, true)
	if err != nil || result.Changed || result.Conditions[0].Type != "Degraded" {
		t.Fatalf("%#v %v", result, err)
	}
}
func TestClientsDeduplicateAddressesAndDoNotExposeExtraFields(t *testing.T) {
	var payload adguardapi.Clients
	if err := json.Unmarshal([]byte(`{"clients": [{"name": "known", "ids": ["192.0.2.1", "client-id"], "secret": "omit"}], "auto_clients": [{"ip": "192.0.2.1"}, {"ip": "192.0.2.2", "source": "ARP"}, {"ip": "invalid"}]}`), &payload); err != nil {
		t.Fatal(err)
	}
	clients := asRecords(clientRecords(payload, "example"))
	if len(clients) != 2 || clients[1]["source"] != "arp" || clients[0]["secret"] != nil {
		t.Fatal(clients)
	}
}
func TestQueriesOnlyAggregateRewrittenNamesAndHonorAnonymization(t *testing.T) {
	now := time.Date(2026, 1, 2, 12, 0, 0, 0, time.UTC)
	get := func(string) (json.RawMessage, error) {
		return encoded(Object{"data": []Object{
			{"time": "2026-01-02T11:00:00Z", "question": Object{"name": "Example.Test."}, "client": "192.0.2.1", "reason": "FilteredBlackList"},
			{"time": "2026-01-02T10:00:00Z", "question": Object{"name": "private.example"}, "client": "192.0.2.2"},
			{"time": "2026-01-02T09:00:00Z", "question": Object{"name": "child.zone.test"}, "client": "192.0.2.1"},
		}})
	}
	for _, anonymized := range []bool{false, true} {
		summaries, err := summarizeQueries(get, []string{"example.test", "unused.test", "*.zone.test"}, "example", now, anonymized)
		found := asRecords(summaries)
		if err != nil || len(found) != 3 {
			t.Fatalf("%#v %v", found, err)
		}
		if found[1]["queries"] != float64(1) || found[1]["blocked"] != float64(1) || found[1]["window_hours"] != float64(3) {
			t.Fatal(found)
		}
		for _, record := range found {
			if _, ok := record["clients"]; ok == anonymized {
				t.Fatal("wrong client privacy", record)
			}
		}
	}
}
func TestQueryPaginationHasAHardLimit(t *testing.T) {
	pages := 0
	now := time.Now()
	entries := []Object{}
	for range 500 {
		entries = append(entries, Object{"time": now.Format(time.RFC3339Nano), "question": Object{"name": "example.test"}})
	}
	get := func(string) (json.RawMessage, error) {
		pages++
		return encoded(Object{"data": entries, "oldest": "unchanged"})
	}
	summaries, err := summarizeQueries(get, []string{"example.test"}, "example", now, false)
	found := asRecords(summaries)
	if err != nil || pages != 10 || found[0]["queries"] != float64(5000) {
		t.Fatalf("pages=%d %#v %v", pages, found, err)
	}
}
func TestDNSOptionalPartFailureDoesNotEraseReadableStatus(t *testing.T) {
	r := adguardFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/control/status"] = Object{"running": true, "version": "example"}
	for _, path := range []string{"/control/dns_info", "/control/filtering/status", "/control/querylog/config", "/control/rewrite/settings"} {
		h.routes[path] = Object{}
	}
	h.fail["/control/dns_info"] = &ProviderError{Message: "Refused.", Refusal: "permission"}
	ledger := &refusals{}
	ctx := context.WithValue(context.Background(), refusalKey{}, ledger)
	records, err := r.adguardDNS(ctx)
	found := asRecords(records)
	if err != nil || len(found) != 1 || found[0]["running"] != true || len(ledger.entries) != 1 || ledger.entries[0].Part != "upstreams" {
		t.Fatalf("%#v %#v %v", found, ledger, err)
	}
}

func TestCommandOutputPastTheLimitIsAFailedStep(t *testing.T) {
	commands := &Commands{Env: runtime.Environment{"PATH": "/usr/bin:/bin"}}
	_, err := commands.Run(context.Background(), []string{"sh", "-c", "head -c 16777217 /dev/zero"}, nil, "a step", "edge", nil)
	if err == nil || err.Error() != "a step failed." {
		t.Fatalf("%v", err)
	}
	if failures := commands.StepFailures(); len(failures) != 1 || failures[0].Reason != "output over limit" {
		t.Fatalf("%#v", failures)
	}
}
