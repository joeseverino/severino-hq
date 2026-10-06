package providers

import (
	"bytes"
	"cmp"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/url"
	"regexp"
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

// githubRegistry is the container registry an app token reads packages from;
// GitHub's package API does not take an installation token. It is fixed here
// and never taken from an image reference: the app's token is sent to it.
const githubRegistry = "ghcr.io"

var (
	githubFailed  = map[string]bool{"failure": true, "timed_out": true, "action_required": true, "startup_failure": true}
	githubRunning = map[string]bool{"queued": true, "in_progress": true, "requested": true, "pending": true, "waiting": true}
	// A uses: value: an action or reusable workflow and the ref it is taken at.
	githubUses   = regexp.MustCompile(`(?m)^\s*(?:-\s*)?uses:\s*["']?([^\s"'#]+)`)
	githubCommit = regexp.MustCompile(`^[0-9a-f]{40}$`)
	// A reusable workflow another repository holds.
	githubCalled = regexp.MustCompile(`^[^./][^/]*/[^/]+/\.github/workflows/[^/]+\.ya?ml$`)
)

// Where the files whose uses: lines run live: each workflow, and each local
// composite action's own action.yml.
const (
	githubWorkflowsDir = ".github/workflows"
	githubActionsDir   = ".github/actions"
)

// GitHubRepositoryRecord is one repository as the app's read-only token sees it.
// A nil part is one refused; HQ's record says which.
type GitHubRepositoryRecord struct {
	ConnectionRef     string                    `json:"connection_ref"`
	Repository        string                    `json:"repository"`
	Private           bool                      `json:"private"`
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
	Number    int    `json:"number"`
	Title     string `json:"title"`
	URL       string `json:"url"`
	Author    string `json:"author"`
	Draft     bool   `json:"draft"`
	UpdatedAt string `json:"updated_at"`
	Head      string `json:"head"`
}

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

type GitHubRules struct {
	PullRequest     bool     `json:"pull_request"`
	Reviews         int      `json:"reviews"`
	RequiredChecks  []string `json:"required_checks"`
	BlocksForcePush bool     `json:"blocks_force_push"`
	BlocksDeletion  bool     `json:"blocks_deletion"`
}

type GitHubEnvironment struct {
	Name        string   `json:"name"`
	Reviewers   []string `json:"reviewers"`
	WaitMinutes int      `json:"wait_minutes"`
	Branches    string   `json:"branches"`
}

type GitHubRunner struct {
	Name   string   `json:"name"`
	Status string   `json:"status"`
	Busy   bool     `json:"busy"`
	Labels []string `json:"labels"`
}

type GitHubImage struct {
	Name   string   `json:"name"`
	Tags   int      `json:"tags"`
	Signed []string `json:"signed"`
}

type GitHubAccess struct {
	Visibility           string               `json:"visibility"`
	Collaborators        []GitHubCollaborator `json:"collaborators"`
	DeployKeys           []GitHubDeployKey    `json:"deploy_keys"`
	AllowedActions       string               `json:"allowed_actions"`
	PinningRequired      bool                 `json:"pinning_required"`
	Token                string               `json:"token"`
	TokenApprovesReviews bool                 `json:"token_approves_reviews"`
	SecurityFixes        bool                 `json:"security_fixes"`
	// Security is each security feature's status; nil where the plan offers
	// none of them, which is not "off".
	Security map[string]*string `json:"security"`
}

type GitHubCollaborator struct {
	Login string `json:"login"`
	Role  string `json:"role"`
}

type GitHubDeployKey struct {
	Title     string `json:"title"`
	ReadOnly  bool   `json:"read_only"`
	LastUsed  string `json:"last_used"`
	CreatedAt string `json:"created_at"`
}

type GitHubPin struct {
	Path   string `json:"path"`
	Uses   string `json:"uses"`
	Action string `json:"action"`
	Ref    string `json:"ref"`
	SHA    string `json:"sha"`
}

// githubRepositories reads every repository the installations cover, a few at
// once. One repository failing fails the read; none started after it is asked.
func (r *Registry) githubRepositories(ctx context.Context) ([]any, error) {
	c, err := r.githubConnection("")
	if err != nil {
		return nil, err
	}
	names, err := r.githubInstallationRepositories(ctx, c)
	if err != nil {
		return nil, err
	}
	records := make([]any, len(names))
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(githubReadConcurrency)
	for i, name := range names {
		group.Go(func() error {
			if err := groupCtx.Err(); err != nil {
				return err
			}
			record, err := (&githubRepository{r: r, c: c, name: name}).read(groupCtx)
			records[i] = record
			return err
		})
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	return records, nil
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
func asRepositoryRefusal(err error) error {
	if failure, _, _ := runtime.Classify(err); failure != runtime.FailureClassPermission {
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
		ConnectionRef: g.c.ref, Repository: g.name, Private: repo.Private, URL: repo.HTMLURL,
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
	record.PullRequestChecks = []string{}
	if len(record.PullRequests) > 0 && record.PullRequests[0].Head != "" {
		checks, err := g.checks(ctx, record.PullRequests[0].Head)
		if err != nil {
			return record, err
		}
		record.PullRequestChecks = checks.Names
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
	record.Alerts = g.alerts(ctx)
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
		found = append(found, GitHubPull{
			Number: pull.Number, Title: pull.Title, URL: pull.HTMLURL, Author: pull.User.Login,
			Draft: pull.Draft, UpdatedAt: pull.UpdatedAt, Head: pull.Head.Sha,
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

// alerts is open alerts by severity. A kind the repository cannot answer is
// that part refused, never zero.
func (g *githubRepository) alerts(ctx context.Context) map[string]map[string]int {
	found := map[string]map[string]int{}
	count := func(name runtime.ReadingPartName, severities []string) {
		counts := map[string]int{}
		for _, severity := range severities {
			counts[strings.ToLower(orDefault(severity, "unknown"))]++
		}
		found[string(name)] = counts
	}
	if alerts, ok := part(ctx, g, runtime.PartCodeScanning, func() ([]githubapi.CodeScanningAlertItems, error) {
		return repoGet[[]githubapi.CodeScanningAlertItems](ctx, g, "/code-scanning/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := []string{}
		for _, alert := range alerts {
			severities = append(severities, orDefault(string(alert.Rule.SecuritySeverityLevel), string(alert.Rule.Severity)))
		}
		count(runtime.PartCodeScanning, severities)
	}
	if alerts, ok := part(ctx, g, runtime.PartDependabot, func() ([]githubapi.DependabotAlert, error) {
		return repoGet[[]githubapi.DependabotAlert](ctx, g, "/dependabot/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := []string{}
		for _, alert := range alerts {
			severities = append(severities, string(alert.SecurityAdvisory.Severity))
		}
		count(runtime.PartDependabot, severities)
	}
	if alerts, ok := part(ctx, g, runtime.PartLeakedCredentials, func() ([]githubapi.SecretScanningAlert, error) {
		return repoGet[[]githubapi.SecretScanningAlert](ctx, g, "/secret-scanning/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := make([]string, len(alerts))
		for i := range alerts {
			severities[i] = "leaked"
		}
		count(runtime.PartLeakedCredentials, severities)
	}
	return found
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

// branchRules is what the default branch requires, from every ruleset that
// applies to it. Each rule is read as the variant its type names.
func branchRules(rules []githubapi.RepositoryRuleDetailed) (GitHubRules, error) {
	found := GitHubRules{RequiredChecks: []string{}}
	checks := map[string]bool{}
	for _, rule := range rules {
		raw, err := rule.MarshalJSON()
		if err != nil {
			return found, err
		}
		var kind struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &kind); err != nil {
			return found, &ProviderError{Message: "decode GitHub branch rule", Err: err}
		}
		switch kind.Type {
		case "pull_request":
			var pull githubapi.RepositoryRulePullRequest
			if err := json.Unmarshal(raw, &pull); err != nil {
				return found, &ProviderError{Message: "decode GitHub pull request rule", Err: err}
			}
			found.PullRequest = true
			found.Reviews = max(found.Reviews, pull.Parameters.RequiredApprovingReviewCount)
		case "required_status_checks":
			var required githubapi.RepositoryRuleRequiredStatusChecks
			if err := json.Unmarshal(raw, &required); err != nil {
				return found, &ProviderError{Message: "decode GitHub status check rule", Err: err}
			}
			for _, check := range required.Parameters.RequiredStatusChecks {
				checks[check.Context] = true
			}
		case "non_fast_forward":
			found.BlocksForcePush = true
		case "deletion":
			found.BlocksDeletion = true
		}
	}
	found.RequiredChecks = sortedKeys(checks)
	return found, nil
}

func githubEnvironments(environments []githubapi.Environment) ([]GitHubEnvironment, error) {
	found := []GitHubEnvironment{}
	for _, environment := range environments {
		reviewers := map[string]bool{}
		wait := 0
		for _, rule := range environment.ProtectionRules {
			typed, err := rule.AsEnvironmentProtectionRules2()
			if err != nil {
				return nil, &ProviderError{Message: "decode GitHub environment rule", Err: err}
			}
			switch typed.Type {
			case "required_reviewers":
				required, err := rule.AsEnvironmentProtectionRules1()
				if err != nil {
					return nil, &ProviderError{Message: "decode GitHub reviewer rule", Err: err}
				}
				for _, entry := range required.Reviewers {
					user, _ := entry.Reviewer.AsSimpleUser()
					team, _ := entry.Reviewer.AsTeam()
					if name := orDefault(user.Login, team.Name); name != "" {
						reviewers[name] = true
					}
				}
			case "wait_timer":
				timer, err := rule.AsEnvironmentProtectionRules0()
				if err != nil {
					return nil, &ProviderError{Message: "decode GitHub wait timer rule", Err: err}
				}
				wait = timer.WaitTimer
			}
		}
		branches := "any"
		switch policy := environment.DeploymentBranchPolicy; {
		case policy.ProtectedBranches:
			branches = "protected"
		case policy.CustomBranchPolicies:
			branches = "custom"
		}
		found = append(found, GitHubEnvironment{Name: environment.Name, Reviewers: sortedKeys(reviewers), WaitMinutes: wait, Branches: branches})
	}
	return found, nil
}

func githubRunners(runners []githubapi.Runner) []GitHubRunner {
	found := []GitHubRunner{}
	for _, runner := range runners {
		labels := []string{}
		for _, label := range runner.Labels {
			labels = append(labels, label.Name)
		}
		slices.Sort(labels)
		found = append(found, GitHubRunner{Name: runner.Name, Status: runner.Status, Busy: runner.Busy, Labels: labels})
	}
	return found
}

// access is who can reach the repository and how its Actions may run.
func (g *githubRepository) access(ctx context.Context, repo githubapi.FullRepository, raw json.RawMessage) (GitHubAccess, error) {
	actions, err := repoGet[githubapi.ActionsRepositoryPermissions](ctx, g, "/actions/permissions")
	if err != nil {
		return GitHubAccess{}, err
	}
	workflow, err := repoGet[githubapi.ActionsGetDefaultWorkflowPermissions](ctx, g, "/actions/permissions/workflow")
	if err != nil {
		return GitHubAccess{}, err
	}
	fixes, err := repoGet[githubapi.CheckAutomatedSecurityFixes](ctx, g, "/automated-security-fixes")
	if err != nil {
		return GitHubAccess{}, err
	}
	collaborators, err := repoGet[[]githubapi.Collaborator](ctx, g, "/collaborators?per_page="+strconv.Itoa(githubListPage))
	if err != nil {
		return GitHubAccess{}, err
	}
	keys, err := repoGet[[]githubapi.DeployKey](ctx, g, "/keys?per_page="+strconv.Itoa(githubListPage))
	if err != nil {
		return GitHubAccess{}, err
	}
	// Which security features the plan offers is which keys are present, so
	// they are read as a map, not the struct that cannot tell absent from off.
	security, err := decodeAnswer[struct {
		SecurityAndAnalysis map[string]*struct {
			Status *string `json:"status"`
		} `json:"security_and_analysis"`
	}](raw, "GitHub repository security")
	if err != nil {
		return GitHubAccess{}, err
	}
	visibility := repo.Visibility
	if visibility == "" {
		visibility = map[bool]string{true: "private", false: "public"}[repo.Private]
	}
	access := GitHubAccess{
		Visibility: visibility, Collaborators: []GitHubCollaborator{}, DeployKeys: []GitHubDeployKey{},
		AllowedActions: string(actions.AllowedActions), PinningRequired: actions.ShaPinningRequired,
		Token: string(workflow.DefaultWorkflowPermissions), TokenApprovesReviews: workflow.CanApprovePullRequestReviews,
		SecurityFixes: fixes.Enabled,
	}
	for _, collaborator := range collaborators {
		access.Collaborators = append(access.Collaborators, GitHubCollaborator{Login: collaborator.Login, Role: collaborator.RoleName})
	}
	for _, key := range keys {
		access.DeployKeys = append(access.DeployKeys, GitHubDeployKey{Title: key.Title, ReadOnly: key.ReadOnly, LastUsed: key.LastUsed, CreatedAt: key.CreatedAt})
	}
	if len(security.SecurityAndAnalysis) > 0 {
		access.Security = map[string]*string{}
		for feature, setting := range security.SecurityAndAnalysis {
			if setting != nil {
				access.Security[feature] = setting.Status
			} else {
				access.Security[feature] = nil
			}
		}
	}
	return access, nil
}

type githubUsesFound struct {
	pins   []GitHubPin
	called []string
}

// listing is a directory's entries; a 404 is the repository saying it has no
// such directory, so nothing in it to pin.
func (g *githubRepository) listing(ctx context.Context, path, branch string) (githubapi.ContentDirectory, error) {
	raw, err := g.raw(ctx, "/contents/"+path+"?ref="+url.QueryEscape(branch))
	if httpStatus(err) == 404 {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("[")) {
		return nil, nil
	}
	return decodeAnswer[githubapi.ContentDirectory](raw, "GitHub directory "+path)
}

// usesFiles is every workflow file, then every local composite action's action.yml.
func (g *githubRepository) usesFiles(ctx context.Context, branch string) ([]string, error) {
	workflows, err := g.listing(ctx, githubWorkflowsDir, branch)
	if err != nil {
		return nil, err
	}
	files := []string{}
	for _, entry := range workflows {
		if strings.HasSuffix(entry.Path, ".yml") || strings.HasSuffix(entry.Path, ".yaml") {
			files = append(files, entry.Path)
		}
	}
	actions, err := g.listing(ctx, githubActionsDir, branch)
	if err != nil {
		return nil, err
	}
	for _, action := range actions {
		if action.Type != githubapi.ContentDirectoryTypeDir {
			continue
		}
		entries, err := g.listing(ctx, action.Path, branch)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if entry.Name == "action.yml" || entry.Name == "action.yaml" {
				files = append(files, entry.Path)
			}
		}
	}
	return files, nil
}

// workflowUses is every uses: line not pinned to a commit, with the commit
// its ref names now, and every workflow called from another repository, pinned
// or not: what a called workflow uses lives in that repository, not this one.
func (g *githubRepository) workflowUses(ctx context.Context, branch string) (githubUsesFound, error) {
	files, err := g.usesFiles(ctx, branch)
	if err != nil {
		return githubUsesFound{}, err
	}
	found := githubUsesFound{pins: []GitHubPin{}, called: []string{}}
	resolved := map[string]string{}
	called := map[string]bool{}
	for _, path := range files {
		file, err := repoGet[githubapi.ContentFile](ctx, g, "/contents/"+path+"?ref="+url.QueryEscape(branch))
		if err != nil {
			return githubUsesFound{}, err
		}
		content, err := base64.StdEncoding.DecodeString(strings.NewReplacer("\n", "", "\r", "").Replace(file.Content))
		if err != nil {
			return githubUsesFound{}, &ProviderError{Message: "decode GitHub file " + path, Err: err}
		}
		for _, match := range githubUses.FindAllStringSubmatch(string(content), -1) {
			uses := match[1]
			if strings.HasPrefix(uses, "./") || strings.HasPrefix(uses, "docker://") || !strings.Contains(uses, "@") {
				continue
			}
			at := uses[strings.LastIndex(uses, "@")+1:]
			action := uses[:strings.LastIndex(uses, "@")]
			if githubCalled.MatchString(action) {
				called[uses] = true
			}
			if githubCommit.MatchString(at) {
				continue
			}
			source := strings.Join(firstN(strings.Split(action, "/"), 2), "/")
			key := source + "@" + at
			sha, seen := resolved[key]
			if !seen {
				sha = g.commitOf(ctx, source, at)
				resolved[key] = sha
			}
			found.pins = append(found.pins, GitHubPin{Path: path, Uses: uses, Action: action, Ref: at, SHA: sha})
		}
	}
	found.called = sortedKeys(called)
	return found, nil
}

func firstN(parts []string, n int) []string {
	if len(parts) > n {
		return parts[:n]
	}
	return parts
}

// commitOf is the commit a tag or branch of source names now, or "" when it
// could not be read. Resolved once per action and ref in a sweep, under the
// read-only token of the repository being read; only a commit that was read
// is shared, since one repository's token failing says nothing of the next's.
func (g *githubRepository) commitOf(ctx context.Context, source, at string) string {
	raw, err := g.r.cached(ctx, "github.commit|"+source+"|"+at, func() (json.RawMessage, error) {
		commit, err := githubGet[githubapi.Commit](ctx, g.r, g.c, "/repos/"+source+"/commits/"+url.PathEscape(at), []string{g.name}, githubRead)
		if err != nil {
			return nil, err
		}
		if !githubCommit.MatchString(commit.Sha) {
			return nil, &ProviderError{Message: "the ref names no commit"}
		}
		return json.Marshal(commit.Sha)
	})
	if err != nil {
		return ""
	}
	var sha string
	_ = json.Unmarshal(raw, &sha)
	return sha
}

// registryToken and registryTags are the OCI distribution API's token and
// tag-list answers.
type registryToken struct {
	Token string `json:"token"`
}

type registryTags struct {
	Tags []string `json:"tags"`
}

// images is the container images this controller's own composition names for
// the repository: the host's image and the composition it runs, read from the
// registry, which takes the app's token where GitHub's package API does not.
// Each image is refused alone.
func (g *githubRepository) images(ctx context.Context) ([]GitHubImage, error) {
	composed := g.r.composition()
	found := []GitHubImage{}
	if composed.Repository != g.name {
		return found, nil
	}
	names := map[string]bool{strings.ToLower(g.name): true}
	if rest, ok := strings.CutPrefix(composed.Image, githubRegistry+"/"); ok {
		rest, _, _ = strings.Cut(rest, "@")
		rest, _, _ = strings.Cut(rest, ":")
		names[strings.ToLower(rest)] = true
	}
	token, err := g.r.githubToken(ctx, g.c, []string{g.name}, githubRead)
	if err != nil {
		return nil, err
	}
	for _, image := range sortedKeys(names) {
		scope := g.name + ":" + image
		read, err := g.image(ctx, image, token)
		if err != nil {
			refuse(ctx, runtime.PartImages, g.c.ref, scope, asRepositoryRefusal(err))
			continue
		}
		found = append(found, read)
	}
	return found, nil
}

// image is an image's tags, and which digests carry a cosign signature: cosign
// names its signature sha256-<digest>.sig, so the tag list alone says which.
func (g *githubRepository) image(ctx context.Context, image, token string) (GitHubImage, error) {
	basic := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + token))
	query := url.Values{"scope": {"repository:" + image + ":pull"}, "service": {githubRegistry}}
	raw, err := g.r.HTTP.Request(ctx, "https://"+githubRegistry+"/token?"+query.Encode(), "GET", map[string]string{"Authorization": "Basic " + basic}, nil)
	if err != nil {
		return GitHubImage{}, err
	}
	pull, err := decodeAnswer[registryToken](raw, "registry token")
	if err != nil {
		return GitHubImage{}, err
	}
	raw, err = g.r.HTTP.Request(ctx, "https://"+githubRegistry+"/v2/"+image+"/tags/list?n="+strconv.Itoa(githubTagsPage), "GET", map[string]string{"Authorization": "Bearer " + pull.Token}, nil)
	if err != nil {
		return GitHubImage{}, err
	}
	listed, err := decodeAnswer[registryTags](raw, "registry tags")
	if err != nil {
		return GitHubImage{}, err
	}
	record := GitHubImage{Name: image, Signed: []string{}}
	for _, tag := range listed.Tags {
		if !strings.HasPrefix(tag, "sha256-") {
			record.Tags++
			continue
		}
		if digest, ok := strings.CutSuffix(strings.TrimPrefix(tag, "sha256-"), ".sig"); ok {
			record.Signed = append(record.Signed, digest)
		}
	}
	slices.Sort(record.Signed)
	return record, nil
}
