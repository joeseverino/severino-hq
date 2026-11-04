package providers

import (
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What a sweep reports of each App installation's hour: where GitHub's count
// stood before the sweep's first request, and what the sweep spent, so that
// HQ can tell what everything else holding the App's key spent between two
// sweeps. The fake counts the hour as GitHub does, apart from the controller.

// allowanceSweep is one sweep of the repository reading alone, and the
// allowance record it reports beside it.
func allowanceSweep(t *testing.T, h *githubHarness) runtime.GitHubAllowanceRecord {
	t.Helper()
	done := h.r.BeginSnapshot()
	defer done()
	controller := NewController(h.r, runtime.ControllerRegistry{})
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{runtime.ResourceKindGitHubRepository})
	if err != nil {
		t.Fatal(err)
	}
	report, read := inventory[string(runtime.ResourceKindGitHubAllowance)]
	if !read || !report.OK || len(report.Records) != 1 {
		t.Fatalf("a sweep of the repositories reports the installation's hour: %+v", report)
	}
	record, ok := report.Records[0].(runtime.GitHubAllowanceRecord)
	if !ok {
		t.Fatalf("the allowance reading sent %T", report.Records[0])
	}
	return record
}

func TestASweepReportsWhereTheHourStoodAndWhatItSpent(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	h.hour = &githubFakeHour{limit: 5000, used: 120}
	record := allowanceSweep(t, h)
	h.mu.Lock()
	counted := h.hour.used - 120
	h.mu.Unlock()
	if counted == 0 {
		t.Fatal("the sweep spent nothing GitHub counts, so it proves nothing")
	}
	want := runtime.GitHubAllowanceRecord{
		ConnectionRef: "hq-app", Installation: 7, Account: "example",
		Limit: 5000, Used: 120, Remaining: 4880, Spent: counted,
		ResetsAt:  time.Unix(githubFakeReset, 0).UTC().Format(time.RFC3339),
		StartedAt: time.Unix(1_900_000_000, 0).UTC().Format(time.RFC3339),
	}
	if record != want {
		t.Errorf("the sweep reported\n%+v\nGitHub counted\n%+v", record, want)
	}
}

// Between two sweeps, what GitHub counted beyond the first sweep's own spend
// is exactly what others spent: nothing of the controller's is left in it,
// even what a sweep spends before it asks.
func TestOthersSpendIsWhatGitHubCountedBeyondTheSweeps(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	h.hour = &githubFakeHour{limit: 5000, used: 0}
	first := allowanceSweep(t, h)
	const others = 37
	h.mu.Lock()
	h.hour.used += others
	h.mu.Unlock()
	second := allowanceSweep(t, h)
	if got := second.Used - (first.Used + first.Spent); got != others {
		t.Errorf("others spent %d between the sweeps; their records say %d (%+v, %+v)", others, got, first, second)
	}
	third := allowanceSweep(t, h)
	if got := third.Used - (second.Used + second.Spent); got != 0 {
		t.Errorf("nothing else spent between the sweeps; their records say %d", got)
	}
}

// A sweep that asks after nothing, its installation's hour unknown, reports no
// record rather than one that would read as nothing spent.
func TestAnInstallationNeverAskedAfterIsNotReported(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	done := h.r.BeginSnapshot()
	defer done()
	records, err := h.r.githubAllowances(t.Context())
	if err != nil || len(records) != 0 {
		t.Fatalf("a sweep that read nothing reported %+v, %v", records, err)
	}
}
