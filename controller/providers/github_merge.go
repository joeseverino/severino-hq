package providers

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A pull request merged at the head a person approved: one squash merge
// under a token for its repository alone, naming that head, so GitHub
// refuses it once the branch moved. A refusal reports why, in the words
// GitHubMergeRefusal declares.

// githubMergeWrite is what a merge's token holds: Contents write to land the
// commit, Pull requests write to merge.
var githubMergeWrite = githubapi.AppPermissions{Contents: githubapi.AppPermissionsContentsWrite, PullRequests: githubapi.AppPermissionsPullRequestsWrite}

// GitHubPullRequestSpec is a github.pull_request declaration.
type GitHubPullRequestSpec = runtime.GitHubPullRequestSpec

// githubMerge merges the pull request at its approved head, once its head's
// checks are read and every one has passed.
func (r *Registry) githubMerge(ctx context.Context, spec GitHubPullRequestSpec, _ Object, apply bool) (Result, error) {
	which := spec.Repository + "#" + strconv.Itoa(spec.Number)
	if !apply {
		return Result{Changed: true, Message: "Would merge " + which + "."}, nil
	}
	c, err := r.githubConnection()
	if err != nil {
		return Result{}, err
	}
	owner, repo, err := githubRepositoryName(spec.Repository)
	if err != nil {
		return Result{}, err
	}
	if _, err := r.githubInstallation(ctx, c, owner, repo); err != nil {
		if httpStatus(err) == http.StatusNotFound {
			return Result{}, mergeRefused(runtime.GitHubMergeRefusalNotInstalled, "The GitHub App is not installed on "+spec.Repository+".")
		}
		return Result{}, err
	}
	g := r.githubRepository(ctx, c, spec.Repository)
	runs, err := json200(g.api.ActionsListWorkflowRunsForRepoWithResponse(ctx, owner, repo, &githubapi.ActionsListWorkflowRunsForRepoParams{HeadSha: new(spec.HeadSha), PerPage: new(githubRunsPage)}))
	if err != nil {
		return Result{}, err
	}
	checks, err := g.checks(ctx, spec.HeadSha, githubOwnSuites(runs.WorkflowRuns), nil)
	if err != nil {
		return Result{}, err
	}
	if len(checks.Failing) > 0 {
		return Result{}, mergeRefused(runtime.GitHubMergeRefusalChecksFailing, which+" fails its checks: "+strings.Join(checks.Failing, ", ")+".")
	}
	if checks.State != "success" {
		return Result{}, mergeRefused(runtime.GitHubMergeRefusalChecksPending, which+" has no passing checks at "+short(spec.HeadSha, 7)+" yet.")
	}
	body := githubapi.PullsMergeJSONRequestBody{MergeMethod: githubapi.PullsMergeJSONBodyMergeMethodSquash, Sha: spec.HeadSha}
	merged, err := json200(r.githubAs(ctx, c, []string{spec.Repository}, githubMergeWrite).PullsMergeWithResponse(ctx, owner, repo, strconv.Itoa(spec.Number), body))
	switch httpStatus(err) {
	case http.StatusConflict:
		return Result{}, mergeRefused(runtime.GitHubMergeRefusalHeadMoved, which+" moved past the approved head "+short(spec.HeadSha, 7)+".")
	case http.StatusMethodNotAllowed:
		return Result{}, mergeRefused(runtime.GitHubMergeRefusalNotMergeable, "GitHub cannot merge "+which+" as it stands.")
	}
	if err != nil {
		return Result{}, err
	}
	if !merged.Merged || merged.Sha == "" {
		return Result{}, &ProviderError{Message: "GitHub did not merge " + which + "."}
	}
	return Result{Changed: true, Status: runtime.GitHubMergeStatus{MergeSha: merged.Sha}, Message: "Merged " + which + " as " + short(merged.Sha, 7) + "."}, nil
}

// mergeRefused is a refusal whose status names why.
func mergeRefused(why runtime.GitHubMergeRefusal, message string) error {
	return &ProviderError{Message: message, Status: runtime.GitHubMergeStatus{Refused: why}}
}
