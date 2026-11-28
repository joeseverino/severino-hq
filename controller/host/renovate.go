package host

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
)

// Where the Renovate container finds what it is given, and the layout of the
// image it runs (ghcr.io/renovatebot/renovate, tools/docker/Dockerfile).
const (
	// renovateUser is the image's own account, USER 12021, the containerbase
	// user its tools are installed for.
	renovateUser   = "12021"
	renovateConfig = "/run/renovate/config.json"
	// renovateReports is where the run writes its report (reportType file,
	// lib/instrumentation/reporting.ts exportStats): a directory of this
	// run's own, the image's account's, and the one host path it may write.
	renovateReports = "/run/renovate/report"
	reportName      = "report.json"
	// reportLimit is far more than one owner's report: each repository's
	// branches and the package files Renovate extracted.
	reportLimit = 32 << 20
	// renovateCache is the cache directory the image makes for every account
	// ("/runner/cache is an alternative cache directory used in both Docker
	// and microVMs"); Renovate's containerbaseDir defaults beneath it, so
	// tool downloads persist with it.
	renovateCache = "/runner/cache"
	// RenovateOwnerBound is the longest one owner's run is given, short of
	// its token's own expiry, which bounds it first.
	RenovateOwnerBound = 45 * time.Minute
	// renovateLogLimit is the size at which the log starts afresh, keeping
	// one generation before it.
	renovateLogLimit = 64 << 20
)

// renovateWritable is every place the container may write, each wiped when
// the run ends but the cache. Renovate keeps its repositories and cache under
// baseDir, /tmp/renovate by default; containerbase installs a run's tools
// into /opt/containerbase (ROOT_DIR, utils/v2/overrides.sh) and the user's
// home, and keeps its state in /tmp/containerbase. The two tool trees are
// anonymous volumes, which docker fills from the image when it makes them and
// --rm removes with the container, so every run starts from the image's own
// tools and nothing a run installs reaches the next.
var renovateWritable = []string{
	"--tmpfs", "/tmp:size=4g,exec,nosuid,nodev",
	"--mount", "type=volume,target=/opt/containerbase",
	"--mount", "type=volume,target=/home/ubuntu",
}

// Renovator runs the spooled dependency handoff: Renovate once per owner, in
// a container that holds that owner's token and nothing of the host's.
type Renovator struct {
	*Machine
	// Spool is RenovateSpool on a host; Outcome is DependencyRunFile.
	Spool, Outcome string
	// OwnerBound is RenovateOwnerBound on a host.
	OwnerBound time.Duration
	Now        func() time.Time
}

// DependencyRun is the outcome root gives the next controller pass.
type DependencyRun struct {
	Version    int        `json:"version"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt time.Time  `json:"finished_at"`
	Runs       []OwnerRun `json:"runs"`
}

// OwnerRun is how one owner's run ended: ok, failed or timeout, and each
// branch its report names.
type OwnerRun struct {
	Owner        string         `json:"owner"`
	Repositories []string       `json:"repositories"`
	ExitCode     int            `json:"exit_code"`
	Outcome      string         `json:"outcome"`
	Branches     []BranchReport `json:"branches"`
}

// BranchReport is one branch as Renovate's report says it ended
// (lib/util/cache/repository/types.ts BranchCache): its result, the pull
// request it has, and the package files its upgrades change.
type BranchReport struct {
	Repository  string   `json:"repository"`
	Branch      string   `json:"branch"`
	Result      string   `json:"result"`
	PullRequest *int     `json:"pull_request"`
	Files       []string `json:"files"`
}

// renovateReport is the part of Renovate's report a run's outcome keeps
// (lib/instrumentation/types.ts Report); the rest is left unread.
type renovateReport struct {
	Repositories map[string]struct {
		Branches []struct {
			BranchName string `json:"branchName"`
			PrNo       *int   `json:"prNo"`
			Result     string `json:"result"`
			Upgrades   []struct {
				PackageFile string `json:"packageFile"`
			} `json:"upgrades"`
		} `json:"branches"`
	} `json:"repositories"`
}

var (
	// A result as Renovate words one (lib/workers/types.ts BranchResult).
	resultShape = api.MustPattern("GitHubDependencyRunBranch", "properties", "result")
	// A package file: a path in the repository, never above it.
	fileShape = regexp.MustCompile(`^[A-Za-z0-9_.][A-Za-z0-9_./ -]{0,254}$`)
)

// reportLimits bound what one run's outcome keeps of its report.
const (
	branchesKept = 500
	filesKept    = 20
)

// Run claims the spooled handoff, runs it, and records the outcome. Nothing
// spooled is nothing to do. The handoff and every environment file are gone
// when it returns, however it ends; a run killed before that leaves them for
// the next to remove.
func (r *Renovator) Run(ctx context.Context) error {
	work, data, err := r.claim()
	if work != "" {
		defer os.RemoveAll(work)
	}
	if err != nil || data == nil {
		return err
	}
	handoff, err := ParseHandoff(data)
	if err != nil {
		return fmt.Errorf("the spooled handoff: %w", err)
	}
	pin, err := os.ReadFile(filepath.Join(r.Lib, "deploy", "renovate", "image"))
	if err != nil {
		return err
	}
	image, err := Pinned(strings.TrimSpace(string(pin)))
	if err != nil {
		return err
	}
	if err := renovateEgress.confine(ctx, r.Machine); err != nil {
		return err
	}
	log, err := r.openLog()
	if err != nil {
		return err
	}
	defer log.Close()

	outcome := DependencyRun{Version: handoffV1, StartedAt: r.Now().UTC(), Runs: []OwnerRun{}}
	for i, run := range handoff.Runs {
		outcome.Runs = append(outcome.Runs, r.runOwner(ctx, work, strconv.Itoa(i), image, log, run))
	}
	outcome.FinishedAt = r.Now().UTC()
	return r.record(outcome)
}

// claim moves the spooled handoff into a directory of this run's, so one
// arriving meanwhile waits for the next. What an earlier run left is removed:
// the unit runs one at a time.
func (r *Renovator) claim() (string, []byte, error) {
	if err := ownDirectory(r.Spool); err != nil {
		return "", nil, err
	}
	left, _ := filepath.Glob(filepath.Join(r.Spool, "run.*"))
	for _, dir := range left {
		os.RemoveAll(dir)
	}
	work, err := os.MkdirTemp(r.Spool, "run.")
	if err != nil {
		return "", nil, err
	}
	claimed := filepath.Join(work, SpoolName)
	if err := os.Rename(filepath.Join(r.Spool, SpoolName), claimed); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			err = nil
		}
		return work, nil, err
	}
	data, err := os.ReadFile(claimed)
	return work, data, err
}

// runOwner runs Renovate over one owner's repositories, until it ends, its
// token expires, or the bound passes.
func (r *Renovator) runOwner(ctx context.Context, work, at, image string, log io.Writer, run HandoffRun) OwnerRun {
	result := OwnerRun{Owner: run.Owner, Repositories: run.Repositories, ExitCode: -1, Outcome: "timeout", Branches: []BranchReport{}}
	deadline := r.Now().Add(r.OwnerBound)
	if run.ExpiresAt.Before(deadline) {
		deadline = run.ExpiresAt
	}
	if !deadline.After(r.Now()) {
		fmt.Fprintf(log, "%s %s: its token expired before its turn\n", r.Now().UTC().Format(time.RFC3339), run.Owner)
		return result
	}
	env, reports := filepath.Join(work, at+".env"), filepath.Join(work, at+".report")
	defer os.Remove(env)
	defer os.RemoveAll(reports)
	if err := r.prepare(env, reports, run); err != nil {
		result.Outcome = "failed"
		fmt.Fprintf(log, "%s %s: %v\n", r.Now().UTC().Format(time.RFC3339), run.Owner, err)
		return result
	}
	own := r.beginRun()
	defer own.end()
	name := "severino-hq-renovate-" + rand.Text()
	runCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	fmt.Fprintf(log, "%s %s: %d repositories\n", r.Now().UTC().Format(time.RFC3339), run.Owner, len(run.Repositories))
	command := r.Command(runCtx, "docker", slices.Insert(RenovateArgs(name, image, env, r.configPath(), reports, run.Owner), 1, own.label())...)
	command.Stdout, command.Stderr = log, log
	err := command.Run()
	if command.ProcessState != nil {
		result.ExitCode = command.ProcessState.ExitCode()
	}
	switch {
	case runCtx.Err() != nil:
		// Killing the client leaves its container running: remove it, and
		// the tool trees with it.
		stop, done := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
		_ = r.Command(stop, "docker", "rm", "--force", "--volumes", name).Run()
		done()
		if !errors.Is(runCtx.Err(), context.DeadlineExceeded) {
			result.Outcome = "failed"
		}
	case err == nil:
		result.Outcome = "ok"
	default:
		result.Outcome = "failed"
	}
	// Whatever the run's end, the report it wrote says what became of each
	// branch it reached.
	result.Branches = readReport(reports, run.Repositories, log)
	fmt.Fprintf(log, "%s %s: %s, exit %d\n", r.Now().UTC().Format(time.RFC3339), run.Owner, result.Outcome, result.ExitCode)
	return result
}

// prepare writes the run's environment file, root's to read, and makes its
// report directory, inside the run's own directory, which nobody but root
// enters. The container's account may make a file in it and never list it
// or remove root's, and root reads the file it makes as any other, since a
// run's unit holds no capability to override a file's mode (01733: sticky,
// writable and searchable by others). A run that names branches
// has Renovate open them in its one repository (checkedBranches, read from
// RENOVATE_CHECKED_BRANCHES as JSON: lib/workers/global/config/parse/env.ts).
func (r *Renovator) prepare(env, reports string, run HandoffRun) error {
	body := "RENOVATE_TOKEN=" + run.Token.Reveal() + "\nRENOVATE_REPOSITORIES=" + strings.Join(run.Repositories, ",") + "\n"
	if len(run.Branches) > 0 {
		branches, err := json.Marshal(run.Branches)
		if err != nil {
			return err
		}
		body += "RENOVATE_CHECKED_BRANCHES=" + string(branches) + "\n"
	}
	if err := os.WriteFile(env, []byte(body), 0o400); err != nil {
		return err
	}
	if err := os.Mkdir(reports, 0o700); err != nil {
		return err
	}
	return os.Chmod(reports, 0o1733) //nolint:gosec // G302: the container's account writes its report here, as the comment above says
}

// readReport is each branch of repositories that the report in reports
// names, as far as it has the shape a branch does; none when there is no
// report. The container wrote it, so it is read as untrusted: a regular file
// with one name, bounded, and only what fits each field's shape is kept.
func readReport(reports string, repositories []string, log io.Writer) []BranchReport {
	found := []BranchReport{}
	root, err := os.OpenRoot(reports)
	if err != nil {
		return found
	}
	defer root.Close()
	data, err := readUntrusted(root, reportName, reportLimit)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(log, "the run's report was not read: %v\n", err)
		}
		return found
	}
	var report renovateReport
	if err := json.Unmarshal(data, &report); err != nil {
		fmt.Fprintln(log, "the run's report is not Renovate's report")
		return found
	}
	for _, repository := range repositories {
		for named, each := range report.Repositories {
			if !strings.EqualFold(named, repository) {
				continue
			}
			for _, branch := range each.Branches {
				if len(found) == branchesKept || !branchShape.MatchString(branch.BranchName) || strings.Contains(branch.BranchName, "..") || !resultShape.MatchString(branch.Result) {
					continue
				}
				kept := BranchReport{Repository: repository, Branch: branch.BranchName, Result: branch.Result, Files: []string{}}
				if branch.PrNo != nil && *branch.PrNo > 0 {
					kept.PullRequest = branch.PrNo
				}
				for _, upgrade := range branch.Upgrades {
					file := upgrade.PackageFile
					if len(kept.Files) < filesKept && fileShape.MatchString(file) && !strings.Contains(file, "..") && !slices.Contains(kept.Files, file) {
						kept.Files = append(kept.Files, file)
					}
				}
				found = append(found, kept)
			}
		}
	}
	return found
}

func (r *Renovator) configPath() string {
	return filepath.Join(r.Lib, "deploy", "renovate", "config.json")
}

// RenovateArgs is the container one owner's run is: the image's own account
// with no capability and no way to gain one, a read-only root with only
// renovateWritable and the owner's cache to write, its own network, from which
// the egress table lets it reach public addresses alone (never the host, the
// LAN, the tailnet or another container), public resolvers, and nothing of
// the host's but the global configuration and the run's own report
// directory. Its token, repositories and branches arrive in the environment
// file, which docker reads.
func RenovateArgs(name, image, envFile, config, reports, owner string) []string {
	args := []string{
		"run", "--rm", "--name", name,
		"--user", renovateUser,
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--read-only",
		"--network", RenovateNetwork,
	}
	for _, resolver := range PublicResolvers {
		args = append(args, "--dns", resolver)
	}
	args = append(args, "--pids-limit", "4096")
	args = append(args, renovateWritable...)
	return append(args,
		"--mount", "type=volume,source="+RenovateCacheVolume(owner)+",target="+renovateCache,
		"--mount", readOnly(config, renovateConfig),
		"--mount", "type=bind,source="+reports+",target="+renovateReports,
		"--env-file", envFile,
		"--env", "RENOVATE_CONFIG_FILE="+renovateConfig,
		"--env", "RENOVATE_CACHE_DIR="+renovateCache,
		"--env", "RENOVATE_REPORT_TYPE=file",
		"--env", "RENOVATE_REPORT_PATH="+renovateReports+"/"+reportName,
		image)
}

// RenovateCacheVolume is one owner's cache: what Renovate keeps of one
// owner's repositories is never mounted into another's run.
func RenovateCacheVolume(owner string) string {
	return "severino-hq-renovate-cache-" + strings.ToLower(owner)
}

// openLog is Renovate's log, root's alone and kept on the host: it names
// repositories and what changed in them.
func (r *Renovator) openLog() (*os.File, error) {
	if err := os.MkdirAll(r.LogDir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(r.LogDir, "renovate.log")
	if info, err := os.Lstat(path); err == nil && info.Size() > renovateLogLimit {
		// A log that cannot rotate keeps growing; the run still logs.
		_ = os.Rename(path, path+".1")
	}
	return os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o600)
}

// record writes the outcome where the next controller pass is given it.
func (r *Renovator) record(outcome DependencyRun) error {
	data, err := json.Marshal(outcome)
	if err != nil {
		return err
	}
	if err := ownDirectory(filepath.Dir(r.Outcome)); err != nil {
		return err
	}
	return atomicfile.WriteFile(r.Outcome, data, 0o600)
}
