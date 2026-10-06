package providers

import (
	"context"
	"reflect"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

func npmFixture() *Registry {
	h := &fakeHTTP{
		routes: map[string]any{
			"/api/tokens": Object{"token": "short-lived"},
		},
		fail: map[string]error{},
	}
	return New(runtime.Environment{}, supplied(loginConnection(runtime.ConnectionProviderNPM, "example-npm", "https://example.invalid", "user", "synthetic")), h)
}

func sampleProxySpec() Object {
	return Object{
		"domain_names":          []string{"hq.example"},
		"forward_scheme":        "http",
		"forward_host":          "192.0.2.10",
		"forward_port":          8000,
		"force_ssl":             false,
		"http2":                 true,
		"websocket":             false,
		"caching_enabled":       false,
		"block_exploits":        true,
		"access_list_id":        0,
		"advanced_config":       "",
		"hsts_enabled":          false,
		"hsts_subdomains":       false,
		"trust_forwarded_proto": false,
		"serving":               true,
	}
}

func TestNPMReconcileAndPlan(t *testing.T) {
	live := []Object{
		{
			"id":                      9,
			"domain_names":            []any{"hq.example"},
			"forward_scheme":          "http",
			"forward_host":            "192.0.2.10",
			"forward_port":            8000,
			"caching_enabled":         false,
			"block_exploits":          true,
			"allow_websocket_upgrade": false,
			"access_list_id":          0,
			"certificate_id":          0,
			"ssl_forced":              false,
			"http2_support":           true,
			"hsts_enabled":            false,
			"hsts_subdomains":         false,
			"trust_forwarded_proto":   false,
			"advanced_config":         "",
			"locations":               []any{},
			"enabled":                 true,
			"meta":                    Object{},
		},
	}
	renamedLive := []Object{
		{
			"id":                      9,
			"domain_names":            []any{"old.example"},
			"forward_scheme":          "http",
			"forward_host":            "192.0.2.10",
			"forward_port":            8000,
			"caching_enabled":         false,
			"block_exploits":          true,
			"allow_websocket_upgrade": false,
			"access_list_id":          0,
			"certificate_id":          0,
			"ssl_forced":              false,
			"http2_support":           true,
			"hsts_enabled":            false,
			"hsts_subdomains":         false,
			"trust_forwarded_proto":   false,
			"advanced_config":         "",
			"locations":               []any{},
			"enabled":                 true,
			"meta":                    Object{},
		},
	}

	for _, tc := range []struct {
		name         string
		live         []Object
		observed     Object
		path, method string
		changed      bool
	}{
		{"create", []Object{}, nil, "/api/nginx/proxy-hosts", "POST", true},
		{"same", live, nil, "", "", false},
		{"update", []Object{{"id": 9, "domain_names": []any{"hq.example"}, "forward_port": 9000}}, nil, "/api/nginx/proxy-hosts/9", "PUT", true},
		{"rename", renamedLive, Object{"domain_names": []string{"old.example"}}, "/api/nginx/proxy-hosts/9", "PUT", true},
	} {
		for _, apply := range []bool{false, true} {
			t.Run(tc.name+map[bool]string{false: "-plan", true: "-apply"}[apply], func(t *testing.T) {
				r := npmFixture()
				h := r.HTTP.(*fakeHTTP)
				h.routes["/api/nginx/proxy-hosts"] = tc.live
				result, err := r.runAction(runtime.ResourceKindNPMProxyHost, "reconcile", t.Context(), sampleProxySpec(), tc.observed, apply)
				if err != nil || result.Changed != tc.changed {
					t.Fatalf("result=%#v err=%v", result, err)
				}
				// 1 token exchange + 1 read proxy-hosts
				want := 2
				if apply && tc.changed {
					want = 3
				}
				if len(h.requests) != want {
					t.Fatalf("got %d requests, want %d: %#v", len(h.requests), want, h.requests)
				}
				if want == 3 && (h.requests[2].path != tc.path || h.requests[2].method != tc.method) {
					t.Fatalf("unexpected write request: %#v", h.requests[2])
				}
			})
		}
	}
}

func TestNPMRefusesHTTPSWithoutCertificate(t *testing.T) {
	r := npmFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/api/nginx/proxy-hosts"] = []Object{}
	spec := sampleProxySpec()
	spec["force_ssl"] = true
	spec["certificate_id"] = 0

	_, err := r.runAction(runtime.ResourceKindNPMProxyHost, "reconcile", t.Context(), spec, nil, true)
	if err == nil {
		t.Fatal("expected error for https without certificate")
	}
}

func TestNPMDelete(t *testing.T) {
	r := npmFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/api/nginx/proxy-hosts"] = []Object{
		{"id": 7, "domain_names": []any{"other.example"}},
		{"id": 9, "domain_names": []any{"hq.example"}},
	}
	result, err := r.runAction(runtime.ResourceKindNPMProxyHost, "delete", t.Context(), Object{"domain_names": []string{"hq.example"}}, nil, true)
	if err != nil || !result.Changed {
		t.Fatalf("%#v %v", result, err)
	}
	if len(h.requests) != 3 || h.requests[2].path != "/api/nginx/proxy-hosts/9" || h.requests[2].method != "DELETE" {
		t.Fatalf("unexpected requests: %#v", h.requests)
	}
}

func TestNPMInventory(t *testing.T) {
	r := npmFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/api/nginx/proxy-hosts"] = []Object{
		{
			"id":             10,
			"domain_names":   []any{"app.example.com"},
			"forward_scheme": "http",
			"forward_host":   "127.0.0.1",
			"forward_port":   8000,
			"certificate_id": 1,
			"access_list_id": 7,
			"enabled":        true,
		},
	}
	h.routes["/api/nginx/access-lists?expand=items,clients"] = []Object{
		{
			"id":          7,
			"name":        "staff",
			"satisfy_any": true,
			"pass_auth":   false,
			"items":       []any{Object{"username": "operator"}},
			"clients":     []any{Object{"directive": "allow", "address": "192.0.2.0/24"}},
		},
	}
	h.routes["/api/nginx/certificates"] = []Object{
		{
			"id":           1,
			"nice_name":    "example wildcard",
			"domain_names": []any{"*.example.com", "example.com"},
			"expires_on":   "2030-01-01 00:00:00",
			"provider":     "letsencrypt",
		},
	}

	found, err := r.npmInventory(t.Context())
	if err != nil || len(found) != 1 {
		t.Fatalf("%#v %v", found, err)
	}
	rec := found[0].(NPMProxyHostRecord)
	if rec.Certificate.NPMCertificateSummary == nil || rec.Certificate.Name != "example wildcard" {
		t.Fatalf("unexpected certificate: %#v", rec.Certificate)
	}
	if rec.AccessPolicy == nil || rec.AccessPolicy.Name != "staff" || !rec.AccessPolicy.SatisfyAny {
		t.Fatalf("unexpected policy: %#v", rec.AccessPolicy)
	}
}

func TestNPMReadings(t *testing.T) {
	r := npmFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/api/nginx/certificates"] = []Object{
		{
			"id":           1,
			"nice_name":    "example wildcard",
			"provider":     "letsencrypt",
			"domain_names": []any{"*.example.com", "example.com"},
			"expires_on":   "2030-01-01 00:00:00",
			"meta":         Object{"certificate_key": "PRIVATE KEY"},
		},
		{
			"id":           2,
			"nice_name":    "unused",
			"provider":     "other",
			"domain_names": []any{"old.example.net"},
			"expires_on":   "2030-02-01 00:00:00",
		},
	}
	h.routes["/api/nginx/proxy-hosts"] = []Object{
		{"id": 10, "domain_names": []any{"app.example.com"}, "certificate_id": 1, "access_list_id": 7, "enabled": true},
		{"id": 11, "domain_names": []any{"off.example.com"}, "certificate_id": 1, "enabled": false},
	}
	h.routes["/api/nginx/redirection-hosts"] = []Object{
		{"id": 20, "domain_names": []any{"www.example.com"}, "forward_scheme": "https", "forward_domain_name": "example.com", "forward_http_code": 301, "preserve_path": true, "certificate_id": 1, "ssl_forced": true, "enabled": true},
		{"id": 21, "domain_names": []any{"auto.example.com"}, "forward_scheme": "auto", "forward_domain_name": "example.org", "forward_http_code": 302, "enabled": false},
	}
	h.routes["/api/nginx/dead-hosts"] = []Object{
		{"id": 30, "domain_names": []any{"gone.example.com"}, "certificate_id": 1, "enabled": true},
	}
	h.routes["/api/nginx/streams"] = []Object{
		{"id": 40, "incoming_port": 2222, "forwarding_host": "192.0.2.30", "forwarding_port": 22, "tcp_forwarding": true, "udp_forwarding": false, "enabled": true},
	}
	h.routes["/api/nginx/access-lists?expand=items,clients"] = []Object{
		{
			"id":          7,
			"name":        "staff",
			"satisfy_any": true,
			"pass_auth":   false,
			"items":       []any{Object{"username": "operator", "password": "omit"}},
			"clients":     []any{Object{"directive": "allow", "address": "192.0.2.0/24"}, Object{"directive": "deny", "address": "all"}},
		},
	}

	ctx := t.Context()

	// 1. Certificates
	certs, err := r.npmCertificates(ctx, "example-npm")
	if err != nil || len(certs) != 2 {
		t.Fatalf("%#v %v", certs, err)
	}
	wildcard := certs[0]
	if !reflect.DeepEqual(wildcard.Serves, []string{"app.example.com", "www.example.com", "gone.example.com"}) {
		t.Fatalf("serves mismatch: %#v", wildcard.Serves)
	}
	if asJSON(wildcard).(map[string]any)["meta"] != nil {
		t.Fatal("private key metadata leaked!")
	}

	// 2. Redirects
	reds, err := r.npmRedirects(ctx, "example-npm")
	if err != nil || len(reds) != 2 {
		t.Fatalf("%#v %v", reds, err)
	}
	if reds[0].Target != "https://example.com" || reds[0].Certificate != "example wildcard" {
		t.Fatalf("redirect mismatch: %#v", reds[0])
	}
	if reds[1].Target != "example.org" {
		t.Fatalf("auto target mismatch: %#v", reds[1])
	}

	// 3. Dead hosts
	dead, err := r.npmDeadHosts(ctx, "example-npm")
	if err != nil || len(dead) != 1 {
		t.Fatalf("%#v %v", dead, err)
	}
	if dead[0].Certificate != "example wildcard" {
		t.Fatalf("dead host cert mismatch: %#v", dead[0])
	}

	// 4. Streams
	streams, err := r.npmStreams(ctx, "example-npm")
	if err != nil || len(streams) != 1 {
		t.Fatalf("%#v %v", streams, err)
	}
	if streams[0].IncomingPort != 2222 || streams[0].ForwardingPort == nil || *streams[0].ForwardingPort != 22 {
		t.Fatalf("stream mismatch: %#v", streams[0])
	}

	// 5. Access Lists
	access, err := r.npmAccessLists(ctx, "example-npm")
	if err != nil || len(access) != 1 {
		t.Fatalf("%#v %v", access, err)
	}
	staff := access[0]
	if !reflect.DeepEqual(staff.Protects, []string{"app.example.com"}) {
		t.Fatalf("protects mismatch: %#v", staff.Protects)
	}
	if !reflect.DeepEqual(staff.Logins, []string{"operator"}) {
		t.Fatalf("logins mismatch: %#v", staff.Logins)
	}
}

func TestNPMReadingRefusals(t *testing.T) {
	r := npmFixture()
	h := r.HTTP.(*fakeHTTP)
	h.routes["/api/nginx/certificates"] = []Object{
		{"id": 1, "nice_name": "wildcard", "provider": "letsencrypt", "domain_names": []any{"example.com"}},
	}
	h.routes["/api/nginx/proxy-hosts"] = []Object{
		{"id": 10, "domain_names": []any{"app.example.com"}, "certificate_id": 1, "enabled": true},
	}
	h.routes["/api/nginx/redirection-hosts"] = []Object{}
	h.routes["/api/nginx/streams"] = []Object{}
	h.fail["/api/nginx/dead-hosts"] = &ProviderError{Message: "Forbidden", Failure: "permission"}

	ledger := &refusals{}
	ctx := context.WithValue(t.Context(), refusalKey{}, ledger)

	certs, err := r.npmCertificates(ctx, "example-npm")
	if err != nil || len(certs) != 1 {
		t.Fatalf("%#v %v", certs, err)
	}
	if len(ledger.entries) != 1 {
		t.Fatalf("expected 1 refused part, got: %#v", ledger.entries)
	}
	refused := ledger.entries[0]
	if refused.Part != "dead_hosts" || refused.Refusal != "permission" {
		t.Fatalf("unexpected refusal record: %#v", refused)
	}
}
