package providers

import (
	"context"
	"maps"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
)

// How a repository's checks stand: the default branch's, each update pull
// request's, and what each failing job says of why.

type GitHubChecks struct {
	State   string   `json:"state"`
	Total   int      `json:"total"`
	Failing []string `json:"failing"`
	Running int      `json:"running"`
	Names   []string `json:"names"`
	// Failures is why each failing job of a workflow run failed, by the
	// job's name (githubFailure).
	Failures map[string]GitHubFailure `json:"failures,omitempty"`
}

// githubOwnSuites is the check suites of GitHub's own dynamic runs (the
// dependency graph's, Copilot's), which no workflow file of the repository
// owns, so their checks are not the repository's.
func githubOwnSuites(runs []githubapi.WorkflowRun) map[int]bool {
	suites := map[int]bool{}
	for _, run := range runs {
		if run.Event == "dynamic" && run.CheckSuiteID != 0 {
			suites[run.CheckSuiteID] = true
		}
	}
	return suites
}

// githubWorkflowSuites is the check suites of the repository's own workflow
// runs, whose check runs are their jobs, by the same id.
func githubWorkflowSuites(runs []githubapi.WorkflowRun) map[int]bool {
	suites := map[int]bool{}
	for _, run := range runs {
		if run.Event != "dynamic" && run.CheckSuiteID != 0 {
			suites[run.CheckSuiteID] = true
		}
	}
	return suites
}

// branchChecks is the default branch's checks: its head commit's, and the
// failing jobs of each workflow whose newest run on the branch, at an older
// commit, failed. A workflow that runs on a schedule, by hand or for some
// paths fails the branch until a run of it passes, whatever lands after it;
// the count and the running are the head commit's.
func (g *githubRepository) branchChecks(ctx context.Context, head string, newest []githubapi.WorkflowRun, githubs map[int]bool) (GitHubChecks, error) {
	runs, err := g.checkRuns(ctx, head)
	if err != nil {
		return summarizeChecks(nil, githubs), err
	}
	checks := summarizeChecks(runs, githubs)
	// A check run in a workflow run's suite is that run's job, by the same id.
	workflows := map[int]bool{}
	for _, run := range newest {
		workflows[run.CheckSuiteID] = run.CheckSuiteID != 0
	}
	failing, failures := map[string]bool{}, map[string]GitHubFailure{}
	said := func(runs []githubapi.CheckRun) ([]string, error) {
		jobs := []string{}
		for _, run := range latestChecks(runs, githubs) {
			if githubFailed[string(run.Conclusion)] {
				jobs = append(jobs, run.Name)
			}
		}
		return jobs, g.failures(ctx, runs, githubs, workflows, failures)
	}
	jobs, err := said(runs)
	if err != nil {
		return checks, err
	}
	for _, name := range jobs {
		failing[name] = true
	}
	for _, run := range newest {
		if run.HeadSha == head || !githubFailed[run.Conclusion] {
			continue
		}
		jobs := []string{}
		if run.CheckSuiteID != 0 {
			found, err := json200(g.api.ChecksListForSuiteWithResponse(ctx, g.owner, g.repo, strconv.Itoa(run.CheckSuiteID), &githubapi.ChecksListForSuiteParams{PerPage: new(githubListPage)}))
			if err != nil {
				return checks, err
			}
			if jobs, err = said(found.CheckRuns); err != nil {
				return checks, err
			}
		}
		if len(jobs) == 0 {
			jobs = []string{run.Name}
		}
		for _, name := range jobs {
			failing[name] = true
		}
	}
	if len(failing) > 0 {
		checks.State, checks.Failing = "failure", sortedKeys(failing)
	}
	checks.Failures = stated(failures)
	return checks, nil
}

// githubFailuresKept is the most failing jobs a commit's checks say why of,
// which is the most job logs read for them.
var githubFailuresKept = api.MustLimit("GitHubRepositoryChecks", "properties", "failures", "maxProperties")

// failures adds why each failing job of runs failed, by its name, until
// githubFailuresKept are read: each check run in one of the workflows' suites
// is that run's job.
func (g *githubRepository) failures(ctx context.Context, runs []githubapi.CheckRun, githubs, workflows map[int]bool, found map[string]GitHubFailure) error {
	for _, run := range latestChecks(runs, githubs) {
		_, read := found[run.Name]
		if len(found) >= githubFailuresKept || read || !githubFailed[string(run.Conclusion)] || !workflows[run.CheckSuite.ID] {
			continue
		}
		failure, err := g.failure(ctx, run.ID)
		if err != nil {
			return err
		}
		found[run.Name] = failure
	}
	return nil
}

// stated is the failures that say why, nil for none: a job that failed before
// any step did says nothing.
func stated(failures map[string]GitHubFailure) map[string]GitHubFailure {
	maps.DeleteFunc(failures, func(_ string, failure GitHubFailure) bool { return failure.Step == "" })
	if len(failures) == 0 {
		return nil
	}
	return failures
}

// checks is a commit's check runs, each standing as its last run did: a rerun
// that passed is what the commit's state is. A check in a suite githubs holds
// is GitHub's own, and left out; one in a suite workflows holds is a job,
// whose failure says why.
func (g *githubRepository) checks(ctx context.Context, sha string, githubs, workflows map[int]bool) (GitHubChecks, error) {
	runs, err := g.checkRuns(ctx, sha)
	checks := summarizeChecks(runs, githubs)
	if err != nil || workflows == nil {
		return checks, err
	}
	failures := map[string]GitHubFailure{}
	if err := g.failures(ctx, runs, githubs, workflows, failures); err != nil {
		return checks, err
	}
	checks.Failures = stated(failures)
	return checks, nil
}

// checkRuns is every check run on a commit, none for no commit.
func (g *githubRepository) checkRuns(ctx context.Context, sha string) ([]githubapi.CheckRun, error) {
	if sha == "" {
		return nil, nil
	}
	found, err := json200(g.api.ChecksListForRefWithResponse(ctx, g.owner, g.repo, sha, &githubapi.ChecksListForRefParams{PerPage: new(githubListPage)}))
	return found.CheckRuns, err
}

// latestChecks is the last run of each check by name, in the order each name
// first appears, leaving out any in a suite githubs holds.
func latestChecks(runs []githubapi.CheckRun, githubs map[int]bool) []githubapi.CheckRun {
	latest := map[string]githubapi.CheckRun{}
	order := []string{}
	for _, run := range runs {
		if githubs[run.CheckSuite.ID] {
			continue
		}
		previous, seen := latest[run.Name]
		if !seen {
			order = append(order, run.Name)
		}
		if !seen || run.StartedAt > previous.StartedAt || (run.StartedAt == previous.StartedAt && run.ID >= previous.ID) {
			latest[run.Name] = run
		}
	}
	found := make([]githubapi.CheckRun, 0, len(order))
	for _, name := range order {
		found = append(found, latest[name])
	}
	return found
}

func summarizeChecks(runs []githubapi.CheckRun, githubs map[int]bool) GitHubChecks {
	latest := latestChecks(runs, githubs)
	failing, names := map[string]bool{}, map[string]bool{}
	running := 0
	for _, run := range latest {
		name := run.Name
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
	case len(latest) > 0:
		state = "success"
	}
	return GitHubChecks{State: state, Total: len(latest), Failing: sortedKeys(failing), Running: running, Names: sortedKeys(names)}
}

// pullChecks reads the checks and files of every update's pull request onto
// it, why each of its failing jobs failed and what its updater's run could
// not update, and the images it moves where compose holds the repository's
// Compose files, and answers the check names the newest pull request runs:
// what a pull request is checked by, for the rules to require.
func (g *githubRepository) pullChecks(ctx context.Context, pulls []GitHubPull, githubs, workflows map[int]bool, compose []string) ([]string, error) {
	for i := range pulls {
		if pulls[i].Head == "" || !strings.HasPrefix(pulls[i].Branch, githubUpdateBranch) {
			continue
		}
		checks, err := g.checks(ctx, pulls[i].Head, githubs, workflows)
		if err != nil {
			return nil, err
		}
		pulls[i].Checks = &checks
		if checks.State == "failure" {
			if pulls[i].ArtifactProblem, err = g.artifactProblem(ctx, pulls[i]); err != nil {
				return nil, err
			}
		}
		files, err := json200(g.api.PullsListFilesWithResponse(ctx, g.owner, g.repo, strconv.Itoa(pulls[i].Number), &githubapi.PullsListFilesParams{PerPage: new(githubPullFilesPage)}))
		if err != nil {
			return nil, err
		}
		pulls[i].Files = pullFiles(files)
		whole, err := json200(g.api.PullsGetWithResponse(ctx, g.owner, g.repo, strconv.Itoa(pulls[i].Number)))
		if err != nil {
			return nil, err
		}
		pulls[i].ChangedFiles = whole.ChangedFiles
		if compose != nil {
			pulls[i].Apps = g.appChanges(ctx, pulls[i], compose)
		}
	}
	if len(pulls) == 0 || pulls[0].Head == "" {
		return []string{}, nil
	}
	if pulls[0].Checks != nil {
		return pulls[0].Checks.Names, nil
	}
	checks, err := g.checks(ctx, pulls[0].Head, githubs, nil)
	return checks.Names, err
}

func pullFiles(entries []githubapi.DiffEntry) []GitHubPullFile {
	found := []GitHubPullFile{}
	for _, entry := range entries {
		app, _ := appDirectory(entry.Filename)
		found = append(found, GitHubPullFile{Path: entry.Filename, Status: string(entry.Status), Additions: entry.Additions, Deletions: entry.Deletions, App: app})
	}
	return found
}
