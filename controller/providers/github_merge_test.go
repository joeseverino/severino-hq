package providers

import (
	"errors"
	"net/http"
	"slices"
	"testing"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	mergeHead  = "eeee000000000000000000000000000000000000"
	mergeRoute = "/repos/example/app/pulls/6/merge"
)

var mergeSpec = Object{"repository": "example/app", "number": 6, "head_sha": mergeHead}

// mergeRoutes is a pull request whose head's checks answer as given.
func mergeRoutes(checks string) map[string]string {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	return map[string]string{
		api("/actions/runs?head_sha=" + mergeHead + "&per_page=50"): `{"workflow_runs":[{"id":5,"name":"Dependency graph","event":"dynamic","check_suite_id":50,"status":"completed","conclusion":"failure"}]}`,
		api("/commits/" + mergeHead + "/check-runs?per_page=100"):   checks,
		githubFakeAPI + " PUT " + mergeRoute:                        `{"sha":"ffff000000000000000000000000000000000000","merged":true,"message":"Pull Request successfully merged"}`,
	}
}

// GitHub's own dynamic check fails here, and is not the repository's.
const mergeChecksPass = `{"check_runs":[{"id":9,"name":"build","status":"completed","conclusion":"success"},{"id":10,"name":"graph","status":"completed","conclusion":"failure","check_suite":{"id":50}}]}`

func TestGitHubMergeSquashesTheApprovedHead(t *testing.T) {
	h := newGitHubHarness(t, mergeRoutes(mergeChecksPass), runtime.Environment{})

	plan, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubPullRequest, "merge", mergeSpec, nil, false)
	if err != nil || !plan.Changed || len(h.recorded()) != 0 {
		t.Fatalf("a plan reaches GitHub not at all: %v %v %v", plan, err, h.recorded())
	}
	result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubPullRequest, "merge", mergeSpec, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	if status := result.Status.(runtime.GitHubMergeStatus); status.MergeSha != "ffff000000000000000000000000000000000000" || !result.Changed {
		t.Fatalf("result %+v", result)
	}
	writes := h.writes()
	if len(writes) != 1 || writes[0].method != http.MethodPut || writes[0].path != mergeRoute {
		t.Fatalf("writes %v", writes)
	}
	if writes[0].body["merge_method"] != "squash" || writes[0].body["sha"] != mergeHead {
		t.Errorf("the merge names the approved head: %v", writes[0].body)
	}
	grant, _ := h.grantOf(writes[0])
	if !slices.Equal(grant.Repositories, []string{"app"}) || grant.Permissions != (githubapi.AppPermissions{Contents: "write", PullRequests: "write"}) {
		t.Errorf("the merge's token holds its repository and its permissions alone: %+v", grant)
	}
}

func TestGitHubMergeRefusalsSayWhy(t *testing.T) {
	failing := `{"check_runs":[{"id":9,"name":"build","status":"completed","conclusion":"failure"}]}`
	for _, test := range []struct {
		name   string
		checks string
		refuse map[string]int
		why    runtime.GitHubMergeRefusal
		merges bool
	}{
		{"the head moved after the approval", mergeChecksPass, map[string]int{mergeRoute: http.StatusConflict}, runtime.GitHubMergeRefusalHeadMoved, true},
		{"GitHub cannot merge it as it stands", mergeChecksPass, map[string]int{mergeRoute: http.StatusMethodNotAllowed}, runtime.GitHubMergeRefusalNotMergeable, true},
		{"its head fails its checks", failing, nil, runtime.GitHubMergeRefusalChecksFailing, false},
		{"the App is not installed on it", mergeChecksPass, map[string]int{"/repos/example/app/installation": http.StatusNotFound}, runtime.GitHubMergeRefusalNotInstalled, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, mergeRoutes(test.checks), runtime.Environment{})
			for path, code := range test.refuse {
				h.refuse(githubFakeAPI, path, code)
			}
			_, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubPullRequest, "merge", mergeSpec, nil, true)
			refusal, ok := errors.AsType[*ProviderError](err)
			if !ok {
				t.Fatalf("err %v", err)
			}
			if status, _ := refusal.Status.(runtime.GitHubMergeStatus); status.Refused != test.why || status.MergeSha != "" {
				t.Errorf("status %+v, want %q", refusal.Status, test.why)
			}
			if merged := len(h.writes()) > 0; merged != test.merges {
				t.Errorf("asked GitHub to merge: %v", h.writes())
			}
		})
	}
}
