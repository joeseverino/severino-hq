package providers

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/edge"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

func rebootInventory(t *testing.T, path string) runtime.KindReport {
	t.Helper()
	kind := runtime.ResourceKindHostReboot
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	controller := &Controller{Registry: New(runtime.Environment{HostReboot: path}, supplied(), &fakeHTTP{}), Declared: declared}
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind})
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)]
}

func writeReboot(t *testing.T, body []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "reboot.json")
	if err := os.WriteFile(path, body, 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

// What root reads on the host is what HQ is sent: the launcher's own document,
// from a flag, its package list and logind's answer, read back as the record.
func TestTheRestartTheLauncherReadIsTheRecord(t *testing.T) {
	flag := filepath.Join(t.TempDir(), "reboot-required")
	if err := os.WriteFile(flag, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	// A name dpkg would never write, between two it would.
	if err := os.WriteFile(flag+".pkgs", []byte("linux-image-example\nNot|A package\nlibc6\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	born := func(string) (time.Time, error) { return time.Date(2029, 12, 31, 18, 0, 0, 0, time.UTC), nil }
	scheduled := []byte(`{"type":"(st)","data":["reboot",1893466800000000]}`)
	document, err := hostcore.ReadReboot(flag, born, scheduled, time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}

	report := rebootInventory(t, writeReboot(t, document))

	if !report.OK {
		t.Fatalf("%+v", report)
	}
	want := []any{runtime.HostRebootRecord{
		Pending: true, Since: "2029-12-31T18:00:00Z", Packages: []string{"linux-image-example", "libc6"},
		ScheduledAction: "reboot", ScheduledAt: "2030-01-01T03:00:00Z", ReadAt: "2030-01-01T00:00:00Z",
	}}
	if !reflect.DeepEqual(report.Records, want) {
		t.Errorf("records %+v, want %+v", report.Records, want)
	}
}

func TestThePackageListStopsAtTheContractsCount(t *testing.T) {
	var names []string
	for i := range hostRebootPackages + 5 {
		names = append(names, fmt.Sprintf("%q", fmt.Sprintf("example-%d", i)))
	}
	body := `{"read_at":"2030-01-01T00:00:00Z","pending":true,"since":"","packages":[` + strings.Join(names, ",") +
		`],"scheduled_action":"","scheduled_at":""}`

	report := rebootInventory(t, writeReboot(t, []byte(body)))

	if !report.OK || len(report.Records) != 1 {
		t.Fatalf("%+v", report)
	}
	if got := report.Records[0].(runtime.HostRebootRecord).Packages; len(got) != hostRebootPackages {
		t.Errorf("%d packages kept, the contract admits %d", len(got), hostRebootPackages)
	}
}

// A run the launcher named the reading for but could not take it fails the
// read, so HQ keeps the last one beside the reason; a run that named nothing
// has no source for the kind. Neither is a reading that nothing waits.
func TestARestartReadingNotTakenIsNeverAnEmptyOne(t *testing.T) {
	for name, path := range map[string]string{
		"missing":          filepath.Join(t.TempDir(), "reboot.json"),
		"not a reading":    writeReboot(t, []byte(`{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":"","path":"/run"}`)),
		"no time":          writeReboot(t, []byte(`{"read_at":"","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":""}`)),
		"an action, no at": writeReboot(t, []byte(`{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"reboot","scheduled_at":""}`)),
	} {
		t.Run(name, func(t *testing.T) {
			report := rebootInventory(t, path)
			if report.OK || len(report.Records) != 0 || report.Error == "" {
				t.Errorf("%+v", report)
			}
		})
	}
	unnamed := rebootInventory(t, "")
	if unnamed.Connected == nil || *unnamed.Connected || len(unnamed.Records) != 0 {
		t.Errorf("a run handed no restart reading: %+v", unnamed)
	}
}

// edgeRebootInventory is the edges' reading with an edge per answer, each
// answering the reboot operation with it: a document, or, when empty, the
// refusal a receiver older than version 5 gives the operation.
func edgeRebootInventory(t *testing.T, answers map[string]string) (runtime.KindReport, []string) {
	t.Helper()
	kind := runtime.ResourceKindHostEdgeReboot
	env := runtime.Environment{SSHDir: t.TempDir()}
	edges := []connections.Connection{}
	for ref := range answers {
		edges = append(edges, sshConnection(ref, connections.SSHTransport{Host: "192.0.2.9", User: "hq", Port: "22", Role: caddyRole, HostKey: "ssh-ed25519 AAAA"}))
	}
	held := supplied(edges...)
	asked := []string{}
	registry := New(env, held, &fakeHTTP{})
	registry.Commands = &Commands{Env: env, Supplied: held, Remote: func(_ context.Context, ref, operation string, _ []byte) ([]byte, []byte, int, error) {
		asked = append(asked, ref+" "+operation)
		if answers[ref] == "" {
			return nil, []byte("Severino HQ edge target v4 does not implement \"reboot\"."), 126, nil
		}
		return []byte(answers[ref]), nil, 0, nil
	}}
	declared := runtime.ControllerRegistry{
		Observations:          map[string]string{string(kind): string(runtime.ConnectionProviderSSH)},
		ConnectionCredentials: []runtime.ConnectionProvider{runtime.ConnectionProviderSSH},
	}
	inventory, err := (&Controller{Registry: registry, Declared: declared}).Inventory(t.Context(), []runtime.ResourceKind{kind})
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)], asked
}

// What an edge's receiver reads of its machine is its record, named by the
// connection it came through.
func TestEachEdgeSaysWhetherItWaitsOnARestart(t *testing.T) {
	flag := filepath.Join(t.TempDir(), "reboot-required")
	if err := os.WriteFile(flag, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(flag+".pkgs", []byte("linux-image-example\nNot|A package\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	born := func(string) (time.Time, error) { return time.Date(2029, 12, 31, 18, 0, 0, 0, time.UTC), nil }
	scheduled := []byte(`{"type":"(st)","data":["reboot",1893466800000000]}`)
	waiting, err := hostcore.ReadReboot(flag, born, scheduled, time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}

	report, asked := edgeRebootInventory(t, map[string]string{"example-edge": string(waiting) + "\n"})

	if !report.OK || len(report.RefusedParts) != 0 {
		t.Fatalf("%+v", report)
	}
	want := []any{runtime.HostEdgeRebootRecord{ConnectionRef: "example-edge", Pending: true, Since: "2029-12-31T18:00:00Z",
		Packages: []string{"linux-image-example"}, ScheduledAction: "reboot", ScheduledAt: "2030-01-01T03:00:00Z", ReadAt: "2030-01-01T00:00:00Z"}}
	if !reflect.DeepEqual(report.Records, want) {
		t.Errorf("records %+v, want %+v", report.Records, want)
	}
	if !reflect.DeepEqual(asked, []string{"example-edge " + edge.OperationReboot}) {
		t.Errorf("asked %v", asked)
	}
}

// An edge whose receiver refuses the operation, as one older than version 5
// does, is a refused part that says to redeploy it, and every other edge's
// record still stands; the read fails only when no edge answered.
func TestAnEdgeThatDoesNotAnswerHidesNoOther(t *testing.T) {
	answered := `{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":""}`

	report, _ := edgeRebootInventory(t, map[string]string{"example-stale": "", "example-edge": answered})

	if !report.OK || len(report.Records) != 1 || report.Records[0].(runtime.HostEdgeRebootRecord).ConnectionRef != "example-edge" {
		t.Fatalf("%+v", report)
	}
	if len(report.RefusedParts) != 1 {
		t.Fatalf("refused %+v", report.RefusedParts)
	}
	refused := report.RefusedParts[0]
	if refused.Part != runtime.PartWhole || refused.ConnectionRef != "example-stale" || refused.Scope != "example-stale" ||
		!strings.Contains(refused.Reason, "version "+edge.Version) {
		t.Errorf("refused %+v", refused)
	}

	none, _ := edgeRebootInventory(t, map[string]string{"example-stale": ""})
	if none.OK || len(none.Records) != 0 || !strings.Contains(string(none.Error), "version "+edge.Version) {
		t.Errorf("every edge refused: %+v", none)
	}
}

// What an edge answers is held to the same contract as the launcher's file.
func TestAnEdgesAnswerThatIsNotAReadingIsRefused(t *testing.T) {
	for name, answer := range map[string]string{
		"not a reading": `{"read_at":"2030-01-01T00:00:00Z","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":"","path":"/run"}`,
		"no time":       `{"read_at":"","pending":false,"since":"","packages":[],"scheduled_action":"","scheduled_at":""}`,
		"too large":     strings.Repeat(" ", hostRebootMaxBytes+1),
	} {
		t.Run(name, func(t *testing.T) {
			report, _ := edgeRebootInventory(t, map[string]string{"example-edge": answer})
			if report.OK || len(report.Records) != 0 {
				t.Errorf("%+v", report)
			}
		})
	}
}
