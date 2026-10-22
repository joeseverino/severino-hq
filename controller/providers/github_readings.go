package providers

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"

	"golang.org/x/sync/errgroup"

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
	ConnectionRef     string                    `json:"connection_ref"`
	Repository        string                    `json:"repository"`
	Private           bool                      `json:"private"`
	Archived          bool                      `json:"archived"`
	URL               string                    `json:"url"`
	DefaultBranch     string                    `json:"default_branch"`
	PushedAt          string                    `json:"pushed_at"`
	Head              GitHubHead                `json:"head"`
	Checks            GitHubChecks              `json:"checks"`
	PullRequests      []GitHubPull              `json:"pull_requests"`
	PullRequestChecks []string                  `json:"pull_request_checks"`
	Runs              []GitHubRun               `json:"runs"`
	Waiting           []GitHubWaitingRun        `json:"waiting"`
	Release           *GitHubRelease            `json:"release"`
	Deployments       []GitHubDeployment        `json:"deployments"`
	Alerts            map[string]map[string]int `json:"alerts"`
	DependabotAlerts  []GitHubDependabotAlert   `json:"dependabot_alerts"`
	Artifacts         []GitHubArtifact          `json:"artifacts"`
	Rules             *GitHubRules              `json:"rules"`
	Environments      []GitHubEnvironment       `json:"environments"`
	Runners           []GitHubRunner            `json:"runners"`
	Images            []GitHubImage             `json:"images"`
	Access            *GitHubAccess             `json:"access"`
	Variables         []string                  `json:"variables"`
	Pins              []GitHubPin               `json:"pins"`
	CalledWorkflows   []string                  `json:"called_workflows"`
}

type GitHubHead struct {
	SHA     string `json:"sha"`
	Message string `json:"message"`
	Author  string `json:"author"`
	Date    string `json:"date"`
	URL     string `json:"url"`
}

type GitHubChecks struct {
	State   string   `json:"state"`
	Total   int      `json:"total"`
	Failing []string `json:"failing"`
	Running int      `json:"running"`
	Names   []string `json:"names"`
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
	Branch    string   `json:"branch"`
	Labels    []string `json:"labels"`
	// Checks is the head commit's checks, read for an update's pull request
	// (its branch under githubUpdateBranch) and nil for any other.
	Checks *GitHubChecks `json:"checks"`
}

// githubUpdateBranch is the prefix of the branches Renovate opens its pull
// requests from.
const githubUpdateBranch = "renovate/"

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
// once. A repository that cannot be read is refused alone and the rest are
// reported; every repository refused is a refused read.
func (r *Registry) githubRepositories(ctx context.Context) ([]any, error) {
	c, err := r.githubConnection("")
	if err != nil {
		return nil, err
	}
	names, err := r.githubInstallationRepositories(ctx, c)
	if err != nil {
		return nil, err
	}
	records := make([]GitHubRepositoryRecord, len(names))
	failures := make([]error, len(names))
	var group errgroup.Group
	group.SetLimit(githubReadConcurrency)
	for i, name := range names {
		group.Go(func() error {
			if failures[i] = ctx.Err(); failures[i] == nil {
				records[i], failures[i] = (&githubRepository{r: r, c: c, name: name}).read(ctx)
			}
			return nil
		})
	}
	_ = group.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	found := []any{}
	var refused []error
	for i, name := range names {
		if failures[i] != nil {
			refused = append(refused, failures[i])
			refuse(ctx, runtime.PartWhole, c.ref, name, failures[i])
			continue
		}
		found = append(found, records[i])
	}
	if len(names) > 0 && len(refused) == len(names) {
		return nil, fmt.Errorf("every repository refused its reading: %w", refused[0])
	}
	return found, nil
}

// githubRepository is one repository being read.
type githubRepository struct {
	r    *Registry
	c    githubConnection
	name string
}

func (g *githubRepository) raw(ctx context.Context, path string) (json.RawMessage, error) {
	return g.r.githubCall(ctx, g.c, "GET", "/repos/"+g.name+path, []string{g.name}, githubRead, nil)
}

func repoGet[T any](ctx context.Context, g *githubRepository, path string) (T, error) {
	return githubGet[T](ctx, g.r, g.c, "/repos/"+g.name+path, []string{g.name}, githubRead)
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
	raw, err := g.raw(ctx, "")
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	repo, err := decodeAnswer[githubapi.FullRepository](raw, "GitHub repository")
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	branch := orDefault(repo.DefaultBranch, "main")
	commit, err := repoGet[githubapi.Commit](ctx, g, "/commits/"+url.PathEscape(branch))
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	runs, err := repoGet[struct {
		WorkflowRuns []githubapi.WorkflowRun `json:"workflow_runs"`
	}](ctx, g, "/actions/runs?per_page="+strconv.Itoa(githubRunsPage))
	if err != nil {
		return GitHubRepositoryRecord{}, err
	}
	record := GitHubRepositoryRecord{
		ConnectionRef: g.c.ref, Repository: g.name, Private: repo.Private, Archived: repo.Archived, URL: repo.HTMLURL,
		DefaultBranch: branch, PushedAt: repo.PushedAt, Head: githubHead(commit),
	}
	if uses, ok := part(ctx, g, runtime.PartWorkflowPins, func() (githubUsesFound, error) { return g.workflowUses(ctx, branch) }); ok {
		record.Pins, record.CalledWorkflows = uses.pins, uses.called
	}
	if record.Checks, err = g.checks(ctx, commit.Sha); err != nil {
		return record, err
	}
	pulls, err := repoGet[[]githubapi.PullRequestSimple](ctx, g, "/pulls?state=open&per_page="+strconv.Itoa(githubPullsPage))
	if err != nil {
		return record, err
	}
	record.PullRequests = githubPulls(pulls)
	if record.PullRequestChecks, err = g.pullChecks(ctx, record.PullRequests); err != nil {
		return record, err
	}
	workflows, err := repoGet[struct {
		Workflows []githubapi.Workflow `json:"workflows"`
	}](ctx, g, "/actions/workflows?per_page="+strconv.Itoa(githubListPage))
	if err != nil {
		return record, err
	}
	record.Runs = latestRuns(runs.WorkflowRuns, workflows.Workflows)
	if record.Waiting, err = g.waiting(ctx, runs.WorkflowRuns); err != nil {
		return record, err
	}
	releases, err := repoGet[[]githubapi.Release](ctx, g, "/releases?per_page=1")
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
	artifacts, err := repoGet[struct {
		Artifacts []githubapi.Artifact `json:"artifacts"`
	}](ctx, g, "/actions/artifacts?per_page="+strconv.Itoa(githubRunsPage))
	if err != nil {
		return record, err
	}
	record.Artifacts = liveArtifacts(artifacts.Artifacts)
	if rules, ok := part(ctx, g, runtime.PartBranchRules, func() (GitHubRules, error) {
		found, err := repoGet[[]githubapi.RepositoryRuleDetailed](ctx, g, "/rules/branches/"+url.PathEscape(branch))
		if err != nil {
			return GitHubRules{}, err
		}
		return branchRules(found)
	}); ok {
		record.Rules = &rules
	}
	record.Environments, _ = part(ctx, g, runtime.PartEnvironments, func() ([]GitHubEnvironment, error) {
		found, err := repoGet[struct {
			Environments []githubapi.Environment `json:"environments"`
		}](ctx, g, "/environments")
		if err != nil {
			return nil, err
		}
		return githubEnvironments(found.Environments)
	})
	if record.Environments == nil {
		record.Environments = []GitHubEnvironment{}
	}
	record.Runners, _ = part(ctx, g, runtime.PartRunners, func() ([]GitHubRunner, error) {
		found, err := repoGet[struct {
			Runners []githubapi.Runner `json:"runners"`
		}](ctx, g, "/actions/runners")
		return githubRunners(found.Runners), err
	})
	if record.Runners == nil {
		record.Runners = []GitHubRunner{}
	}
	if record.Images, err = g.images(ctx); err != nil {
		return record, err
	}
	if access, ok := part(ctx, g, runtime.PartAccess, func() (GitHubAccess, error) { return g.access(ctx, repo, raw) }); ok {
		record.Access = &access
	}
	record.Variables, _ = part(ctx, g, runtime.PartVariables, func() ([]string, error) {
		found, err := repoGet[struct {
			Variables []githubapi.ActionsVariable `json:"variables"`
		}](ctx, g, "/actions/variables?per_page="+strconv.Itoa(githubListPage))
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

// checks is a commit's check runs, each standing as its last run did: a rerun
// that passed is what the commit's state is.
func (g *githubRepository) checks(ctx context.Context, sha string) (GitHubChecks, error) {
	if sha == "" {
		return summarizeChecks(nil), nil
	}
	found, err := repoGet[struct {
		CheckRuns []githubapi.CheckRun `json:"check_runs"`
	}](ctx, g, "/commits/"+sha+"/check-runs?per_page="+strconv.Itoa(githubListPage))
	return summarizeChecks(found.CheckRuns), err
}

func summarizeChecks(runs []githubapi.CheckRun) GitHubChecks {
	latest := map[string]githubapi.CheckRun{}
	order := []string{}
	for _, run := range runs {
		previous, seen := latest[run.Name]
		if !seen {
			order = append(order, run.Name)
		}
		if !seen || run.StartedAt > previous.StartedAt || (run.StartedAt == previous.StartedAt && run.ID >= previous.ID) {
			latest[run.Name] = run
		}
	}
	failing, names := map[string]bool{}, map[string]bool{}
	running := 0
	for _, name := range order {
		run := latest[name]
		if githubFailed[string(run.Conclusion)] {
			failing[name] = true
		}
		if githubRunning[string(run.Status)] {
			running++
		}
		if name != "" {
			names[name] = true
		}
	}
	state := ""
	switch {
	case len(failing) > 0:
		state = "failure"
	case running > 0:
		state = "pending"
	case len(order) > 0:
		state = "success"
	}
	return GitHubChecks{State: state, Total: len(order), Failing: sortedKeys(failing), Running: running, Names: sortedKeys(names)}
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
			Draft: pull.Draft, CreatedAt: pull.CreatedAt, UpdatedAt: pull.UpdatedAt, Head: pull.Head.Sha,
			Branch: pull.Head.Ref, Labels: labels,
		})
	}
	return found
}

// pullChecks reads the checks of every update's pull request onto it, and
// answers the check names the newest pull request runs: what a pull request
// is checked by, for the rules to require.
func (g *githubRepository) pullChecks(ctx context.Context, pulls []GitHubPull) ([]string, error) {
	for i := range pulls {
		if pulls[i].Head == "" || !strings.HasPrefix(pulls[i].Branch, githubUpdateBranch) {
			continue
		}
		checks, err := g.checks(ctx, pulls[i].Head)
		if err != nil {
			return nil, err
		}
		pulls[i].Checks = &checks
	}
	if len(pulls) == 0 || pulls[0].Head == "" {
		return []string{}, nil
	}
	if pulls[0].Checks != nil {
		return pulls[0].Checks.Names, nil
	}
	checks, err := g.checks(ctx, pulls[0].Head)
	return checks.Names, err
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
		pending, err := repoGet[[]githubapi.PendingDeployment](ctx, g, "/actions/runs/"+strconv.Itoa(run.ID)+"/pending_deployments")
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

// deployments is the newest deployment to each environment, with its latest status.
func (g *githubRepository) deployments(ctx context.Context) ([]GitHubDeployment, error) {
	listed, err := repoGet[[]githubapi.Deployment](ctx, g, "/deployments?per_page="+strconv.Itoa(githubDeploymentsPage))
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
		statuses, err := repoGet[[]githubapi.DeploymentStatus](ctx, g, "/deployments/"+strconv.Itoa(deployment.ID)+"/statuses?per_page=1")
		if err != nil {
			return nil, err
		}
		record := GitHubDeployment{Environment: deployment.Environment, SHA: deployment.Sha, CreatedAt: deployment.CreatedAt, Verified: []GitHubVerification{}}
		if len(statuses) > 0 {
			record.State, record.URL = string(statuses[0].State), orDefault(statuses[0].LogURL, statuses[0].TargetURL)
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
	answer, err := repoGet[githubapi.Job](ctx, g, "/actions/jobs/"+job)
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
