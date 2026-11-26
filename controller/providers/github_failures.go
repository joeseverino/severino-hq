package providers

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Why a failing job failed, in GitHub's words and its log's: the failed
// step's log read up to the line GitHub marks as its error, as GitHub's own
// view of a failed job opens it there. Only a job that failed is read, one
// call for its log, and only a cut of it is kept: the text is the job's own
// output, so every line is bounded and stripped of terminal control codes,
// and HQ shows it as text.

// GitHubFailure is why one job failed: the step it failed at, the error
// GitHub's log marks there, and the step's last lines of output before it.
// A log GitHub would not hand out leaves both unsaid, and is a refused
// job_logs part.
type GitHubFailure struct {
	Step  string   `json:"step"`
	Error string   `json:"error"`
	Log   []string `json:"log"`
}

// The bounds of what a failure keeps: lines of output before the error, each
// said as runtime.Line, and the bytes of a step's log read to find them.
const (
	githubLogLines = 30
	githubLogRead  = 4 << 20
)

var (
	// githubLogStamp is the time GitHub writes before each line of a log.
	githubLogStamp = regexp.MustCompile(`^\x{FEFF}?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?`)
	// githubTerminal is a terminal's escape sequence: a color, a cursor move.
	githubTerminal = regexp.MustCompile(`\x1b\[[0-9;?]*[\x20-\x2f]*[\x40-\x7e]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[\x40-\x5f]`)
)

// failure is why a workflow run's job failed, from the first step that did;
// none for a job that failed before any step did.
func (g *githubRepository) failure(ctx context.Context, job int) (GitHubFailure, error) {
	answer, err := json200(g.api.ActionsGetJobForWorkflowRunWithResponse(ctx, g.owner, g.repo, strconv.Itoa(job)))
	if err != nil {
		return GitHubFailure{}, err
	}
	for _, step := range answer.Steps {
		if !githubFailed[step.Conclusion] {
			continue
		}
		found := GitHubFailure{Step: step.Name, Log: []string{}}
		said, lines, err := g.stepLog(ctx, job, step.Number)
		if limited, ok := errors.AsType[*ProviderError](err); ok && limited.Limited {
			// A spent allowance ends the read, as on any other call.
			return found, err
		}
		if err != nil {
			refuse(ctx, runtime.PartJobLogs, g.c.ref, g.name, err)
			return found, nil
		}
		found.Error, found.Log = said, lines
		return found, nil
	}
	return GitHubFailure{}, nil
}

// stepLog is the error one step's log marks and its output before it, read
// through the signed link GitHub hands out for the step's log.
func (g *githubRepository) stepLog(ctx context.Context, job, step int) (string, []string, error) {
	address := fmt.Sprintf("%s/repos/%s/%s/actions/jobs/%d/steps/%d/logs", githubAPI, url.PathEscape(g.owner), url.PathEscape(g.repo), job, step)
	var said string
	var lines []string
	_, err := g.r.githubSpending(g.token, true, func() (runtime.Answer, error) {
		return g.r.HTTP.Download(ctx, address, githubHeaders(g.token), githubLogRead, func(body io.Reader) error {
			var err error
			said, lines, err = githubLogError(body)
			return err
		})
	})
	return said, lines, err
}

// githubLogError reads a step's log to the first line GitHub marks as an
// error: that error's words, and the last githubLogLines lines of output
// before it. The command a step runs, which GitHub writes as a group headed
// "Run", and GitHub's own markers are not output.
func githubLogError(body io.Reader) (string, []string, error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	lines := []string{}
	command := false
	for scanner.Scan() {
		line := githubLogLine(scanner.Text())
		switch {
		case strings.HasPrefix(line, "##[error]"):
			return runtime.Line(strings.TrimPrefix(line, "##[error]")), lines, nil
		case strings.HasPrefix(line, "##[group]"):
			command = strings.HasPrefix(line, "##[group]Run ")
		case strings.HasPrefix(line, "##[endgroup]"):
			command = false
		case command, strings.HasPrefix(line, "##["), strings.TrimSpace(line) == "":
		default:
			lines = append(lines, runtime.Line(line))
			if len(lines) > githubLogLines {
				lines = lines[1:]
			}
		}
	}
	// A line too long to scan, or a log cut at its bound, ends what is read.
	if err := scanner.Err(); err != nil && !errors.Is(err, bufio.ErrTooLong) {
		return "", nil, err
	}
	return "", lines, nil
}

// githubLogLine is a log's line as text: without its time, a terminal's
// escapes or any other control character.
func githubLogLine(line string) string {
	line = githubTerminal.ReplaceAllString(githubLogStamp.ReplaceAllString(line, ""), "")
	return strings.TrimRightFunc(strings.Map(func(r rune) rune {
		if r == '\t' {
			return ' '
		}
		if unicode.IsControl(r) || r == '\uFEFF' {
			return -1
		}
		return r
	}, line), unicode.IsSpace)
}

// githubArtifactProblem is the heading of the comment Renovate leaves on its
// pull request when it could not update a lock file the update moves, and
// edits away once it can: its fenced blocks are each tool's own words.
const githubArtifactProblem = "Artifact update problem"

// artifactProblem is what the newest artifact problem the updater left on
// an update's pull request says, its fenced lines cut as a log's are, or
// none where the updater left none or is not known.
func (g *githubRepository) artifactProblem(ctx context.Context, pull GitHubPull) ([]string, error) {
	updater, err := g.updater()
	if err != nil || updater == "" || pull.Author != updater {
		return nil, nil
	}
	comments, err := json200(g.api.IssuesListCommentsWithResponse(ctx, g.owner, g.repo, strconv.Itoa(pull.Number), &githubapi.IssuesListCommentsParams{PerPage: new(githubListPage)}))
	if err != nil {
		return nil, err
	}
	for _, comment := range slices.Backward(comments) {
		heading, _, _ := strings.Cut(comment.Body, "\n")
		if comment.User.Login == updater && strings.Contains(heading, githubArtifactProblem) {
			return githubFenced(comment.Body), nil
		}
	}
	return nil, nil
}

// githubFenced is every line inside a markdown body's fenced blocks, the
// last githubLogLines of them.
func githubFenced(body string) []string {
	lines := []string{}
	fenced := false
	for line := range strings.SplitSeq(body, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "```") {
			fenced = !fenced
			continue
		}
		if text := githubLogLine(line); fenced && strings.TrimSpace(text) != "" {
			lines = append(lines, runtime.Line(text))
		}
	}
	return lines[max(0, len(lines)-githubLogLines):]
}
