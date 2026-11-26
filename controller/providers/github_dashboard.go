package providers

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A box on a repository's Dependency Dashboard, ticked so Renovate opens the
// pull request of a branch it holds on its next run: the one line changed in
// the body Renovate wrote, under a token for that repository's issues alone.

// githubIssuesWrite is what a tick's token holds: Issues write to edit the
// Dashboard's body.
var githubIssuesWrite = githubapi.AppPermissions{Issues: githubapi.AppPermissionsIssuesWrite}

// GitHubDashboardBoxSpec is a github.dashboard_box declaration.
type GitHubDashboardBoxSpec = runtime.GitHubDashboardBoxSpec

// githubDashboardBox ticks the box Renovate wrote for the branch, as
// dependency-dashboard.ts writes it (getCheckbox): " - [ ] <!-- marker-branch=name -->".
func (r *Registry) githubDashboardBox(ctx context.Context, spec GitHubDashboardBoxSpec, _ Object, apply bool) (Result, error) {
	box := "<!-- " + string(spec.Marker) + "-branch=" + spec.Branch + " -->"
	if !apply {
		return Result{Changed: true, Message: "Would tick " + spec.Branch + " on the Dependency Dashboard."}, nil
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
			return Result{}, &ProviderError{Message: "The GitHub App is not installed on " + spec.Repository + "."}
		}
		return Result{}, err
	}
	issue, err := r.githubRepository(ctx, c, spec.Repository).dashboard(ctx)
	if err != nil {
		return Result{}, err
	}
	if issue.Number == 0 {
		return Result{}, &ProviderError{Message: spec.Repository + " has no open Dependency Dashboard."}
	}
	if strings.Contains(issue.Body, " - [x] "+box) {
		return Result{Message: "Renovate opens " + spec.Branch + " on its next run."}, nil
	}
	if !strings.Contains(issue.Body, " - [ ] "+box) {
		return Result{}, &ProviderError{Message: "Renovate no longer holds " + spec.Branch + "."}
	}
	body := map[string]string{"body": strings.Replace(issue.Body, " - [ ] "+box, " - [x] "+box, 1)}
	api := r.githubAs(ctx, c, []string{spec.Repository}, githubIssuesWrite)
	if _, err := json200(api.IssuesUpdateWithResponse(ctx, owner, repo, strconv.Itoa(issue.Number), body)); err != nil {
		return Result{}, err
	}
	return Result{Changed: true, Message: "Ticked " + spec.Branch + " on the Dependency Dashboard."}, nil
}
