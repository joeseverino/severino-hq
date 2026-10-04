package runtime

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

const contractFile = "../api/hq-controller.openapi.json"

// The bridge actions the worker calls. Each must be a path in the contract.
var workerActions = []string{"claim", "peek", "material", "report", "glance-plan", "glance", "sweep-due", "connections", "inventory", "analytics-plan", "analytics", "steps", "schedule"}

func contractDoc(t *testing.T) map[string]any {
	t.Helper()
	data, err := os.ReadFile(contractFile)
	if err != nil {
		t.Fatalf("read contract: %v", err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(data))
	if err != nil {
		t.Fatalf("parse contract: %v", err)
	}
	return doc.(map[string]any)
}

func contractSchema(t *testing.T, name string) *jsonschema.Schema {
	t.Helper()
	c := jsonschema.NewCompiler()
	c.DefaultDraft(jsonschema.Draft2020)
	if err := c.AddResource(contractFile, contractDoc(t)); err != nil {
		t.Fatalf("load contract: %v", err)
	}
	schema, err := c.Compile(contractFile + "#/components/schemas/" + name)
	if err != nil {
		t.Fatalf("compile %s: %v", name, err)
	}
	return schema
}

// conforms validates raw JSON against a component schema.
func conforms(t *testing.T, name string, raw []byte) {
	t.Helper()
	value, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("%s: invalid JSON: %v", name, err)
	}
	if err := contractSchema(t, name).Validate(value); err != nil {
		t.Fatalf("%s does not match the contract: %v\n%s", name, err, raw)
	}
}

// strict decodes Django's output into the generated type, refusing unknown fields.
func strict[T any](t *testing.T, raw string) T {
	t.Helper()
	var value T
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		t.Fatalf("decode %T: %v", value, err)
	}
	return value
}

func marshal(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal %T: %v", value, err)
	}
	return data
}

func TestContractCoversEveryWorkerAction(t *testing.T) {
	paths, _ := contractDoc(t)["paths"].(map[string]any)
	for _, action := range workerActions {
		if _, ok := paths["/"+action]; !ok {
			t.Errorf("contract has no path for bridge action %q", action)
		}
	}
}

// Responses: fixtures are shaped exactly as the Django command prints them.

func TestPendingResponses(t *testing.T) {
	none := `{"ok": true, "operation": null}`
	conforms(t, "Pending", []byte(none))
	if strict[Pending](t, none).Operation != nil {
		t.Fatal("an empty queue must decode to a nil operation")
	}
	claimed := `{"ok": true, "operation": {"action": "reconcile", "attempt_count": 1, "claimed_by": "controller-1", "completed_at": null, "created_at": "2026-10-03T12:00:00.123456+00:00", "id": "7", "lease_expires_at": "2026-10-03T12:05:00+00:00", "reason": "", "requested_actor": "operator", "requested_interface": "web", "resource": "dns-a", "result": {}, "state": "claimed"}, "resource": {"enabled": true, "generation": 3, "key": "dns-a", "kind": "adguard.rewrite", "observed": {}, "spec": {"answer": "192.0.2.10", "domain": "a.example"}}, "schema_version": 1}`
	conforms(t, "Pending", []byte(claimed))
	pending := strict[Pending](t, claimed)
	if pending.Operation == nil || pending.Operation.ID != "7" || pending.Operation.CompletedAt != nil || pending.Resource.Generation != 3 {
		t.Fatalf("claimed operation decoded wrong: %+v", pending)
	}
	unresolvable := `{"ok": true, "operation": {"action": "reconcile", "attempt_count": 0, "claimed_by": "", "completed_at": null, "created_at": "2026-10-03T12:00:00+00:00", "id": "8", "lease_expires_at": null, "reason": "", "requested_actor": "operator", "requested_interface": "web", "resource": "dns-b", "result": null, "state": "queued"}, "unresolvable": "Missing delivery target."}`
	conforms(t, "Pending", []byte(unresolvable))
}

func TestGlancePlanResponse(t *testing.T) {
	raw := `{"ok": true, "panels": ["infrastructure"], "targets": {"infrastructure": [{"connections": ["ssh-host"], "key": "host-1", "request_id": "machine-1"}], "weather": {"point": ""}}}`
	conforms(t, "GlancePlan", []byte(raw))
	plan := strict[GlancePlan](t, raw)
	if len(plan.Targets.Infrastructure) != 1 || plan.Targets.Infrastructure[0].RequestID != "machine-1" {
		t.Fatalf("glance plan decoded wrong: %+v", plan)
	}
}

func TestSweepVerdictResponses(t *testing.T) {
	first := `{"carry": [], "due": true, "forced": [], "interval_seconds": 900, "ok": true, "only_kinds": [], "reason": "Nothing has been swept yet."}`
	conforms(t, "SweepVerdict", []byte(first))
	asked := `{"age_seconds": 120, "carry": ["ssh-host"], "due": true, "forced": [{"connection_ref": "", "kind": "adguard.rewrite", "kinds": null, "requested_at": "2026-10-03T12:00:00+00:00"}], "interval_seconds": 900, "ok": true, "only_kinds": [], "reason": "The kind tried longest ago was tried 2 minutes ago; not due every 15 minutes. Read now asked for adguard.rewrite."}`
	conforms(t, "SweepVerdict", []byte(asked))
	verdict := strict[SweepVerdict](t, asked)
	if !verdict.Due || len(verdict.Forced) != 1 || verdict.Forced[0].Kinds != nil {
		t.Fatalf("sweep verdict decoded wrong: %+v", verdict)
	}
}

func TestAnalyticsPlanResponse(t *testing.T) {
	raw := `{"ok": true, "windows": [{"connection_ref": "cf", "end": "2026-10-02", "reason": "backfill", "site_tag": "abc", "start": "2026-07-05"}]}`
	conforms(t, "AnalyticsPlan", []byte(raw))
	if strict[AnalyticsPlan](t, raw).Windows[0].Reason != Backfill {
		t.Fatal("analytics window reason decoded wrong")
	}
}

func TestMaterialResponse(t *testing.T) {
	raw := `{"domains": ["a.example"], "fullchain": "-----BEGIN CERTIFICATE-----", "private_key": "-----BEGIN PRIVATE KEY-----"}`
	conforms(t, "Material", []byte(raw))
	strict[Material](t, raw)
}

// Payloads: what the controller sends must match what Django accepts.

func TestControllerReportPayload(t *testing.T) {
	report := ControllerReport{Success: true, ObservedGeneration: 3, Status: Object{"domain": "a.example"}, Conditions: []Condition{{Type: "Ready", Status: true, Reason: "Reconciled", Message: "current"}}, Message: "unchanged"}
	conforms(t, "ControllerReport", marshal(t, report))
}

func TestConnectionsPayload(t *testing.T) {
	record := ConnectionRecord{ConnectionRef: "adguard", Provider: "adguard", Endpoint: "https://dns.example", Manages: true, Probed: true, OK: true, Detail: "AdGuard v0.107", Reaches: []string{}}
	conforms(t, "ConnectionRecord", marshal(t, record))
}

func TestStepsPayload(t *testing.T) {
	conforms(t, "StepFailure", marshal(t, StepFailure{Step: "adguard.rewrite:reconcile", Subject: "adguard", Reason: "refused"}))
}

func TestInventoryPayload(t *testing.T) {
	inventory := Inventory{"adguard.rewrite": {OK: true, Records: []any{map[string]any{"connection_ref": "adguard", "domain": "a.example", "answer": "192.0.2.10", "enabled": true}}, RefusedParts: []RefusedPart{{Part: "querylog", ConnectionRef: "adguard", Refusal: "permission"}}}}
	conforms(t, "Inventory", marshal(t, inventory))
}

func TestContractRejectsDrift(t *testing.T) {
	schema := contractSchema(t, "StepFailure")
	value, _ := jsonschema.UnmarshalJSON(strings.NewReader(`{"step": "x", "subject": "y", "reason": "z", "message": "renamed field"}`))
	if schema.Validate(value) == nil {
		t.Fatal("an unknown field must fail the contract")
	}
	value, _ = jsonschema.UnmarshalJSON(strings.NewReader(`{"step": "x", "reason": "z"}`))
	if schema.Validate(value) == nil {
		t.Fatal("a missing required field must fail the contract")
	}
}
