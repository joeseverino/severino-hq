package providers

import (
	"errors"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

func mustPy(t *testing.T, raw string) pyValue {
	t.Helper()
	value, err := parsePy([]byte(raw))
	if err != nil {
		t.Fatalf("parse %s: %v", raw, err)
	}
	return value
}

func TestPyValueFollowsPython(t *testing.T) {
	for _, tc := range []struct{ a, b string }{{"1", "1.0"}, {"true", "1"}, {"false", "0"}, {`[1, "x"]`, `[1.0, "x"]`}, {`{"a": 1}`, `{"a": true}`}} {
		if !mustPy(t, tc.a).eq(mustPy(t, tc.b)) {
			t.Errorf("%s == %s should hold", tc.a, tc.b)
		}
	}
	for _, tc := range []struct{ a, b string }{{"1", `"1"`}, {"null", "0"}, {`[1]`, `[1, 1]`}} {
		if mustPy(t, tc.a).eq(mustPy(t, tc.b)) {
			t.Errorf("%s == %s should not hold", tc.a, tc.b)
		}
	}
	for raw, want := range map[string]string{`"x"`: "x", "3": "3", "3.0": "3.0", "true": "True", "null": "None", `["a", 1]`: "['a', 1]", `{"k": null}`: "{'k': None}"} {
		if got := mustPy(t, raw).str(); got != want {
			t.Errorf("str(%s) = %q, want %q", raw, got, want)
		}
	}
	if (pyValue{}).str() != "None" || (pyValue{}).truthy() {
		t.Error("an absent value is None and falsy")
	}
	for raw, want := range map[string]int64{"7": 7, "1.9": 1, "-1.5": -1, `" 12 "`: 12, "true": 1} {
		if got, ok := mustPy(t, raw).toInt(); !ok || got != want {
			t.Errorf("int(%s) = %d, %v; want %d", raw, got, ok, want)
		}
	}
	for _, raw := range []string{`"1.5"`, `"x"`, "null", "[]"} {
		if _, ok := mustPy(t, raw).toInt(); ok {
			t.Errorf("int(%s) should raise", raw)
		}
	}
	if !mustPy(t, "true").isInt() || mustPy(t, "1.0").isInt() {
		t.Error("isinstance(int) counts bools and not floats")
	}
}

func TestPortainerHelpers(t *testing.T) {
	if roundTwo(0.125) != 0.12 || roundTwo(1.5) != 1.5 || roundTwo(2.675) != 2.67 {
		t.Error("roundTwo should match Python's round(x, 2)")
	}
	for value, want := range map[string]bool{"192.0.2.1": true, "[::1]:8000": true, "192.0.2.1:9001": true, "::1": true, "example.invalid": false, "": false} {
		if pyParseIP(value) != want {
			t.Errorf("parse_ip(%q) = %v", value, !want)
		}
	}
	if pyStamp(mustPy(t, "1767355200")) != "2026-01-02T12:00:00+00:00" || pyStamp(mustPy(t, `"x"`)) != "" || pyStamp(mustPy(t, "0")) != "" {
		t.Error("pyStamp should be ISO 8601 in UTC, empty for none")
	}
	r := New(runtime.Environment{}, &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
	r.Resolve = func(host string) (string, error) {
		if host == "named.example" {
			return "192.0.2.7", nil
		}
		return "", errors.New("does not resolve")
	}
	if r.anAddress("named.example") != "192.0.2.7" || r.anAddress("other.example") != "other.example" || r.anAddress("192.0.2.1") != "192.0.2.1" {
		t.Error("anAddress resolves names, keeps addresses, and falls back to the name")
	}
}

func TestPortainerCoverage(t *testing.T) {
	coverage := New(runtime.Environment{}, &fakeHTTP{}).Coverage()
	want := map[string][]string{
		"actions": {"portainer.container:restart", "portainer.container:start", "portainer.container:stop", "portainer.stack:delete", "portainer.stack:reconcile"},
		"readers": {"portainer.compose_project", "portainer.container", "portainer.environment", "portainer.image", "portainer.network", "portainer.runtime", "portainer.volume"},
		"probes":  {"portainer"},
	}
	have := map[string][]string{"actions": coverage.Actions, "readers": coverage.Readers, "probes": coverage.Probes}
	for group, names := range want {
		for _, name := range names {
			if indexOf(have[group], name) < 0 {
				t.Errorf("coverage %s is missing %s", group, name)
			}
		}
	}
}
