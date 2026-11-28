package providers

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What the GitHub App reads about each repository its installations cover:
// one record per repository, every call under a token scoped to that
// repository alone with read permissions only.

// githubRead is every permission a reading's token carries; the app may hold
// more, and a reading asks for none of it.
var githubRead = githubapi.AppPermissions{
	Metadata:             githubapi.AppPermissionsMetadataRead,
	Contents:             githubapi.AppPermissionsContentsRead,
	Checks:               githubapi.AppPermissionsChecksRead,
	Issues:               githubapi.AppPermissionsIssuesRead,
	PullRequests:         githubapi.AppPermissionsPullRequestsRead,
	Actions:              githubapi.AppPermissionsActionsRead,
	Deployments:          githubapi.AppPermissionsDeploymentsRead,
	SecurityEvents:       githubapi.AppPermissionsSecurityEventsRead,
	VulnerabilityAlerts:  githubapi.AppPermissionsVulnerabilityAlertsRead,
	SecretScanningAlerts: githubapi.AppPermissionsSecretScanningAlertsRead,
	Administration:       githubapi.AppPermissionsAdministrationRead,
	Packages:             githubapi.AppPermissionsPackagesRead,
	ActionsVariables:     githubapi.AppPermissionsActionsVariablesRead,
}

// githubReadConcurrency is how many repositories are read at once: each is a
// couple of dozen requests under a token of its own.
const githubReadConcurrency = 4

// githubProduction is the environment whose deployments are releases, as
// Deploy records them (.github/workflows/deploy.yml): a record keeps the
// last githubReleasesKept of it, and the newest deployment of any other.
const githubProduction = "production"

// Page sizes, each the number of items a record keeps or reads.
const (
	githubRunsPage        = 50
	githubPullsPage       = 30
	githubDeploymentsPage = 20
	githubStatusesPage    = 10
	githubReleasesKept    = 10
	githubListPage        = 100
	githubArtifactsKept   = 20
	githubTagsPage        = 1000
)

var (
	githubFailed  = map[string]bool{"failure": true, "timed_out": true, "action_required": true, "startup_failure": true}
	githubRunning = map[string]bool{"queued": true, "in_progress": true, "requested": true, "pending": true, "waiting": true}
)

// GitHubRepositoryRecord is one repository as the app's read-only token sees it.
// A nil part is one refused; HQ's record says which.
type GitHubRepositoryRecord struct {
	ConnectionRef string       `json:"connection_ref"`
	Repository    string       `json:"repository"`
	Private       bool         `json:"private"`
	Archived      bool         `json:"archived"`
	URL           string       `json:"url"`
	DefaultBranch string       `json:"default_branch"`
	PushedAt      string       `json:"pushed_at"`
	Head          GitHubHead   `json:"head"`
	Checks        GitHubChecks `json:"checks"`
	// PullRequestChecks is the check names the newest pull request runs.
	PullRequestChecks []string                                   `json:"pull_request_checks"`
	Waiting           []GitHubWaitingRun                         `json:"waiting"`
	Release           *GitHubRelease                             `json:"release"`
	Deployments       []GitHubDeployment                         `json:"deployments"`
	Alerts            map[runtime.ReadingPartName]map[string]int `json:"alerts"`
	DependabotAlerts  []GitHubDependabotAlert                    `json:"dependabot_alerts"`
	Artifacts         []GitHubArtifact                           `json:"artifacts"`
	Rules             *GitHubRules                               `json:"rules"`
	Environments      []GitHubEnvironment                        `json:"environments"`
	Runners           []GitHubRunner                             `json:"runners"`
	Images            []GitHubImage                              `json:"images"`
	Access            *GitHubAccess                              `json:"access"`
	Variables         []string                                   `json:"variables"`
	Pins              []GitHubPin                                `json:"pins"`
	CalledWorkflows   []string                                   `json:"called_workflows"`
	// Compose is each app's Compose files on the default branch, read for a
	// repository opted in to updates (github_compose.go).
	Compose []string `json:"compose"`
	// Updater is the login HQ's dependency update run opens its pull
	// requests as: the App's bot (githubBot).
	Updater string `json:"updater"`
	// ComposeImages is each service's image those files pin at the head.
	ComposeImages []GitHubComposeImage `json:"compose_images"`
	// Builds is each app built on its host and the revisions that build what
	// the head does.
	Builds []GitHubBuild `json:"builds"`
	// Fingerprint is what GitHub's change query answered before the read,
	// hashed, and ReadAt when the read was made: what HQ's plan sends back.
	Fingerprint string `json:"fingerprint"`
	ReadAt      string `json:"read_at"`
	// Carried is false on a record read; a carried repository is sent as
	// githubCarried, which names only itself.
	Carried bool `json:"carried"`
}

type GitHubHead struct {
	SHA     string `json:"sha"`
	Message string `json:"message"`
	Author  string `json:"author"`
	Date    string `json:"date"`
	URL     string `json:"url"`
}

// GitHubOpenPullRequestRecord is one open pull request, read in the same
// pass as its repository and carried with it.
type GitHubOpenPullRequestRecord struct {
	ConnectionRef string   `json:"connection_ref"`
	Repository    string   `json:"repository"`
	Number        int      `json:"number"`
	Title         string   `json:"title"`
	URL           string   `json:"url"`
	Author        string   `json:"author"`
	Draft         bool     `json:"draft"`
	CreatedAt     string   `json:"created_at"`
	UpdatedAt     string   `json:"updated_at"`
	Head          string   `json:"head"`
	Base          string   `json:"base"`
	Branch        string   `json:"branch"`
	BaseBranch    string   `json:"base_branch"`
	Labels        []string `json:"labels"`
	// HeadRepository is the repository its head branch lives in, as
	// owner/name: another repository's for a fork, "" once the fork is gone.
	HeadRepository string `json:"head_repository"`
	// Checks is the head commit's checks, Files what it changes (one page of
	// githubPullFilesPage) and ChangedFiles how many files GitHub says it
	// changes, and Apps the images it moves, read for an update's pull
	// request (its branch under githubUpdateBranch) and nil or 0 for any
	// other; Apps is read in a repository opted in to updates.
	Checks       *GitHubChecks     `json:"checks"`
	Files        []GitHubPullFile  `json:"files"`
	ChangedFiles int               `json:"changed_files"`
	Apps         []GitHubAppChange `json:"apps"`
	// ArtifactProblem is what the updater's run said it could not update
	// (githubArtifactProblem), read for an update's pull request whose
	// checks fail.
	ArtifactProblem []string `json:"artifact_problem,omitempty"`
	// MergeableState, ReviewState and CheckRuns are what GitHub's GraphQL
	// says of it (github_pulls.go), left empty where that part is refused.
	MergeableState string           `json:"mergeable_state"`
	ReviewState    string           `json:"review_state"`
	CheckRuns      []GitHubCheckRun `json:"check_runs"`
	// Carried is false on a record read; a carried repository is sent as
	// githubCarried.
	Carried bool `json:"carried"`
}

// GitHubCheckRun is one check as its last run stands, in GitHub's words,
// lower-cased: a workflow run's job, another app's check, or a commit status.
type GitHubCheckRun struct {
	Name       string `json:"name"`
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
	// When it started and finished, and its page, where GitHub says them.
	StartedAt   string `json:"started_at,omitempty"`
	CompletedAt string `json:"completed_at,omitempty"`
	URL         string `json:"url,omitempty"`
}

// GitHubWorkflowRunRecord is a github.workflow_run record: one workflow's
// newest run on one branch, and its jobs, read in the same pass as its
// repository and carried with it.
type GitHubWorkflowRunRecord struct {
	ConnectionRef string `json:"connection_ref"`
	Repository    string `json:"repository"`
	GitHubRun
	Jobs    []GitHubCheckRun `json:"jobs"`
	Carried bool             `json:"carried"`
}

// githubRunBranchesKept is the most branches besides the default one whose
// runs a repository's read keeps: the ones run most lately, each a check-runs
// call for its jobs.
const githubRunBranchesKept = 10

// GitHubPullFile is one file a pull request changes, and App the app whose
// directory holds it (appDirectory), "" for a file outside every app's.
type GitHubPullFile struct {
	Path      string `json:"path"`
	Status    string `json:"status"`
	Additions int    `json:"additions"`
	Deletions int    `json:"deletions"`
	App       string `json:"app"`
}

// githubUpdateBranch is the prefix of the branches Renovate opens its pull
// requests from.
const githubUpdateBranch = "renovate/"

// githubPullFilesPage is the one page of files an update's read keeps.
const githubPullFilesPage = 100

type GitHubRun struct {
	ID         int    `json:"id"`
	Name       string `json:"name"`
	Status     string `json:"status"`
	Conclusion string `json:"conclusion"`
	Event      string `json:"event"`
	Branch     string `json:"branch"`
	SHA        string `json:"sha"`
	URL        string `json:"url"`
	CreatedAt  string `json:"created_at"`
}

// GitHubWaitingRun is a run held for a person's approval, and the
// environments it waits on.
type GitHubWaitingRun struct {
	GitHubRun
	Environments []string `json:"environments"`
}

type GitHubRelease struct {
	Tag         string `json:"tag"`
	URL         string `json:"url"`
	PublishedAt string `json:"published_at"`
}

type GitHubDeployment struct {
	Environment string               `json:"environment"`
	SHA         string               `json:"sha"`
	CreatedAt   string               `json:"created_at"`
	State       string               `json:"state"`
	URL         string               `json:"url"`
	Verified    []GitHubVerification `json:"verified"`
	// CheckFailed is whether its status is an outcome deploy/outcomes.json
	// declares with a check failed, as a release live with one failed is.
	CheckFailed bool `json:"check_failed"`
	// Image is the digest-pinned image its payload names, as Deploy records
	// it: what production was asked to run, which HQ compares with the
	// digest it runs.
	Image string `json:"image,omitempty"`
	// Outcome is the outcome deploy/outcomes.json names that its deploy
	// reported ("live", "not-deployed"), and EndedAt when it reported it;
	// both "" while it reported none.
	Outcome string `json:"outcome,omitempty"`
	EndedAt string `json:"ended_at,omitempty"`
	// ApprovedBy is the login that approved a release's run, as GitHub's
	// reviews of the run record it; "" where no one did.
	ApprovedBy string `json:"approved_by,omitempty"`
}

type GitHubVerification struct {
	Name       string `json:"name"`
	Conclusion string `json:"conclusion"`
}

type GitHubArtifact struct {
	Name      string `json:"name"`
	ExpiresAt string `json:"expires_at"`
	SHA       string `json:"sha"`
}

// githubPass is one read of every repository the installations cover, and
// the records of each kind it is read as: a sweep reads it once, whichever of
// the kinds asks first (cached), and each kind reports its share.
type githubPass struct {
	repositories, runs, pulls []any
	refused                   []runtime.RefusedPart
	err                       error
}

// githubPassReaders are the kinds a repository's read is reported as, each
// with the refused parts that are its own: a repository refused whole is
// refused for every kind, and a part for the kind whose records it fills.
func (r *Registry) githubPassReaders() {
	share := func(kind runtime.ResourceKind, records func(githubPass) []any, owns func(runtime.ReadingPartName) bool) {
		r.reader(kind, func(ctx context.Context) ([]any, error) {
			pass, _ := cached(ctx, r, "github.pass", func() (githubPass, error) { return r.githubRepositories(ctx), nil })
			if ledger, _ := ctx.Value(refusalKey{}).(*refusals); ledger != nil {
				ledger.mu.Lock()
				for _, refused := range pass.refused {
					if refused.Part == runtime.PartWhole || owns(refused.Part) {
						ledger.entries = append(ledger.entries, refused)
					}
				}
				ledger.mu.Unlock()
			}
			if pass.err != nil {
				return nil, pass.err
			}
			return records(pass), nil
		})
	}
	share(runtime.ResourceKindGitHubRepository, func(pass githubPass) []any { return pass.repositories },
		func(part runtime.ReadingPartName) bool { return part != runtime.PartPullStates })
	share(runtime.ResourceKindGitHubWorkflowRun, func(pass githubPass) []any { return pass.runs },
		func(runtime.ReadingPartName) bool { return false })
	share(runtime.ResourceKindGitHubOpenPullRequest, func(pass githubPass) []any { return pass.pulls },
		func(part runtime.ReadingPartName) bool { return part == runtime.PartPullStates })
}

// githubRepositories reads every repository the installations cover, a few at
// once, each in full only when GitHub's change query says it moved since the
// record HQ holds, or HQ's clock says its full read is due; the rest are
// carried, as one record of each kind that names only it. A repository that
// cannot be read is refused alone, and carried where HQ holds it; every
// repository refused is a refused read.
func (r *Registry) githubRepositories(ctx context.Context) githubPass {
	ledger := &refusals{}
	ctx = context.WithValue(ctx, refusalKey{}, ledger)
	pass := r.githubRead(ctx)
	pass.refused = ledger.entries
	return pass
}

func (r *Registry) githubRead(ctx context.Context) githubPass {
	c, err := r.githubConnection("")
	if err != nil {
		return githubPass{err: err}
	}
	names, err := r.githubInstallationRepositories(ctx, c)
	if err != nil {
		return githubPass{err: err}
	}
	held := map[string]runtime.GitHubRepositoryHeld{}
	order := map[string]int{}
	for i, repository := range r.Repositories.Held {
		held[repository.Repository], order[repository.Repository] = repository, i
	}
	// A pass an event asked for reads in full the repositories it names, and
	// any HQ holds no record of, and carries the rest unprobed.
	told := runtime.Told(r.told, runtime.ResourceKindGitHubRepository)
	reads := func(name string) bool { _, isHeld := held[name]; return !isHeld || slices.Contains(told, name) }
	probed := names
	if told != nil {
		probed = slices.DeleteFunc(slices.Clone(names), func(name string) bool { return !reads(name) })
	}
	// A query that failed leaves every repository unknown, so each is read
	// in full, as far as the fuse allows.
	fingerprints, _ := r.githubFingerprints(ctx, c, probed)
	updater := sync.OnceValues(func() (string, error) { return r.githubBot(ctx, c) })
	records := make([]*githubRepositoryRead, len(names))
	failures := make([]error, len(names))
	read := func(i int) {
		if failures[i] = ctx.Err(); failures[i] != nil {
			return
		}
		g := r.githubRepository(ctx, c, names[i])
		g.updater = updater
		found, err := g.read(ctx)
		found.record.Fingerprint, found.record.ReadAt = fingerprints[names[i]], r.Now().UTC().Format(time.RFC3339)
		records[i], failures[i] = &found, err
	}
	var group errgroup.Group
	group.SetLimit(githubReadConcurrency)
	due := []int{}
	for i, name := range names {
		if told != nil {
			if reads(name) {
				group.Go(func() error { read(i); return nil })
			}
			continue
		}
		standing, isHeld := held[name]
		if fingerprint := fingerprints[name]; !isHeld || fingerprint == "" || fingerprint != standing.Fingerprint {
			group.Go(func() error { read(i); return nil })
			continue
		}
		if standing.Due {
			due = append(due, i)
		}
	}
	_ = group.Wait()
	// One unchanged but due is read after what moved, one at a time in the
	// plan's order, while the round has spent under half its ceiling; the rest
	// stay carried and due, leaving room for the reconciles after the sweep.
	slices.SortFunc(due, func(a, b int) int { return cmp.Compare(order[names[a]], order[names[b]]) })
	for _, i := range due {
		if r.GitHubSpent() >= githubSweepCeiling/2 {
			break
		}
		read(i)
	}
	if err := ctx.Err(); err != nil {
		return githubPass{err: err}
	}
	pass := githubPass{repositories: []any{}, runs: []any{}, pulls: []any{}}
	carry := func(name string) {
		carried := githubCarried{ConnectionRef: c.ref, Repository: name, Carried: true}
		pass.repositories, pass.runs, pass.pulls = append(pass.repositories, carried), append(pass.runs, carried), append(pass.pulls, carried)
	}
	var refused []error
	for i, name := range names {
		switch {
		case failures[i] != nil:
			refuse(ctx, runtime.PartWhole, c.ref, name, failures[i])
			if _, isHeld := held[name]; isHeld {
				carry(name)
				continue
			}
			refused = append(refused, failures[i])
		case records[i] == nil:
			carry(name)
		default:
			pass.repositories = append(pass.repositories, records[i].record)
			for _, run := range records[i].runs {
				pass.runs = append(pass.runs, run)
			}
			for _, pull := range records[i].pulls {
				pass.pulls = append(pass.pulls, pull)
			}
		}
	}
	if len(names) > 0 && len(refused) == len(names) {
		return githubPass{err: fmt.Errorf("every repository refused its reading: %w", refused[0])}
	}
	return pass
}

// githubCarried is a repository sent in place of its record: HQ keeps the one
// it holds.
type githubCarried struct {
	ConnectionRef string `json:"connection_ref"`
	Repository    string `json:"repository"`
	Carried       bool   `json:"carried"`
}

// githubRepository is one repository being read, through a client under a
// token for it alone that holds githubRead.
type githubRepository struct {
	r           *Registry
	c           githubConnection
	name        string
	owner, repo string
	api         *githubapi.ClientWithResponses
	// archived is what GitHub said of the repository, which leaves its parts unread.
	archived bool
	// updater is githubBot, read once: a sweep shares one across every
	// repository it reads.
	updater func() (string, error)
	// token is the one api sends, for a request outside the generated client.
	token string
	// checksAt is each commit's check runs and suitesAt each check suite's,
	// read once in a repository's read.
	checksAt map[string][]githubapi.CheckRun
	suitesAt map[int][]githubapi.CheckRun
}

// githubRepositoryRead is what one repository's read reports: its record, its
// workflows' runs and its open pull requests.
type githubRepositoryRead struct {
	record GitHubRepositoryRecord
	runs   []GitHubWorkflowRunRecord
	pulls  []GitHubOpenPullRequestRecord
}

func (r *Registry) githubRepository(ctx context.Context, c githubConnection, name string) *githubRepository {
	g := &githubRepository{r: r, c: c, name: name, updater: sync.OnceValues(func() (string, error) { return r.githubBot(ctx, c) })}
	var err error
	if g.owner, g.repo, err = githubRepositoryName(name); err != nil {
		g.api = r.githubClient("", false, err)
	} else {
		g.token, err = r.githubToken(ctx, c, []string{name}, githubRead)
		g.api = r.githubClient(g.token, true, err)
	}
	return g
}

// part reads one refusable part: what it read, or ok false with the refusal
// reported. An archived repository's parts are not read: GitHub keeps it
// read-only and scans it no more, and HQ measures nothing of it.
func part[T any](ctx context.Context, g *githubRepository, name runtime.ReadingPartName, read func() (T, error)) (T, bool) {
	var zero T
	if g.archived {
		return zero, false
	}
	found, err := read()
	if err != nil {
		refuse(ctx, name, g.c.ref, g.name, asRepositoryRefusal(err))
		return zero, false
	}
	return found, true
}

// asRepositoryRefusal: every read runs under a token minted with all of
// githubRead, and GitHub mints none for a permission the installation lacks,
// so a 403 is a feature the repository does not offer, never a permission to grant.
// A part is read from a repository the token already reached, so its 404 is the
// same: GitHub answers a feature that is off (code scanning never set up) as not found.
// GitHub's own words say which (a plan without it, a setting left off); an
// answer with none is said as either.
func asRepositoryRefusal(err error) error {
	failure, _, _ := runtime.Classify(err)
	answered, _ := errors.AsType[*ProviderError](err)
	if failure != runtime.FailureClassPermission && (answered == nil || answered.HTTPStatus != http.StatusNotFound) {
		return err
	}
	if answered != nil {
		if said := githubSaid(answered.Body); said != "" {
			return &ProviderError{Message: said}
		}
	}
	return &ProviderError{Message: "not available on this repository (its plan or settings leave it off)"}
}

func (g *githubRepository) read(ctx context.Context) (githubRepositoryRead, error) {
	found := githubRepositoryRead{}
	var err error
	found.record, found.runs, found.pulls, err = g.readAll(ctx)
	return found, err
}

func (g *githubRepository) readAll(ctx context.Context) (GitHubRepositoryRecord, []GitHubWorkflowRunRecord, []GitHubOpenPullRequestRecord, error) {
	record, runs, pulls := GitHubRepositoryRecord{}, []GitHubWorkflowRunRecord{}, []GitHubOpenPullRequestRecord{}
	reply, err := g.api.ReposGetWithResponse(ctx, g.owner, g.repo)
	if err != nil {
		return record, runs, pulls, err
	}
	repo := deref(reply.JSON200)
	g.archived = repo.Archived
	branch := orDefault(repo.DefaultBranch, "main")
	commit, err := json200(g.api.ReposGetCommitWithResponse(ctx, g.owner, g.repo, branch))
	if err != nil {
		return record, runs, pulls, err
	}
	page, err := json200(g.api.ActionsListWorkflowRunsForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListWorkflowRunsForRepoParams{PerPage: new(githubRunsPage)}))
	if err != nil {
		return record, runs, pulls, err
	}
	record = GitHubRepositoryRecord{
		ConnectionRef: g.c.ref, Repository: g.name, Private: repo.Private, Archived: repo.Archived, URL: repo.HTMLURL,
		DefaultBranch: branch, PushedAt: repo.PushedAt, Head: githubHead(commit), Compose: []string{},
		ComposeImages: []GitHubComposeImage{}, Builds: []GitHubBuild{},
	}
	if uses, ok := part(ctx, g, runtime.PartWorkflowPins, func() (githubUsesFound, error) { return g.workflowUses(ctx, branch) }); ok {
		record.Pins, record.CalledWorkflows = uses.pins, uses.called
	}
	// layout is nil where the apps are not read, so no update's images are.
	var layout []string
	if g.optedIn() {
		if apps, ok := part(ctx, g, runtime.PartCompose, func() (githubLayout, error) { return g.apps(ctx, branch, commit.Sha) }); ok {
			record.Compose, record.Updater, layout = apps.compose, apps.updater, apps.compose
			record.ComposeImages, record.Builds = apps.images, apps.builds
		}
	}
	workflows, err := json200(g.api.ActionsListRepoWorkflowsWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListRepoWorkflowsParams{PerPage: new(githubListPage)}))
	if err != nil {
		return record, runs, pulls, err
	}
	onBranch, read, err := g.branchRuns(ctx, branch, workflows.Workflows)
	if err != nil {
		return record, runs, pulls, err
	}
	aside := githubAsideSuites(slices.Concat(page.WorkflowRuns, read))
	if record.Checks, err = g.branchChecks(ctx, commit.Sha, onBranch, aside); err != nil {
		return record, runs, pulls, err
	}
	if runs, err = g.workflowRuns(ctx, branch, onBranch, page.WorkflowRuns, workflows.Workflows); err != nil {
		return record, runs, pulls, err
	}
	listed, err := json200(g.api.PullsListWithResponse(ctx, g.owner, g.repo, &githubapi.PullsListParams{State: new(githubapi.PullsListParamsStateOpen), PerPage: new(githubPullsPage)}))
	if err != nil {
		return record, runs, pulls, err
	}
	pulls = githubPulls(g.c.ref, g.name, listed)
	if record.PullRequestChecks, err = g.pullChecks(ctx, pulls, aside, githubWorkflowSuites(slices.Concat(page.WorkflowRuns, read)), layout); err != nil {
		return record, runs, pulls, err
	}
	g.pullStates(ctx, pulls)
	if record.Waiting, err = g.waiting(ctx, page.WorkflowRuns); err != nil {
		return record, runs, pulls, err
	}
	releases, err := json200(g.api.ReposListReleasesWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListReleasesParams{PerPage: new(1)}))
	if err != nil {
		return record, runs, pulls, err
	}
	if len(releases) > 0 {
		record.Release = &GitHubRelease{Tag: releases[0].TagName, URL: releases[0].HTMLURL, PublishedAt: releases[0].PublishedAt}
	}
	if record.Deployments, err = g.deployments(ctx); err != nil {
		return record, runs, pulls, err
	}
	record.Alerts, record.DependabotAlerts = g.alerts(ctx)
	artifacts, err := json200(g.api.ActionsListArtifactsForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListArtifactsForRepoParams{PerPage: new(githubRunsPage)}))
	if err != nil {
		return record, runs, pulls, err
	}
	record.Artifacts = liveArtifacts(artifacts.Artifacts)
	if rules, ok := part(ctx, g, runtime.PartBranchRules, func() (GitHubRules, error) {
		found, err := json200(g.api.ReposGetBranchRulesWithResponse(ctx, g.owner, g.repo, branch))
		if err != nil {
			return GitHubRules{}, err
		}
		return branchRules(found)
	}); ok {
		record.Rules = &rules
	}
	record.Environments, _ = part(ctx, g, runtime.PartEnvironments, func() ([]GitHubEnvironment, error) {
		found, err := json200(g.api.ReposGetAllEnvironmentsWithResponse(ctx, g.owner, g.repo))
		if err != nil {
			return nil, err
		}
		return githubEnvironments(found.Environments)
	})
	if record.Environments == nil {
		record.Environments = []GitHubEnvironment{}
	}
	record.Runners, _ = part(ctx, g, runtime.PartRunners, func() ([]GitHubRunner, error) {
		found, err := json200(g.api.ActionsListSelfHostedRunnersForRepoWithResponse(ctx, g.owner, g.repo))
		return githubRunners(found.Runners), err
	})
	if record.Runners == nil {
		record.Runners = []GitHubRunner{}
	}
	if record.Images, err = g.images(ctx); err != nil {
		return record, runs, pulls, err
	}
	if access, ok := part(ctx, g, runtime.PartAccess, func() (GitHubAccess, error) { return g.access(ctx, repo, reply.Body) }); ok {
		record.Access = &access
	}
	record.Variables, _ = part(ctx, g, runtime.PartVariables, func() ([]string, error) {
		found, err := json200(g.api.ActionsListRepoVariablesWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListRepoVariablesParams{PerPage: new(githubListPage)}))
		names := []string{}
		for _, variable := range found.Variables {
			names = append(names, variable.Name)
		}
		return names, err
	})
	return record, runs, pulls, nil
}

func githubHead(commit githubapi.Commit) GitHubHead {
	message, _, _ := strings.Cut(commit.Commit.Message, "\n")
	author := ""
	if user, err := commit.Author.AsSimpleUser(); err == nil {
		author = user.Login
	}
	date := commit.Commit.Committer.Date
	if date == "" {
		date = commit.Commit.Author.Date
	}
	return GitHubHead{
		SHA: commit.Sha, Message: strings.TrimRight(message, "\r"), Author: orDefault(author, commit.Commit.Author.Name),
		Date: date, URL: commit.HTMLURL,
	}
}

// githubPullRequestEvents are the events whose runs are a pull request's,
// whatever its head branch is called.
var githubPullRequestEvents = map[string]bool{"pull_request": true, "pull_request_target": true}

// branchRuns is the newest run on branch of each active workflow, named as the
// workflow is now, and the page of the branch's runs it read them from. A run
// of a pull request, or one githubAside sets apart, is not the branch's. Where the page is
// full, a workflow it misses (a weekly schedule behind a busy week) is read
// alone.
func (g *githubRepository) branchRuns(ctx context.Context, branch string, workflows []githubapi.Workflow) ([]githubapi.WorkflowRun, []githubapi.WorkflowRun, error) {
	page, err := json200(g.api.ActionsListWorkflowRunsForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListWorkflowRunsForRepoParams{Branch: &branch, PerPage: new(githubListPage)}))
	if err != nil {
		return nil, nil, err
	}
	newest := map[int]githubapi.WorkflowRun{}
	take := func(runs []githubapi.WorkflowRun) {
		for _, run := range runs {
			if _, seen := newest[run.WorkflowID]; !seen && !githubAside(run) && !githubPullRequestEvents[run.Event] {
				newest[run.WorkflowID] = run
			}
		}
	}
	take(page.WorkflowRuns)
	found := []githubapi.WorkflowRun{}
	for _, workflow := range workflows {
		if workflow.State != githubapi.WorkflowStateActive {
			continue
		}
		run, seen := newest[workflow.ID]
		if !seen && len(page.WorkflowRuns) == githubListPage {
			alone, err := json200(g.api.ActionsListWorkflowRunsWithResponse(ctx, g.owner, g.repo, strconv.Itoa(workflow.ID), &githubapi.ActionsListWorkflowRunsParams{Branch: &branch, PerPage: new(githubRunsPage)}))
			if err != nil {
				return nil, nil, err
			}
			take(alone.WorkflowRuns)
			run, seen = newest[workflow.ID]
		}
		if seen {
			run.Name = workflow.Name
			found = append(found, run)
		}
	}
	return found, page.WorkflowRuns, nil
}

func githubPulls(ref, repository string, pulls []githubapi.PullRequestSimple) []GitHubOpenPullRequestRecord {
	found := []GitHubOpenPullRequestRecord{}
	for _, pull := range pulls {
		labels := []string{}
		for _, label := range pull.Labels {
			labels = append(labels, label.Name)
		}
		found = append(found, GitHubOpenPullRequestRecord{
			ConnectionRef: ref, Repository: repository,
			Number: pull.Number, Title: pull.Title, URL: pull.HTMLURL, Author: pull.User.Login,
			Draft: pull.Draft, CreatedAt: pull.CreatedAt, UpdatedAt: pull.UpdatedAt, Head: pull.Head.Sha, Base: pull.Base.Sha,
			Branch: pull.Head.Ref, BaseBranch: pull.Base.Ref, Labels: labels, HeadRepository: pull.Head.Repo.FullName,
			CheckRuns: []GitHubCheckRun{},
		})
	}
	return found
}

func githubRunOf(run githubapi.WorkflowRun) GitHubRun {
	return GitHubRun{
		ID: run.ID, Name: run.Name, Status: run.Status, Conclusion: run.Conclusion, Event: run.Event,
		Branch: run.HeadBranch, SHA: run.HeadSha, URL: run.HTMLURL, CreatedAt: run.CreatedAt,
	}
}

// workflowRuns is each workflow's newest run on the default branch (onBranch,
// as branchRuns read them) and on each other branch the page holds of the
// githubRunBranchesKept run most lately, under the workflow's name now, with
// each run's jobs (jobsOf). A run keeps the
// name its workflow had when it ran, so runs are grouped by workflow id and
// named by the repository's active workflows. GitHub's own dynamic runs are
// named per run and are not the repository's.
func (g *githubRepository) workflowRuns(ctx context.Context, branch string, onBranch, page []githubapi.WorkflowRun, workflows []githubapi.Workflow) ([]GitHubWorkflowRunRecord, error) {
	current := map[string]string{}
	for _, workflow := range workflows {
		if workflow.State == githubapi.WorkflowStateActive {
			current["id:"+strconv.Itoa(workflow.ID)] = workflow.Name
		}
	}
	chosen := slices.Clone(onBranch)
	seen, branches := map[string]bool{}, map[string]bool{}
	for _, run := range page {
		if githubAside(run) || run.HeadBranch == "" || run.HeadBranch == branch {
			continue
		}
		key := "name:" + run.Name
		if run.WorkflowID != 0 {
			key = "id:" + strconv.Itoa(run.WorkflowID)
		}
		name, active := current[key]
		if seen[run.HeadBranch+"\x00"+key] || (len(current) > 0 && !active) || (!branches[run.HeadBranch] && len(branches) == githubRunBranchesKept) {
			continue
		}
		seen[run.HeadBranch+"\x00"+key], branches[run.HeadBranch] = true, true
		if active {
			run.Name = name
		}
		chosen = append(chosen, run)
	}
	found := []GitHubWorkflowRunRecord{}
	for _, run := range chosen {
		jobs, err := g.jobsOf(ctx, run)
		if err != nil {
			return nil, err
		}
		found = append(found, GitHubWorkflowRunRecord{ConnectionRef: g.c.ref, Repository: g.name, GitHubRun: githubRunOf(run), Jobs: jobs})
	}
	return found, nil
}

func (g *githubRepository) waiting(ctx context.Context, runs []githubapi.WorkflowRun) ([]GitHubWaitingRun, error) {
	found := []GitHubWaitingRun{}
	for _, run := range runs {
		if run.Status != "waiting" {
			continue
		}
		pending, err := json200(g.api.ActionsGetPendingDeploymentsForRunWithResponse(ctx, g.owner, g.repo, strconv.Itoa(run.ID)))
		if err != nil {
			return nil, err
		}
		environments := map[string]bool{}
		for _, deployment := range pending {
			if deployment.Environment.Name != "" {
				environments[deployment.Environment.Name] = true
			}
		}
		found = append(found, GitHubWaitingRun{GitHubRun: githubRunOf(run), Environments: sortedKeys(environments)})
	}
	return found, nil
}

// deployments is the newest deployment to each environment, and production's
// last githubReleasesKept, newest first: GitHub lists them so. Each carries
// its latest status, and the outcome its deploy reported.
func (g *githubRepository) deployments(ctx context.Context) ([]GitHubDeployment, error) {
	listed, err := json200(g.api.ReposListDeploymentsWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListDeploymentsParams{PerPage: new(githubDeploymentsPage)}))
	if err != nil {
		return nil, err
	}
	kept := map[string]int{}
	found := []GitHubDeployment{}
	for _, deployment := range listed {
		releases := deployment.Environment == githubProduction
		if kept[deployment.Environment] > 0 && (!releases || kept[deployment.Environment] >= githubReleasesKept) {
			continue
		}
		kept[deployment.Environment]++
		record, err := g.deployment(ctx, deployment, releases)
		if err != nil {
			return nil, err
		}
		found = append(found, record)
	}
	return found, nil
}

// deployment is one deployment with its latest status, the outcome deploy/
// outcomes.json declares that its newest status naming one reports, what its
// job verified and, for a release, who approved its run. A release keeps its
// outcome once a newer one goes live and GitHub marks it inactive.
func (g *githubRepository) deployment(ctx context.Context, deployment githubapi.Deployment, release bool) (GitHubDeployment, error) {
	record := GitHubDeployment{Environment: deployment.Environment, SHA: deployment.Sha, CreatedAt: deployment.CreatedAt, Image: payloadImage(deployment.Payload), Verified: []GitHubVerification{}}
	statuses, err := json200(g.api.ReposListDeploymentStatusesWithResponse(ctx, g.owner, g.repo, strconv.Itoa(deployment.ID), &githubapi.ReposListDeploymentStatusesParams{PerPage: new(githubStatusesPage)}))
	if err != nil {
		return record, err
	}
	if len(statuses) > 0 {
		record.State, record.URL = string(statuses[0].State), orDefault(statuses[0].LogURL, statuses[0].TargetURL)
	}
	for _, status := range statuses {
		if outcome, declared := host.ReportedOutcome(string(status.State), status.Description); declared {
			record.Outcome, record.EndedAt, record.CheckFailed = outcome.Name, status.CreatedAt, outcome.CheckFailed
			break
		}
	}
	if record.Verified, err = g.verifications(ctx, record.URL); err != nil {
		return record, err
	}
	if release {
		record.ApprovedBy, err = g.approver(ctx, record.URL)
	}
	return record, err
}

// approver is who approved the workflow run a deployment's log names, as
// GitHub's reviews of the run record it: "" for a run no one approved, or a
// log that names no run.
func (g *githubRepository) approver(ctx context.Context, link string) (string, error) {
	_, run, found := strings.Cut(link, "/actions/runs/")
	run, _, _ = strings.Cut(run, "/")
	run, _, _ = strings.Cut(run, "?")
	if !found || run == "" || strings.Trim(run, "0123456789") != "" {
		return "", nil
	}
	reviews, err := json200(g.api.ActionsGetReviewsForRunWithResponse(ctx, g.owner, g.repo, run))
	if err != nil {
		return "", err
	}
	approver := ""
	for _, review := range reviews {
		if review.State == githubapi.EnvironmentApprovalsStateApproved {
			approver = review.User.Login
		}
	}
	return approver, nil
}

// payloadImage is the image a deployment's payload names, or "" for a payload
// of another shape: a creator may send any object or a string.
func payloadImage(payload json.RawMessage) string {
	var named struct {
		Image string `json:"image"`
	}
	if json.Unmarshal(payload, &named) != nil {
		return ""
	}
	return named.Image
}

// verifications is what the job that made a deployment verified before it
// deployed: its steps named for verifying. The deployment's own log link names
// the job, so nothing here guesses which run deployed.
func (g *githubRepository) verifications(ctx context.Context, link string) ([]GitHubVerification, error) {
	_, job, found := strings.Cut(link, "/job/")
	job, _, _ = strings.Cut(job, "/")
	job, _, _ = strings.Cut(job, "?")
	found = found && job != "" && strings.Trim(job, "0123456789") == ""
	verified := []GitHubVerification{}
	if !found {
		return verified, nil
	}
	answer, err := json200(g.api.ActionsGetJobForWorkflowRunWithResponse(ctx, g.owner, g.repo, job))
	if err != nil {
		return nil, err
	}
	for _, step := range answer.Steps {
		if strings.HasPrefix(strings.ToLower(step.Name), "verify") {
			verified = append(verified, GitHubVerification{Name: step.Name, Conclusion: step.Conclusion})
		}
	}
	return verified, nil
}

// liveArtifacts is the artifacts that have not expired, soonest to expire first.
func liveArtifacts(artifacts []githubapi.Artifact) []GitHubArtifact {
	live := []GitHubArtifact{}
	for _, artifact := range artifacts {
		if !artifact.Expired && artifact.ExpiresAt != "" {
			live = append(live, GitHubArtifact{Name: artifact.Name, ExpiresAt: artifact.ExpiresAt, SHA: artifact.WorkflowRun.HeadSha})
		}
	}
	slices.SortStableFunc(live, func(a, b GitHubArtifact) int { return cmp.Compare(a.ExpiresAt, b.ExpiresAt) })
	if len(live) > githubArtifactsKept {
		live = live[:githubArtifactsKept]
	}
	return live
}
