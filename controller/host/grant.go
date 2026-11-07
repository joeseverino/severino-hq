package host

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
)

// Grant is one GitHub App installation token a controller pass mints and root
// puts on this host: which repositories it reaches, with which permissions,
// and where it goes. The controller is told what to mint and never where:
// root takes a hand-off only as the file its grant names in the pass's out
// directory, checks it against its own copy of the grant, and writes it only
// to the path that copy holds.
type Grant struct {
	// Name is the grant's own, and its hand-off is Name.json.
	Name string `json:"name"`
	// Repositories are owner/name, all under one owner: everything the token
	// reaches.
	Repositories []string `json:"repositories"`
	// Permissions are GitHub's permission names and levels, within what
	// deploy/github-apps.json registers: GitHub refuses a mint asking more.
	Permissions map[string]string `json:"permissions"`
	// Path is the file root writes the token alone to: root's, mode 0400,
	// under /run, which systemd mounts as tmpfs.
	Path string `json:"path"`
	// Planned is a grant HQ's plan scopes and times rather than this
	// declaration, the dependency update run: its repositories are the
	// plan's, it is minted when the plan says one is due, and root spools its
	// whole hand-off in Path, the directory the unit that runs it watches.
	Planned bool `json:"-"`
}

// DependencyUpdates is a dependency update run's grant, HQ's own: Renovate's
// branches and their commits, the pull requests, the Dependency Dashboard
// issue, the stability status, the workflow files it pins, and the alerts a
// security fix answers. Renovate's GitHub App guide
// (docs.renovatebot.com/modules/platform/github/, "Running as a GitHub App")
// also lists administration read, checks and merge queues write, and an
// organization's members read; a run is given none of them.
var DependencyUpdates = Grant{
	Name: "dependency-updates",
	Permissions: map[string]string{
		"contents":             "write",
		"pull_requests":        "write",
		"issues":               "write",
		"statuses":             "write",
		"workflows":            "write",
		"vulnerability_alerts": "read",
	},
	Path:    RenovateSpool,
	Planned: true,
}

// HandoffName is the file a pass leaves in its out directory for the grant.
func (g Grant) HandoffName() string { return g.Name + ".json" }

// Due is whether a placed grant is to be minted this pass: no token is at
// its path yet, or the one there is past TokenRefresh. A planned grant is
// never due here; HQ's plan says when.
func (g Grant) Due(now time.Time) bool {
	if g.Planned {
		return false
	}
	info, err := os.Lstat(g.Path)
	return err != nil || !info.Mode().IsRegular() || now.Sub(info.ModTime()) >= TokenRefresh
}

// admits is whether handoff is this grant's: for a placed grant, one run of
// exactly its repositories.
func (g Grant) admits(handoff Handoff) error {
	if g.Planned {
		return nil
	}
	if len(handoff.Runs) != 1 {
		return errors.New("a placed grant is one token")
	}
	got := slices.Sorted(slices.Values(handoff.Runs[0].Repositories))
	want := slices.Sorted(slices.Values(g.Repositories))
	if !slices.EqualFunc(got, want, strings.EqualFold) {
		return errors.New("its repositories are not the grant's")
	}
	return nil
}

var (
	// GrantNamed is a grant's name: it names a file in the pass's out
	// directory, so it holds no separator and no dot.
	GrantNamed      = regexp.MustCompile(`^[a-z0-9](?:-?[a-z0-9])*$`)
	permissionNamed = regexp.MustCompile(`^[a-z][a-z_]{0,63}$`)
	permissionLevel = []string{"read", "write", "admin"}
	// grantReserved is every directory of HQ's own under /run: a token placed
	// there would reach the account or unit that directory is for.
	grantReserved = []string{hostcore.RunDir, hostcore.SecretDir, RenovateSpool, UnitSocketDir}
)

// checkGrants refuses a host's grants unless each has a name of its own, one
// owner's repositories, permissions by GitHub's names and levels, and a path
// of its own under /run outside HQ's own directories.
func checkGrants(grants []Grant) error {
	names, paths := map[string]bool{DependencyUpdates.Name: true}, map[string]bool{}
	for _, grant := range grants {
		if len(grant.Name) > 63 || !GrantNamed.MatchString(grant.Name) || names[grant.Name] {
			return fmt.Errorf("grant %q: a grant's name is lowercase letters, digits and single hyphens, and its own", grant.Name)
		}
		names[grant.Name] = true
		if err := checkRepositories(grant.Repositories); err != nil {
			return fmt.Errorf("grant %s: %w", grant.Name, err)
		}
		if len(grant.Permissions) == 0 {
			return fmt.Errorf("grant %s names no permissions", grant.Name)
		}
		for name, level := range grant.Permissions {
			if !permissionNamed.MatchString(name) || !slices.Contains(permissionLevel, level) {
				return fmt.Errorf("grant %s: %q: %q is not a GitHub permission and level", grant.Name, name, level)
			}
		}
		if err := checkGrantPath(grant.Path); err != nil || paths[grant.Path] {
			return fmt.Errorf("grant %s: the path must be a file of its own under /run, outside HQ's own directories", grant.Name)
		}
		paths[grant.Path] = true
	}
	return nil
}

func checkRepositories(repositories []string) error {
	if len(repositories) == 0 {
		return errors.New("it names no repositories")
	}
	owner, _, _ := strings.Cut(repositories[0], "/")
	if len(owner) > ownerLimit || !githubOwner.MatchString(owner) {
		return errors.New("the owner is not a GitHub account name")
	}
	for _, repository := range repositories {
		named, name, _ := strings.Cut(repository, "/")
		if !strings.EqualFold(named, owner) || !githubName.MatchString(name) || name == "." || name == ".." {
			return errors.New("a repository is not one of its owner's")
		}
	}
	if sorted := slices.Sorted(slices.Values(repositories)); len(slices.Compact(sorted)) != len(repositories) {
		return errors.New("a repository is named twice")
	}
	return nil
}

func checkGrantPath(path string) error {
	dir := filepath.Dir(path)
	if filepath.Clean(path) != path || !strings.HasPrefix(path, "/run/") || dir == "/run" || strings.HasPrefix(filepath.Base(path), ".") {
		return errors.New("not a file in a directory of /run")
	}
	for _, reserved := range grantReserved {
		if path == reserved || strings.HasPrefix(path, reserved+"/") {
			return errors.New("inside a directory of HQ's own")
		}
	}
	return nil
}

// GrantRequest is a grant as the pass is told it: what to mint, never where
// it goes.
type GrantRequest struct {
	Name         string            `json:"name"`
	Repositories []string          `json:"repositories"`
	Permissions  map[string]string `json:"permissions"`
}

// GrantRequests is the file a pass finds at grantsTarget.
type GrantRequests struct {
	Version int            `json:"version"`
	Grants  []GrantRequest `json:"grants"`
}

// dueGrants gives the pass the placed grants due now, and nothing when none is.
func (c *Controller) dueGrants(run string) ([]string, error) {
	requests := GrantRequests{Version: handoffV1, Grants: []GrantRequest{}}
	for _, grant := range c.Grants {
		if grant.Due(c.Now()) {
			requests.Grants = append(requests.Grants, GrantRequest{Name: grant.Name, Repositories: grant.Repositories, Permissions: grant.Permissions})
		}
	}
	if len(requests.Grants) == 0 {
		return nil, nil
	}
	data, err := json.Marshal(requests)
	if err != nil {
		return nil, err
	}
	mount, err := c.give(run, "grants.json", data, grantsTarget)
	if err != nil {
		return nil, err
	}
	return append(mount, "--env", grantsEnv+"="+grantsTarget), nil
}

// Grants is every grant this host declares: HQ's own, then those its
// configuration adds.
func Grants(config Config) []Grant {
	return append([]Grant{DependencyUpdates}, config.Grants...)
}
