package providers

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// tailnetAnswer is one canned answer of the fake Tailscale API.
type tailnetAnswer struct {
	status int    // 0 is 200
	body   string // raw JSON; empty is an empty body
	etag   string
	// then replaces answers once this one was given, as a write changes later reads.
	then map[string]tailnetAnswer
}

// tailnetSeen is one request the fake API received.
type tailnetSeen struct {
	method, path, ifMatch, body string
}

// tailnetAPIServer is Tailscale's API over real HTTP: answers keyed by
// "METHOD /path?query" under /api/v2, every request recorded.
type tailnetAPIServer struct {
	mu      sync.Mutex
	answers map[string]tailnetAnswer
	seen    []tailnetSeen
	server  *httptest.Server
}

func newTailnetAPI(t *testing.T, answers map[string]tailnetAnswer) *tailnetAPIServer {
	t.Helper()
	api := &tailnetAPIServer{answers: map[string]tailnetAnswer{"POST /oauth/token": {body: `{"access_token":"ts-token"}`}}}
	maps.Copy(api.answers, answers)
	api.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		path := strings.TrimPrefix(r.URL.RequestURI(), "/api/v2")
		key := r.Method + " " + path
		api.mu.Lock()
		api.seen = append(api.seen, tailnetSeen{method: r.Method, path: path, ifMatch: r.Header.Get("If-Match"), body: string(body)})
		answer, ok := api.answers[key]
		maps.Copy(api.answers, answer.then)
		api.mu.Unlock()
		if !ok {
			answer = tailnetAnswer{status: http.StatusNotFound, body: `{"message":"not found"}`}
		}
		if answer.etag != "" {
			w.Header().Set("ETag", answer.etag)
		}
		w.Header().Set("Content-Type", "application/json")
		if answer.status != 0 {
			w.WriteHeader(answer.status)
		}
		_, _ = io.WriteString(w, answer.body)
	}))
	t.Cleanup(api.server.Close)
	return api
}

func (api *tailnetAPIServer) answer(key string, answer tailnetAnswer) {
	api.mu.Lock()
	defer api.mu.Unlock()
	api.answers[key] = answer
}

// requests is what was sent, as "METHOD /path", in order.
func (api *tailnetAPIServer) requests() []string {
	api.mu.Lock()
	defer api.mu.Unlock()
	out := []string{}
	for _, seen := range api.seen {
		out = append(out, seen.method+" "+seen.path)
	}
	return out
}

func (api *tailnetAPIServer) sent(key string) (tailnetSeen, bool) {
	api.mu.Lock()
	defer api.mu.Unlock()
	for _, seen := range api.seen {
		if seen.method+" "+seen.path == key {
			return seen, true
		}
	}
	return tailnetSeen{}, false
}

// tailnetRegistry is a registry whose tailscale connection points at api,
// through the real HTTP transport.
func tailnetRegistry(t *testing.T, api *tailnetAPIServer) *Registry {
	t.Helper()
	client, err := runtime.NewHTTPClient("")
	if err != nil {
		t.Fatal(err)
	}
	r := New(runtime.Environment{
		"TAILSCALE_CONNECTION_REF": "example-tailnet",
		"TAILSCALE_CLIENT_ID":      "client-id",
		"TAILSCALE_CLIENT_SECRET":  "client-secret",
		"TAILSCALE_URL":            api.server.URL + "/api/v2",
	}, client)
	t.Cleanup(r.BeginSnapshot())
	return r
}

func writeTailnetStatus(t *testing.T, status string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tailnet.json")
	if err := os.WriteFile(path, []byte(status), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const tailnetStatus = `{
	"Self": {"HostName": "this-node", "ID": "nSELF", "Online": true, "TailscaleIPs": ["192.0.2.1"]},
	"Peer": {
		"p1": {"HostName": "an-edge", "ID": "nEDGE", "Online": true, "KeyExpiry": "2026-11-04T00:00:00Z"},
		"p2": {"HostName": "a-server", "ID": "nSERV", "Online": true}
	}
}`

func withLedger() (context.Context, *refusals) {
	ledger := &refusals{}
	return context.WithValue(context.Background(), refusalKey{}, ledger), ledger
}

func TestTailnetTokenIsExchangedOncePerSweep(t *testing.T) {
	api := newTailnetAPI(t, nil)
	r := tailnetRegistry(t, api)
	for range 2 {
		if token, err := r.tailnetToken(t.Context(), "example-tailnet"); err != nil || token != "ts-token" {
			t.Fatalf("%q %v", token, err)
		}
	}
	if got := api.requests(); !reflect.DeepEqual(got, []string{"POST /oauth/token"}) {
		t.Fatalf("%v", got)
	}
}

func TestTailnetRefusalsAreClassified(t *testing.T) {
	cases := []struct {
		name    string
		key     string
		status  int
		failure runtime.FailureClass
		refusal runtime.Refusal
		scope   string
	}{
		{"token refused", "POST /oauth/token", 401, runtime.FailureClassCredential, runtime.RefusalCredential, "OAuth client"},
		{"scope missing", "GET /tailnet/-/devices?fields=all", 403, runtime.FailureClassPermission, runtime.RefusalPermission, "devices:core:read"},
		{"endpoint hidden", "GET /tailnet/-/devices?fields=all", 404, runtime.FailureClassPermission, runtime.RefusalPermission, "devices:core:read"},
		{"token rejected", "GET /tailnet/-/devices?fields=all", 401, runtime.FailureClassCredential, runtime.RefusalCredential, ""},
		{"server error", "GET /tailnet/-/devices?fields=all", 500, runtime.FailureClassUnclassified, runtime.RefusalUnclassified, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api := newTailnetAPI(t, map[string]tailnetAnswer{c.key: {status: c.status, body: `{"message":"no"}`}})
			r := tailnetRegistry(t, api)
			_, err := r.tailscaleDeviceInventory(t.Context())
			failure, refusal, reason := runtime.Classify(err)
			if failure != c.failure || refusal != c.refusal || !strings.Contains(err.Error(), c.scope) {
				t.Fatalf("%v: %q %q", err, failure, refusal)
			}
			if refusal == runtime.RefusalCredential && reason == "" {
				t.Error("a refused credential says why")
			}
		})
	}
}

func TestTailnetDeviceReconcile(t *testing.T) {
	spec := func(name string, disabled bool) Object {
		return Object{"name": name, "key_expiry_disabled": disabled, "connection_ref": "example-tailnet"}
	}
	cases := []struct {
		name     string
		spec     Object
		apply    bool
		answer   tailnetAnswer
		changed  bool
		wantBody string
		failure  runtime.FailureClass
	}{
		{name: "already as declared", spec: spec("a-server", true), apply: true},
		{name: "plan", spec: spec("an-edge", true), changed: true},
		{name: "apply", spec: spec("an-edge", true), apply: true, answer: tailnetAnswer{body: `{}`}, changed: true, wantBody: `{"keyExpiryDisabled":true}`},
		{name: "scope missing", spec: spec("an-edge", true), apply: true, answer: tailnetAnswer{status: 403}, failure: runtime.FailureClassPermission},
		{name: "unknown device", spec: spec("nowhere", true), apply: true, failure: runtime.FailureClassUnclassified},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api := newTailnetAPI(t, map[string]tailnetAnswer{"POST /device/nEDGE/key": c.answer})
			r := tailnetRegistry(t, api)
			r.Env["SEVERINO_TAILNET_STATUS"] = writeTailnetStatus(t, tailnetStatus)
			res, err := r.runAction(runtime.ResourceKindTailscaleDevice, "reconcile", t.Context(), c.spec, nil, c.apply)
			if c.failure != "" || c.name == "unknown device" {
				if failure, _, _ := runtime.Classify(err); err == nil || failure != c.failure {
					t.Fatalf("%v %q", err, failure)
				}
				return
			}
			if err != nil || res.Changed != c.changed {
				t.Fatalf("%v %#v", err, res)
			}
			seen, wrote := api.sent("POST /device/nEDGE/key")
			if wrote != (c.wantBody != "") || (wrote && seen.body != c.wantBody) {
				t.Fatalf("write %v %q", wrote, seen.body)
			}
		})
	}
}

func TestTailscaleApproveRoutes(t *testing.T) {
	routes := "GET /device/nEDGE/routes"
	cases := []struct {
		name    string
		read    tailnetAnswer
		apply   bool
		changed bool
		posted  string
		failure runtime.FailureClass
	}{
		{name: "nothing pending", read: tailnetAnswer{body: `{"advertisedRoutes":["10.0.0.0/24"],"enabledRoutes":["10.0.0.0/24"]}`}, apply: true},
		{name: "plan", read: tailnetAnswer{body: `{"advertisedRoutes":["10.0.0.0/24"],"enabledRoutes":[]}`}, changed: true},
		{name: "apply", read: tailnetAnswer{body: `{"advertisedRoutes":["10.0.1.0/24","10.0.0.0/24"],"enabledRoutes":[]}`}, apply: true, changed: true, posted: `{"routes":["10.0.0.0/24","10.0.1.0/24"]}`},
		{name: "read refused", read: tailnetAnswer{status: 403}, failure: runtime.FailureClassPermission},
		{name: "malformed routes", read: tailnetAnswer{body: `{"advertisedRoutes":"10.0.0.0/24"}`}, failure: runtime.FailureClassUnclassified},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api := newTailnetAPI(t, map[string]tailnetAnswer{
				routes:                      c.read,
				"POST /device/nEDGE/routes": {body: `{"advertisedRoutes":["10.0.0.0/24","10.0.1.0/24"],"enabledRoutes":["10.0.0.0/24","10.0.1.0/24"]}`},
			})
			r := tailnetRegistry(t, api)
			r.Env["SEVERINO_TAILNET_STATUS"] = writeTailnetStatus(t, tailnetStatus)
			res, err := r.runAction(runtime.ResourceKindTailscaleDevice, "approve-routes", t.Context(), Object{"name": "an-edge"}, nil, c.apply)
			if c.read.status != 0 || c.name == "malformed routes" {
				if failure, _, _ := runtime.Classify(err); err == nil || failure != c.failure {
					t.Fatalf("%v %q", err, failure)
				}
				return
			}
			if err != nil || res.Changed != c.changed {
				t.Fatalf("%v %#v", err, res)
			}
			seen, posted := api.sent("POST /device/nEDGE/routes")
			if posted != (c.posted != "") || (posted && seen.body != c.posted) {
				t.Fatalf("posted %v %q", posted, seen.body)
			}
			if posted && !reflect.DeepEqual(res.Status.(TailnetRouteStatus).EnabledRoutes, []string{"10.0.0.0/24", "10.0.1.0/24"}) {
				t.Fatalf("%#v", res.Status)
			}
		})
	}
}

func policyView(t *testing.T, policy string) tailnetPolicyView {
	t.Helper()
	view, err := tailnetPolicyDocument(policy).view()
	if err != nil {
		t.Fatal(err)
	}
	return view
}

func TestRefuseWeakerTests(t *testing.T) {
	live := `{"tests": [
		{"src": "a-laptop", "accept": ["a-server:443"], "deny": ["a-server:22"]},
		{"src": "a-phone", "proto": "tcp", "accept": ["a-server:443"]}]}`
	cases := []struct {
		name, declared, refusal string
	}{
		{"no tests", `{"tests": []}`, "carries no tests"},
		{"dropped deny", `{"tests": [
			{"src": "a-laptop", "accept": ["a-server:443"]},
			{"src": "a-phone", "proto": "tcp", "accept": ["a-server:443"]}]}`, `no longer tests that "a-laptop" is denied "a-server:22"`},
		{"dropped source", `{"tests": [{"src": "a-laptop", "accept": ["a-server:443"], "deny": ["a-server:22"]}]}`, `drops the tests for "a-phone" over tcp`},
		{"kept and extended", `{"tests": [
			{"src": "a-laptop", "accept": ["a-server:443", "a-server:80"], "deny": ["a-server:22", "a-server:23"]},
			{"src": "a-phone", "proto": "tcp", "accept": ["a-server:443"]}]}`, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := refuseWeakerTests(policyView(t, live), policyView(t, c.declared))
			if c.refusal == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), c.refusal) {
				t.Fatalf("%v", err)
			}
		})
	}
}

const (
	livePolicy     = `{"acls":[{"action":"accept","src":["*"],"dst":["*:*"]}],"tests":[{"src":"a-laptop","accept":["a-server:443"]}]}`
	declaredPolicy = `{"acls":[{"action":"accept","src":["a-laptop"],"dst":["a-server:443"]}],"tests":[{"src":"a-laptop","accept":["a-server:443"]}]}`
)

func policyReconcile(t *testing.T, api *tailnetAPIServer, document string, apply bool) (Result, error) {
	t.Helper()
	r := tailnetRegistry(t, api)
	return r.runAction(runtime.ResourceKindTailscalePolicy, "reconcile", t.Context(), Object{"document": document}, nil, apply)
}

// Stage 1 M1: the write is held to the version the deny check judged, taken
// from that same read, never a second GET.
func TestPolicyWriteUsesTheETagOfTheCheckedRead(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{
		"GET /tailnet/-/acl":           {body: livePolicy, etag: `"v1"`},
		"POST /tailnet/-/acl/validate": {},
		"POST /tailnet/-/acl": {body: declaredPolicy, then: map[string]tailnetAnswer{
			"GET /tailnet/-/acl": {body: declaredPolicy, etag: `"v2"`},
		}},
	})
	res, err := policyReconcile(t, api, declaredPolicy, true)
	if err != nil || !res.Changed {
		t.Fatalf("%v %#v", err, res)
	}
	want := []string{"POST /oauth/token", "GET /tailnet/-/acl", "POST /tailnet/-/acl/validate", "POST /tailnet/-/acl", "GET /tailnet/-/acl"}
	if got := api.requests(); !reflect.DeepEqual(got, want) {
		t.Fatalf("%v", got)
	}
	if write, _ := api.sent("POST /tailnet/-/acl"); write.ifMatch != `"v1"` {
		t.Fatalf("If-Match %q", write.ifMatch)
	}
}

func TestPolicyWriteRefusedWithoutETag(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{
		"GET /tailnet/-/acl":           {body: livePolicy},
		"POST /tailnet/-/acl/validate": {},
	})
	_, err := policyReconcile(t, api, declaredPolicy, true)
	if !errors.Is(err, errNoPolicyVersion) {
		t.Fatalf("%v", err)
	}
	if _, wrote := api.sent("POST /tailnet/-/acl"); wrote {
		t.Fatal("nothing is written without a version")
	}
}

func TestPolicyWriteRefusedWhenThePolicyChangedMeanwhile(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{
		"GET /tailnet/-/acl":           {body: livePolicy, etag: `"v1"`},
		"POST /tailnet/-/acl/validate": {},
		"POST /tailnet/-/acl":          {status: http.StatusPreconditionFailed},
	})
	if _, err := policyReconcile(t, api, declaredPolicy, true); !errors.Is(err, errPolicyChanged) {
		t.Fatalf("%v", err)
	}
}

// A policy that is valid JSON but not an object is refused, declared or live.
func TestNonObjectPolicyIsRefused(t *testing.T) {
	for _, declared := range []string{`[]`, `"policy"`, `null`, `42`} {
		api := newTailnetAPI(t, nil)
		if _, err := policyReconcile(t, api, declared, true); !errors.Is(err, errPolicyNotObject) {
			t.Errorf("declared %s: %v", declared, err)
		}
		if got := api.requests(); len(got) != 0 {
			t.Errorf("declared %s asked Tailscale %v", declared, got)
		}
	}
	for _, live := range []string{`[{"src":"a"}]`, `"policy"`} {
		api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/acl": {body: live, etag: `"v1"`}})
		if _, err := policyReconcile(t, api, declaredPolicy, true); !errors.Is(err, errPolicyNotObject) {
			t.Errorf("live %s: %v", live, err)
		}
		if _, wrote := api.sent("POST /tailnet/-/acl"); wrote {
			t.Errorf("live %s was overwritten", live)
		}
	}
	if _, err := policyReconcile(t, newTailnetAPI(t, nil), `{"tests": [`, true); err == nil || errors.Is(err, errPolicyNotObject) {
		t.Errorf("unreadable JSON is its own refusal: %v", err)
	}
}

func TestPolicyReconcileDecisions(t *testing.T) {
	reordered := `{"tests":[{"accept":["a-server:443"],"src":"a-laptop"}],"acls":[{"dst":["a-server:443"],"src":["a-laptop"],"action":"accept"}]}`
	cases := []struct {
		name      string
		live      string
		validate  tailnetAnswer
		declared  string
		changed   bool
		refusal   string
		validated bool
	}{
		{name: "current whatever the key order", live: reordered, declared: declaredPolicy},
		{name: "current but untested", live: `{"acls":[]}`, declared: `{"acls":[]}`},
		{name: "passes its tests", live: livePolicy, declared: declaredPolicy, changed: true, validated: true},
		{name: "fails its tests", live: livePolicy, declared: declaredPolicy, validated: true,
			validate: tailnetAnswer{body: `{"message":"test(s) failed","data":[{"user":"a-laptop","errors":["a-server:443 denied"]}]}`},
			refusal:  "does not pass its own tests; not applied: test(s) failed; a-laptop: a-server:443 denied"},
		{name: "empty verdict passes", live: livePolicy, declared: declaredPolicy, changed: true, validated: true, validate: tailnetAnswer{body: `{}`}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			api := newTailnetAPI(t, map[string]tailnetAnswer{
				"GET /tailnet/-/acl":           {body: c.live, etag: `"v1"`},
				"POST /tailnet/-/acl/validate": c.validate,
			})
			res, err := policyReconcile(t, api, c.declared, false)
			if c.refusal != "" {
				if err == nil || !strings.Contains(err.Error(), c.refusal) {
					t.Fatalf("%v", err)
				}
			} else if err != nil || res.Changed != c.changed {
				t.Fatalf("%v %#v", err, res)
			}
			if _, validated := api.sent("POST /tailnet/-/acl/validate"); validated != c.validated {
				t.Fatalf("validated %v", validated)
			}
			if _, wrote := api.sent("POST /tailnet/-/acl"); wrote {
				t.Fatal("a plan writes nothing")
			}
		})
	}
}

func TestPolicyPrettyIsSortedAndKeepsNumbers(t *testing.T) {
	got := tailnetPolicyDocument(`{"b":1,"a":{"d":12345678901234567890,"c":"<x>"}}`).pretty()
	want := "{\n  \"a\": {\n    \"c\": \"<x>\",\n    \"d\": 12345678901234567890\n  },\n  \"b\": 1\n}"
	if got != want {
		t.Fatalf("%s", got)
	}
}

// A withheld setting is null in the record and a refused part, never false.
func TestTailscaleSettingsWithheldIsNullAndRefused(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{
		"GET /tailnet/-/settings": {body: `{"devicesApprovalOn":true,"devicesKeyDurationDays":90,"aclsExternallyManagedOn":null}`},
	})
	ctx, ledger := withLedger()
	records, err := tailnetRegistry(t, api).tailscaleSettings(ctx)
	if err != nil {
		t.Fatal(err)
	}
	got := asRecords(records)[0]
	if got["devices_approval_on"] != true || got["devices_key_duration_days"] != float64(90) || got["https_enabled"] != nil || got["acls_externally_managed_on"] != nil {
		t.Fatalf("%#v", got)
	}
	parts := []string{}
	for _, entry := range ledger.entries {
		if entry.Refusal != runtime.FailureClassPermission {
			t.Errorf("%#v", entry)
		}
		parts = append(parts, string(entry.Part))
	}
	if !reflect.DeepEqual(parts, []string{"https", "acl_management"}) {
		t.Fatalf("%v", parts)
	}
}

func TestTailscaleSettingsOfTheWrongTypeAreAnError(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/settings": {body: `{"devicesApprovalOn":"yes"}`}})
	if _, err := tailnetRegistry(t, api).tailscaleSettings(t.Context()); err == nil {
		t.Fatal("a setting of the wrong type is not coerced")
	}
}

func TestTailscaleDNSResolversTakeEitherShape(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/dns/configuration": {body: `{
		"nameservers": [{"address": "198.51.100.53"}, "192.0.2.53"],
		"splitDNS": {"corp.example.com": [{"address": "192.0.2.54", "useWithExitNode": true}]},
		"searchPaths": ["example.com"],
		"preferences": {"magicDNS": true, "overrideLocalDNS": true}}`}})
	records, err := tailnetRegistry(t, api).tailscaleDNS(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	want := TailscaleDNSRecord{Record: "dns", Nameservers: []string{"198.51.100.53", "192.0.2.53"}, OverrideLocalDNS: true, MagicDNS: true,
		SearchPaths: []string{"example.com"}, SplitDNS: map[string][]string{"corp.example.com": {"192.0.2.54"}}}
	if !reflect.DeepEqual(records[0], want) {
		t.Fatalf("%#v", records[0])
	}
	api.answer("GET /tailnet/-/dns/configuration", tailnetAnswer{body: `{"nameservers": [53]}`})
	if _, err := tailnetRegistry(t, api).tailscaleDNS(t.Context()); err == nil {
		t.Fatal("a resolver that is neither shape is an error")
	}
}

func TestTailscaleUsersFromTheOfficialModel(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/users": {body: `{"users": [
		{"id": "u1", "displayName": "Test User", "loginName": "user@example.com", "role": "owner", "status": "active",
		 "created": "2026-01-01T00:00:00Z", "lastSeen": "2026-09-01T12:30:00+02:00"},
		{"displayName": "no id"}]}`}})
	records, err := tailnetRegistry(t, api).tailscaleUsers(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	want := []any{TailscaleUserRecord{ID: "u1", DisplayName: "Test User", LoginName: "user@example.com", Role: "owner", Status: "active",
		Created: "2026-01-01T00:00:00Z", LastSeen: "2026-09-01T10:30:00Z"}}
	if !reflect.DeepEqual(records, want) {
		t.Fatalf("%#v", records)
	}
	api.answer("GET /tailnet/-/users", tailnetAnswer{body: `{"users": [{"id": "u1", "created": "yesterday"}]}`})
	if _, err := tailnetRegistry(t, api).tailscaleUsers(t.Context()); err == nil {
		t.Fatal("a timestamp that is not one is an error")
	}
}

const apiDevices = `{"devices": [
	{"hostname": "an-edge", "name": "an-edge.example.ts.net", "nodeKey": "node-key-1", "connectedToControl": true,
	 "expires": "2027-01-01T00:00:00Z", "keyExpiryDisabled": false, "addresses": ["192.0.2.2"], "os": "linux",
	 "authorized": true, "user": "ops@example.com", "tags": ["tag:server", "tag:edge"],
	 "advertisedRoutes": ["0.0.0.0/0", "::/0"], "enabledRoutes": ["0.0.0.0/0"],
	 "clientConnectivity": {"endpoints": ["192.0.2.10:41641"]}, "sshEnabled": true},
	{"hostname": "a-laptop", "name": "a-laptop.example.ts.net.", "lastSeen": "2026-09-01T00:00:00Z",
	 "expires": "2027-01-01T00:00:00Z", "keyExpiryDisabled": true, "authorized": false}]}`

func TestDeviceInventoryFromTheAPI(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/devices?fields=all": {body: apiDevices}})
	records, err := tailnetRegistry(t, api).tailscaleDeviceInventory(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	edge, laptop := records[0].(TailscaleDeviceRecord), records[1].(TailscaleDeviceRecord)
	if edge.Name != "an-edge" || !edge.Online || edge.KeyExpires != "2027-01-01T00:00:00Z" || !edge.Authorized ||
		!reflect.DeepEqual(edge.Tags, []string{"tag:edge", "tag:server"}) || !edge.OffersExitNode || !edge.ExitNodeApproved ||
		!reflect.DeepEqual(edge.Endpoints, []string{"192.0.2.10:41641"}) || edge.KeyExpiryDisabled == nil || *edge.KeyExpiryDisabled {
		t.Fatalf("%#v", edge)
	}
	if laptop.DNSName != "a-laptop.example.ts.net" || laptop.KeyExpires != "" || laptop.LastSeen != "2026-09-01T00:00:00Z" || laptop.Authorized ||
		!reflect.DeepEqual(laptop.Endpoints, []string{}) || laptop.KeyExpiryDisabled == nil || !*laptop.KeyExpiryDisabled {
		t.Fatalf("%#v", laptop)
	}
	api.answer("GET /tailnet/-/devices?fields=all", tailnetAnswer{body: `{"devices": [{"hostname": "x", "authorized": "yes"}]}`})
	if _, err := tailnetRegistry(t, api).tailscaleDeviceInventory(t.Context()); err == nil {
		t.Fatal("a device of the wrong shape is an error, not skipped")
	}
}

func TestDeviceInventoryFromTheLocalReading(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{"GET /tailnet/-/devices?fields=all": {body: apiDevices}})
	r := tailnetRegistry(t, api)
	r.Env["SEVERINO_TAILNET_STATUS"] = writeTailnetStatus(t, tailnetStatus)
	records, err := r.tailscaleDeviceInventory(t.Context())
	if err != nil || len(records) != 3 {
		t.Fatalf("%v %#v", err, records)
	}
	self, edge, server := records[0].(TailscaleDeviceRecord), records[1].(TailscaleDeviceRecord), records[2].(TailscaleDeviceRecord)
	if !self.Self || edge.Self || edge.User != "ops@example.com" || !edge.ExitNodeApproved || server.KeyExpiryDisabled != nil || !server.Authorized {
		t.Fatalf("%#v\n%#v\n%#v", self, edge, server)
	}

	r.Env["SEVERINO_TAILNET_STATUS"] = writeTailnetStatus(t, `{"Self": "not a node"}`)
	if _, err := r.tailscaleDeviceInventory(t.Context()); err == nil {
		t.Fatal("an unreadable local reading is an error")
	}
	r.Env["SEVERINO_TAILNET_STATUS"] = filepath.Join(t.TempDir(), "absent.json")
	if _, err := r.tailscaleDeviceInventory(t.Context()); err == nil {
		t.Fatal("a missing local reading is an error")
	}
}

func TestTailnetPolicyInventoryKeepsWhatWasReadBeforeARefusal(t *testing.T) {
	api := newTailnetAPI(t, map[string]tailnetAnswer{
		"GET /tailnet/-/acl": {body: `{
			"nodeAttrs": [{"app": {"tailscale.com/app-connectors": [{"name": "example-connector", "connectors": ["tag:server"], "domains": ["example.test"]}]}}],
			"groups": {"group:eng": ["user2", "user1"]},
			"tagOwners": {"tag:server": ["group:eng"]},
			"grants": [{"src": ["group:eng"], "dst": ["tag:server"], "ip": ["*"]}],
			"tests": [{"src": "user1", "accept": ["tag:server:443"]}]}`},
		"GET /tailnet/-/settings":        {body: `{"httpsEnabled": true}`},
		"GET /tailnet/-/dns/preferences": {body: `{"magicDNS": true}`},
		"GET /tailnet/-/dns/nameservers": {status: 403},
		"GET /tailnet/-/services":        {body: `{"vipServices": [{"name": "svc:example", "addrs": ["192.0.2.9"], "ports": ["tcp:443"], "comment": "Example service"}]}`},
	})
	ctx, ledger := withLedger()
	records, err := tailnetRegistry(t, api).tailnetPolicyInventory(ctx)
	if err != nil {
		t.Fatal(err)
	}
	record := records[0].(TailscalePolicyRecord)
	if string(record.Settings) != `{"httpsEnabled":true}` || string(record.DNS) != `{"magicDNS":true}` {
		t.Fatalf("%s %s", record.Settings, record.DNS)
	}
	if len(record.AppConnectors) != 1 || len(record.Services) != 1 || !reflect.DeepEqual(record.Groups, []TailnetGroup{{Name: "group:eng", Members: []string{"user1", "user2"}}}) {
		t.Fatalf("%#v", record)
	}
	var tests []json.RawMessage
	if json.Unmarshal(record.Tests, &tests) != nil || len(tests) != 1 {
		t.Fatalf("%s", record.Tests)
	}
	if len(ledger.entries) != 1 || ledger.entries[0].Part != "dns" || ledger.entries[0].Refusal != runtime.FailureClassPermission {
		t.Fatalf("%#v", ledger.entries)
	}
}

// The ports asked about include the one each SSH connection declares, so a
// host that moved SSH off 22 is still answered for. A connection whose port
// does not parse adds nothing.
func TestReachAsksAboutDeclaredSSHPorts(t *testing.T) {
	r := New(runtime.Environment{
		"EDGE_CONNECTION_REF": "edge", "EDGE_HOST": "192.0.2.9", "EDGE_USER": "hq", "EDGE_PORT": "2222", "EDGE_HOST_KEY": "ssh-ed25519 AAAA",
		"ODD_CONNECTION_REF": "odd", "ODD_HOST": "192.0.2.10", "ODD_USER": "hq", "ODD_PORT": "ssh", "ODD_HOST_KEY": "ssh-ed25519 AAAA",
	}, &fakeHTTP{})
	if got := r.portsWorthAsking(t.Context()); !slices.Equal(got, []int{22, 53, 80, 443, 2222}) {
		t.Fatalf("tailnet reach ports = %v", got)
	}
}
