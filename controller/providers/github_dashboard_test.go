package providers

import (
	"encoding/json"
	"net/http"
	"slices"
	"testing"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

const dashboardRoute = "/repos/example/app/issues/1"

var dashboardSpec = Object{"repository": "example/app", "branch": "renovate/example-app-2.x", "marker": "unpend"}

// dashboardRoutes is a repository whose open Dashboard has the given body,
// as Renovate writes its boxes.
func dashboardRoutes(body string) map[string]string {
	issues, _ := json.Marshal([]map[string]any{{"number": 1, "title": "Dependency Dashboard", "html_url": "https://github.com/example/app/issues/1", "body": body}})
	return map[string]string{
		githubFakeAPI + " /repos/example/app/issues?state=open&sort=updated&per_page=100": string(issues),
		githubFakeAPI + " PATCH " + dashboardRoute:                                        `{"number":1,"title":"Dependency Dashboard","html_url":"https://github.com/example/app/issues/1"}`,
	}
}

// A tick changes the branch's box alone, under a token for the repository's
// issues alone; a plan reaches GitHub not at all.
func TestADashboardBoxIsTickedAlone(t *testing.T) {
	held := " - [ ] <!-- unpend-branch=renovate/example-app-2.x -->Update example/app\n - [ ] <!-- approve-branch=renovate/other -->Update other\n"
	h := newGitHubHarness(t, dashboardRoutes(held), runtime.Environment{})
	plan, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDashboardBox, "reconcile", dashboardSpec, nil, false)
	if err != nil || !plan.Changed || len(h.recorded()) != 0 {
		t.Fatalf("plan %v %v %v", plan, err, h.recorded())
	}
	result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDashboardBox, "reconcile", dashboardSpec, nil, true)
	if err != nil || !result.Changed {
		t.Fatalf("result %v %v", result, err)
	}
	writes := h.writes()
	if len(writes) != 1 || writes[0].method != http.MethodPatch || writes[0].path != dashboardRoute {
		t.Fatalf("writes %v", writes)
	}
	if want := " - [x] <!-- unpend-branch=renovate/example-app-2.x -->Update example/app\n - [ ] <!-- approve-branch=renovate/other -->Update other\n"; writes[0].body["body"] != want || len(writes[0].body) != 1 {
		t.Errorf("body %v", writes[0].body)
	}
	grant, _ := h.grantOf(writes[0])
	if !slices.Equal(grant.Repositories, []string{"app"}) || grant.Permissions != (githubapi.AppPermissions{Issues: "write"}) {
		t.Errorf("grant %+v", grant)
	}
}

// A box already ticked is left as it is, and one Renovate no longer writes
// is refused: nothing is written for either.
func TestADashboardBoxAlreadyTickedOrGoneWritesNothing(t *testing.T) {
	for _, test := range []struct {
		name, body string
		refused    bool
	}{
		{"ticked", " - [x] <!-- unpend-branch=renovate/example-app-2.x -->Update example/app\n", false},
		{"gone", " - [ ] <!-- unpend-branch=renovate/example-app-3.x -->Update example/app\n", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, dashboardRoutes(test.body), runtime.Environment{})
			result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDashboardBox, "reconcile", dashboardSpec, nil, true)
			if (err != nil) != test.refused || result.Changed || len(h.writes()) != 0 {
				t.Fatalf("result %v, err %v, writes %v", result, err, h.writes())
			}
		})
	}
}
