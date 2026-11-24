package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
)

// Every uses: line a repository's workflows and composite actions run, and
// the commit each unpinned one names now.

var (
	// A uses: value: an action or reusable workflow and the ref it is taken at.
	githubUses   = regexp.MustCompile(`(?m)^\s*(?:-\s*)?uses:\s*["']?([^\s"'#]+)`)
	githubCommit = regexp.MustCompile(`^[0-9a-f]{40}$`)
	// A reusable workflow another repository holds.
	githubCalled = regexp.MustCompile(`^[^./][^/]*/[^/]+/\.github/workflows/[^/]+\.ya?ml$`)
	// A file whose uses: lines run: a workflow, or a local composite action's own.
	githubUsesFile = regexp.MustCompile(`^\.github/(workflows/[^/]+|actions/[^/]+/action)\.ya?ml$`)
)

// githubPinsQuery is every workflow file, and each local composite action's
// action.yml, with its text, in one GraphQL query, which spends GraphQL's
// points rather than the installation's REST requests. A directory the
// branch does not hold answers null: nothing in it to pin.
const githubPinsQuery = `query($owner: String!, $name: String!, $workflows: String!, $actions: String!) {
  repository(owner: $owner, name: $name) {
    workflows: object(expression: $workflows) { ...files }
    actions: object(expression: $actions) { ... on Tree { entries { object { ...files } } } }
  }
}
fragment files on Tree { entries { path object { ... on Blob { text } } } }`

// githubTree is a directory as the query answers it: each entry's path, and
// its text where it is a file.
type githubTree struct {
	Entries []struct {
		Path   string `json:"path"`
		Object struct {
			Text *string `json:"text"`
		} `json:"object"`
	} `json:"entries"`
}

type GitHubPin struct {
	Path   string `json:"path"`
	Uses   string `json:"uses"`
	Action string `json:"action"`
	Ref    string `json:"ref"`
	SHA    string `json:"sha"`
}

type githubUsesFound struct {
	pins   []GitHubPin
	called []string
}

// usesFiles is every workflow file's path and text, then every local
// composite action's.
func (g *githubRepository) usesFiles(ctx context.Context, branch string) ([][2]string, error) {
	owner, name, err := githubRepositoryName(g.name)
	if err != nil {
		return nil, err
	}
	raw, err := g.r.githubCall(ctx, g.c, http.MethodPost, githubGraphQL, []string{g.name}, githubRead, map[string]any{
		"query":     githubPinsQuery,
		"variables": map[string]string{"owner": owner, "name": name, "workflows": branch + ":.github/workflows", "actions": branch + ":.github/actions"},
	})
	if err != nil {
		return nil, err
	}
	answer, err := decodeAnswer[struct {
		Data struct {
			Repository *struct {
				Workflows githubTree `json:"workflows"`
				Actions   struct {
					Entries []struct {
						Object githubTree `json:"object"`
					} `json:"entries"`
				} `json:"actions"`
			} `json:"repository"`
		} `json:"data"`
		Errors []struct {
			Message string `json:"message"`
		} `json:"errors"`
	}](raw, "GitHub workflow files")
	if err == nil && (len(answer.Errors) > 0 || answer.Data.Repository == nil) {
		err = &ProviderError{Message: fmt.Sprintf("GitHub refused the workflow files query: %+v", answer.Errors)}
	}
	if err != nil {
		return nil, err
	}
	trees := []githubTree{answer.Data.Repository.Workflows}
	for _, action := range answer.Data.Repository.Actions.Entries {
		trees = append(trees, action.Object)
	}
	files := [][2]string{}
	for _, tree := range trees {
		for _, entry := range tree.Entries {
			if entry.Object.Text != nil && githubUsesFile.MatchString(entry.Path) {
				files = append(files, [2]string{entry.Path, *entry.Object.Text})
			}
		}
	}
	return files, nil
}

// workflowUses is every uses: line not pinned to a commit, with the commit
// its ref names now, and every workflow called from another repository, pinned
// or not: what a called workflow uses lives in that repository, not this one.
func (g *githubRepository) workflowUses(ctx context.Context, branch string) (githubUsesFound, error) {
	files, err := g.usesFiles(ctx, branch)
	if err != nil {
		return githubUsesFound{}, err
	}
	found := githubUsesFound{pins: []GitHubPin{}, called: []string{}}
	resolved := map[string]string{}
	called := map[string]bool{}
	for _, file := range files {
		path := file[0]
		for _, match := range githubUses.FindAllStringSubmatch(file[1], -1) {
			uses := match[1]
			action, at, versioned := strings.CutLast(uses, "@")
			if strings.HasPrefix(uses, "./") || strings.HasPrefix(uses, "docker://") || !versioned {
				continue
			}
			if githubCalled.MatchString(action) {
				called[uses] = true
			}
			if githubCommit.MatchString(at) {
				continue
			}
			source := strings.Join(firstN(strings.Split(action, "/"), 2), "/")
			key := source + "@" + at
			sha, seen := resolved[key]
			if !seen {
				sha = g.commitOf(ctx, source, at)
				resolved[key] = sha
			}
			found.pins = append(found.pins, GitHubPin{Path: path, Uses: uses, Action: action, Ref: at, SHA: sha})
		}
	}
	found.called = sortedKeys(called)
	return found, nil
}

func firstN(parts []string, n int) []string {
	if len(parts) > n {
		return parts[:n]
	}
	return parts
}

// commitOf is the commit a tag or branch of source names now, or "" when it
// could not be read. Resolved once per action and ref in a sweep, under the
// read-only token of the repository being read; only a commit that was read
// is shared, since one repository's token failing says nothing of the next's.
func (g *githubRepository) commitOf(ctx context.Context, source, at string) string {
	raw, err := cached(ctx, g.r, "github.commit|"+source+"|"+at, func() (json.RawMessage, error) {
		commit, err := githubGet[githubapi.Commit](ctx, g.r, g.c, "/repos/"+source+"/commits/"+url.PathEscape(at), []string{g.name}, githubRead)
		if err != nil {
			return nil, err
		}
		if !githubCommit.MatchString(commit.Sha) {
			return nil, &ProviderError{Message: "the ref names no commit"}
		}
		return json.Marshal(commit.Sha)
	})
	if err != nil {
		return ""
	}
	var sha string
	_ = json.Unmarshal(raw, &sha)
	return sha
}
