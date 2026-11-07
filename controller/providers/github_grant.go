package providers

import (
	"context"
	"encoding/json"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A grant handed to root (host.Grant): this controller holds the App's key,
// so it mints one installation token per owner, scoped to exactly that
// owner's repositories and the grant's permissions, and leaves them for root
// in one file of its out directory, named for the grant. Root decides where
// the file goes. A token is written only to that file: never a record, a log
// line or an error.

// grantsMaxBytes bounds the grants root names for a pass, a few lines each.
const grantsMaxBytes = 64 << 10

// TokenHandoff is the file the controller writes for root: one run per
// owner, each with the token minted for it and when GitHub expires it.
type TokenHandoff struct {
	Version int               `json:"version"`
	Runs    []TokenHandoffRun `json:"runs"`
}

type TokenHandoffRun struct {
	Owner        string   `json:"owner"`
	Repositories []string `json:"repositories"`
	Token        string   `json:"token"`
	ExpiresAt    string   `json:"expires_at"`
}

// grantPermissions is a grant's permissions as GitHub's token request names
// them; a name GitHub does not is refused.
func grantPermissions(levels map[string]string) (githubapi.AppPermissions, error) {
	var permissions githubapi.AppPermissions
	encoded, err := json.Marshal(levels)
	if err == nil {
		err = jsonv2.Unmarshal(encoded, &permissions, jsonv2.RejectUnknownMembers(true))
	}
	if err != nil {
		return githubapi.AppPermissions{}, &ProviderError{Message: "a grant names a permission GitHub does not"}
	}
	return permissions, nil
}

// owners is repositories grouped by their owner, in the order first named.
func owners(repositories []string) []runtime.DependencyUpdatesOwner {
	found := []runtime.DependencyUpdatesOwner{}
	index := map[string]int{}
	for _, repository := range repositories {
		owner, _, _ := strings.Cut(repository, "/")
		key := strings.ToLower(owner)
		if _, seen := index[key]; !seen {
			index[key] = len(found)
			found = append(found, runtime.DependencyUpdatesOwner{Owner: owner})
		}
		found[index[key]].Repositories = append(found[index[key]].Repositories, repository)
	}
	return found
}

// handOffGrant mints each owner's token and writes the hand-off for grant,
// answering the owners handed off. An owner the App is not installed on, or
// whose token GitHub refuses, is told to refused and left out; when every
// owner is, nothing is written and the last refusal is the error.
func (r *Registry) handOffGrant(ctx context.Context, c githubConnection, grant string, scope []runtime.DependencyUpdatesOwner, permissions githubapi.AppPermissions, refused func(owner string, err error)) ([]runtime.DependencyUpdatesOwner, error) {
	app, err := r.githubApp(c)
	if err != nil {
		return nil, err
	}
	installations, err := r.githubInstallations(ctx, c)
	if err != nil {
		return nil, err
	}
	covering := map[string]int{}
	for _, installation := range installations {
		if account, err := installation.Account.AsSimpleUser(); err == nil && account.Login != "" {
			covering[strings.ToLower(account.Login)] = installation.ID
		}
	}
	handoff := TokenHandoff{Version: 1, Runs: []TokenHandoffRun{}}
	handed := []runtime.DependencyUpdatesOwner{}
	var last error
	for _, owner := range scope {
		run, err := r.mintFor(ctx, c, app, covering, owner, permissions)
		if err != nil {
			refused(owner.Owner, err)
			last = err
			continue
		}
		handoff.Runs = append(handoff.Runs, run)
		handed = append(handed, owner)
	}
	if len(handoff.Runs) == 0 {
		return nil, last
	}
	if err := writeTokenHandoff(r.Env.Out, grant, handoff); err != nil {
		return nil, err
	}
	return handed, nil
}

// mintFor is one owner's run: a token for exactly its repositories and
// permissions, from the installation on that account.
func (r *Registry) mintFor(ctx context.Context, c githubConnection, app GitHubApp, covering map[string]int, owner runtime.DependencyUpdatesOwner, permissions githubapi.AppPermissions) (TokenHandoffRun, error) {
	installation, ok := covering[strings.ToLower(owner.Owner)]
	if !ok {
		return TokenHandoffRun{}, &ProviderError{Message: "the GitHub App is not installed on " + owner.Owner}
	}
	names := make([]string, len(owner.Repositories))
	for i, repository := range owner.Repositories {
		_, names[i], _ = strings.Cut(repository, "/")
	}
	minted, err := MintGitHubToken(ctx, r.HTTP, r.Now(), app, installation, names, permissions)
	if httpStatus(err) == http.StatusUnprocessableEntity {
		err = r.notInstalledOn(ctx, c, owner, err)
	}
	if err != nil {
		return TokenHandoffRun{}, err
	}
	expires, err := time.Parse(time.RFC3339, minted.ExpiresAt)
	if err != nil {
		return TokenHandoffRun{}, &ProviderError{Message: "GitHub did not say when the token for " + owner.Owner + " expires"}
	}
	return TokenHandoffRun{
		Owner: owner.Owner, Repositories: owner.Repositories, Token: minted.Token, ExpiresAt: expires.UTC().Format(time.RFC3339),
	}, nil
}

// notInstalledOn names the repositories the App's installation on an account
// does not cover, which is why GitHub refuses a token naming them; refused
// is GitHub's answer when every one is covered, since the refusal was then
// for something else.
func (r *Registry) notInstalledOn(ctx context.Context, c githubConnection, owner runtime.DependencyUpdatesOwner, refused error) error {
	missing := []string{}
	for _, repository := range owner.Repositories {
		account, name, _ := strings.Cut(repository, "/")
		if _, err := r.githubInstallation(ctx, c, account, name); httpStatus(err) == http.StatusNotFound {
			missing = append(missing, repository)
		}
	}
	if len(missing) == 0 {
		return refused
	}
	return &ProviderError{Message: "the GitHub App is not installed on " + strings.Join(missing, ", ")}
}

// writeTokenHandoff puts grant's hand-off in dir, readable by its owner
// alone, all at once: root never sees half a file.
func writeTokenHandoff(dir, grant string, handoff TokenHandoff) error {
	data, err := json.Marshal(handoff)
	if err != nil {
		return err
	}
	if err := atomicfile.WriteFile(filepath.Join(dir, grant+".json"), data, 0o600); err != nil {
		return &ProviderError{Message: "the " + grant + " hand-off could not be written", Err: err}
	}
	return nil
}

// grantStep is the step a grant's failure is reported as, on the App's connection.
func grantStep(name string) string { return "github.grant:" + name }

// HandOffGrants mints each grant root says is due this pass and leaves it
// for root. A grant that cannot be handed off is a step this pass could not
// finish, on the App's connection, so HQ shows why until a pass hands it
// off; the rest of the pass runs.
func (r *Registry) HandOffGrants(ctx context.Context) {
	if r.Env.Grants == "" {
		return
	}
	log := r.commands().logger()
	c, err := r.githubConnection("")
	if err != nil {
		log.Warn("grants are due, but this pass holds no GitHub App to mint them with",
			slog.String("event", "controller.grant.skipped"), slog.Any("error", err))
		return
	}
	fail := func(step string, err error) { r.commands().record(step, c.ref, runtime.ReportText(err.Error())) }
	requests, err := readGrantRequests(r.Env.Grants)
	if err != nil {
		fail(grantStep("requests"), err)
		return
	}
	if len(requests) > 0 && r.Env.Out == "" {
		log.Warn("grants are due, but this pass was given no HQ_CONTROLLER_OUT to hand them to root through",
			slog.String("event", "controller.grant.skipped"))
		return
	}
	for _, request := range requests {
		step := grantStep(request.Name)
		scope := owners(request.Repositories)
		if err := checkGrantRequest(request, scope); err != nil {
			fail(step, err)
			continue
		}
		permissions, err := grantPermissions(request.Permissions)
		if err != nil {
			fail(step, err)
			continue
		}
		if _, err := r.handOffGrant(ctx, c, request.Name, scope, permissions, func(string, error) {}); err != nil {
			fail(step, err)
		}
	}
}

// checkGrantRequest refuses a grant this controller would not mint: the
// dependency run's name, which only HQ's plan mints, or a repository that
// is not GitHub's.
func checkGrantRequest(request host.GrantRequest, scope []runtime.DependencyUpdatesOwner) error {
	if request.Name == host.DependencyUpdates.Name || !host.GrantNamed.MatchString(request.Name) {
		return &ProviderError{Message: fmt.Sprintf("%q is not a grant root may name", request.Name)}
	}
	if len(scope) != 1 {
		return &ProviderError{Message: "a grant names one account's repositories"}
	}
	return checkRunRepositories(scope[0].Owner, scope[0].Repositories)
}

// errGrantRequests is a grants file that is not root's account of what is due.
var errGrantRequests = &ProviderError{Message: "the grants root named are not a grant list"}

// readGrantRequests is what root says is due at path, or nothing when root
// named no file. Anything but the exact shape is refused.
func readGrantRequests(path string) ([]host.GrantRequest, error) {
	if path == "" {
		return nil, nil
	}
	file, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, &ProviderError{Message: "the grants root named cannot be read", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, grantsMaxBytes+1))
	if err != nil || len(data) > grantsMaxBytes {
		return nil, errGrantRequests
	}
	var requests host.GrantRequests
	if err := jsonv2.Unmarshal(data, &requests, jsonv2.RejectUnknownMembers(true)); err != nil || requests.Version != 1 {
		return nil, errGrantRequests
	}
	return requests.Grants, nil
}
