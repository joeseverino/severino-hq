package providers

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// GitHub's answers when a token names a repository the installation does not
// cover, and when the App is asked for a repository it is not installed on.
const (
	githubMintRefused = `{"message":"There is at least one repository that does not exist or is not accessible to the parent installation.","documentation_url":"https://docs.github.com/rest/apps/apps#create-an-installation-access-token-for-an-app","status":"422"}`
	githubNotFound    = `{"message":"Not Found","documentation_url":"https://docs.github.com/rest/apps/apps#get-a-repository-installation-for-the-authenticated-app","status":"404"}`
)

const exampleGrants = `{"version":1,"grants":[{"name":"example-pull","repositories":["example/notes"],"permissions":{"contents":"read","metadata":"read"}}]}`

// newGrantHarness is the dependency run's harness, told by root that grants is due.
func newGrantHarness(t *testing.T, plan runtime.DependencyUpdatesPlan, grants string) (*dependencyRunHarness, string) {
	t.Helper()
	out := t.TempDir()
	path := filepath.Join(t.TempDir(), "grants.json")
	if err := os.WriteFile(path, []byte(grants), 0o400); err != nil {
		t.Fatal(err)
	}
	return newDependencyRunHarness(t, plan, runtime.Environment{Out: out, Grants: path}), out
}

func (h *dependencyRunHarness) steps(t *testing.T) []runtime.StepFailure {
	t.Helper()
	failures := h.r.commands().StepFailures()
	said, _ := json.Marshal(failures)
	if strings.Contains(string(said)+h.log.String(), "minted-") {
		t.Fatalf("a token left the hand-off: %s", said)
	}
	return failures
}

// Both uses mint through their declaration: the dependency run's grant for
// what HQ planned, and a placed grant for exactly what root names, each a
// token for exactly its repositories and permissions in a file of its own.
func TestEachGrantIsMintedForExactlyWhatItDeclares(t *testing.T) {
	h, out := newGrantHarness(t, duePlan(owner("example", "example/app")), exampleGrants)

	h.r.HandOffGrants(t.Context())
	if _, err := h.read(t); err != nil {
		t.Fatal(err)
	}

	if failures := h.steps(t); len(failures) != 0 {
		t.Fatalf("failed %+v", failures)
	}
	want := map[string]struct {
		repositories []string
		permissions  string
	}{
		"example-pull.json":   {[]string{"notes"}, `{"contents":"read","metadata":"read"}`},
		DependencyHandoffName: {[]string{"app"}, func() string { encoded, _ := json.Marshal(githubRenovate); return string(encoded) }()},
	}
	for file, scope := range want {
		handoff, mode := readHandoff(t, out, file)
		runs := handoff["runs"].([]any)
		if mode.Perm() != 0o600 || len(runs) != 1 {
			t.Fatalf("%s: %v %v", file, mode, handoff)
		}
		run := runs[0].(map[string]any)
		grant, minted := h.grantOf(githubFakeCall{auth: "Bearer " + run["token"].(string)})
		got, _ := json.Marshal(grant.Permissions)
		if !minted || !slices.Equal(grant.Repositories, scope.repositories) || string(got) != scope.permissions || run["expires_at"] != "2030-01-01T00:00:00Z" {
			t.Errorf("%s minted %v with %+v", file, minted, grant)
		}
	}
	if entries, _ := os.ReadDir(out); len(entries) != len(want) {
		t.Errorf("the pass left %v", entries)
	}
}

// A repository the App is not installed on is a step the pass could not
// finish, said in those words on the App's connection; nothing is handed off
// and the pass goes on.
func TestAGrantTheAppIsNotInstalledForIsAFinding(t *testing.T) {
	for reason, setup := range map[string]func(h *dependencyRunHarness){
		"the GitHub App is not installed on example/notes": func(h *dependencyRunHarness) {
			h.routes[githubFakeAPI+" "+"/app/installations/7/access_tokens"] = githubMintRefused
			h.refuse(githubFakeAPI, "/app/installations/7/access_tokens", 422)
			h.routes[githubFakeAPI+" "+"/repos/example/notes/installation"] = githubNotFound
			h.refuse(githubFakeAPI, "/repos/example/notes/installation", 404)
		},
		"the GitHub App is not installed on example": func(h *dependencyRunHarness) {
			h.routes[githubFakeAPI+" "+githubInstallationsPage+"1"] = `[{"id":8,"account":{"login":"example-org"}}]`
		},
	} {
		t.Run(reason, func(t *testing.T) {
			h, out := newGrantHarness(t, runtime.DependencyUpdatesPlan{}, exampleGrants)
			setup(h)

			h.r.HandOffGrants(t.Context())

			failures := h.steps(t)
			if len(failures) != 1 || failures[0].Step != "github.grant:example-pull" || failures[0].Subject != "hq-app" ||
				failures[0].Reason != reason {
				t.Fatalf("failed %+v", failures)
			}
			if entries, _ := os.ReadDir(out); len(entries) != 0 {
				t.Errorf("handed off %v", entries)
			}
		})
	}
}

// A grant list that is not root's, or a grant this controller would not
// mint, is refused before anything is minted.
func TestAGrantRootMayNotNameIsRefused(t *testing.T) {
	valid := `{"name":"example-pull","repositories":["example/notes"],"permissions":{"contents":"read","metadata":"read"}}`
	for name, grants := range map[string]string{
		"not json":                    `{`,
		"another version":             `{"version":2,"grants":[]}`,
		"an unknown key":              `{"version":1,"grants":[` + strings.Replace(valid, `"name"`, `"path":"/etc/shadow","name"`, 1) + `]}`,
		"the dependency run's name":   `{"version":1,"grants":[` + strings.Replace(valid, "example-pull", "dependency-updates", 1) + `]}`,
		"a name that is a path":       `{"version":1,"grants":[` + strings.Replace(valid, "example-pull", "../pull", 1) + `]}`,
		"two accounts in one token":   `{"version":1,"grants":[` + strings.Replace(valid, `["example/notes"]`, `["example/notes","example-org/notes"]`, 1) + `]}`,
		"a permission GitHub has not": `{"version":1,"grants":[` + strings.Replace(valid, `"contents"`, `"everything"`, 1) + `]}`,
	} {
		t.Run(name, func(t *testing.T) {
			h, out := newGrantHarness(t, runtime.DependencyUpdatesPlan{}, grants)

			h.r.HandOffGrants(t.Context())

			if failures := h.steps(t); len(failures) != 1 || failures[0].Subject != "hq-app" {
				t.Errorf("failed %+v", failures)
			}
			if entries, _ := os.ReadDir(out); h.minted() != 0 || len(entries) != 0 {
				t.Errorf("minted %d, handed off %v", h.minted(), entries)
			}
		})
	}
}

// With no grant due, nothing is asked of GitHub.
func TestNoGrantDueAsksNothing(t *testing.T) {
	h := newDependencyRunHarness(t, runtime.DependencyUpdatesPlan{}, runtime.Environment{Out: t.TempDir()})

	h.r.HandOffGrants(t.Context())

	if calls := h.recorded(); len(calls) != 0 || len(h.steps(t)) != 0 {
		t.Fatalf("called %+v", calls)
	}
}
