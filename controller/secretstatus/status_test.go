package secretstatus

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

const healthy = `{"schema_version":1,` +
	`"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"},` +
	`"last_success":{"at":"2026-01-01T00:00:00Z","rendered_at":"2025-12-31T23:00:00Z",` +
	`"content_version":42,"attribute_version":3,` +
	`"counts":{"items_read":20,"connections":9,"app_variables":31,"identities":2,"signing_keys":1}},` +
	`"connect":{"read_at":"2026-01-01T00:00:00Z","version":"1.8.1","dependencies":[{"service":"sync","status":"ACTIVE"}]}}`

func TestDecodeReadsWhatTheRendererWrites(t *testing.T) {
	status, err := Decode([]byte(healthy))
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	content, attribute := 42, 3
	want := Status{
		SchemaVersion: 1,
		LastAttempt:   Attempt{At: at, Outcome: OutcomeCurrent},
		LastSuccess: &Success{At: at, RenderedAt: at.Add(-time.Hour), ContentVersion: &content, AttributeVersion: &attribute,
			Counts: Counts{ItemsRead: 20, Connections: 9, AppVariables: 31, Identities: 2, SigningKeys: 1}},
		Connect: &Connect{ReadAt: at, Version: "1.8.1", Dependencies: []Dependency{{Service: "sync", Status: "ACTIVE"}}},
	}
	if !reflect.DeepEqual(status, want) {
		t.Fatalf("got %+v", status)
	}
	written, err := json.Marshal(status)
	if err != nil || string(written) != healthy {
		t.Fatalf("the document does not survive a round trip: %v\n%s", err, written)
	}
}

func TestDecodeIsStrict(t *testing.T) {
	attempt := func(body string) string { return `{"schema_version":1,"last_attempt":` + body + `}` }
	cases := map[string]string{
		"an unknown member":           `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"},"vault":"x"}`,
		"an unknown nested member":    attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"current","detail":"x"}`),
		"a repeated member":           `{"schema_version":1,"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"}}`,
		"another schema version":      `{"schema_version":2,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"}}`,
		"no schema version":           `{"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"}}`,
		"a second value":              attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"current"}`) + `{}`,
		"an outcome of its own":       attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"fine"}`),
		"a failure in a sentence":     attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"failed","failure":"Connect said no"}`),
		"a failed run with no class":  attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"failed"}`),
		"a failure on a run that did": attempt(`{"at":"2026-01-01T00:00:00Z","outcome":"current","failure":"config"}`),
		"a time that is not one":      attempt(`{"at":"yesterday","outcome":"current"}`),
		"a dependency in a sentence": `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"},` +
			`"connect":{"read_at":"2026-01-01T00:00:00Z","version":"1.8.1","dependencies":[{"service":"sync","status":"token needed"}]}}`,
		"more dependencies than a document names": `{"schema_version":1,"last_attempt":{"at":"2026-01-01T00:00:00Z","outcome":"current"},` +
			`"connect":{"read_at":"2026-01-01T00:00:00Z","version":"1.8.1","dependencies":[` +
			strings.TrimSuffix(strings.Repeat(`{"service":"sync","status":"ACTIVE"},`, MaxDependencies+1), ",") + `]}}`,
		"more bytes than a document holds": healthy + strings.Repeat(" ", MaxBytes),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Decode([]byte(body)); !errors.Is(err, ErrInvalid) {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestWordKeepsOnlyShortWords(t *testing.T) {
	for value, want := range map[string]string{
		"ACTIVE": "ACTIVE", "1.8.1": "1.8.1", "TOKEN_NEEDED": "TOKEN_NEEDED",
		"": Unreadable, "two words": Unreadable, "line\nbreak": Unreadable, strings.Repeat("a", 65): Unreadable,
	} {
		if got := Word(&value); got != want {
			t.Errorf("Word(%q) = %q", value, got)
		}
	}
	if Word(nil) != Unreadable {
		t.Error("an absent word is unreadable")
	}
}
