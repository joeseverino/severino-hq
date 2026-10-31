package providers

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/host/unit"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// serving answers one connection the way hq-host unit --serve does, with
// the result given, and keeps the request line it read.
func serving(t *testing.T, result unit.Result) (func(context.Context, string) (net.Conn, error), *string) {
	t.Helper()
	read := new(string)
	return func(context.Context, string) (net.Conn, error) {
		client, server := net.Pipe()
		go func() {
			defer server.Close()
			line, _ := bufio.NewReader(server).ReadString('\n')
			*read = line
			_ = json.NewEncoder(server).Encode(result)
		}()
		return client, nil
	}, read
}

func TestTheUnitSocketSendsOneRequestLineAndReadsItsResult(t *testing.T) {
	dial, read := serving(t, unit.Result{Outcome: unit.Done, Verb: "stop", Unit: "web.service"})
	socket := unitSocket{path: "/run/example.sock", machine: "hq-node", dial: dial}
	if err := socket.Act(t.Context(), "hq-node", "stop", "web.service"); err != nil {
		t.Fatal(err)
	}
	if *read != "stop web.service\n" {
		t.Errorf("sent %q", *read)
	}
}

func TestARefusedUnitRequestIsAnError(t *testing.T) {
	dial, _ := serving(t, unit.Result{Outcome: unit.Refused, Reason: "web.service does not start web"})
	socket := unitSocket{path: "/run/example.sock", machine: "hq-node", dial: dial}
	err := socket.Act(t.Context(), "hq-node", "stop", "web.service")
	if err == nil || !strings.Contains(err.Error(), "does not start web") {
		t.Fatalf("got %v", err)
	}
}

func TestTheUnitSocketReachesOnlyTheControllersMachine(t *testing.T) {
	r := New(runtime.Environment{UnitSocket: "/run/example.sock"}, supplied(), &fakeHTTP{})
	r.ControllerID = "hq-node"
	control := r.unitControl()
	if !control.Reaches("hq-node") || control.Reaches("example-edge") || control.Reaches("") {
		t.Fatal("the socket reaches another machine, or not its own")
	}
	if New(runtime.Environment{}, supplied(), &fakeHTTP{}).Units != nil {
		t.Fatal("a run given no socket reaches units")
	}
}
