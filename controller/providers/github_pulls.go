package providers

import (
	"context"
	"fmt"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What GitHub says of each open pull request beyond its REST listing: whether
// it can merge, where its review stands and every check on its head, commit
// statuses among them, asked in one GraphQL query per repository. GraphQL
// spends its own points budget, not the installation's REST requests.

// githubPullStatesRead is what the query reads; a commit status needs a
// permission of its own, so the query runs under a token of its own and is
// refused alone where the installation lacks it.
var githubPullStatesRead = githubapi.AppPermissions{
	Metadata:     githubapi.AppPermissionsMetadataRead,
	PullRequests: githubapi.AppPermissionsPullRequestsRead,
	Checks:       githubapi.AppPermissionsChecksRead,
	Statuses:     githubapi.AppPermissionsStatusesRead,
	Actions:      githubapi.AppPermissionsActionsRead,
}

// githubPullStatesQuery asks for the same page of open pull requests the REST
// listing reads, newest first. A check of a run githubAside sets apart is
// not the pull request's, so each check run names its run's event and title.
const githubPullStatesQuery = `query($owner: String!, $name: String!, $pulls: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $pulls, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number mergeStateStatus reviewDecision
        statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name status conclusion startedAt completedAt url checkSuite { workflowRun { event displayTitle } } }
          ... on StatusContext { context state createdAt targetUrl }
        } } }
      }
    }
  }
}`

type githubPullState struct {
	Number            int    `json:"number"`
	MergeStateStatus  string `json:"mergeStateStatus"`
	ReviewDecision    string `json:"reviewDecision"`
	StatusCheckRollup *struct {
		Contexts struct {
			Nodes []githubRollupContext `json:"nodes"`
		} `json:"contexts"`
	} `json:"statusCheckRollup"`
}

type githubRollupContext struct {
	Typename    string `json:"__typename"`
	Name        string `json:"name"`
	Status      string `json:"status"`
	Conclusion  string `json:"conclusion"`
	StartedAt   string `json:"startedAt"`
	CompletedAt string `json:"completedAt"`
	URL         string `json:"url"`
	CheckSuite  struct {
		WorkflowRun *struct {
			Event        string `json:"event"`
			DisplayTitle string `json:"displayTitle"`
		} `json:"workflowRun"`
	} `json:"checkSuite"`
	Context   string `json:"context"`
	State     string `json:"state"`
	CreatedAt string `json:"createdAt"`
	TargetURL string `json:"targetUrl"`
}

// pullStates fills each pull request's merge state, review decision and
// checks from one query, or refuses that part for the repository.
func (g *githubRepository) pullStates(ctx context.Context, pulls []GitHubOpenPullRequestRecord) {
	if len(pulls) == 0 {
		return
	}
	states, ok := part(ctx, g, runtime.PartPullStates, func() (map[int]githubPullState, error) { return g.readPullStates(ctx) })
	if !ok {
		return
	}
	for i := range pulls {
		state, found := states[pulls[i].Number]
		if !found {
			continue
		}
		pulls[i].MergeableState, pulls[i].ReviewState = strings.ToLower(state.MergeStateStatus), strings.ToLower(state.ReviewDecision)
		if state.StatusCheckRollup != nil {
			pulls[i].CheckRuns = rollupChecks(state.StatusCheckRollup.Contexts.Nodes, pulls[i].Head)
		}
	}
}

func (g *githubRepository) readPullStates(ctx context.Context) (map[int]githubPullState, error) {
	token, err := g.r.githubToken(ctx, g.c, []string{g.name}, githubPullStatesRead)
	if err != nil {
		return nil, err
	}
	raw, err := g.r.githubQuery(ctx, token, githubPullStatesQuery, map[string]any{"owner": g.owner, "name": g.repo, "pulls": githubPullsPage})
	if err != nil {
		return nil, err
	}
	answer, err := decodeAnswer[struct {
		Data struct {
			Repository *struct {
				PullRequests struct {
					Nodes []githubPullState `json:"nodes"`
				} `json:"pullRequests"`
			} `json:"repository"`
		} `json:"data"`
		Errors []struct {
			Message string `json:"message"`
		} `json:"errors"`
	}](raw, "GitHub pull request states")
	if err == nil && (len(answer.Errors) > 0 || answer.Data.Repository == nil) {
		err = &ProviderError{Message: fmt.Sprintf("GitHub refused the pull request states query: %+v", answer.Errors)}
	}
	if err != nil {
		return nil, err
	}
	found := map[int]githubPullState{}
	for _, node := range answer.Data.Repository.PullRequests.Nodes {
		found[node.Number] = node
	}
	return found, nil
}

// rollupChecks is each check as its last run stands, by name in the order
// each first appears: a check run in GitHub's words, lower-cased, and a
// commit status completed once it succeeded or failed, an error a failure.
func rollupChecks(contexts []githubRollupContext, head string) []GitHubCheckRun {
	latest, started, order := map[string]GitHubCheckRun{}, map[string]string{}, []string{}
	for _, context := range contexts {
		check := GitHubCheckRun{}
		switch context.Typename {
		case "CheckRun":
			if run := context.CheckSuite.WorkflowRun; run != nil && githubAside(githubapi.WorkflowRun{Event: run.Event, DisplayTitle: run.DisplayTitle, HeadSha: head}) {
				continue
			}
			check = GitHubCheckRun{
				Name: context.Name, Status: strings.ToLower(context.Status), Conclusion: strings.ToLower(context.Conclusion),
				StartedAt: context.StartedAt, CompletedAt: context.CompletedAt, URL: context.URL,
			}
		case "StatusContext":
			// A commit status has one moment, when it was set, and no end of its own.
			check = GitHubCheckRun{Name: context.Context, Status: "pending", StartedAt: context.CreatedAt, URL: context.TargetURL}
			switch context.State {
			case "SUCCESS":
				check.Status, check.Conclusion = "completed", "success"
			case "FAILURE", "ERROR":
				check.Status, check.Conclusion = "completed", "failure"
			}
		default:
			continue
		}
		if _, seen := latest[check.Name]; !seen {
			order = append(order, check.Name)
		} else if context.StartedAt < started[check.Name] {
			continue
		}
		latest[check.Name], started[check.Name] = check, context.StartedAt
	}
	found := make([]GitHubCheckRun, 0, len(order))
	for _, name := range order {
		found = append(found, latest[name])
	}
	return found
}
