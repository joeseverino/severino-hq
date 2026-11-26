package providers

import (
	"net/http"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubLogHost is where the fake's signed log links point.
const githubLogHost = "logs.example.com"

// stepLog serves one step's log as GitHub does: the API answers with a
// redirect to a signed link, and the link's host serves the plain text.
func (f *githubFake) stepLog(job, step int, log string) {
	key := "/repos/example/app/actions/jobs/" + strconv.Itoa(job) + "/steps/" + strconv.Itoa(step) + "/logs"
	link := "/signed/" + strconv.Itoa(job) + "/" + strconv.Itoa(step)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.status[githubFakeAPI+" "+key] = http.StatusFound
	f.headers[githubFakeAPI+" "+key] = map[string]string{"Location": "https://" + githubLogHost + link + "?sig=example"}
	f.routes[githubLogHost+" "+link+"?sig=example"] = log
}

// githubLog is a step's log as GitHub writes it: each line after its time.
func githubLog(lines ...string) string {
	stamped := make([]string, len(lines))
	for i, line := range lines {
		stamped[i] = "2030-01-01T00:00:0" + strconv.Itoa(i%10) + ".1234567Z " + line
	}
	return "\uFEFF" + strings.Join(stamped, "\n") + "\n"
}

// The two shapes a failure's cause takes in a log: a tool that prints why
// above the generic exit GitHub marks (mise's lock refusing an install), and
// a scanner whose report runs many lines above it (govulncheck naming a
// standard library fixed in a later release).
var (
	githubMiseLog = githubLog(
		"##[group]Run jdx/mise-action@0123456789abcdef0123456789abcdef01234567",
		"with:",
		"  install: true",
		"##[endgroup]",
		"[command]/home/runner/.local/bin/mise install",
		"\x1b[31mmise ERROR\x1b[0m Failed to install aqua:astral-sh/uv@0.13.10: uv@0.13.10 is not in the lockfile",
		"\x1b[31mmise ERROR\x1b[0m Run with --verbose or MISE_VERBOSE=1 for more information",
		"##[error]The process '/home/runner/.local/bin/mise' failed with exit code 1",
		"##[group]Run never read",
	)
	githubVulnLog = githubLog(
		"##[group]Run go tool govulncheck ./...",
		"go tool govulncheck ./...",
		"shell: /usr/bin/bash -e {0}",
		"##[endgroup]",
		"=== Symbol Results ===",
		"",
		"Vulnerability #1: GO-2026-6859",
		"    A crafted request can exhaust a server's memory in net/http.",
		"  More info: https://pkg.go.dev/vuln/GO-2026-6859",
		"  Standard library",
		"    Found in: net/http@go1.27.3",
		"    Fixed in: net/http@go1.27.4",
		"    Example traces found:",
		"\t#1: main.go:12:3: example.serve calls http.ListenAndServe",
		"",
		"Your code is affected by 1 vulnerability from the Go standard library.",
		"Use '-show verbose' for more details.",
		"##[error]Process completed with exit code 3.",
	)
)

// A failing job says why from its failed step's log: GitHub's error and the
// output before it, read through the signed link with no credential sent
// past it, its times, terminal colors and the step's command left out, and
// cut to its bound however long the log runs, with every secret the
// controller learned struck.
func TestAFailingJobSaysWhyFromItsStepsLog(t *testing.T) {
	mask.Learn("example-learned-secret")
	long := []string{}
	for i := range githubLogLines * 3 {
		long = append(long, "line "+strconv.Itoa(i)+" "+strings.Repeat("x", runtime.SaidLimit))
	}
	for _, test := range []struct {
		name, log, error string
		says             []string
	}{
		{"a lock that refuses an install", githubMiseLog, "The process '/home/runner/.local/bin/mise' failed with exit code 1", []string{
			"[command]/home/runner/.local/bin/mise install",
			"mise ERROR Failed to install aqua:astral-sh/uv@0.13.10: uv@0.13.10 is not in the lockfile",
			"mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information",
		}},
		{"a standard library fixed in a later release", githubVulnLog, "Process completed with exit code 3.", []string{
			"=== Symbol Results ===",
			"Vulnerability #1: GO-2026-6859",
			"    A crafted request can exhaust a server's memory in net/http.",
			"  More info: https://pkg.go.dev/vuln/GO-2026-6859",
			"  Standard library",
			"    Found in: net/http@go1.27.3",
			"    Fixed in: net/http@go1.27.4",
			"    Example traces found:",
			" #1: main.go:12:3: example.serve calls http.ListenAndServe",
			"Your code is affected by 1 vulnerability from the Go standard library.",
			"Use '-show verbose' for more details.",
		}},
		{"a secret the controller learned", githubLog("token example-learned-secret", "##[error]Process completed with exit code 1."), "Process completed with exit code 1.", []string{"token " + mask.Redacted}},
		{"a log longer than its bound", githubLog(append(long, "##[error]Process completed with exit code 1.")...), "Process completed with exit code 1.", nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			routes := githubRepositoryRoutes()
			api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
			routes[api("/actions/runs?branch=main&per_page=100")] = `{"workflow_runs":[{"id":50,"name":"CI","workflow_id":11,"check_suite_id":80,"status":"completed","conclusion":"failure","event":"push","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","created_at":"2030-01-03T00:00:00Z"}]}`
			routes[api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100")] = `{"check_runs":[{"id":81,"name":"lint","status":"completed","conclusion":"failure","check_suite":{"id":80}}]}`
			routes[api("/actions/jobs/81")] = `{"id":81,"steps":[{"name":"Set up job","status":"completed","conclusion":"success","number":1},{"name":"Check","status":"completed","conclusion":"failure","number":2}]}`
			h := newGitHubHarness(t, routes, runtime.Environment{})
			h.stepLog(81, 2, test.log)
			record, _ := readRepository(t, h)

			failure := record.Checks.Failures["lint"]
			if failure.Step != "Check" || failure.Error != test.error {
				t.Fatalf("failure %+v", failure)
			}
			if test.says != nil && !slices.Equal(failure.Log, test.says) {
				t.Errorf("log %q", failure.Log)
			}
			if len(failure.Log) > githubLogLines {
				t.Errorf("kept %d lines", len(failure.Log))
			}
			for _, line := range failure.Log {
				if len([]rune(line)) > runtime.SaidLimit {
					t.Errorf("kept a line of %d characters", len([]rune(line)))
				}
			}
			for _, call := range h.recorded() {
				if call.host == githubLogHost && call.auth != "" {
					t.Errorf("the log's host was sent %q", call.auth)
				}
			}
		})
	}
}

// A commit with more failing jobs than a reading says why of reads only as
// many job logs as it keeps, and a log GitHub would not hand out is a refused
// part, the job still named by the step it failed at.
func TestFailingJobsReadOnlyTheLogsKeptAndARefusedLogShows(t *testing.T) {
	routes := githubRepositoryRoutes()
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes[api("/actions/runs?branch=main&per_page=100")] = `{"workflow_runs":[{"id":50,"name":"CI","workflow_id":11,"check_suite_id":80,"status":"completed","conclusion":"failure","event":"push","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","created_at":"2030-01-03T00:00:00Z"}]}`
	jobs := githubFailuresKept + 2
	checks := []string{}
	for job := 81; job < 81+jobs; job++ {
		checks = append(checks, `{"id":`+strconv.Itoa(job)+`,"name":"job `+strconv.Itoa(job)+`","status":"completed","conclusion":"failure","check_suite":{"id":80}}`)
		routes[api("/actions/jobs/"+strconv.Itoa(job))] = `{"id":` + strconv.Itoa(job) + `,"steps":[{"name":"Check","status":"completed","conclusion":"failure","number":2}]}`
	}
	routes[api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100")] = `{"check_runs":[` + strings.Join(checks, ",") + `]}`
	h := newGitHubHarness(t, routes, runtime.Environment{})
	for job := 82; job < 81+jobs; job++ {
		h.stepLog(job, 2, githubLog("##[error]Process completed with exit code 1."))
	}
	h.refuse(githubFakeAPI, "/repos/example/app/actions/jobs/81/steps/2/logs", http.StatusForbidden)
	record, refused := readRepository(t, h)

	if len(record.Checks.Failures) != githubFailuresKept || record.Checks.Failures["job 81"].Step != "Check" {
		t.Fatalf("failures %+v", record.Checks.Failures)
	}
	read := 0
	for _, call := range h.recorded() {
		if job, err := strconv.Atoi(strings.TrimPrefix(call.path, "/repos/example/app/actions/jobs/")); err == nil && job >= 81 {
			read++
		}
	}
	if read != githubFailuresKept {
		t.Errorf("read %d jobs", read)
	}
	if !slices.ContainsFunc(refused, func(part runtime.RefusedPart) bool {
		return part.Part == runtime.PartJobLogs && part.Scope == "example/app"
	}) {
		t.Errorf("refused %+v", refused)
	}
}

// A failing check no workflow owns says why in its own output, its title and
// its summary, at no request of its own; one whose output is empty says nothing.
func TestAFailingCheckNoWorkflowOwnsSaysWhyFromItsOutput(t *testing.T) {
	routes := githubRepositoryRoutes()
	routes[githubFakeAPI+" /repos/example/app/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100"] = `{"check_runs":[` +
		`{"id":91,"name":"license","status":"completed","conclusion":"failure","check_suite":{"id":90},"output":{"title":"1 dependency has a denied license","summary":"example-lib 1.2.0: GPL-3.0 is denied\n\nSee the policy.","text":null,"annotations_count":0,"annotations_url":"https://api.github.com/repos/example/app/check-runs/91/annotations"}},` +
		`{"id":92,"name":"quiet","status":"completed","conclusion":"failure","check_suite":{"id":90},"output":{"title":null,"summary":null,"text":null,"annotations_count":0,"annotations_url":"https://api.github.com/repos/example/app/check-runs/92/annotations"}}]}`
	h := newGitHubHarness(t, routes, runtime.Environment{})
	record, _ := readRepository(t, h)

	said := record.Checks.Failures["license"]
	if said.Error != "1 dependency has a denied license" || !slices.Equal(said.Log, []string{"example-lib 1.2.0: GPL-3.0 is denied", "See the policy."}) {
		t.Fatalf("failure %+v", said)
	}
	if _, ok := record.Checks.Failures["quiet"]; ok {
		t.Errorf("an empty output said %+v", record.Checks.Failures["quiet"])
	}
}
