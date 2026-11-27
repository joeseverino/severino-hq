package providers

import (
	"context"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Each Worker on the account as Cloudflare runs it: the deployment serving
// it, each version in it with the tag its deploy gave it, and what Workers
// analytics counted of its invocations over the last day.

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
			record, err := r.cloudflareWorker(ctx, client, account, script.ID)
			if err != nil {
				return nil, err
			}
			record.ConnectionRef = ref
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
// list answers, with each of its versions' number and tag.
func (r *Registry) cloudflareWorker(ctx context.Context, client *cfapi.ClientWithResponses, account, name string) (runtime.CloudflareWorkerRecord, error) {
	record := runtime.CloudflareWorkerRecord{AccountID: account, Name: name}
	listed, err := cloudflareOptional[struct {
		Deployments []cfapi.WorkersDeployment `json:"deployments"`
	}](cloudflareResult(client.WorkerDeploymentsListDeploymentsWithResponse(ctx, account, name)))
	if err != nil || len(listed.Deployments) == 0 {
		return record, err
	}
	deployment := listed.Deployments[0]
	record.DeploymentID, record.DeployedAt = deployment.ID, deployment.CreatedOn
	record.Versions = []runtime.CloudflareWorkerWorkerVersionRecord{}
	for _, serving := range deployment.Versions {
		version, err := cloudflareOptional[cfapi.WorkersVersion](cloudflareResult(client.GetWorkerVersionWithResponse(ctx, account, name, serving.VersionID)))
		if err != nil {
			return record, err
		}
		record.Versions = append(record.Versions, runtime.CloudflareWorkerWorkerVersionRecord{
			ID: serving.VersionID, Number: version.Number, Percentage: serving.Percentage, Tag: strings.TrimSpace(version.Annotations.WorkersTag),
		})
	}
	return record, nil
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
