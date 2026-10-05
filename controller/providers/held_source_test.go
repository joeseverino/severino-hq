package providers

import (
	"os"
	"regexp"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A kind read from a file this controller was given has a source when the
// file was mounted, and when a credentialed connection of its provider exists.
func TestAHeldReadingIsASourceBesideAConnection(t *testing.T) {
	declared := runtime.ControllerRegistry{
		Observations: map[string]string{
			string(runtime.ResourceKindTailscaleDevice): "tailscale",
			string(runtime.ResourceKindHostFirewall):    "host",
		},
		ConnectionCredentials: []runtime.ConnectionProvider{"tailscale"},
	}
	cases := []struct {
		kind      runtime.ResourceKind
		env       runtime.Environment
		connected map[runtime.ConnectionProvider]bool
		want      bool
	}{
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{}, nil, false},
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{"SEVERINO_TAILNET_STATUS": "/run/status.json"}, nil, true},
		{runtime.ResourceKindTailscaleDevice, runtime.Environment{}, map[runtime.ConnectionProvider]bool{"tailscale": true}, true},
		{runtime.ResourceKindHostFirewall, runtime.Environment{}, map[runtime.ConnectionProvider]bool{"tailscale": true}, false},
		{runtime.ResourceKindHostFirewall, runtime.Environment{"SEVERINO_HOST_FIREWALL": "/run/firewall.json"}, nil, true},
	}
	for _, c := range cases {
		controller := NewController(New(c.env, &fakeHTTP{}), declared)
		if got := controller.hasSource(string(c.kind), c.connected); got != c.want {
			t.Errorf("%s with %v and %v: source %v, want %v", c.kind, c.env, c.connected, got, c.want)
		}
	}
}

// A kind only ever read from what is held ignores every connection.
func TestAnOnlyHeldReadingIgnoresConnections(t *testing.T) {
	kind := string(runtime.ResourceKindGitHubProfile)
	declared := runtime.ControllerRegistry{
		Observations:          map[string]string{kind: "github"},
		ConnectionCredentials: []runtime.ConnectionProvider{"github"},
	}
	connected := map[runtime.ConnectionProvider]bool{"github": true}
	if NewController(New(runtime.Environment{}, &fakeHTTP{}), declared).hasSource(kind, connected) {
		t.Error("a GitHub connection is not a profile's source")
	}
}

func TestAHeldSourceIsDeclaredOncePerKind(t *testing.T) {
	r := New(runtime.Environment{}, &fakeHTTP{})
	defer func() {
		if recover() == nil {
			t.Error("a second held source for one kind was accepted")
		}
	}()
	r.readsHeld(runtime.ResourceKindHostFirewall, func() bool { return true })
}

// The dispatcher asks what each provider declared beside its reader; it names
// no kind of its own.
func TestTheDispatcherNamesNoKind(t *testing.T) {
	source, err := os.ReadFile("controller.go")
	if err != nil {
		t.Fatal(err)
	}
	if named := regexp.MustCompile(`runtime\.ResourceKind[A-Z]\w+`).FindAll(source, -1); len(named) > 0 {
		t.Errorf("controller.go names kinds: %s", named)
	}
}
