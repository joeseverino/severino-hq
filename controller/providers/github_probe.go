package providers

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
)

// Whether a repository changed since HQ last read it, asked of every covered
// repository at once in one GraphQL query per account. GraphQL spends a points
// budget of its own, not the installation's REST requests, and the query costs
// a point or two, so a sweep where nothing changed spends next to nothing of
// what the full read would. A repository whose answer hashes to the
// fingerprint HQ holds, and whose full read is not due on HQ's clock, is
// carried: HQ keeps its record. What the query sees is what moves on the
// sweep's cadence: the default branch's head and its check suites, every
// push, each open pull request and its head's check suites, the newest
// deployments' latest statuses and the latest release. Everything else
// (alerts, variables, runners, environments, branch rules, access, artifacts
// and what an unpinned action's tag names now) waits for the full read HQ's
// clock makes due.

// githubGraphQL is GitHub's GraphQL endpoint under the REST API's host.
const githubGraphQL = "/graphql"

// githubProbeRead is what the change query reads: the repository, its commits
// and their check suites, its pull requests and its deployments.
var githubProbeRead = githubapi.AppPermissions{
	Metadata:     githubapi.AppPermissionsMetadataRead,
	Contents:     githubapi.AppPermissionsContentsRead,
	Checks:       githubapi.AppPermissionsChecksRead,
	PullRequests: githubapi.AppPermissionsPullRequestsRead,
	Deployments:  githubapi.AppPermissionsDeploymentsRead,
}

// githubProbeFields is what one repository's answer holds: its pages are as
// long as the full read's, so a change the full read would show changes it.
var githubProbeFields = `fragment probe on Repository {
  pushedAt
  updatedAt
  isArchived
  defaultBranchRef { name target { oid ... on Commit { checkSuites(first: 50) { nodes { status conclusion updatedAt } } } } }
  pullRequests(states: OPEN, first: ` + strconv.Itoa(githubPullsPage) + `, orderBy: {field: CREATED_AT, direction: DESC}) {
    totalCount
    nodes { number updatedAt headRefOid commits(last: 1) { nodes { commit { checkSuites(first: 50) { nodes { status conclusion updatedAt } } } } } }
  }
  releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) { nodes { tagName publishedAt } }
  deployments(first: ` + strconv.Itoa(githubDeploymentsPage) + `, orderBy: {field: CREATED_AT, direction: DESC}) { nodes { databaseId environment latestStatus { state createdAt } } }
}`

// githubProbeQuery is one query asking after every named repository of one
// account, each under an alias of its own; names travel as variables.
func githubProbeQuery(owner string, repos []string) (string, map[string]any) {
	declared := []string{"$owner: String!"}
	asked := []string{}
	variables := map[string]any{"owner": owner}
	for i, repo := range repos {
		name := "n" + strconv.Itoa(i)
		declared = append(declared, "$"+name+": String!")
		asked = append(asked, fmt.Sprintf("r%d: repository(owner: $owner, name: $%s) { ...probe }", i, name))
		variables[name] = repo
	}
	return "query(" + strings.Join(declared, ", ") + ") {\n" + strings.Join(asked, "\n") + "\n}\n" + githubProbeFields, variables
}

// githubFingerprints is each repository's fingerprint, by owner/name. A
// repository the query could not answer is absent, so it is read in full; a
// query that failed whole is the error, and every repository is absent.
func (r *Registry) githubFingerprints(ctx context.Context, c githubConnection, names []string) (map[string]string, error) {
	byOwner := map[string][]string{}
	for _, name := range names {
		owner, repo, err := githubRepositoryName(name)
		if err != nil {
			continue
		}
		byOwner[owner] = append(byOwner[owner], repo)
	}
	found := map[string]string{}
	for _, owner := range sortedKeys(byOwner) {
		repos := byOwner[owner]
		scope := make([]string, len(repos))
		for i, repo := range repos {
			scope[i] = owner + "/" + repo
		}
		token, err := r.githubToken(ctx, c, scope, githubProbeRead)
		if err != nil {
			return found, err
		}
		// What the installation has left is free to ask, and the fuse holds
		// every read after it to that.
		if err := r.githubAllowance(ctx, token); err != nil {
			return found, err
		}
		query, variables := githubProbeQuery(owner, repos)
		raw, err := r.githubExchange(ctx, http.MethodPost, githubGraphQL, githubHeaders(token), struct {
			Query     string         `json:"query"`
			Variables map[string]any `json:"variables"`
		}{query, variables}, token)
		if err != nil {
			return found, err
		}
		answer, err := decodeAnswer[struct {
			Data   map[string]json.RawMessage `json:"data"`
			Errors []struct {
				Message string `json:"message"`
				Path    []any  `json:"path"`
			} `json:"errors"`
		}](raw, "GitHub change query")
		if err != nil {
			return found, err
		}
		unanswered := map[string]bool{}
		for _, failed := range answer.Errors {
			if len(failed.Path) == 0 {
				return found, &ProviderError{Message: "GitHub refused the change query: " + failed.Message}
			}
			alias, _ := failed.Path[0].(string)
			unanswered[alias] = true
		}
		for i, repo := range repos {
			alias := "r" + strconv.Itoa(i)
			node := answer.Data[alias]
			if unanswered[alias] || len(node) == 0 || string(node) == "null" {
				continue
			}
			var compact bytes.Buffer
			if json.Compact(&compact, node) != nil {
				continue
			}
			digest := sha256.Sum256(compact.Bytes())
			found[owner+"/"+repo] = hex.EncodeToString(digest[:16])
		}
	}
	return found, nil
}
