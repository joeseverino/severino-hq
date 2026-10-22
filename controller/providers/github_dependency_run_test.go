package providers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

const githubTwoInstallations = `[{"id":7,"account":{"login":"example"}},{"id":8,"account":{"login":"Example-Org"}}]`

// dependencyRunHarness is a GitHub fake with two installations, a plan and a
// log, the hand-off directory given unless out is false.
type dependencyRunHarness struct {
	*githubHarness
	log    *bytes.Buffer
	ledger *refusals
}

func newDependencyRunHarness(t *testing.T, plan runtime.DependencyUpdatesPlan, env runtime.Environment) *dependencyRunHarness {
	t.Helper()
	h := newGitHubHarness(t, map[string]string{githubFakeAPI + " " + githubInstallationsPage + "1": githubTwoInstallations}, env)
	h.r.Updates = plan
	log := &bytes.Buffer{}
	h.r.Commands.Log = slog.New(slog.NewTextHandler(log, nil))
	return &dependencyRunHarness{githubHarness: h, log: log, ledger: &refusals{}}
}

func (h *dependencyRunHarness) read(t *testing.T) ([]any, error) {
	t.Helper()
	records, err := h.r.githubDependencyRuns(context.WithValue(t.Context(), refusalKey{}, h.ledger))
	h.noTokenOutsideTheHandoff(t, records, err)
	return records, err
}

// noTokenOutsideTheHandoff fails when a minted token reached a record, an
// error, a refusal or the log.
func (h *dependencyRunHarness) noTokenOutsideTheHandoff(t *testing.T, records []any, err error) {
	t.Helper()
	said, _ := json.Marshal(records)
	refusals, _ := json.Marshal(h.ledger.entries)
	text := string(said) + string(refusals) + h.log.String()
	if err != nil {
		text += err.Error()
	}
	if strings.Contains(text, "minted-") {
		t.Fatalf("a token left the hand-off: %s", text)
	}
}

func duePlan(owners ...runtime.DependencyUpdatesOwner) runtime.DependencyUpdatesPlan {
	return runtime.DependencyUpdatesPlan{Due: true, Owners: owners}
}

func owner(name string, repositories ...string) runtime.DependencyUpdatesOwner {
	return runtime.DependencyUpdatesOwner{Owner: name, Repositories: repositories}
}

func readHandoff(t *testing.T, dir string) (map[string]any, os.FileMode) {
	t.Helper()
	path := filepath.Join(dir, DependencyHandoffName)
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	var found map[string]any
	if err := json.Unmarshal(data, &found); err != nil {
		t.Fatal(err)
	}
	return found, info.Mode()
}

func sortedMapKeys(value any) []string {
	return slices.Sorted(maps.Keys(value.(map[string]any)))
}

// A due plan hands root one file: a run per owner, each token scoped to
// exactly that owner's repositories and the Renovate grant, minted on that
// owner's installation, and readable by the controller's account alone.
func TestADueRunHandsEachOwnerOneScopedToken(t *testing.T) {
	out := t.TempDir()
	plan := duePlan(owner("example", "example/app", "example/tool"), owner("example-org", "example-org/site"))
	h := newDependencyRunHarness(t, plan, runtime.Environment{Out: out})

	records, err := h.read(t)
	if err != nil {
		t.Fatal(err)
	}

	handoff, mode := readHandoff(t, out)
	if mode != 0o600 || !mode.IsRegular() {
		t.Fatalf("mode %v", mode)
	}
	if keys := sortedMapKeys(handoff); !slices.Equal(keys, []string{"runs", "version"}) || handoff["version"] != float64(1) {
		t.Fatalf("hand-off %v", handoff)
	}
	runs := handoff["runs"].([]any)
	if len(runs) != 2 {
		t.Fatalf("runs %v", runs)
	}
	want := map[string][]string{"example": {"app", "tool"}, "example-org": {"site"}}
	for _, entry := range runs {
		run := entry.(map[string]any)
		if keys := sortedMapKeys(run); !slices.Equal(keys, []string{"expires_at", "owner", "repositories", "token"}) {
			t.Fatalf("run %v", keys)
		}
		name := run["owner"].(string)
		if run["expires_at"] != "2030-01-01T00:00:00Z" {
			t.Fatalf("expires %v", run["expires_at"])
		}
		grant, minted := h.grantOf(githubFakeCall{auth: "Bearer " + run["token"].(string)})
		got, _ := json.Marshal(grant.Permissions)
		renovate, _ := json.Marshal(githubRenovate)
		if !minted || !slices.Equal(grant.Repositories, want[name]) || string(got) != string(renovate) {
			t.Fatalf("%s minted %v with %+v", name, minted, grant)
		}
	}
	// Each owner's token comes from the installation on that account.
	mintedOn := map[string]string{}
	for _, call := range h.recorded() {
		if call.method == "POST" {
			repositories, _ := json.Marshal(call.body["repositories"])
			mintedOn[call.path] = string(repositories)
		}
	}
	if !maps.Equal(mintedOn, map[string]string{
		"/app/installations/7/access_tokens": `["app","tool"]`,
		"/app/installations/8/access_tokens": `["site"]`,
	}) {
		t.Fatalf("minted %v", mintedOn)
	}
	if len(records) != 2 {
		t.Fatalf("records %+v", records)
	}
	for _, record := range records {
		run := record.(GitHubDependencyRunRecord)
		if run.Outcome != "handed_off" || run.ExitCode != nil || run.StartedAt == "" {
			t.Fatalf("record %+v", run)
		}
	}
}

// Without the directory root mounts there is nowhere to hand a token to, so
// none is minted, and the pass says why.
func TestADueRunWithoutTheHandoffDirectoryMintsNothing(t *testing.T) {
	h := newDependencyRunHarness(t, duePlan(owner("example", "example/app")), runtime.Environment{})

	_, err := h.read(t)

	if !errors.Is(err, errCarried) || h.minted() != 0 {
		t.Fatalf("minted %d: %v", h.minted(), err)
	}
	if !strings.Contains(h.log.String(), "HQ_CONTROLLER_OUT") {
		t.Fatalf("log %q", h.log.String())
	}
}

// An owner the App is not installed on is refused alone; the others run.
func TestAnOwnerWithoutAnInstallationIsRefusedAlone(t *testing.T) {
	out := t.TempDir()
	h := newDependencyRunHarness(t, duePlan(owner("example", "example/app"), owner("example-gone", "example-gone/app")), runtime.Environment{Out: out})

	records, err := h.read(t)
	if err != nil {
		t.Fatal(err)
	}

	handoff, _ := readHandoff(t, out)
	if runs := handoff["runs"].([]any); len(runs) != 1 || runs[0].(map[string]any)["owner"] != "example" || len(records) != 1 {
		t.Fatalf("runs %v records %v", runs, records)
	}
	if len(h.ledger.entries) != 1 || h.ledger.entries[0].Scope != "example-gone" || h.ledger.entries[0].Part != runtime.PartWhole {
		t.Fatalf("refused %+v", h.ledger.entries)
	}
}

// A plan naming a repository under another owner is refused before any mint.
func TestAPlanSpanningTwoOwnersInOneTokenIsRefused(t *testing.T) {
	for _, bad := range []runtime.DependencyUpdatesOwner{
		owner("example", "example-org/site"),
		owner("example", "example/app/extra"),
		owner("Example!", "Example!/app"),
		owner("example"),
	} {
		h := newDependencyRunHarness(t, duePlan(bad), runtime.Environment{Out: t.TempDir()})
		if _, err := h.read(t); err == nil || errors.Is(err, errCarried) || h.minted() != 0 {
			t.Fatalf("%+v: minted %d: %v", bad, h.minted(), err)
		}
	}
}

const dependencyResult = `{"version":1,"started_at":"2030-01-02T03:00:00Z","finished_at":"2030-01-02T03:20:00Z",
"runs":[{"owner":"example","repositories":["example/app"],"exit_code":0,"outcome":"ok"},
{"owner":"example-org","repositories":["example-org/site"],"exit_code":1,"outcome":"failed"}]}`

func writeResult(t *testing.T, text string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "result.json")
	if err := os.WriteFile(path, []byte(text), 0o400); err != nil {
		t.Fatal(err)
	}
	return path
}

// Root's result is the reading when it is newer than the latest run HQ
// holds; a result HQ already holds is carried.
func TestTheRunResultIsReadOnceItIsNews(t *testing.T) {
	path := writeResult(t, dependencyResult)
	plan := runtime.DependencyUpdatesPlan{Owners: []runtime.DependencyUpdatesOwner{owner("example", "example/app")}, LastStartedAt: "2030-01-02T02:59:00+00:00"}
	h := newDependencyRunHarness(t, plan, runtime.Environment{DependencyRun: path})

	records, err := h.read(t)
	if err != nil || len(records) != 2 {
		t.Fatalf("%+v: %v", records, err)
	}
	failed := records[1].(GitHubDependencyRunRecord)
	if failed.Owner != "example-org" || failed.Outcome != "failed" || failed.ExitCode == nil || *failed.ExitCode != 1 ||
		failed.StartedAt != "2030-01-02T03:00:00Z" || failed.FinishedAt != "2030-01-02T03:20:00Z" {
		t.Fatalf("record %+v", failed)
	}
	if ok := records[0].(GitHubDependencyRunRecord); ok.ExitCode == nil || *ok.ExitCode != 0 {
		t.Fatalf("a run that exited 0 says so: %+v", ok)
	}
	if h.minted() != 0 {
		t.Fatal("a run that is not due mints nothing")
	}

	h.r.Updates.LastStartedAt = "2030-01-02T03:00:00Z"
	if _, err := h.read(t); !errors.Is(err, errCarried) {
		t.Fatalf("a result HQ holds is carried: %v", err)
	}
	h.r.Env.DependencyRun = filepath.Join(t.TempDir(), "absent.json")
	if _, err := h.read(t); !errors.Is(err, errCarried) {
		t.Fatalf("no result yet is nothing to say: %v", err)
	}
}

// Anything but root's exact shape is refused, and the refusal quotes none of it.
func TestAMalformedRunResultIsRefused(t *testing.T) {
	for name, text := range map[string]string{
		"not json":       `{`,
		"another shape":  `{"version":2,"started_at":"2030-01-02T03:00:00Z","finished_at":"2030-01-02T03:20:00Z","runs":[]}`,
		"unknown field":  strings.Replace(dependencyResult, `"outcome":"ok"`, `"outcome":"ok","log":"secret-log-line"`, 1),
		"no exit code":   strings.Replace(dependencyResult, `"exit_code":0,`, ``, 1),
		"not finished":   strings.Replace(dependencyResult, `"outcome":"ok"`, `"outcome":"handed_off"`, 1),
		"unknown end":    strings.Replace(dependencyResult, `"outcome":"ok"`, `"outcome":"great"`, 1),
		"foreign repo":   strings.Replace(dependencyResult, `["example/app"]`, `["example-org/app"]`, 1),
		"no repository":  strings.Replace(dependencyResult, `["example/app"]`, `[]`, 1),
		"bad time":       strings.Replace(dependencyResult, `"2030-01-02T03:00:00Z"`, `"yesterday"`, 1),
		"ends too early": strings.Replace(dependencyResult, `"2030-01-02T03:20:00Z"`, `"2030-01-02T02:00:00Z"`, 1),
		"two documents":  dependencyResult + `{}`,
	} {
		h := newDependencyRunHarness(t, runtime.DependencyUpdatesPlan{}, runtime.Environment{DependencyRun: writeResult(t, text)})
		_, err := h.read(t)
		if err == nil || errors.Is(err, errCarried) || strings.Contains(err.Error(), "secret-log-line") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// The reading is held: no connection reads it, and with neither a plan nor
// a result there is nothing to read.
func TestTheRunIsReadFromWhatTheControllerHolds(t *testing.T) {
	h := newDependencyRunHarness(t, runtime.DependencyUpdatesPlan{}, runtime.Environment{})
	held := h.r.held[string(runtime.ResourceKindGitHubDependencyRun)]
	if !held.only || held.has() {
		t.Fatal("nothing planned and no result is nothing held")
	}
	h.r.Updates = duePlan(owner("example", "example/app"))
	if !held.has() {
		t.Fatal("a plan is held")
	}
}
