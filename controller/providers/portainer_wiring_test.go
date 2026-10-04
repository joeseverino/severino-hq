package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

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
	if err != nil || len(environments) != 1 || environments[0].ID != 1 || !environments[0].Reachable || !environments[0].Local {
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

// inspectCounting answers every container inspect after a short wait and
// records how many were in flight at once.
type inspectCounting struct {
	*fakeHTTP
	inflight, peak atomic.Int32
	failOn         string
}

func (h *inspectCounting) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	if !strings.HasSuffix(address, "/json") || strings.Contains(address, "containers/json") {
		return h.fakeHTTP.Request(ctx, address, method, headers, payload)
	}
	now := h.inflight.Add(1)
	defer h.inflight.Add(-1)
	for peak := h.peak.Load(); now > peak && !h.peak.CompareAndSwap(peak, now); peak = h.peak.Load() {
	}
	time.Sleep(10 * time.Millisecond)
	if strings.Contains(address, h.failOn) && h.failOn != "" {
		return nil, &ProviderError{Message: "inspect failed"}
	}
	return json.Marshal(map[string]any{"Image": "sha256:" + address})
}

func inspectFixture(count int, failOn string) (*Registry, *inspectCounting) {
	containers := []any{}
	for i := 0; i < count; i++ {
		containers = append(containers, map[string]any{"Id": fmt.Sprintf("c%02d", i), "Names": []any{fmt.Sprintf("/n%02d", i)}})
	}
	h := &inspectCounting{fakeHTTP: &fakeHTTP{routes: map[string]any{
		"/api/endpoints/1/docker/containers/json?all=1": containers,
	}, fail: map[string]error{}}, failOn: failOn}
	env := runtime.Environment{"PORTAINER_CONNECTION_REF": "portainer-example", "PORTAINER_URL": "https://example.invalid", "PORTAINER_API_TOKEN": "synthetic"}
	return New(env, h), h
}

func TestPortainerInspectsRunConcurrentlyInOrder(t *testing.T) {
	r, h := inspectFixture(40, "")
	site := portainerSite{ConnectionRef: "portainer-example", EnvironmentID: 1}
	found, err := r.portainerRuntime(context.Background(), site)
	if err != nil || len(found) != 40 {
		t.Fatalf("%d records, %v", len(found), err)
	}
	for i, record := range found {
		if want := fmt.Sprintf("n%02d", i); record.(PortainerRuntimeRecord).Container != want {
			t.Fatalf("record %d is %s, want %s", i, record.(PortainerRuntimeRecord).Container, want)
		}
	}
	if peak := h.peak.Load(); peak < 2 || peak > portainerInspectLimit {
		t.Fatalf("peak in flight %d, want 2..%d", peak, portainerInspectLimit)
	}
}

func TestPortainerInspectFailureKeepsTheRecordsBeforeIt(t *testing.T) {
	r, _ := inspectFixture(40, "/containers/c05/json")
	site := portainerSite{ConnectionRef: "portainer-example", EnvironmentID: 1}
	found, err := r.portainerRuntime(context.Background(), site)
	if err == nil || len(found) != 5 {
		t.Fatalf("%d records, %v; want the 5 before c05 and its error", len(found), err)
	}
}
