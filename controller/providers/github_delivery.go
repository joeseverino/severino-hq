package providers

import (
	"context"
	"fmt"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Continuous delivery: every extension's latest admission runs in production.
// The running image's lock says which commit of each extension production
// runs; GitHub says which commit each extension last admitted on main, and
// which composition run started after that. Each stage is worded on its own,
// so each new stage is a new reconcile and the same stage is never acted on
// twice. HQ starts nothing here: it reports each stage as a check run on the
// extension's commit, and comments once on the merged pull request once
// production runs it. The token it mints can never start a workflow.

const (
	githubCheckName = "Severino HQ · Production"
	// githubDeploy is where a composition goes after Compose publishes it:
	// approval, then the host.
	githubDeploy       = ".github/workflows/deploy.yml"
	githubDeliveryMark = "<!-- severino-hq-delivery:%s -->"
	githubPipelinePage = 20
	// githubAdmissionPage is how many successful admissions are read to find
	// the newest: GitHub promises no order for a filtered listing, so the
	// first of a page of one is whichever run the listing happened to lead with.
	githubAdmissionPage = 20
	githubWeb           = "https://github.com"
	githubComposedImage = "the composed image"
)

// The declaration a sweep reports when none was written: the contract's
// defaults, which HQ's own declaration takes from the same text.
var (
	githubCompose    = api.MustKeyword("GitHubDeliverySpec", "properties", "workflow", "default")
	githubMainBranch = api.MustKeyword("GitHubDeliverySpec", "properties", "branch", "default")
)

// A composition run that has not finished.
var githubPipelineRunning = map[string]bool{"queued": true, "in_progress": true, "requested": true, "pending": true}

// githubReports is what reporting delivery writes: check runs and one comment.
var (
	githubChecksRead  = githubapi.AppPermissions{Checks: githubapi.AppPermissionsChecksRead}
	githubChecksWrite = githubapi.AppPermissions{Checks: githubapi.AppPermissionsChecksWrite}
	githubPullsRead   = githubapi.AppPermissions{PullRequests: githubapi.AppPermissionsPullRequestsRead}
	githubPullsWrite  = githubapi.AppPermissions{PullRequests: githubapi.AppPermissionsPullRequestsWrite}
	githubActionsRead = githubapi.AppPermissions{Actions: githubapi.AppPermissionsActionsRead}
)

// githubRenovate is what a dependency update run's token holds, as its
// grant declares it.
var githubRenovate = func() githubapi.AppPermissions {
	permissions, err := grantPermissions(host.DependencyUpdates.Permissions)
	if err != nil {
		panic("host.DependencyUpdates names a permission GitHub does not")
	}
	return permissions
}()

// Composition is what the running image was composed from: the repository
// that delivers it, the image, and each admitted extension.
type Composition struct {
	Repository string
	Image      string
	Extensions []runtime.AdmittedExtension
}

func (r *Registry) composition() Composition {
	return Composition{
		Repository: r.Env.SourceRepository,
		Image:      r.Env.Image,
		Extensions: r.Extensions,
	}
}

// GitHubDeliverySpec is a github.delivery declaration.
type GitHubDeliverySpec = runtime.GitHubDeliverySpec

// GitHubDeliveryRecord is the delivery record and a reconcile's status.
type GitHubDeliveryRecord struct {
	Repository string                    `json:"repository"`
	Workflow   string                    `json:"workflow"`
	Branch     string                    `json:"branch"`
	Production string                    `json:"production"`
	Extensions []GitHubDeliveryExtension `json:"extensions"`
}

type GitHubDeliveryExtension struct {
	Plugin   string `json:"plugin"`
	Running  string `json:"running"`
	Admitted string `json:"admitted"`
	Stage    string `json:"stage"`
	// StageState is the stage as HQ reads it (delivery_progress.py), for an
	// extension behind production.
	StageState string `json:"stage_state,omitempty"`
	RunURL     string `json:"run_url"`
}

// extensionDelivery is how far one extension's latest admission is from production.
type extensionDelivery struct {
	plugin, repository, workflow, running string
	admitted, admittedAt                  string
	// run is the newest Compose or Deploy run that started after the admission.
	run *githubapi.WorkflowRun
	// unreported is HQ's check run on the running commit while it does not yet
	// say so: still open, or closed as not delivered before the new image.
	unreported *githubapi.CheckRun
}

func (e extensionDelivery) behind() bool { return e.admitted != "" && e.admitted != e.running }

func short(sha string, n int) string { return sha[:min(n, len(sha))] }

// Where a delivery behind production stands, as HQ reads it
// (delivery_progress.py).
const (
	stageStarting = "not_started"
	stageWaiting  = "waiting"
	stageRunning  = "running"
	stageFailed   = "failed"
)

// stage is where the delivery stands, and in words; the check run and the
// condition follow it. A run that ended in success while production still
// reads behind is between steps: Compose has not yet started its deploy, or
// production has not been read since it. So only a run that ended otherwise
// has failed, and a next step that never comes stalls in HQ
// (delivery_progress.STALLED_AFTER).
func (e extensionDelivery) stage() (state, words string) {
	run := e.run
	if run == nil {
		return stageStarting, "No deploy has started"
	}
	which := orDefault(run.Name, "Compose") + " run " + strconv.Itoa(run.ID)
	switch {
	case run.Status == "waiting":
		return stageWaiting, which + " is waiting for approval"
	case githubPipelineRunning[run.Status]:
		return stageRunning, which + " is running"
	}
	switch run.Conclusion {
	case "success":
		return stageRunning, which + " finished"
	case "failure":
		return stageFailed, which + " failed"
	case "":
		return stageFailed, which + " ended"
	default:
		return stageFailed, which + " ended (" + strings.ReplaceAll(run.Conclusion, "_", " ") + ")"
	}
}

// says is one plugin's distance from production, as whole sentences.
func (e extensionDelivery) says() string {
	if e.behind() {
		_, words := e.stage()
		return e.plugin + ": " + short(e.admitted, 7) + " is approved, production still runs " + short(e.running, 7) + ". " + words + "."
	}
	return e.plugin + ": " + short(e.running, 7) + " is live, not yet confirmed on GitHub."
}

func (e extensionDelivery) pending() bool { return e.behind() || e.unreported != nil }

// workflowRuns is a workflow's runs on a branch, by its file name.
func (r *Registry) workflowRuns(ctx context.Context, c githubConnection, repository, workflow string, query githubapi.ActionsListWorkflowRunsParams, scope []string) ([]githubapi.WorkflowRun, error) {
	owner, repo, err := githubRepositoryName(repository)
	if err != nil {
		return nil, err
	}
	answer, err := json200(r.githubAs(ctx, c, scope, githubActionsRead).ActionsListWorkflowRunsWithResponse(ctx, owner, repo, path.Base(workflow), &query))
	return answer.WorkflowRuns, err
}

// checkRun is HQ's latest check run on a commit, or nil.
func (r *Registry) checkRun(ctx context.Context, c githubConnection, repository, sha string) (*githubapi.CheckRun, error) {
	owner, repo, err := githubRepositoryName(repository)
	if err != nil {
		return nil, err
	}
	app, _ := strconv.Atoi(c.appID)
	query := githubapi.ChecksListForRefParams{CheckName: new(githubCheckName), AppID: new(app), Filter: new(githubapi.ChecksListForRefParamsFilterLatest)}
	answer, err := json200(r.githubAs(ctx, c, []string{repository}, githubChecksRead).ChecksListForRefWithResponse(ctx, owner, repo, sha, &query))
	if err != nil || len(answer.CheckRuns) == 0 {
		return nil, err
	}
	return &answer.CheckRuns[0], nil
}

// createdLater is whether a was created after b: by the moment each was
// created, and by run number where the moments are equal or do not read.
func createdLater(a, b *githubapi.WorkflowRun) bool {
	at, errA := time.Parse(time.RFC3339, a.CreatedAt)
	bt, errB := time.Parse(time.RFC3339, b.CreatedAt)
	if errA == nil && errB == nil && !at.Equal(bt) {
		return at.After(bt)
	}
	return a.ID > b.ID
}

// newestRun is the run created last among those keep admits, wherever the
// listing put it: the order of a listing is never read.
func newestRun(runs []githubapi.WorkflowRun, keep func(*githubapi.WorkflowRun) bool) *githubapi.WorkflowRun {
	var newest *githubapi.WorkflowRun
	for i := range runs {
		run := &runs[i]
		if keep != nil && !keep(run) {
			continue
		}
		if newest == nil || createdLater(run, newest) {
			newest = run
		}
	}
	return newest
}

// runAfter is the newest run created at or after since. A run whose time does
// not read is kept; a since that does not read matches none.
func runAfter(runs []githubapi.WorkflowRun, since string) *githubapi.WorkflowRun {
	start, err := time.Parse(time.RFC3339, since)
	if err != nil {
		return nil
	}
	return newestRun(runs, func(run *githubapi.WorkflowRun) bool {
		created, err := time.Parse(time.RFC3339, run.CreatedAt)
		return err != nil || !created.Before(start)
	})
}

// delivery is every extension, with how far its latest admission is from production.
func (r *Registry) delivery(ctx context.Context, c githubConnection, spec GitHubDeliverySpec) ([]extensionDelivery, error) {
	composed := r.composition()
	if len(composed.Extensions) == 0 {
		return []extensionDelivery{}, nil
	}
	every := []string{}
	for _, extension := range composed.Extensions {
		every = append(every, extension.SourceRepository)
	}
	var pipeline []githubapi.WorkflowRun
	pipelineRead := false
	settled := []extensionDelivery{}
	for _, extension := range composed.Extensions {
		found := extensionDelivery{plugin: extension.Plugin, repository: extension.SourceRepository, workflow: extension.SourceWorkflow, running: extension.SourceCommit}
		admissions, err := r.workflowRuns(ctx, c, found.repository, found.workflow,
			githubapi.ActionsListWorkflowRunsParams{Branch: new(githubMainBranch), Status: new(githubapi.ActionsListWorkflowRunsParamsStatusSuccess), PerPage: new(githubAdmissionPage)}, every)
		if err != nil {
			return nil, err
		}
		if latest := newestRun(admissions, nil); latest != nil {
			found.admitted, found.admittedAt = latest.HeadSha, latest.UpdatedAt
		}
		if found.behind() {
			if !pipelineRead {
				if pipeline, err = r.pipelineRuns(ctx, c, spec); err != nil {
					return nil, err
				}
				pipelineRead = true
			}
			found.run = runAfter(pipeline, found.admittedAt)
			settled = append(settled, found)
			continue
		}
		check, err := r.checkRun(ctx, c, found.repository, found.running)
		if err != nil {
			return nil, err
		}
		if check != nil && check.Conclusion != "success" {
			found.unreported = check
		}
		settled = append(settled, found)
	}
	return settled, nil
}

// pipelineRuns is Compose's and Deploy's recent runs on the branch: a
// composition is one until it publishes, then the other until it is live.
func (r *Registry) pipelineRuns(ctx context.Context, c githubConnection, spec GitHubDeliverySpec) ([]githubapi.WorkflowRun, error) {
	found := []githubapi.WorkflowRun{}
	for _, workflow := range []string{spec.Workflow, githubDeploy} {
		runs, err := r.workflowRuns(ctx, c, spec.Repository, workflow,
			githubapi.ActionsListWorkflowRunsParams{Branch: new(spec.Branch), PerPage: new(githubPipelinePage)}, []string{spec.Repository})
		if err != nil {
			return nil, err
		}
		found = append(found, runs...)
	}
	return found, nil
}

// production is the contract's word for current, or what stands between
// production and it.
func production(extensions []extensionDelivery) string {
	pending := []string{}
	for _, extension := range extensions {
		if extension.pending() {
			pending = append(pending, extension.says())
		}
	}
	if len(pending) == 0 {
		return string(runtime.GitHubDeliveryProductionCurrent)
	}
	return strings.Join(pending, " ")
}

func deliveryRecord(spec GitHubDeliverySpec, extensions []extensionDelivery) GitHubDeliveryRecord {
	record := GitHubDeliveryRecord{Repository: spec.Repository, Workflow: spec.Workflow, Branch: spec.Branch, Production: production(extensions), Extensions: []GitHubDeliveryExtension{}}
	for _, extension := range extensions {
		stage, state := "live", ""
		if extension.behind() {
			state, stage = extension.stage()
		}
		runURL := ""
		if extension.run != nil {
			runURL = extension.run.HTMLURL
		}
		record.Extensions = append(record.Extensions, GitHubDeliveryExtension{
			Plugin: extension.plugin, Running: extension.running, Admitted: extension.admitted, Stage: stage, StageState: state, RunURL: runURL,
		})
	}
	return record
}

// githubDeliveryInventory is the host's delivery record, when the image says
// which repository delivers it.
func (r *Registry) githubDeliveryInventory(ctx context.Context) ([]any, error) {
	repository := r.composition().Repository
	if repository == "" {
		return []any{}, nil
	}
	c, err := r.githubConnection()
	if err != nil {
		return nil, err
	}
	spec := GitHubDeliverySpec{Repository: repository, Workflow: githubCompose, Branch: githubMainBranch, Production: runtime.GitHubDeliveryProductionCurrent}
	extensions, err := r.delivery(ctx, c, spec)
	if err != nil {
		return nil, err
	}
	return []any{deliveryRecord(spec, extensions)}, nil
}

// githubCheckReport is the check run for one extension's admitted or live commit.
type githubCheckReport struct {
	Status, Conclusion, Title, Summary, DetailsURL, ExternalID string
}

func checkReport(extension extensionDelivery, spec GitHubDeliverySpec, image string) githubCheckReport {
	if !extension.behind() {
		return githubCheckReport{Status: "completed", Conclusion: "success", Title: "Live in production",
			Summary: "Production runs `" + short(extension.running, 12) + "` in `" + orDefault(image, githubComposedImage) + "`."}
	}
	run := extension.run
	if run == nil {
		return githubCheckReport{Status: "queued", DetailsURL: githubWeb + "/" + spec.Repository + "/actions/workflows/" + path.Base(spec.Workflow),
			Title: "Waiting for its deploy", Summary: "Its approval starts the deploy. If that approval failed, re-run it."}
	}
	state, words := extension.stage()
	report := githubCheckReport{DetailsURL: run.HTMLURL, ExternalID: strconv.Itoa(run.ID), Summary: words}
	if report.DetailsURL == "" {
		report.DetailsURL = githubWeb + "/" + spec.Repository + "/actions/workflows/" + path.Base(spec.Workflow)
	}
	switch state {
	case stageWaiting:
		report.Status, report.Title = "in_progress", "Waiting for deploy approval"
	case stageRunning:
		report.Status, report.Title = "in_progress", "Deploying"
		if run.Status == "queued" {
			report.Status, report.Title = "queued", "Deploy queued"
		}
	default:
		report.Status, report.Conclusion, report.Title = "completed", "failure", "Not deployed"
		report.Summary = words + ". Re-run it from the run page."
	}
	return report
}

func (e extensionDelivery) sha() string {
	if e.behind() {
		return e.admitted
	}
	return e.running
}

// upsertCheck updates HQ's check run on the commit, or creates it.
func (r *Registry) upsertCheck(ctx context.Context, c githubConnection, extension extensionDelivery, report githubCheckReport) error {
	owner, repo, err := githubRepositoryName(extension.repository)
	if err != nil {
		return err
	}
	existing, err := r.checkRun(ctx, c, extension.repository, extension.sha())
	if err != nil {
		return err
	}
	client := r.githubAs(ctx, c, []string{extension.repository}, githubChecksWrite)
	if existing != nil && existing.ID != 0 {
		body := githubapi.ChecksUpdateJSONRequestBody{Status: githubapi.ChecksUpdateJSONBodyStatus(report.Status), Conclusion: githubapi.ChecksUpdateJSONBodyConclusion(report.Conclusion),
			DetailsURL: report.DetailsURL, ExternalID: report.ExternalID}
		body.Output.Title, body.Output.Summary = report.Title, report.Summary
		_, err = client.ChecksUpdateWithResponse(ctx, owner, repo, strconv.Itoa(existing.ID), body)
		return err
	}
	body := githubapi.ChecksCreateJSONRequestBody{Name: githubCheckName, HeadSha: extension.sha(), Status: githubapi.ChecksCreateJSONBodyStatus(report.Status),
		Conclusion: githubapi.ChecksCreateJSONBodyConclusion(report.Conclusion), DetailsURL: report.DetailsURL, ExternalID: report.ExternalID}
	body.Output.Title, body.Output.Summary = report.Title, report.Summary
	_, err = client.ChecksCreateWithResponse(ctx, owner, repo, body)
	return err
}

// announce comments once on the merged pull request the first time its
// commit is live; the marker says a comment was already made.
func (r *Registry) announce(ctx context.Context, c githubConnection, extension extensionDelivery, image string) error {
	owner, repo, err := githubRepositoryName(extension.repository)
	if err != nil {
		return err
	}
	scope := []string{extension.repository}
	read := r.githubAs(ctx, c, scope, githubPullsRead)
	pulls, err := json200(read.ReposListPullRequestsAssociatedWithCommitWithResponse(ctx, owner, repo, extension.running))
	if err != nil {
		return err
	}
	var merged *githubapi.PullRequestSimple
	for i := range pulls {
		if pulls[i].MergedAt != "" {
			merged = &pulls[i]
			break
		}
	}
	if merged == nil {
		return nil
	}
	number := strconv.Itoa(merged.Number)
	marker := fmt.Sprintf(githubDeliveryMark, extension.running)
	existing, err := json200(read.IssuesListCommentsWithResponse(ctx, owner, repo, number, &githubapi.IssuesListCommentsParams{PerPage: new(githubListPage)}))
	if err != nil {
		return err
	}
	for _, comment := range existing {
		if strings.Contains(comment.Body, marker) {
			return nil
		}
	}
	body := githubapi.IssuesCreateCommentJSONRequestBody{Body: marker + "\nLive in production: `" + short(extension.running, 12) + "` in `" + orDefault(image, githubComposedImage) + "`."}
	_, err = r.githubAs(ctx, c, scope, githubPullsWrite).IssuesCreateCommentWithResponse(ctx, owner, repo, number, body)
	return err
}

// githubDeliveryReconcile reports each pending extension's stage on its
// commit, and announces a newly live one. A composition that ended without
// deploying is degraded and never retried here.
func (r *Registry) githubDeliveryReconcile(ctx context.Context, spec GitHubDeliverySpec, _ Object, apply bool) (Result, error) {
	c, err := r.githubConnection()
	if err != nil {
		return Result{}, err
	}
	extensions, err := r.delivery(ctx, c, spec)
	if err != nil {
		return Result{}, err
	}
	image := r.composition().Image
	reporting := []extensionDelivery{}
	for _, extension := range extensions {
		if extension.pending() {
			reporting = append(reporting, extension)
		}
	}
	if apply {
		for _, extension := range reporting {
			if err := r.upsertCheck(ctx, c, extension, checkReport(extension, spec, image)); err != nil {
				return Result{}, err
			}
			if !extension.behind() {
				if err := r.announce(ctx, c, extension, image); err != nil {
					return Result{}, err
				}
			}
		}
	}
	status := deliveryRecord(spec, extensions)
	failed := []string{}
	for _, extension := range extensions {
		if state, _ := extension.stage(); extension.behind() && state == stageFailed {
			failed = append(failed, extension.says())
		}
	}
	conditions := []Condition{}
	switch {
	case len(failed) > 0:
		conditions = append(conditions, condition(runtime.ConditionDegraded, "NotDelivered", strings.Join(failed, " ")+" Re-run it on GitHub."))
	case len(reporting) > 0:
		conditions = append(conditions, condition(runtime.ConditionReady, "Delivering", status.Production))
	default:
		conditions = append(conditions, condition(runtime.ConditionReady, "Current", status.Production+"."))
	}
	return Result{Changed: len(reporting) > 0, Status: status, Conditions: conditions, Message: deliveryMessage(len(failed), len(reporting))}, nil
}

// deliveryMessage is one reconcile's result in a line.
func deliveryMessage(failed, reporting int) string {
	switch {
	case failed > 0:
		return "Not deployed."
	case reporting > 0:
		return "Deploying."
	default:
		return "Up to date."
	}
}
