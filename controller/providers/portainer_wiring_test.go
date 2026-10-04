package providers

import (
	"context"
	"reflect"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// New wires the glance and both published-port readers to the Portainer
// provider, so all three see the same environments and containers.
func TestPortainerFeedsGlanceAndPublishedPorts(t *testing.T) {
	ctx := context.Background()
	env := runtime.Environment{
		"PORTAINER_CONNECTION_REF": "portainer-example",
		"PORTAINER_URL":            "https://example.invalid",
		"PORTAINER_API_TOKEN":      "synthetic",
		"HQ_CONTROLLER_ID":         "hq-node",
	}
	routes := map[string]any{
		"/api/endpoints": []any{
			map[string]any{"Id": 1, "Name": "local", "URL": "unix:///var/run/docker.sock", "Type": 1, "Status": 1},
		},
		"/api/stacks": []any{},
		"/api/endpoints/1/docker/containers/json?all=1": []any{
			map[string]any{
				"Id": "abcdef0123456789", "Names": []any{"/web-app-1"}, "Image": "web:1", "State": "running",
				"Ports": []any{map[string]any{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}},
			},
		},
	}
	r := New(env, &fakeHTTP{routes: routes, fail: map[string]error{}})
	if r.Portainer == nil || r.PublishedContainers == nil {
		t.Fatal("New left the Portainer hooks unwired")
	}

	environments, err := r.Portainer.Environments(ctx, "portainer-example")
	if err != nil || len(environments) != 1 || environments[0].ID.str() != "1" || !environments[0].Reachable || !environments[0].Local {
		t.Fatalf("glance environments = %+v, %v", environments, err)
	}

	containers, err := r.PublishedContainers(ctx)
	if err != nil || len(containers) != 1 || !reflect.DeepEqual(containers[0].Ports, []int{8080}) {
		t.Fatalf("published containers = %+v, %v", containers, err)
	}
	host := containers[0].Host
	if got := sortedPorts(r.publishedPortsAt(ctx, "portainer-example", map[string]bool{host: true})); !reflect.DeepEqual(got, []int{8080}) {
		t.Fatalf("host readings ports = %v", got)
	}
	if got := r.portsWorthAsking(ctx); !reflect.DeepEqual(got, []int{22, 53, 80, 443, 8080}) {
		t.Fatalf("tailnet reach ports = %v", got)
	}
}
