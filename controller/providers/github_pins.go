package providers

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
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
)

// Where the files whose uses: lines run live: each workflow, and each local
// composite action's own action.yml.
const (
	githubWorkflowsDir = ".github/workflows"
	githubActionsDir   = ".github/actions"
)

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

// listing is a directory's entries; a 404 is the repository saying it has no
// such directory, so nothing in it to pin.
func (g *githubRepository) listing(ctx context.Context, path, branch string) (githubapi.ContentDirectory, error) {
	raw, err := g.raw(ctx, "/contents/"+path+"?ref="+url.QueryEscape(branch))
	if httpStatus(err) == 404 {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("[")) {
		return nil, nil
	}
	return decodeAnswer[githubapi.ContentDirectory](raw, "GitHub directory "+path)
}

// usesFiles is every workflow file, then every local composite action's action.yml.
func (g *githubRepository) usesFiles(ctx context.Context, branch string) ([]string, error) {
	workflows, err := g.listing(ctx, githubWorkflowsDir, branch)
	if err != nil {
		return nil, err
	}
	files := []string{}
	for _, entry := range workflows {
		if strings.HasSuffix(entry.Path, ".yml") || strings.HasSuffix(entry.Path, ".yaml") {
			files = append(files, entry.Path)
		}
	}
	actions, err := g.listing(ctx, githubActionsDir, branch)
	if err != nil {
		return nil, err
	}
	for _, action := range actions {
		if action.Type != githubapi.ContentDirectoryTypeDir {
			continue
		}
		entries, err := g.listing(ctx, action.Path, branch)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if entry.Name == "action.yml" || entry.Name == "action.yaml" {
				files = append(files, entry.Path)
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
	for _, path := range files {
		file, err := repoGet[githubapi.ContentFile](ctx, g, "/contents/"+path+"?ref="+url.QueryEscape(branch))
		if err != nil {
			return githubUsesFound{}, err
		}
		content, err := base64.StdEncoding.DecodeString(strings.NewReplacer("\n", "", "\r", "").Replace(file.Content))
		if err != nil {
			return githubUsesFound{}, &ProviderError{Message: "decode GitHub file " + path, Err: err}
		}
		for _, match := range githubUses.FindAllStringSubmatch(string(content), -1) {
			uses := match[1]
			if strings.HasPrefix(uses, "./") || strings.HasPrefix(uses, "docker://") || !strings.Contains(uses, "@") {
				continue
			}
			at := uses[strings.LastIndex(uses, "@")+1:]
			action := uses[:strings.LastIndex(uses, "@")]
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
	raw, err := g.r.cached(ctx, "github.commit|"+source+"|"+at, func() (json.RawMessage, error) {
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
