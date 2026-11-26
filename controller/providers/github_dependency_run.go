package providers

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// HQ's dependency update run. HQ plans it (which repositories, by owner, and
// whether a run is due); this controller hands it to root as the grant
// host.DependencyUpdates (github_grant.go). Root runs Renovate outside this
// container and gives the next pass the result, which is the
// github.dependency_run reading.

// DependencyHandoffName is the file root takes from HQ_CONTROLLER_OUT.
var DependencyHandoffName = host.DependencyUpdates.HandoffName()

const (
	dependencyRunVersion = 1
	// dependencyRunMaxBytes bounds the result root gives: a run per owner,
	// each naming repositories, is far smaller.
	dependencyRunMaxBytes = 1 << 20
	// dependencyHandedOff is a run's outcome before root reports one.
	dependencyHandedOff = "handed_off"
)

var (
	githubRunOwner      = api.MustPattern("GitHubDependencyRunRecord", "properties", "owner")
	githubRunRepository = api.MustPattern("GitHubDependencyRunRecord", "properties", "repositories", "items")
	githubRunOutcome    = api.MustPattern("GitHubDependencyRunRecord", "properties", "outcome")
)

// GitHubDependencyRunRecord is one owner's run: handed off, or as root
// reported it. ExitCode is null until the run has finished.
type GitHubDependencyRunRecord struct {
	Owner        string   `json:"owner"`
	Repositories []string `json:"repositories"`
	Outcome      string   `json:"outcome"`
	ExitCode     *int     `json:"exit_code"`
	StartedAt    string   `json:"started_at"`
	FinishedAt   string   `json:"finished_at"`
}

// DependencyRunResult is what root reports of the last run.
type DependencyRunResult struct {
	Version    int                      `json:"version"`
	StartedAt  string                   `json:"started_at"`
	FinishedAt string                   `json:"finished_at"`
	Runs       []DependencyRunResultRun `json:"runs"`
}

type DependencyRunResultRun struct {
	Owner        string   `json:"owner"`
	Repositories []string `json:"repositories"`
	ExitCode     *int     `json:"exit_code"`
	Outcome      string   `json:"outcome"`
}

func (r *Registry) admitGitHubDependencyRun() {
	r.reader(runtime.ResourceKindGitHubDependencyRun, r.githubDependencyRuns)
	// What HQ planned and what root reported are the sources, not a read of
	// GitHub: with neither there is nothing to say.
	r.readsOnlyHeld(runtime.ResourceKindGitHubDependencyRun, func() bool {
		return len(r.Updates.Owners) > 0 || r.Env.DependencyRun != ""
	})
}

// githubDependencyRuns reports root's result when it is newer than the
// latest run HQ holds, and hands off a run when the plan says one is due.
// With neither, HQ keeps what it holds.
func (r *Registry) githubDependencyRuns(ctx context.Context) ([]any, error) {
	plan := r.Updates
	if err := checkDependencyPlan(plan); err != nil {
		return nil, err
	}
	records := []any{}
	result, err := readDependencyRunResult(r.Env.DependencyRun)
	if err != nil {
		return nil, err
	}
	if result != nil && newerThan(result.StartedAt, plan.LastStartedAt) {
		for _, run := range result.Runs {
			records = append(records, GitHubDependencyRunRecord{
				Owner: run.Owner, Repositories: run.Repositories, Outcome: run.Outcome, ExitCode: run.ExitCode,
				StartedAt: result.StartedAt, FinishedAt: result.FinishedAt,
			})
		}
	}
	if plan.Due && len(plan.Owners) > 0 {
		handed, err := r.handOffDependencyRuns(ctx, plan)
		if err != nil {
			return nil, err
		}
		for _, record := range handed {
			records = append(records, record)
		}
	}
	if len(records) == 0 {
		return nil, errCarried
	}
	return records, nil
}

// newerThan is whether started is after held; nothing held is older than any run.
func newerThan(started, held string) bool {
	if held == "" {
		return true
	}
	at, _ := time.Parse(time.RFC3339, started)
	before, err := time.Parse(time.RFC3339Nano, held)
	return err != nil || at.After(before)
}

// checkDependencyPlan refuses a plan naming an owner or a repository that is
// not GitHub's, or a repository under another owner: one token is one account.
func checkDependencyPlan(plan runtime.DependencyUpdatesPlan) error {
	for _, owner := range plan.Owners {
		if err := checkRunRepositories(owner.Owner, owner.Repositories); err != nil {
			return err
		}
	}
	return nil
}

func checkRunRepositories(owner string, repositories []string) error {
	if !githubRunOwner.MatchString(owner) {
		return &ProviderError{Message: fmt.Sprintf("%q is not a GitHub account", owner)}
	}
	if len(repositories) == 0 {
		return &ProviderError{Message: owner + " names no repository"}
	}
	for _, name := range repositories {
		if !githubRunRepository.MatchString(name) || !strings.HasPrefix(name, owner+"/") {
			return &ProviderError{Message: fmt.Sprintf("%q is not a repository of %s", name, owner)}
		}
	}
	return nil
}

// handOffDependencyRuns hands off the planned run, answering a record per
// owner handed off. Without HQ_CONTROLLER_OUT root has given this pass
// nowhere to hand to, so nothing is minted. An owner the App is not installed
// on, or whose token GitHub refuses, is refused alone; when every owner is,
// the read fails.
func (r *Registry) handOffDependencyRuns(ctx context.Context, plan runtime.DependencyUpdatesPlan) ([]GitHubDependencyRunRecord, error) {
	if r.Env.Out == "" {
		r.commands().logger().Warn("a dependency update run is due, but this pass was given no HQ_CONTROLLER_OUT to hand it to root through",
			slog.String("event", "controller.dependency_run.skipped"))
		return nil, nil
	}
	c, err := r.githubConnection("")
	if err != nil {
		return nil, err
	}
	started := r.Now().UTC().Format(time.RFC3339)
	handed, err := r.handOffGrant(ctx, c, host.DependencyUpdates.Name, plan.Owners, githubRenovate, func(owner string, err error) {
		refuse(ctx, runtime.PartWhole, c.ref, owner, err)
	})
	if err != nil {
		return nil, err
	}
	records := make([]GitHubDependencyRunRecord, len(handed))
	for i, owner := range handed {
		records[i] = GitHubDependencyRunRecord{Owner: owner.Owner, Repositories: owner.Repositories, Outcome: dependencyHandedOff, StartedAt: started}
	}
	return records, nil
}

// errDependencyResult is a result file that is not root's account of a run.
var errDependencyResult = &ProviderError{Message: "the dependency update result is not a run's result"}

// readDependencyRunResult is root's result at path, or nil when root gave
// none. Anything but the exact shape is refused, naming no line of it.
func readDependencyRunResult(path string) (*DependencyRunResult, error) {
	if path == "" {
		return nil, nil
	}
	file, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, &ProviderError{Message: "the dependency update result cannot be read", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, dependencyRunMaxBytes+1))
	if err != nil {
		return nil, &ProviderError{Message: "the dependency update result cannot be read", Err: err}
	}
	if len(data) > dependencyRunMaxBytes {
		return nil, errDependencyResult
	}
	return parseDependencyRunResult(data)
}

func parseDependencyRunResult(data []byte) (*DependencyRunResult, error) {
	var result DependencyRunResult
	if err := jsonv2.Unmarshal(data, &result, jsonv2.RejectUnknownMembers(true)); err != nil || result.Version != dependencyRunVersion {
		return nil, errDependencyResult
	}
	started, err := time.Parse(time.RFC3339, result.StartedAt)
	if err != nil {
		return nil, errDependencyResult
	}
	finished, err := time.Parse(time.RFC3339, result.FinishedAt)
	if err != nil || finished.Before(started) {
		return nil, errDependencyResult
	}
	for _, run := range result.Runs {
		finishedRun := run.Outcome != dependencyHandedOff && githubRunOutcome.MatchString(run.Outcome)
		if checkRunRepositories(run.Owner, run.Repositories) != nil || run.ExitCode == nil || !finishedRun {
			return nil, errDependencyResult
		}
	}
	result.StartedAt, result.FinishedAt = started.UTC().Format(time.RFC3339), finished.UTC().Format(time.RFC3339)
	return &result, nil
}
