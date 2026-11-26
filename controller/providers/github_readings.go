package providers

import (
	"cmp"
	"context"
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

// Page sizes, each the number of items a record keeps or reads.
const (
	githubRunsPage        = 50
	githubPullsPage       = 30
	githubDeploymentsPage = 20
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
	// FailingRuns is each workflow's newest run on the default branch where
	// it failed, at whatever commit (branchChecks).
	FailingRuns       []GitHubRun                                `json:"failing_runs"`
	PullRequests      []GitHubPull                               `json:"pull_requests"`
	PullRequestChecks []string                                   `json:"pull_request_checks"`
	Runs              []GitHubRun                                `json:"runs"`
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
	// Compose is each app's Compose files on the default branch and Dashboard
	// the open Dependency Dashboard's address, read for a repository opted in
	// to updates (github_compose.go).
	Compose   []string `json:"compose"`
	Dashboard string   `json:"dashboard"`
	// Updater is the login HQ's dependency update run opens its pull
	// requests and its Dashboard as: the App's bot (githubBot).
	Updater string `json:"updater"`
	// ComposeImages is each service's image those files pin at the head, and
	// HeldBranches each branch the Dashboard holds with no pull request.
	ComposeImages []GitHubComposeImage `json:"compose_images"`
	HeldBranches  []GitHubHeldBranch   `json:"held_branches"`
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

type GitHubPull struct {
	Number    int      `json:"number"`
	Title     string   `json:"title"`
	URL       string   `json:"url"`
	Author    string   `json:"author"`
	Draft     bool     `json:"draft"`
	CreatedAt string   `json:"created_at"`
	UpdatedAt string   `json:"updated_at"`
	Head      string   `json:"head"`
	Base      string   `json:"base"`
	Branch    string   `json:"branch"`
	Labels    []string `json:"labels"`
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
}

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

// githubRepositories reads every repository the installations cover, a few at
// once, each in full only when GitHub's change query says it moved since the
// record HQ holds, or HQ's clock says its full read is due; the rest are
// carried. A repository that cannot be read is refused alone, and carried where
// HQ holds it; every repository refused is a refused read.
func (r *Registry) githubRepositories(ctx context.Context) ([]any, error) {
	c, err := r.githubConnection("")
	if err != nil {
		return nil, err
	}
	names, err := r.githubInstallationRepositories(ctx, c)
	if err != nil {
		return nil, err
	}
	held := map[string]runtime.GitHubRepositoryHeld{}
	order := map[string]int{}
	for i, repository := range r.Repositories.Held {
		held[repository.Repository], order[repository.Repository] = repository, i
	}
	// A query that failed leaves every repository unknown, so each is read
	// in full, as far as the fuse allows.
	fingerprints, _ := r.githubFingerprints(ctx, c, names)
	updater := sync.OnceValues(func() (string, error) { return r.githubBot(ctx, c) })
	records := make([]any, len(names))
	failures := make([]error, len(names))
	read := func(i int) {
		if failures[i] = ctx.Err(); failures[i] != nil {
			return
		}
		g := r.githubRepository(ctx, c, names[i])
		g.updater = updater
		record, err := g.read(ctx)
		record.Fingerprint, record.ReadAt = fingerprints[names[i]], r.Now().UTC().Format(time.RFC3339)
		records[i], failures[i] = record, err
	}
	var group errgroup.Group
	group.SetLimit(githubReadConcurrency)
	due := []int{}
	for i, name := range names {
		standing, isHeld := held[name]
		if fingerprint := fingerprints[name]; !isHeld || fingerprint == "" || fingerprint != standing.Fingerprint {
			group.Go(func() error { read(i); return nil })
			continue
		}
		records[i] = githubCarried{ConnectionRef: c.ref, Repository: name, Carried: true}
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
		return nil, err
	}
	found := []any{}
	var refused []error
	for i, name := range names {
		if failures[i] != nil {
			refuse(ctx, runtime.PartWhole, c.ref, name, failures[i])
			if _, isHeld := held[name]; isHeld {
				found = append(found, githubCarried{ConnectionRef: c.ref, Repository: name, Carried: true})
				continue
			}
			refused = append(refused, failures[i])
			continue
		}
		found = append(found, records[i])
	}
	if len(names) > 0 && len(refused) == len(names) {
		return nil, fmt.Errorf("every repository refused its reading: %w", refused[0])
	}
	return found, nil
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
	// updater is githubBot, read once: a sweep shares one across every
	// repository it reads.
	updater func() (string, error)
	// token is the one api sends, for a request outside the generated client.
	token string
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

// part reads one refusable part: what it read, or ok false with the refusal reported.
func part[T any](ctx context.Context, g *githubRepository, name runtime.ReadingPartName, read func() (T, error)) (T, bool) {
	found, err := read()
	if err != nil {
		refuse(ctx, name, g.c.ref, g.name, asRepositoryRefusal(err))
		var zero T
		return zero, false
	}
	return found, true
}

// asRepositoryRefusal: every read runs under a token minted with all of
// githubRead, and GitHub mints none for a permission the installation lacks,
// so a 403 is a feature the repository does not offer, never a permission to grant.
// A part is read from a repository the token already reached, so its 404 is the
// same: GitHub answers a feature that is off (code scanning never set up) as not found.
func asRepositoryRefusal(err error) error {
	failure, _, _ := runtime.Classify(err)
	answered, _ := errors.AsType[*ProviderError](err)
	if failure != runtime.FailureClassPermission && (answered == nil || answered.HTTPStatus != http.StatusNotFound) {
		return err
	}
	return &ProviderError{Message: "not available on this repository (its plan or settings leave it off)"}
}

func (g *githubRepository) read(ctx context.Context) (GitHubRepositoryRecord, error) {
	reply, err := g.api.ReposGetWithResponse(ctx, g.owner, g.repo)
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	repo := deref(reply.JSON200)
	branch := orDefault(repo.DefaultBranch, "main")
	commit, err := json200(g.api.ReposGetCommitWithResponse(ctx, g.owner, g.repo, branch))
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	runs, err := json200(g.api.ActionsListWorkflowRunsForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListWorkflowRunsForRepoParams{PerPage: new(githubRunsPage)}))
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	record := GitHubRepositoryRecord{
		ConnectionRef: g.c.ref, Repository: g.name, Private: repo.Private, Archived: repo.Archived, URL: repo.HTMLURL,
		DefaultBranch: branch, PushedAt: repo.PushedAt, Head: githubHead(commit), Compose: []string{},
		ComposeImages: []GitHubComposeImage{}, HeldBranches: []GitHubHeldBranch{}, Builds: []GitHubBuild{},
	}
	if uses, ok := part(ctx, g, runtime.PartWorkflowPins, func() (githubUsesFound, error) { return g.workflowUses(ctx, branch) }); ok {
		record.Pins, record.CalledWorkflows = uses.pins, uses.called
	}
	// layout is nil where the apps are not read, so no update's images are.
	var layout []string
	if g.optedIn() {
		if apps, ok := part(ctx, g, runtime.PartCompose, func() (githubLayout, error) { return g.apps(ctx, branch, commit.Sha) }); ok {
			record.Compose, record.Dashboard, record.Updater, layout = apps.compose, apps.dashboard, apps.updater, apps.compose
			record.ComposeImages, record.HeldBranches, record.Builds = apps.images, apps.held, apps.builds
		}
	}
	workflows, err := json200(g.api.ActionsListRepoWorkflowsWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListRepoWorkflowsParams{PerPage: new(githubListPage)}))
	if err != nil {
		return record, err
	}
	onBranch, read, err := g.branchRuns(ctx, branch, workflows.Workflows)
	if err != nil {
		return record, err
	}
	githubs := githubOwnSuites(slices.Concat(runs.WorkflowRuns, read))
	if record.Checks, err = g.branchChecks(ctx, commit.Sha, onBranch, githubs); err != nil {
		return record, err
	}
	record.FailingRuns = []GitHubRun{}
	for _, run := range onBranch {
		if githubFailed[run.Conclusion] {
			record.FailingRuns = append(record.FailingRuns, githubRunOf(run))
		}
	}
	pulls, err := json200(g.api.PullsListWithResponse(ctx, g.owner, g.repo, &githubapi.PullsListParams{State: new(githubapi.PullsListParamsStateOpen), PerPage: new(githubPullsPage)}))
	if err != nil {
		return record, err
	}
	record.PullRequests = githubPulls(pulls)
	if record.PullRequestChecks, err = g.pullChecks(ctx, record.PullRequests, githubs, githubWorkflowSuites(slices.Concat(runs.WorkflowRuns, read)), layout); err != nil {
		return record, err
	}
	record.Runs = latestRuns(runs.WorkflowRuns, workflows.Workflows)
	if record.Waiting, err = g.waiting(ctx, runs.WorkflowRuns); err != nil {
		return record, err
	}
	releases, err := json200(g.api.ReposListReleasesWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListReleasesParams{PerPage: new(1)}))
	if err != nil {
		return record, err
	}
	if len(releases) > 0 {
		record.Release = &GitHubRelease{Tag: releases[0].TagName, URL: releases[0].HTMLURL, PublishedAt: releases[0].PublishedAt}
	}
	if record.Deployments, err = g.deployments(ctx); err != nil {
		return record, err
	}
	record.Alerts, record.DependabotAlerts = g.alerts(ctx)
	artifacts, err := json200(g.api.ActionsListArtifactsForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.ActionsListArtifactsForRepoParams{PerPage: new(githubRunsPage)}))
	if err != nil {
		return record, err
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
		environments, err := githubEnvironments(found.Environments)
		if err != nil {
			return nil, err
		}
		return environments, g.hqRules(ctx, environments)
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
		return record, err
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
	return record, nil
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
// of a pull request or of GitHub's own is not the branch's. Where the page is
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
			if _, seen := newest[run.WorkflowID]; !seen && run.Event != "dynamic" && !githubPullRequestEvents[run.Event] {
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

func githubPulls(pulls []githubapi.PullRequestSimple) []GitHubPull {
	found := []GitHubPull{}
	for _, pull := range pulls {
		labels := []string{}
		for _, label := range pull.Labels {
			labels = append(labels, label.Name)
		}
		found = append(found, GitHubPull{
			Number: pull.Number, Title: pull.Title, URL: pull.HTMLURL, Author: pull.User.Login,
			Draft: pull.Draft, CreatedAt: pull.CreatedAt, UpdatedAt: pull.UpdatedAt, Head: pull.Head.Sha, Base: pull.Base.Sha,
			Branch: pull.Head.Ref, Labels: labels, HeadRepository: pull.Head.Repo.FullName,
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

// latestRuns is the newest run of each workflow that exists now, under its
// name now. A run keeps the name its workflow had when it ran, so runs are
// grouped by workflow id and named by the repository's active workflows.
// GitHub's own dynamic runs are named per run and are not the repository's.
func latestRuns(runs []githubapi.WorkflowRun, workflows []githubapi.Workflow) []GitHubRun {
	current := map[string]string{}
	for _, workflow := range workflows {
		if workflow.State == githubapi.WorkflowStateActive {
			current["id:"+strconv.Itoa(workflow.ID)] = workflow.Name
		}
	}
	seen := map[string]bool{}
	found := []GitHubRun{}
	for _, run := range runs {
		if run.Event == "dynamic" {
			continue
		}
		key := "name:" + run.Name
		if run.WorkflowID != 0 {
			key = "id:" + strconv.Itoa(run.WorkflowID)
		}
		name, active := current[key]
		if seen[key] || (len(current) > 0 && !active) {
			continue
		}
		seen[key] = true
		record := githubRunOf(run)
		if active {
			record.Name = name
		}
		found = append(found, record)
	}
	return found
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

// hqRules marks each environment where this connection's App is an enabled
// custom deployment protection rule. GitHub lists custom rules per
// environment, apart from the built-in ones the environments list carries.
func (g *githubRepository) hqRules(ctx context.Context, environments []GitHubEnvironment) error {
	for i := range environments {
		found, err := json200(g.api.ReposGetAllDeploymentProtectionRulesWithResponse(ctx, g.owner, g.repo, environments[i].Name))
		if err != nil {
			return err
		}
		environments[i].HQRule = slices.ContainsFunc(found.CustomDeploymentProtectionRules, func(rule githubapi.DeploymentProtectionRule) bool {
			return rule.Enabled && strconv.Itoa(rule.App.ID) == g.c.appID
		})
	}
	return nil
}

// deployments is the newest deployment to each environment, with its latest status.
func (g *githubRepository) deployments(ctx context.Context) ([]GitHubDeployment, error) {
	listed, err := json200(g.api.ReposListDeploymentsWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListDeploymentsParams{PerPage: new(githubDeploymentsPage)}))
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	found := []GitHubDeployment{}
	for _, deployment := range listed {
		if seen[deployment.Environment] {
			continue
		}
		seen[deployment.Environment] = true
		statuses, err := json200(g.api.ReposListDeploymentStatusesWithResponse(ctx, g.owner, g.repo, strconv.Itoa(deployment.ID), &githubapi.ReposListDeploymentStatusesParams{PerPage: new(1)}))
		if err != nil {
			return nil, err
		}
		record := GitHubDeployment{Environment: deployment.Environment, SHA: deployment.Sha, CreatedAt: deployment.CreatedAt, Verified: []GitHubVerification{}}
		if len(statuses) > 0 {
			record.State, record.URL = string(statuses[0].State), orDefault(statuses[0].LogURL, statuses[0].TargetURL)
			outcome, declared := host.ReportedOutcome(record.State, statuses[0].Description)
			record.CheckFailed = declared && outcome.CheckFailed
		}
		if record.Verified, err = g.verifications(ctx, record.URL); err != nil {
			return nil, err
		}
		found = append(found, record)
	}
	return found, nil
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
