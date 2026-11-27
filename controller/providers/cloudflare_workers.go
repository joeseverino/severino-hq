package providers

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Each Worker on the account as Cloudflare runs it: the deployment serving
// it, each version in it with the tag its deploy gave it, what Workers
// analytics counted of its invocations over the last day, and, where a version
// binds HQ's answers dataset, how it answered each route over the same day.

// workersWindow is how far back the invocations are counted.
const workersWindow = 24 * time.Hour

// workersInvocationsQuery groups the window's invocations by Worker, so one
// query answers for every Worker on the account. CPU time is in microseconds.
const workersInvocationsQuery = `query($account: String!, $since: Time!, $until: Time!) {
  viewer {
    accounts(filter: { accountTag: $account }) {
      workersInvocationsAdaptive(limit: 1000, filter: { datetime_geq: $since, datetime_leq: $until }) {
        sum { requests errors }
        quantiles { cpuTimeP50 cpuTimeP99 }
        dimensions { scriptName }
      }
    }
  }
}`

// workersInvocations is one Worker's row of the answer.
type workersInvocations struct {
	Sum struct {
		Requests int `json:"requests"`
		Errors   int `json:"errors"`
	} `json:"sum"`
	Quantiles struct {
		CPUTimeP50 float32 `json:"cpuTimeP50"`
		CPUTimeP99 float32 `json:"cpuTimeP99"`
	} `json:"quantiles"`
	Dimensions struct {
		ScriptName string `json:"scriptName"`
	} `json:"dimensions"`
}

// answersHour is the last part of the window a route's answers are also
// counted over, which tells a route refusing now from one that refused
// earlier in the day.
const answersHour = time.Hour

// workersAnswersQuery counts the answers dataset's data points by route and
// status, each weighed by its _sample_interval so a sampled point counts the
// requests it stands for. The window is given in Unix seconds.
const workersAnswersQuery = `SELECT blob1 AS route, double1 AS status, sum(_sample_interval) AS requests,
  sumIf(_sample_interval, timestamp > toDateTime(%[2]d)) AS last_hour
FROM %[1]s
WHERE timestamp > toDateTime(%[3]d) AND timestamp <= toDateTime(%[4]d)
GROUP BY route, status
FORMAT JSON`

// workersAnswers is one row of the answer. A double is a Float64 column.
type workersAnswers struct {
	Route    string  `json:"route"`
	Status   float64 `json:"status"`
	Requests int     `json:"requests"`
	LastHour int     `json:"last_hour"`
}

type workersInvocationsAnswer struct {
	Viewer struct {
		Accounts []struct {
			WorkersInvocationsAdaptive []workersInvocations `json:"workersInvocationsAdaptive"`
		} `json:"accounts"`
	} `json:"viewer"`
}

func (r *Registry) cloudflareWorkers(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		client := r.cloudflare(runtime.ConnectionProviderCloudflareAPI, ref, true)
		scripts, err := cloudflareOptional[[]cfapi.WorkersScriptResponse](cloudflareResult(client.WorkerScriptListWorkersWithResponse(ctx, account)))
		if err != nil {
			return nil, err
		}
		invocations, invocationsErr := r.cloudflareWorkersInvocations(ctx, account, ref)
		if invocationsErr != nil {
			refuse(ctx, runtime.PartInvocations, ref, "", invocationsErr)
		}
		for _, script := range scripts {
			if script.ID == "" {
				continue
			}
			record, counted, err := r.cloudflareWorker(ctx, client, account, script.ID)
			if err != nil {
				return nil, err
			}
			record.ConnectionRef = ref
			if counted {
				if answers, err := r.cloudflareWorkersAnswers(ctx, account, ref); err != nil {
					refuse(ctx, runtime.PartAnswers, ref, "", err)
				} else {
					record.Answers = &answers
				}
			}
			if invocationsErr == nil {
				// A Worker nothing invoked in the window has no row: none of each.
				counted := invocations[script.ID]
				record.Requests, record.Errors = &counted.Sum.Requests, &counted.Sum.Errors
				if counted.Sum.Requests > 0 {
					record.CPUTimeP50Us, record.CPUTimeP99Us = &counted.Quantiles.CPUTimeP50, &counted.Quantiles.CPUTimeP99
				}
			}
			found = append(found, record)
		}
	}
	return found, nil
}

// cloudflareWorker is one Worker and its latest deployment, the first the
// list answers, with each of its versions' number and tag, and whether a
// version it serves binds HQ's answers dataset.
func (r *Registry) cloudflareWorker(ctx context.Context, client *cfapi.ClientWithResponses, account, name string) (runtime.CloudflareWorkerRecord, bool, error) {
	record := runtime.CloudflareWorkerRecord{AccountID: account, Name: name}
	listed, err := cloudflareOptional[struct {
		Deployments []cfapi.WorkersDeployment `json:"deployments"`
	}](cloudflareResult(client.WorkerDeploymentsListDeploymentsWithResponse(ctx, account, name)))
	if err != nil || len(listed.Deployments) == 0 {
		return record, false, err
	}
	deployment := listed.Deployments[0]
	record.DeploymentID, record.DeployedAt = deployment.ID, deployment.CreatedOn
	record.Versions = []runtime.CloudflareWorkerWorkerVersionRecord{}
	counted := false
	for _, serving := range deployment.Versions {
		version, err := cloudflareOptional[cfapi.WorkersVersion](cloudflareResult(client.GetWorkerVersionWithResponse(ctx, account, name, serving.VersionID)))
		if err != nil {
			return record, false, err
		}
		record.Versions = append(record.Versions, runtime.CloudflareWorkerWorkerVersionRecord{
			ID: serving.VersionID, Number: version.Number, Percentage: serving.Percentage, Tag: strings.TrimSpace(version.Annotations.WorkersTag),
		})
		counted = counted || bindsAnswers(version.Bindings)
	}
	return record, counted, nil
}

// bindsAnswers is whether one of a version's bindings is HQ's answers dataset.
func bindsAnswers(bindings cfapi.WorkersBindings) bool {
	for _, binding := range bindings {
		if kind, _ := binding.Discriminator(); kind != string(cfapi.AnalyticsEngine) {
			continue
		}
		if engine, err := binding.AsWorkersBindingKindAnalyticsEngine(); err == nil && engine.Dataset == string(runtime.WorkerAnswersDatasetHQEvents) {
			return true
		}
	}
	return false
}

// cloudflareWorkersAnswers is the answers dataset's count of each route and
// status over the window and its last hour.
func (r *Registry) cloudflareWorkersAnswers(ctx context.Context, account, ref string) ([]runtime.CloudflareWorkerWorkerAnswersRecord, error) {
	until := r.Now().UTC().Truncate(time.Minute)
	query := fmt.Sprintf(workersAnswersQuery, runtime.WorkerAnswersDatasetHQEvents, until.Add(-answersHour).Unix(), until.Add(-workersWindow).Unix(), until.Unix())
	data, err := r.cloudflareSQL(ctx, account, query, ref)
	if err != nil {
		return nil, err
	}
	rows, err := cloudflareDecode[[]workersAnswers](data, "Analytics Engine rows")
	if err != nil {
		return nil, err
	}
	found := make([]runtime.CloudflareWorkerWorkerAnswersRecord, 0, len(rows))
	for _, row := range rows {
		found = append(found, runtime.CloudflareWorkerWorkerAnswersRecord{Route: row.Route, Status: int(row.Status), Requests: row.Requests, LastHour: row.LastHour})
	}
	return found, nil
}

// cloudflareWorkersInvocations is each Worker's invocations over the window, by name.
func (r *Registry) cloudflareWorkersInvocations(ctx context.Context, account, ref string) (map[string]workersInvocations, error) {
	until := r.Now().UTC().Truncate(time.Minute)
	data, err := r.cloudflareGraphQL(ctx, workersInvocationsQuery, map[string]string{
		"account": account, "since": until.Add(-workersWindow).Format(time.RFC3339), "until": until.Format(time.RFC3339),
	}, ref)
	if err != nil {
		return nil, err
	}
	var answer workersInvocationsAnswer
	if present(data) {
		if answer, err = cloudflareDecode[workersInvocationsAnswer](data, "Workers analytics answer"); err != nil {
			return nil, err
		}
	}
	if len(answer.Viewer.Accounts) != 1 {
		return nil, &ProviderError{Message: "cloudflare Workers analytics returned no matching account"}
	}
	found := map[string]workersInvocations{}
	for _, row := range answer.Viewer.Accounts[0].WorkersInvocationsAdaptive {
		found[row.Dimensions.ScriptName] = row
	}
	return found, nil
}
