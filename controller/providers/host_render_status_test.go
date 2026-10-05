package providers

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
	"github.com/joeseverino/severino-hq/controller/secretstatus"
)

const renderStatusFailed = `{"schema_version":1,` +
	`"last_attempt":{"at":"2026-01-02T00:00:00Z","outcome":"failed","failure":"connect_denied"},` +
	`"last_success":{"at":"2026-01-01T00:00:00Z","rendered_at":"2025-12-31T23:00:00Z","content_version":42,` +
	`"counts":{"items_read":20,"connections":9,"app_variables":31,"identities":2,"signing_keys":1}},` +
	`"connect":{"read_at":"2026-01-02T00:00:00Z","version":"1.8.1","dependencies":[{"service":"sync","status":"TOKEN_NEEDED"}]}}`

func writeRenderStatus(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "status.json")
	if err := os.WriteFile(path, []byte(body), 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

func renderStatus(t *testing.T, configured string) []any {
	t.Helper()
	records, err := New(runtime.Environment{renderStatusEnv: configured}, &fakeHTTP{}).hostRenderStatus(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	return records
}

func TestHostRenderStatusReadingIsTheRenderersOwnDocument(t *testing.T) {
	records := renderStatus(t, "example="+writeRenderStatus(t, renderStatusFailed))

	attempt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	success := attempt.Add(-24 * time.Hour)
	content := 42
	want := HostRenderStatusRecord{Renderer: "example", State: "read", Status: &secretstatus.Status{
		SchemaVersion: 1,
		LastAttempt:   secretstatus.Attempt{At: attempt, Outcome: "failed", Failure: "connect_denied"},
		LastSuccess: &secretstatus.Success{At: success, RenderedAt: success.Add(-time.Hour), ContentVersion: &content,
			Counts: secretstatus.Counts{ItemsRead: 20, Connections: 9, AppVariables: 31, Identities: 2, SigningKeys: 1}},
		Connect: &secretstatus.Connect{ReadAt: attempt, Version: "1.8.1",
			Dependencies: []secretstatus.Dependency{{Service: "sync", Status: "TOKEN_NEEDED"}}},
	}}
	if !reflect.DeepEqual(records, []any{want}) {
		t.Fatalf("got %+v", records)
	}
	// What HQ receives: the renderer's name, the read's state and the document.
	sent, err := json.Marshal(records[0])
	if err != nil || string(sent) != `{"renderer":"example","state":"read","status":`+renderStatusFailed+`}` {
		t.Fatalf("%v\n%s", err, sent)
	}
}

func TestHostRenderStatusReportsADocumentItCannotReadAsARecord(t *testing.T) {
	directory := t.TempDir()
	cases := []struct {
		name, path, reason string
	}{
		{"no document", filepath.Join(directory, "absent.json"), "missing"},
		{"a directory where the document is", directory, "unreadable"},
		{"a member the type does not name", writeRenderStatus(t, `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"},"vault":"example"}`), "invalid"},
		{"another schema version", writeRenderStatus(t, `{"schema_version":2,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"}}`), "invalid"},
		{"a sentence where a word belongs", writeRenderStatus(t, `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"failed","failure":"it broke badly"}}`), "invalid"},
		{"not JSON", writeRenderStatus(t, "status: fine\n"), "invalid"},
		{"more than a status document holds", writeRenderStatus(t, strings.Repeat(" ", secretstatus.MaxBytes+1)), "oversized"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			want := HostRenderStatusRecord{Renderer: "example", State: "unreadable", Reason: c.reason}
			if records := renderStatus(t, "example="+c.path); !reflect.DeepEqual(records, []any{want}) {
				t.Fatalf("got %+v", records)
			}
		})
	}
	sent, err := json.Marshal(HostRenderStatusRecord{Renderer: "example", State: "unreadable", Reason: "missing"})
	if err != nil || string(sent) != `{"renderer":"example","state":"unreadable","reason":"missing"}` {
		t.Fatalf("%v\n%s", err, sent)
	}
}

func TestHostRenderStatusReadsEveryConfiguredDocument(t *testing.T) {
	healthy := writeRenderStatus(t, `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"rendered"}}`)
	absent := filepath.Join(t.TempDir(), "absent.json")

	records := renderStatus(t, "example="+healthy+",other-apps="+absent)

	if len(records) != 2 {
		t.Fatalf("got %+v", records)
	}
	first, second := records[0].(HostRenderStatusRecord), records[1].(HostRenderStatusRecord)
	if first.Renderer != "example" || first.State != "read" || first.Status.LastAttempt.Outcome != "rendered" {
		t.Errorf("first: %+v", first)
	}
	if second != (HostRenderStatusRecord{Renderer: "other-apps", State: "unreadable", Reason: "missing"}) {
		t.Errorf("second: %+v", second)
	}
}

func TestHostRenderStatusRefusesAListItCannotName(t *testing.T) {
	path := writeRenderStatus(t, `{}`)
	for name, configured := range map[string]string{
		"nothing configured": "",
		"a path alone":       path,
		"a name alone":       "example=",
		"a path as the name": "/run/example=" + path,
		"a name twice":       "example=" + path + ",example=" + path,
		"an empty entry":     "example=" + path + ",",
	} {
		t.Run(name, func(t *testing.T) {
			records, err := New(runtime.Environment{renderStatusEnv: configured}, &fakeHTTP{}).hostRenderStatus(t.Context())
			if err == nil || records != nil {
				t.Fatalf("got %v %v", records, err)
			}
			if strings.Contains(err.Error(), path) {
				t.Fatalf("the refusal names a path: %v", err)
			}
		})
	}
}

func TestHostRenderStatusIsReadOnlyWhereTheLauncherNamesADocument(t *testing.T) {
	kind := runtime.ResourceKindHostRenderStatus
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	absent := filepath.Join(t.TempDir(), "absent.json")
	for configured, want := range map[string]bool{"": false, "example=" + absent: true} {
		controller := &Controller{Registry: New(runtime.Environment{renderStatusEnv: configured}, &fakeHTTP{}), Declared: declared}
		inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
		if err != nil {
			t.Fatal(err)
		}
		report := inventory[string(kind)]
		if connected := report.Connected == nil || *report.Connected; connected != want || !report.OK {
			t.Errorf("configured %q: %+v", configured, report)
		}
		if want && len(report.Records) != 1 {
			t.Errorf("a named document that is missing is one record: %+v", report.Records)
		}
	}
}
