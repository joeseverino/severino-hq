package providers

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

func tailscaleFixture() *Registry {
	h := &fakeHTTP{
		routes: map[string]any{
			"/oauth/token": Object{"access_token": "ts-token-123"},
		},
		fail: map[string]error{},
	}
	return New(runtime.Environment{
		"TAILSCALE_CONNECTION_REF": "example-tailnet",
		"TAILSCALE_CLIENT_ID":      "client-id-123",
		"TAILSCALE_CLIENT_SECRET":  "client-secret-123",
	}, h)
}

func writeTempTailnetStatus(t *testing.T, status Object) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "tailnet.json")
	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("marshal status: %v", err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	return path
}

func TestTailnetTokenExchangeAndCache(t *testing.T) {
	r := tailscaleFixture()
	h := r.HTTP.(*fakeHTTP)
	cleanup := r.BeginSnapshot()
	defer cleanup()

	ctx := context.Background()
	tok1, err := r.tailnetToken(ctx, "example-tailnet")
	if err != nil || tok1 != "ts-token-123" {
		t.Fatalf("token 1: %q, %v", tok1, err)
	}
	tok2, err := r.tailnetToken(ctx, "example-tailnet")
	if err != nil || tok2 != "ts-token-123" {
		t.Fatalf("token 2: %q, %v", tok2, err)
	}
	// Verify cached in snapshot: only 1 request made
	count := 0
	for _, req := range h.requests {
		if req.path == "/oauth/token" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("expected 1 token request, got %d", count)
	}
}

func TestTailnetTokenRefusal(t *testing.T) {
	r := tailscaleFixture()
	h := r.HTTP.(*fakeHTTP)
	cleanup := r.BeginSnapshot()
	defer cleanup()

	h.fail["/oauth/token"] = runtime.HTTPRefusal(401)
	_, err := r.tailnetToken(context.Background(), "example-tailnet")
	if err == nil {
		t.Fatal("expected error")
	}
	var pe *ProviderError
	if ok := reflect.TypeOf(err).AssignableTo(reflect.TypeOf(pe)); !ok {
		t.Fatalf("expected ProviderError, got %T", err)
	}
}

func TestTailnetDeviceReconcile(t *testing.T) {
	status := Object{
		"Self": Object{"HostName": "this-node", "ID": "nSELF", "Online": true},
		"Peer": Object{
			"p1": Object{"HostName": "an-edge", "ID": "nEDGE", "Online": true, "KeyExpiry": "2026-11-04T00:00:00Z"},
			"p2": Object{"HostName": "a-server", "ID": "nSERV", "Online": true},
		},
	}
	path := writeTempTailnetStatus(t, status)
	t.Setenv("SEVERINO_TAILNET_STATUS", path)

	r := tailscaleFixture()
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ctx := context.Background()

	// 1. Device already as declared (a-server has no expiry -> disabled=true)
	res, err := r.tailscaleDeviceReconcile(ctx, Object{"name": "a-server", "key_expiry_disabled": true, "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed {
		t.Fatalf("a-server already current: %v, %#v", err, res)
	}
	if res.Message != "Tailnet device is current." {
		t.Fatalf("unexpected message: %s", res.Message)
	}

	// 2. Dry run (apply=false) on an-edge
	res, err = r.tailscaleDeviceReconcile(ctx, Object{"name": "an-edge", "key_expiry_disabled": true, "connection_ref": "example-tailnet"}, nil, false)
	if err != nil || !res.Changed {
		t.Fatalf("an-edge dry run: %v, %#v", err, res)
	}
	if res.Message != "Key expiry would be disabled for an-edge." {
		t.Fatalf("unexpected dry run message: %s", res.Message)
	}

	// 3. Apply on an-edge
	h := r.HTTP.(*fakeHTTP)
	h.routes["/device/nEDGE/key"] = Object{"success": true}
	res, err = r.tailscaleDeviceReconcile(ctx, Object{"name": "an-edge", "key_expiry_disabled": true, "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || !res.Changed {
		t.Fatalf("an-edge apply: %v, %#v", err, res)
	}
	if res.Message != "an-edge now stays on the tailnet." {
		t.Fatalf("unexpected message: %s", res.Message)
	}

	// 4. Missing scope 403 on device key
	h.fail["/device/nEDGE/key"] = runtime.HTTPRefusal(403)
	_, err = r.tailscaleDeviceReconcile(ctx, Object{"name": "an-edge", "key_expiry_disabled": true, "connection_ref": "example-tailnet"}, nil, true)
	if err == nil || !strings.Contains(err.Error(), "devices:core") {
		t.Fatalf("expected devices:core scope error, got: %v", err)
	}
}

func TestTailscaleApproveRoutes(t *testing.T) {
	status := Object{
		"Self": Object{"HostName": "a-router", "ID": "node-1", "Online": true},
	}
	path := writeTempTailnetStatus(t, status)
	t.Setenv("SEVERINO_TAILNET_STATUS", path)

	r := tailscaleFixture()
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ctx := context.Background()
	h := r.HTTP.(*fakeHTTP)

	// 1. Pending routes to approve
	h.routes["/device/node-1/routes"] = Object{
		"advertisedRoutes": []any{"10.0.0.0/24", "0.0.0.0/0"},
		"enabledRoutes":    []any{},
	}
	res, err := r.tailscaleApproveRoutes(ctx, Object{"name": "a-router", "connection_ref": "example-tailnet"}, nil, false)
	if err != nil || !res.Changed {
		t.Fatalf("dry run approve: %v, %#v", err, res)
	}
	if !strings.Contains(res.Message, "Would approve") {
		t.Fatalf("unexpected message: %s", res.Message)
	}

	// Apply
	res, err = r.tailscaleApproveRoutes(ctx, Object{"name": "a-router", "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || !res.Changed {
		t.Fatalf("apply approve: %v, %#v", err, res)
	}
	if !strings.Contains(res.Message, "Approved") {
		t.Fatalf("unexpected message: %s", res.Message)
	}

	// 2. Nothing pending
	h.routes["/device/node-1/routes"] = Object{
		"advertisedRoutes": []any{"10.0.0.0/24"},
		"enabledRoutes":    []any{"10.0.0.0/24"},
	}
	res, err = r.tailscaleApproveRoutes(ctx, Object{"name": "a-router", "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed {
		t.Fatalf("already approved: %v, %#v", err, res)
	}
	if res.Message != "Nothing to approve." {
		t.Fatalf("unexpected message: %s", res.Message)
	}

	// 3. No advertised routes
	h.routes["/device/node-1/routes"] = Object{
		"advertisedRoutes": []any{},
		"enabledRoutes":    []any{},
	}
	res, err = r.tailscaleApproveRoutes(ctx, Object{"name": "a-router", "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed {
		t.Fatalf("no routes: %v, %#v", err, res)
	}
	if res.Message != "a-router advertises no routes." {
		t.Fatalf("unexpected message: %s", res.Message)
	}
}

// policyView is a test policy as the reconciler reads it.
func policyView(policy Object) tailnetPolicyView {
	data, _ := json.Marshal(policy)
	return tailnetPolicyDocument(data).view()
}

func TestRefuseWeakerTests(t *testing.T) {
	live := Object{
		"tests": []any{
			Object{"src": "a-laptop", "accept": []any{"a-server:443"}, "deny": []any{"a-server:22"}},
			Object{"src": "a-phone", "proto": "tcp", "accept": []any{"a-server:443"}},
		},
	}

	// 1. No tests at all
	err := refuseWeakerTests(policyView(live), policyView(Object{"tests": []any{}}))
	if err == nil || !strings.Contains(err.Error(), "no tests") {
		t.Fatalf("expected no tests error, got: %v", err)
	}

	// 2. Dropped deny
	droppedDeny := Object{
		"tests": []any{
			Object{"src": "a-laptop", "accept": []any{"a-server:443"}},
			Object{"src": "a-phone", "proto": "tcp", "accept": []any{"a-server:443"}},
		},
	}
	err = refuseWeakerTests(policyView(live), policyView(droppedDeny))
	if err == nil || !strings.Contains(err.Error(), "no longer tests that 'a-laptop' is denied 'a-server:22'") {
		t.Fatalf("expected dropped deny error, got: %v", err)
	}

	// 3. Dropped (src, proto)
	droppedSrc := Object{
		"tests": []any{
			Object{"src": "a-laptop", "accept": []any{"a-server:443"}, "deny": []any{"a-server:22"}},
		},
	}
	err = refuseWeakerTests(policyView(live), policyView(droppedSrc))
	if err == nil || !strings.Contains(err.Error(), "drops the tests for 'a-phone' over tcp") {
		t.Fatalf("expected dropped pair error, got: %v", err)
	}

	// 4. Retains all checks and adds new ones
	kept := Object{
		"tests": []any{
			Object{"src": "a-laptop", "accept": []any{"a-server:443", "a-server:80"}, "deny": []any{"a-server:22", "a-server:23"}},
			Object{"src": "a-phone", "proto": "tcp", "accept": []any{"a-server:443"}},
		},
	}
	if err := refuseWeakerTests(policyView(live), policyView(kept)); err != nil {
		t.Fatalf("expected valid merge to pass, got: %v", err)
	}
}

func TestTailnetPolicyReconcile(t *testing.T) {
	r := tailscaleFixture()
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ctx := context.Background()
	h := r.HTTP.(*fakeHTTP)

	live := Object{
		"grants": []any{Object{"src": []any{"*"}, "dst": []any{"*"}, "ip": []any{"*"}}},
		"tests":  []any{Object{"src": "a-laptop", "accept": []any{"a-server:443"}}},
	}
	h.routes["/tailnet/-/acl"] = live
	h.routes["/tailnet/-/acl/validate"] = Object{}

	// 1. Empty declaration
	res, err := r.tailnetPolicyReconcile(ctx, Object{"document": "   ", "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed || !strings.Contains(res.Message, "No policy is declared") {
		t.Fatalf("empty declaration: %v, %#v", err, res)
	}

	// 2. Unreadable JSON
	_, err = r.tailnetPolicyReconcile(ctx, Object{"document": "{not-json", "connection_ref": "example-tailnet"}, nil, true)
	if err == nil || !strings.Contains(err.Error(), "not readable JSON") {
		t.Fatalf("expected unreadable JSON error, got: %v", err)
	}

	// 3. Current tested policy
	liveBytes, _ := json.Marshal(live)
	res, err = r.tailnetPolicyReconcile(ctx, Object{"document": string(liveBytes), "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed || res.Message != "Tailnet policy is current." {
		t.Fatalf("current policy: %v, %#v", err, res)
	}
	if len(res.Conditions) != 1 || res.Conditions[0].Reason != "Reconciled" {
		t.Fatalf("conditions: %#v", res.Conditions)
	}

	// 4. Untested policy current
	untested := Object{
		"grants": []any{},
		"tests":  []any{},
	}
	h.routes["/tailnet/-/acl"] = untested
	untestedBytes, _ := json.Marshal(untested)
	res, err = r.tailnetPolicyReconcile(ctx, Object{"document": string(untestedBytes), "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || res.Changed || res.Message != "Tailnet policy is current and untested." {
		t.Fatalf("untested current: %v, %#v", err, res)
	}
	if len(res.Conditions) != 1 || res.Conditions[0].Reason != "Untested" {
		t.Fatalf("conditions: %#v", res.Conditions)
	}

	// 5. Update policy with dry run (apply=false)
	h.routes["/tailnet/-/acl"] = live
	updated := Object{
		"grants": []any{Object{"src": []any{"*"}, "dst": []any{"*"}, "ip": []any{"*"}}},
		"tests":  []any{Object{"src": "a-laptop", "accept": []any{"a-server:443", "a-server:80"}}},
	}
	updatedBytes, _ := json.Marshal(updated)
	res, err = r.tailnetPolicyReconcile(ctx, Object{"document": string(updatedBytes), "connection_ref": "example-tailnet"}, nil, false)
	if err != nil || !res.Changed || res.Message != "The policy passes its own tests and would be applied." {
		t.Fatalf("dry run update: %v, %#v", err, res)
	}

	// 6. Update policy with apply=true
	res, err = r.tailnetPolicyReconcile(ctx, Object{"document": string(updatedBytes), "connection_ref": "example-tailnet"}, nil, true)
	if err != nil || !res.Changed || res.Message != "Tailnet policy applied after its own tests passed." {
		t.Fatalf("apply update: %v, %#v", err, res)
	}
}

func TestTailscaleReadings(t *testing.T) {
	r := tailscaleFixture()
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ctx := context.Background()
	h := r.HTTP.(*fakeHTTP)

	// 1. DNS
	h.routes["/tailnet/-/dns/configuration"] = Object{
		"nameservers": []any{
			Object{"address": "198.51.100.53"},
			"192.0.2.53",
		},
		"splitDNS": Object{
			"corp.example.com": []any{Object{"address": "192.0.2.54"}},
		},
		"searchPaths": []any{"example.com"},
		"preferences": Object{"magicDNS": true, "overrideLocalDNS": true},
	}
	dns, err := r.tailscaleDNS(ctx)
	if err != nil || len(dns) != 1 {
		t.Fatalf("dns: %v, %#v", err, dns)
	}
	if nameservers := dns[0].(TailscaleDNSRecord).Nameservers; !reflect.DeepEqual(nameservers, []string{"198.51.100.53", "192.0.2.53"}) {
		t.Fatalf("nameservers mismatch: %#v", nameservers)
	}

	// 2. Settings with part refusals
	h.routes["/tailnet/-/settings"] = Object{
		"devicesApprovalOn":      true,
		"devicesKeyDurationDays": 90,
	}
	ledger := &refusals{}
	ctxWithLedger := context.WithValue(ctx, refusalKey{}, ledger)
	settings, err := r.tailscaleSettings(ctxWithLedger)
	if err != nil || len(settings) != 1 {
		t.Fatalf("settings: %v, %#v", err, settings)
	}
	if len(ledger.entries) != 2 {
		t.Fatalf("expected 2 refused parts (https, acl_management), got %d: %#v", len(ledger.entries), ledger.entries)
	}

	// 3. Users
	h.routes["/tailnet/-/users"] = Object{
		"users": []any{
			Object{
				"id":          "u1",
				"displayName": "Test User",
				"loginName":   "user@example.com",
				"role":        "owner",
				"status":      "active",
				"created":     "2026-01-01T00:00:00Z",
				"lastSeen":    "2026-09-01T00:00:00Z",
			},
			Object{"displayName": "no id"},
		},
	}
	users, err := r.tailscaleUsers(ctx)
	if err != nil || len(users) != 1 {
		t.Fatalf("users: %v, %#v", err, users)
	}
	if user := users[0].(TailscaleUserRecord); user.ID != "u1" || user.DisplayName != "Test User" {
		t.Fatalf("user record mismatch: %#v", users[0])
	}

	// 4. Probe
	probeFn := r.probes["tailscale"]
	probeRes, err := probeFn(ctx, "example-tailnet")
	if err != nil || probeRes.Detail != "OAuth credential accepted." {
		t.Fatalf("probe: %v, %#v", err, probeRes)
	}
}

func TestTailnetPolicyInventoryAndAppConnectors(t *testing.T) {
	r := tailscaleFixture()
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ctx := context.Background()
	h := r.HTTP.(*fakeHTTP)

	policy := Object{
		"nodeAttrs": []any{
			Object{
				"app": Object{
					"tailscale.com/app-connectors": []any{
						Object{
							"name":       "example-connector",
							"connectors": []any{"tag:server"},
							"domains":    []any{"example.test"},
						},
					},
				},
			},
		},
		"groups": Object{
			"group:eng": []any{"user1", "user2"},
		},
		"tagOwners": Object{
			"tag:server": []any{"group:eng"},
		},
		"grants": []any{
			Object{"src": []any{"group:eng"}, "dst": []any{"tag:server"}, "ip": []any{"*"}},
		},
	}
	h.routes["/tailnet/-/acl"] = policy
	h.routes["/tailnet/-/settings"] = Object{}
	h.routes["/tailnet/-/dns/preferences"] = Object{}
	h.routes["/tailnet/-/dns/nameservers"] = Object{}
	h.routes["/tailnet/-/dns/searchpaths"] = Object{}
	h.routes["/tailnet/-/services"] = Object{
		"vipServices": []any{
			Object{
				"name":    "svc:example",
				"addrs":   []any{"192.0.2.9"},
				"ports":   []any{"tcp:443"},
				"comment": "Example service",
			},
		},
	}

	inv, err := r.tailnetPolicyInventory(ctx)
	if err != nil || len(inv) != 1 {
		t.Fatalf("inventory: %v, %#v", err, inv)
	}
	rec := inv[0].(TailscalePolicyRecord)
	if len(rec.AppConnectors) != 1 || rec.AppConnectors[0].Name != "example-connector" {
		t.Fatalf("connectors mismatch: %#v", rec.AppConnectors)
	}
	if len(rec.Services) != 1 || rec.Services[0].Name != "svc:example" {
		t.Fatalf("services mismatch: %#v", rec.Services)
	}
}
