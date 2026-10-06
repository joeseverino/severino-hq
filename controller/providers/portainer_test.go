package providers

import (
	"errors"
	"slices"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

func TestIsIPAddress(t *testing.T) {
	for value, want := range map[string]bool{"192.0.2.1": true, "[::1]:8000": true, "192.0.2.1:9001": true, "::1": true, "example.invalid": false, "": false} {
		if isIPAddress(value) != want {
			t.Errorf("isIPAddress(%q) = %v", value, !want)
		}
	}
}

func TestUnixStamp(t *testing.T) {
	for seconds, want := range map[int64]string{1767355200: "2026-01-02T12:00:00Z", 0: "", -5: ""} {
		if got := unixStamp(seconds); got != want {
			t.Errorf("unixStamp(%d) = %q, want %q", seconds, got, want)
		}
	}
}

func TestAnAddressResolvesNamesAndKeepsAddresses(t *testing.T) {
	r := New(runtime.Environment{}, supplied(), &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
	r.Resolve = func(host string) (string, error) {
		if host == "named.example" {
			return "192.0.2.7", nil
		}
		return "", errors.New("does not resolve")
	}
	for host, want := range map[string]string{"named.example": "192.0.2.7", "other.example": "other.example", "192.0.2.1": "192.0.2.1"} {
		if got := r.anAddress(host); got != want {
			t.Errorf("anAddress(%q) = %q, want %q", host, got, want)
		}
	}
}

func TestPortainerCoverage(t *testing.T) {
	coverage := New(runtime.Environment{}, supplied(), &fakeHTTP{}).Coverage()
	want := map[string][]string{
		"actions": {"portainer.container:restart", "portainer.container:start", "portainer.container:stop", "portainer.stack:delete", "portainer.stack:reconcile"},
		"readers": {"portainer.compose_project", "portainer.container", "portainer.environment", "portainer.image", "portainer.network", "portainer.runtime", "portainer.volume"},
		"probes":  {"portainer"},
	}
	have := map[string][]string{"actions": coverage.Actions, "readers": coverage.Readers, "probes": coverage.Probes}
	for group, names := range want {
		for _, name := range names {
			if !slices.Contains(have[group], name) {
				t.Errorf("coverage %s is missing %s", group, name)
			}
		}
	}
}
