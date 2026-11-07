package host

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// examplePull is a placed grant as a host's configuration declares one.
var examplePull = Grant{
	Name:         "example-pull",
	Repositories: []string{"example/notes"},
	Permissions:  map[string]string{"contents": "read", "metadata": "read"},
	Path:         "/run/example-pull/github_token",
}

// newGrantLaunch is a launch whose host also declares examplePull.
func newGrantLaunch(t *testing.T) (*launch, Grant, *bytes.Buffer) {
	t.Helper()
	l := newLaunch(t)
	grant := relocated(examplePull, filepath.Dir(l.spool()))
	l.controller.Grants = append(l.controller.Grants, grant)
	var stderr bytes.Buffer
	l.controller.Stderr = &stderr
	return l, grant, &stderr
}

func placedHandoff(t *testing.T, edit func(run, top map[string]any)) []byte {
	t.Helper()
	return handoffDocument(t, func(run, top map[string]any) {
		run["repositories"] = []string{"example/notes"}
		if edit != nil {
			edit(run, top)
		}
	})
}

// A placed grant with no token yet is due: the pass is told what to mint,
// and never where it goes.
func TestADueGrantIsToldToThePassWithoutItsPath(t *testing.T) {
	l, grant, _ := newGrantLaunch(t)

	l.run(t, Apply)

	if !slices.Contains(l.envs(), "HQ_CONTROLLER_GRANTS="+grantsTarget) {
		t.Fatalf("the pass is not told of its grants: %v", l.envs())
	}
	var told GrantRequests
	if err := json.Unmarshal(l.mounted[grantsTarget], &told); err != nil {
		t.Fatal(err)
	}
	if told.Version != 1 || len(told.Grants) != 1 || told.Grants[0].Name != grant.Name ||
		!slices.Equal(told.Grants[0].Repositories, grant.Repositories) || told.Grants[0].Permissions["contents"] != "read" {
		t.Fatalf("told %+v", told)
	}
	if strings.Contains(string(l.mounted[grantsTarget]), grant.Path) || strings.Contains(string(l.mounted[grantsTarget]), "github_token") {
		t.Error("the pass was told where the token goes")
	}
}

// A token placed less than TokenRefresh ago is not minted again; one older is.
func TestAGrantIsDueAgainHalfwayThroughItsToken(t *testing.T) {
	now := time.Now()
	grant := relocated(examplePull, t.TempDir())
	if !grant.Due(now) {
		t.Fatal("a grant with no token is not due")
	}
	if err := place(grant.Path, exampleToken); err != nil {
		t.Fatal(err)
	}
	now = time.Now()
	if grant.Due(now) {
		t.Error("a fresh token is minted again")
	}
	if !grant.Due(now.Add(TokenRefresh)) {
		t.Error("a token past its refresh is not minted again")
	}
	if DependencyUpdates.Due(now) {
		t.Error("the dependency run's grant is HQ's plan to time")
	}
}

// A valid hand-off is placed where root's declaration says: the token
// alone, root's to read and nobody's to write, in a directory only root
// enters, replacing what was there whole.
func TestAValidHandoffPlacesTheTokenForRootAlone(t *testing.T) {
	l, grant, stderr := newGrantLaunch(t)
	if err := place(grant.Path, "ghs_the-last-token"); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(grant.Path, time.Now().Add(-TokenRefresh), time.Now().Add(-TokenRefresh)); err != nil {
		t.Fatal(err)
	}
	l.container = func(out string) {
		if err := os.WriteFile(filepath.Join(out, grant.HandoffName()), placedHandoff(t, nil), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	l.run(t, Apply)

	data, err := os.ReadFile(grant.Path)
	if err != nil || string(data) != exampleToken {
		t.Fatalf("placed %q (%v)", data, err)
	}
	for path, want := range map[string]os.FileMode{filepath.Dir(grant.Path): 0o700 | os.ModeDir, grant.Path: 0o400} {
		if info, _ := os.Lstat(path); info.Mode() != want {
			t.Errorf("%s is %v, want %v", path, info.Mode(), want)
		}
	}
	if entries, _ := os.ReadDir(filepath.Dir(grant.Path)); len(entries) != 1 {
		t.Errorf("the token's directory holds %v", entries)
	}
	if _, err := os.Lstat(l.spool()); err == nil {
		t.Error("a placed grant reached the dependency run's spool")
	}
	if stderr.Len() != 0 {
		t.Errorf("said %q", stderr.String())
	}
}

// What is no declared grant's hand-off, or one its grant does not admit, is
// dropped, never placed, and the refusal names no token.
func TestAHandoffItsGrantDoesNotAdmitIsDropped(t *testing.T) {
	for name, leave := range map[string]func(grant Grant) (string, []byte){
		"a grant this host does not declare": func(Grant) (string, []byte) {
			return "example-other.json", placedHandoff(t, nil)
		},
		"a path the pass names": func(Grant) (string, []byte) {
			return "github_token", []byte(exampleToken)
		},
		"repositories the grant does not name": func(grant Grant) (string, []byte) {
			return grant.HandoffName(), placedHandoff(t, func(run, _ map[string]any) { run["repositories"] = []string{"example/notes", "example/app"} })
		},
		"a second token": func(grant Grant) (string, []byte) {
			return grant.HandoffName(), placedHandoff(t, func(run, top map[string]any) {
				other := map[string]any{"owner": "example-org", "repositories": []string{"example-org/notes"}, "token": exampleToken, "expires_at": run["expires_at"]}
				top["runs"] = []any{run, other}
			})
		},
		"an expired token": func(grant Grant) (string, []byte) {
			return grant.HandoffName(), placedHandoff(t, func(run, _ map[string]any) {
				run["expires_at"] = time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
			})
		},
		"a key the contract does not name": func(grant Grant) (string, []byte) {
			return grant.HandoffName(), placedHandoff(t, func(run, _ map[string]any) { run["path"] = "/etc/shadow" })
		},
	} {
		t.Run(name, func(t *testing.T) {
			l, grant, stderr := newGrantLaunch(t)
			l.container = func(dir string) {
				file, data := leave(grant)
				if err := os.WriteFile(filepath.Join(dir, file), data, 0o600); err != nil {
					t.Fatal(err)
				}
			}

			l.run(t, Apply)

			if _, err := os.Lstat(grant.Path); err == nil {
				t.Error("a token was placed")
			}
			if _, err := os.Lstat(l.spool()); err == nil {
				t.Error("a hand-off was spooled")
			}
			if !strings.Contains(stderr.String(), "refused") || strings.Contains(stderr.String(), exampleToken) {
				t.Errorf("said %q", stderr.String())
			}
		})
	}
}

// The shape deploy/host.example.json shows is one ReadConfig takes, a
// grant included.
func TestTheExampleHostConfigurationIsReadable(t *testing.T) {
	data, err := os.ReadFile("../../deploy/host.example.json")
	if err != nil {
		t.Fatal(err)
	}
	config, err := ReadConfig(data)
	if err != nil || len(config.Grants) == 0 {
		t.Fatalf("the example is refused or declares no grant: %v", err)
	}
}

// A host's grants are refused unless each is a token of its own for one
// owner's repositories, placed under /run outside HQ's own directories.
func TestAHostsGrantsAreReadStrictly(t *testing.T) {
	declared := func(grants ...string) string {
		return strings.Replace(hostJSON, `"vault"`, `"grants": [`+strings.Join(grants, ",")+`], "vault"`, 1)
	}
	valid := `{"name": "example-pull", "repositories": ["example/notes"], "permissions": {"contents": "read", "metadata": "read"}, "path": "/run/example-pull/github_token"}`
	config, err := ReadConfig([]byte(declared(valid)))
	if err != nil || len(config.Grants) != 1 || config.Grants[0].Planned {
		t.Fatalf("the example was refused: %+v %v", config.Grants, err)
	}
	if grants := Grants(config); len(grants) != 2 || grants[0].Name != DependencyUpdates.Name {
		t.Fatalf("HQ's own grant is not first: %+v", grants)
	}
	for name, grants := range map[string][]string{
		"HQ's own name":               {strings.Replace(valid, "example-pull", "dependency-updates", 1)},
		"a name that is a path":       {strings.Replace(valid, `"example-pull"`, `"../pull"`, 1)},
		"a name given twice":          {valid, strings.Replace(valid, "/run/example-pull/", "/run/example-other/", 1)},
		"a path given twice":          {valid, strings.Replace(valid, `"name": "example-pull"`, `"name": "example-other"`, 1)},
		"a path on disk":              {strings.Replace(valid, "/run/example-pull/", "/var/lib/example-pull/", 1)},
		"a path directly in /run":     {strings.Replace(valid, "/run/example-pull/github_token", "/run/github_token", 1)},
		"a path that climbs":          {strings.Replace(valid, "/run/example-pull/", "/run/example-pull/../severino-hq/", 1)},
		"HQ's doorbell directory":     {strings.Replace(valid, "/run/example-pull/", "/run/severino-hq/", 1)},
		"HQ's secrets":                {strings.Replace(valid, "/run/example-pull/", "/run/severino-hq-secrets/", 1)},
		"the dependency run's spool":  {strings.Replace(valid, "/run/example-pull/", "/run/severino-hq-renovate/", 1)},
		"two owners":                  {strings.Replace(valid, `["example/notes"]`, `["example/notes", "example-org/notes"]`, 1)},
		"no repository":               {strings.Replace(valid, `["example/notes"]`, `[]`, 1)},
		"no permission":               {strings.Replace(valid, `{"contents": "read", "metadata": "read"}`, `{}`, 1)},
		"a level GitHub has not":      {strings.Replace(valid, `"contents": "read"`, `"contents": "all"`, 1)},
		"a key a grant does not name": {strings.Replace(valid, `"name"`, `"planned": true, "name"`, 1)},
	} {
		if _, err := ReadConfig([]byte(declared(grants...))); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}
