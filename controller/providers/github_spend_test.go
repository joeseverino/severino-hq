package providers

import (
	"errors"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What a sweep of the GitHub repositories spends of the installation's hourly
// allowance, counted as GitHub counts it: the fake counts every answer under
// an installation token except /rate_limit, a GraphQL query and a 304.

// repositorySweep is one sweep's repository reading under HQ's plan, and how
// many requests GitHub counted against the installation for it.
func repositorySweep(t *testing.T, h *githubHarness, held ...runtime.GitHubRepositoryHeld) (runtime.KindReport, int) {
	t.Helper()
	done := h.r.BeginSnapshot()
	defer done()
	h.mu.Lock()
	before := h.counted
	h.mu.Unlock()
	controller := NewController(h.r, runtime.ControllerRegistry{GithubRepositories: runtime.GitHubRepositoriesPlan{Held: held}})
	report := controller.readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	h.mu.Lock()
	defer h.mu.Unlock()
	if spent := h.r.GitHubSpent(); spent != h.counted-before {
		t.Errorf("the controller counted %d requests, GitHub %d", spent, h.counted-before)
	}
	return report, h.counted - before
}

// heldAfter is the plan HQ sends after storing a report: each record read in
// full, with its fingerprint, not due.
func heldAfter(t *testing.T, report runtime.KindReport) []runtime.GitHubRepositoryHeld {
	t.Helper()
	held := []runtime.GitHubRepositoryHeld{}
	for _, record := range report.Records {
		read, ok := record.(GitHubRepositoryRecord)
		if !ok {
			t.Fatalf("a first read carried %+v", record)
		}
		if read.Fingerprint == "" || read.ReadAt == "" {
			t.Fatalf("%s was read without a fingerprint or a time: %+v", read.Repository, read)
		}
		held = append(held, runtime.GitHubRepositoryHeld{Repository: read.Repository, Fingerprint: read.Fingerprint})
	}
	return held
}

// carried is the repositories a report carries rather than reads.
func carried(report runtime.KindReport) []string {
	found := []string{}
	for _, record := range report.Records {
		if stub, ok := record.(githubCarried); ok && stub.Carried {
			found = append(found, stub.Repository)
		}
	}
	return found
}

func repositoryReads(h *githubHarness, repository string) int {
	reads := 0
	for _, call := range h.recorded() {
		if call.method == "GET" && strings.HasPrefix(call.path, "/repos/"+repository+"/") || call.path == "/repos/"+repository {
			reads++
		}
	}
	return reads
}

func TestASweepOfUnchangedRepositoriesSpendsNothingCounted(t *testing.T) {
	h := newGitHubHarness(t, withSecondRepository(githubRepositoryRoutes()), runtime.Environment{})
	first, full := repositorySweep(t, h)
	if !first.OK || len(first.Records) != 2 {
		t.Fatalf("first sweep %+v", first)
	}
	held := heldAfter(t, first)
	before := repositoryReads(h, "example/app") + repositoryReads(h, "example/other")
	report, spent := repositorySweep(t, h, held...)
	if got := carried(report); !report.OK || len(got) != 2 {
		t.Fatalf("an unchanged repository is carried: %+v", report)
	}
	if after := repositoryReads(h, "example/app") + repositoryReads(h, "example/other"); after != before {
		t.Errorf("an unchanged repository was read %d times", after-before)
	}
	// Listing what the installation covers is the one counted request left.
	if spent > 1 {
		t.Errorf("an unchanged sweep spent %d requests, a full one %d", spent, full)
	}
	t.Logf("two repositories: a full sweep spent %d counted requests, an unchanged one %d", full, spent)
}

func TestOnlyTheRepositoryThatChangedIsRead(t *testing.T) {
	h := newGitHubHarness(t, withSecondRepository(githubRepositoryRoutes()), runtime.Environment{})
	first, _ := repositorySweep(t, h)
	held := heldAfter(t, first)
	h.mu.Lock()
	h.nodes["example/other"] = strings.Replace(githubFakeNode, "aaaa0000", "bbbb0000", 1)
	h.mu.Unlock()
	app, other := repositoryReads(h, "example/app"), repositoryReads(h, "example/other")
	report, spent := repositorySweep(t, h, held...)
	if got := carried(report); len(got) != 1 || got[0] != "example/app" {
		t.Fatalf("the unchanged repository is carried and the changed one read: %v", got)
	}
	if repositoryReads(h, "example/app") != app || repositoryReads(h, "example/other") == other {
		t.Errorf("read the unchanged one %d times, the changed one %d", repositoryReads(h, "example/app")-app, repositoryReads(h, "example/other")-other)
	}
	t.Logf("one repository changed: the sweep spent %d counted requests", spent)
}

// HQ's clock says when a repository is read in full whatever the change
// query says, and a repository HQ holds no fingerprint for is read.
func TestADueRepositoryIsReadInFull(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	first, _ := repositorySweep(t, h)
	held := heldAfter(t, first)
	for _, c := range []struct {
		name string
		held runtime.GitHubRepositoryHeld
	}{
		{"due on HQ's clock", runtime.GitHubRepositoryHeld{Repository: held[0].Repository, Fingerprint: held[0].Fingerprint, Due: true}},
		{"no fingerprint held", runtime.GitHubRepositoryHeld{Repository: held[0].Repository}},
	} {
		t.Run(c.name, func(t *testing.T) {
			report, _ := repositorySweep(t, h, c.held)
			if len(carried(report)) != 0 || len(report.Records) != 1 {
				t.Fatalf("read in full: %+v", report)
			}
		})
	}
}

// limitReason is the words a read refused for the allowance says: until when,
// and that the last reading stands.
const limitReason = "rate limited until 18:06 UTC, the last reading stands"

func TestTheFuseStopsReadsUnderTheFloorAndSaysUntilWhen(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	first, _ := repositorySweep(t, h)
	held := heldAfter(t, first)
	h.mu.Lock()
	h.remaining = githubFloor - 1
	h.nodes["example/app"] = strings.Replace(githubFakeNode, "aaaa0000", "bbbb0000", 1)
	h.mu.Unlock()
	reads := repositoryReads(h, "example/app")

	report, spent := repositorySweep(t, h, held...)
	if got := carried(report); !report.OK || len(got) != 1 {
		t.Fatalf("a held repository stands while the installation is low: %+v", report)
	}
	if spent > 1 || repositoryReads(h, "example/app") != reads {
		t.Errorf("under the floor the sweep spent %d and read the repository %d times", spent, repositoryReads(h, "example/app")-reads)
	}
	if len(report.RefusedParts) != 1 || !strings.Contains(report.RefusedParts[0].Reason, limitReason) || report.RefusedParts[0].Refusal == runtime.FailureClassPermission {
		t.Fatalf("the refusal says until when, and is no permission to grant: %+v", report.RefusedParts)
	}

	// With nothing held, the reading itself is refused, in the same words.
	fresh, _ := repositorySweep(t, h)
	if fresh.OK || !strings.Contains(string(fresh.Error), limitReason) || fresh.Refusal == runtime.RefusalPermission {
		t.Fatalf("refused for the limit: %+v", fresh)
	}
}

func TestTheFuseStopsASweepAtItsCeiling(t *testing.T) {
	routes := githubRepositoryRoutes()
	names := []string{}
	for i := range 8 {
		name := "example/many-" + strconv.Itoa(i)
		names = append(names, `{"full_name":"`+name+`"}`)
		for key, body := range githubRepositoryRoutes() {
			if rest, ok := strings.CutPrefix(key, githubFakeAPI+" /repos/example/app"); ok {
				routes[githubFakeAPI+" /repos/"+name+rest] = body
			}
		}
	}
	routes[githubFakeAPI+" "+githubRepositoriesPage+"1"] = `{"total_count":8,"repositories":[` + strings.Join(names, ",") + `]}`
	h := newGitHubHarness(t, routes, runtime.Environment{})
	report, spent := repositorySweep(t, h)
	// A read already asked when the fuse trips still answers.
	if spent > githubSweepCeiling+githubReadConcurrency {
		t.Errorf("the sweep spent %d past its ceiling of %d", spent, githubSweepCeiling)
	}
	stopped := 0
	for _, refused := range report.RefusedParts {
		if strings.Contains(refused.Reason, "the most HQ spends on one") && strings.Contains(refused.Reason, "the last reading stands") {
			stopped++
		}
	}
	if stopped == 0 {
		t.Fatalf("what the ceiling stopped says so: %+v", report.RefusedParts)
	}
}

// A 403 or 429 GitHub gives for its rate limit is the limit, with when it
// lifts: never "the service answered 403", and never a permission to grant.
func TestARefusalForTheRateLimitIsNamedAsTheLimit(t *testing.T) {
	for _, c := range []struct {
		name    string
		status  int
		headers map[string]string
		body    string
		want    string
	}{
		{"primary", 403, map[string]string{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": strconv.Itoa(githubFakeReset)},
			`{"message":"API rate limit exceeded for installation ID 7."}`, "GitHub's hourly allowance for the app's installation is spent: " + limitReason},
		{"secondary", 429, map[string]string{"Retry-After": "60"},
			`{"message":"You have exceeded a secondary rate limit."}`, "rate limited until 17:47 UTC, the last reading stands"},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
			key := githubFakeAPI + " /repos/example/app"
			h.mu.Lock()
			h.status[key], h.headers[key] = c.status, c.headers
			h.mu.Unlock()
			h.routes[key] = c.body
			report, _ := repositorySweep(t, h)
			if report.OK || !strings.Contains(string(report.Error), c.want) || report.Refusal == runtime.RefusalPermission {
				t.Fatalf("got %+v, want %q", report, c.want)
			}
		})
	}
	// A 403 that is not for the limit is still a refusal of permission.
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app", 403)
	if report, _ := repositorySweep(t, h); report.Refusal != runtime.RefusalPermission {
		t.Fatalf("%+v", report)
	}
}

// Every stop for an allowance, the controller's own or GitHub's, says so in
// a typed mark the worker reads, so a pass it ends is not a failure; a 403
// for anything else is not marked.
func TestEveryStopForAnAllowanceIsMarkedLimited(t *testing.T) {
	now := time.Unix(githubFakeReset-3600, 0).UTC()
	limited := func(err error) bool {
		refusal, ok := errors.AsType[*ProviderError](err)
		return ok && refusal.Limited
	}
	for name, budget := range map[string]*githubBudget{
		"the sweep's ceiling":      {spent: githubSweepCeiling},
		"the floor left to others": {known: true, remaining: githubFloor - 1},
	} {
		budget.check(now)
		if !limited(budget.tripped) {
			t.Errorf("%s: %#v", name, budget.tripped)
		}
	}
	secondary := githubRateLimited(http.Header{"Retry-After": {"60"}}, &ProviderError{HTTPStatus: http.StatusTooManyRequests}, now)
	if !limited(secondary) {
		t.Errorf("GitHub's own limit: %#v", secondary)
	}
	refused := githubRateLimited(http.Header{}, &ProviderError{HTTPStatus: http.StatusForbidden, Failure: runtime.FailureClassPermission}, now)
	if limited(refused) {
		t.Errorf("a refusal of permission is no limit: %#v", refused)
	}
}
