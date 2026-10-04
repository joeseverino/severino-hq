package api

import (
	"strings"
	"testing"
)

func TestKeywordReadsWhatTheContractStates(t *testing.T) {
	workflow, err := Keyword("GitHubDeliverySpec", "properties", "workflow", "default")
	if err != nil || !strings.HasPrefix(workflow, ".github/workflows/") {
		t.Fatalf("workflow default = %q, %v", workflow, err)
	}
	if !MustPattern("GitHubDeliverySpec", "properties", "workflow").MatchString(workflow) {
		t.Errorf("the default workflow %q does not match its own pattern", workflow)
	}
}

// A keyword the contract does not state is an error, so nothing derived from
// it can pass by being empty.
func TestKeywordFailsClosed(t *testing.T) {
	for name, path := range map[string][]string{
		"no such schema":   {"NoSuchSchema", "pattern"},
		"no such keyword":  {"CaddyRouteInFile", "properties", "domain", "default"},
		"not a string":     {"CaddyRouteInFile", "properties", "domain", "maxLength"},
		"not an object":    {"CaddyRouteInFile", "type", "pattern"},
		"an object itself": {"CaddyRouteInFile", "properties"},
	} {
		t.Run(name, func(t *testing.T) {
			if value, err := Keyword(path[0], path[1:]...); err == nil {
				t.Errorf("Keyword(%v) = %q, want an error", path, value)
			}
			defer func() {
				if recover() == nil {
					t.Errorf("MustKeyword(%v) did not panic", path)
				}
			}()
			MustKeyword(path[0], path[1:]...)
		})
	}
	if limit, err := Limit("CaddyRouteInFile", "properties", "domain", "pattern"); err == nil {
		t.Errorf("a pattern read as the limit %d", limit)
	}
	if limit := MustLimit("AnalyticsRow", "properties", "value", "maxLength"); limit <= 0 {
		t.Errorf("limit = %d", limit)
	}
}
