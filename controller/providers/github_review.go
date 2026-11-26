package providers

import (
	"context"
	"net/http"
	"slices"
	"strconv"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// HQ's answer as a repository's custom deployment protection rule, signed as
// the deploy review App alone: GitHub takes a rule's answer only from the App
// that is the rule, so the main App's key cannot release a deploy. The run is
// read again and answered only while it still waits on the environment at the
// commit a person saw.

// githubReviewGrant is what a review's token holds, the two permissions
// GitHub's custom protection rule guide names: Actions read for the run,
// Deployments write for the answer.
var githubReviewGrant = githubapi.AppPermissions{Actions: githubapi.AppPermissionsActionsRead, Deployments: githubapi.AppPermissionsDeploymentsWrite}

// GitHubDeploymentReviewSpec is a github.deployment_review declaration.
type GitHubDeploymentReviewSpec = runtime.GitHubDeploymentReviewSpec

// githubReview answers the run's wait on its environment with the declared state.
func (r *Registry) githubReview(ctx context.Context, spec GitHubDeploymentReviewSpec, _ Object, apply bool) (Result, error) {
	run := strconv.Itoa(spec.RunID)
	which := spec.Repository + " run " + run + " to " + spec.Environment
	verb := map[runtime.GitHubDeploymentReviewSpecState]string{runtime.Approved: "Approved", runtime.Rejected: "Rejected"}[spec.State]
	if verb == "" {
		return Result{}, &ProviderError{Message: "A review answers approved or rejected."}
	}
	if !apply {
		return Result{Changed: true, Message: "Would answer " + string(spec.State) + " to " + which + "."}, nil
	}
	c, err := r.githubConnectionOf(runtime.ConnectionProviderGitHubDeployReview, "")
	if err != nil {
		return Result{}, err
	}
	owner, repo, err := githubRepositoryName(spec.Repository)
	if err != nil {
		return Result{}, err
	}
	if _, err := r.githubInstallation(ctx, c, owner, repo); err != nil {
		if httpStatus(err) == http.StatusNotFound {
			return Result{}, &ProviderError{Message: "The deploy review App is not installed on " + spec.Repository + "."}
		}
		return Result{}, err
	}
	api := r.githubAs(ctx, c, []string{spec.Repository}, githubReviewGrant)
	found, err := json200(api.ActionsGetWorkflowRunWithResponse(ctx, owner, repo, run))
	if httpStatus(err) == http.StatusNotFound {
		return Result{}, &ProviderError{Message: which + " no longer exists."}
	}
	if err != nil {
		return Result{}, err
	}
	if found.Status != "waiting" {
		return Result{}, &ProviderError{Message: which + " no longer waits."}
	}
	if found.HeadSha != spec.HeadSha {
		return Result{}, &ProviderError{Message: which + " deploys " + short(found.HeadSha, 7) + ", not " + short(spec.HeadSha, 7) + "."}
	}
	pending, err := json200(api.ActionsGetPendingDeploymentsForRunWithResponse(ctx, owner, repo, run))
	if err != nil {
		return Result{}, err
	}
	if !slices.ContainsFunc(pending, func(each githubapi.PendingDeployment) bool { return each.Environment.Name == spec.Environment }) {
		return Result{}, &ProviderError{Message: which + " no longer waits on " + spec.Environment + "."}
	}
	body := githubapi.ActionsReviewCustomGatesForRunJSONRequestBody{EnvironmentName: spec.Environment, State: githubapi.ReviewCustomGatesStateRequiredState(spec.State)}
	if _, err := api.ActionsReviewCustomGatesForRunWithResponse(ctx, owner, repo, run, body); err != nil {
		return Result{}, err
	}
	return Result{Changed: true, Message: verb + " " + which + " at " + short(spec.HeadSha, 7) + "."}, nil
}
