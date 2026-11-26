package providers

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

const (
	// The deploy review App's connection, beside the main App's hq-app.
	reviewApp   = "deploy-review"
	reviewAppID = "67890"
	reviewHead  = "aaaa000000000000000000000000000000000000"
	reviewRun   = "/repos/example/app/actions/runs/41"
	reviewRoute = reviewRun + "/deployment_protection_rule"
)

func reviewSpec(state string) Object {
	return Object{"repository": "example/app", "run_id": 41, "environment": "release", "head_sha": reviewHead, "state": state}
}

// reviewRoutes is run 41 as GitHub's workflow-run and pending-deployment
// schemas answer it: its status and commit, and the environments it waits on.
func reviewRoutes(status, sha, waitsOn string) map[string]string {
	return map[string]string{
		githubFakeAPI + " " + reviewRun:                          `{"id":41,"name":"Deploy","head_branch":"main","head_sha":"` + sha + `","status":"` + status + `","conclusion":null}`,
		githubFakeAPI + " " + reviewRun + "/pending_deployments": `[{"environment":{"id":9,"name":"` + waitsOn + `"},"wait_timer":0,"wait_timer_started_at":null,"current_user_can_approve":false,"reviewers":[]}]`,
	}
}

func TestGitHubReviewAnswersTheRunAtTheCommitSeen(t *testing.T) {
	for _, state := range []string{"approved", "rejected"} {
		t.Run(state, func(t *testing.T) {
			h := newGitHubHarness(t, reviewRoutes("waiting", reviewHead, "release"), runtime.Environment{})
			plan, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDeploymentReview, "reconcile", reviewSpec(state), nil, false)
			if err != nil || !plan.Changed || len(h.recorded()) != 0 {
				t.Fatalf("a plan reaches GitHub not at all: %v %v %v", plan, err, h.recorded())
			}
			if _, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDeploymentReview, "reconcile", reviewSpec(state), nil, true); err != nil {
				t.Fatal(err)
			}
			writes := h.writes()
			if len(writes) != 1 || writes[0].method != http.MethodPost || writes[0].path != reviewRoute {
				t.Fatalf("writes %v", writes)
			}
			if writes[0].body["environment_name"] != "release" || writes[0].body["state"] != state {
				t.Errorf("the review names the environment and the answer: %v", writes[0].body)
			}
			grant, _ := h.grantOf(writes[0])
			if !slices.Equal(grant.Repositories, []string{"app"}) || grant.Permissions != githubReviewGrant {
				t.Errorf("the review's token holds its repository and the review's grant alone: %+v", grant)
			}
			for _, call := range h.recorded() {
				if strings.HasSuffix(call.path, "/access_tokens") && jwtIssuer(call.auth) != reviewAppID {
					t.Errorf("a review signs as the deploy review App alone: %s", call.auth)
				}
			}
		})
	}
}

func TestGitHubReviewRefusesAnyMismatch(t *testing.T) {
	for _, test := range []struct {
		name   string
		routes map[string]string
		refuse string
		state  string
	}{
		{"the run no longer waits", reviewRoutes("completed", reviewHead, "release"), "", "approved"},
		{"the run deploys another commit", reviewRoutes("waiting", "bbbb000000000000000000000000000000000000", "release"), "", "approved"},
		{"the run waits on another environment", reviewRoutes("waiting", reviewHead, "staging"), "", "approved"},
		{"the run is gone", reviewRoutes("waiting", reviewHead, "release"), reviewRun, "approved"},
		{"the App is not installed", reviewRoutes("waiting", reviewHead, "release"), "/repos/example/app/installation", "approved"},
		{"the answer is neither", reviewRoutes("waiting", reviewHead, "release"), "", "skipped"},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, test.routes, runtime.Environment{})
			if test.refuse != "" {
				h.refuse(githubFakeAPI, test.refuse, http.StatusNotFound)
			}
			_, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDeploymentReview, "reconcile", reviewSpec(test.state), nil, true)
			if _, ok := errors.AsType[*ProviderError](err); !ok {
				t.Fatalf("err %v", err)
			}
			if len(h.writes()) != 0 {
				t.Errorf("answered GitHub: %v", h.writes())
			}
		})
	}
}

// Only the deploy review App answers a rule, so with the main App alone
// connected a review reaches GitHub not at all.
func TestGitHubReviewNeverSignsAsTheMainApp(t *testing.T) {
	h := newGitHubHarness(t, reviewRoutes("waiting", reviewHead, "release"), runtime.Environment{})
	h.r.Supplied = supplied(githubAppConnection("hq-app", "12345"))
	if _, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDeploymentReview, "reconcile", reviewSpec("approved"), nil, true); err == nil {
		t.Fatal("a review with no deploy review App answered")
	}
	if calls := h.recorded(); len(calls) != 0 {
		t.Errorf("reached GitHub: %v", calls)
	}
}

// jwtIssuer is the app a JWT in an Authorization header signs for.
func jwtIssuer(auth string) string {
	parts := strings.Split(strings.TrimPrefix(auth, "Bearer "), ".")
	if len(parts) != 3 {
		return ""
	}
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var claims struct {
		Issuer string `json:"iss"`
	}
	_ = json.Unmarshal(raw, &claims)
	return claims.Issuer
}
