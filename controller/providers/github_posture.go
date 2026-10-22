package providers

import (
	"context"
	"encoding/json"
	"slices"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What a repository's security posture reads: its open alerts, the rules its
// default branch keeps, its environments and runners, and who can reach it.

// GitHubDependabotAlert is one open Dependabot alert: the package it is
// about, where the repository declares it, and the version that fixes it,
// nil where none does yet.
type GitHubDependabotAlert struct {
	Number              int     `json:"number"`
	Package             string  `json:"package"`
	Ecosystem           string  `json:"ecosystem"`
	ManifestPath        string  `json:"manifest_path"`
	Severity            string  `json:"severity"`
	FirstPatchedVersion *string `json:"first_patched_version"`
	URL                 string  `json:"url"`
}

type GitHubRules struct {
	PullRequest     bool     `json:"pull_request"`
	Reviews         int      `json:"reviews"`
	RequiredChecks  []string `json:"required_checks"`
	BlocksForcePush bool     `json:"blocks_force_push"`
	BlocksDeletion  bool     `json:"blocks_deletion"`
}

type GitHubEnvironment struct {
	Name        string   `json:"name"`
	Reviewers   []string `json:"reviewers"`
	WaitMinutes int      `json:"wait_minutes"`
	Branches    string   `json:"branches"`
}

type GitHubRunner struct {
	Name   string   `json:"name"`
	Status string   `json:"status"`
	Busy   bool     `json:"busy"`
	Labels []string `json:"labels"`
}

type GitHubAccess struct {
	Visibility           string               `json:"visibility"`
	Collaborators        []GitHubCollaborator `json:"collaborators"`
	DeployKeys           []GitHubDeployKey    `json:"deploy_keys"`
	AllowedActions       string               `json:"allowed_actions"`
	PinningRequired      bool                 `json:"pinning_required"`
	Token                string               `json:"token"`
	TokenApprovesReviews bool                 `json:"token_approves_reviews"`
	// VulnerabilityAlerts is whether GitHub raises the dependency alerts
	// Renovate opens its security fixes from.
	VulnerabilityAlerts bool `json:"vulnerability_alerts"`
	// Security is each security feature's status; nil where the plan offers
	// none of them, which is not "off".
	Security map[string]*string `json:"security"`
}

type GitHubCollaborator struct {
	Login string `json:"login"`
	Role  string `json:"role"`
}

type GitHubDeployKey struct {
	Title     string `json:"title"`
	ReadOnly  bool   `json:"read_only"`
	LastUsed  string `json:"last_used"`
	CreatedAt string `json:"created_at"`
}

// alerts is open alerts by severity, and each open Dependabot alert. A kind
// the repository cannot answer is that part refused, never zero, and its
// alerts nil.
func (g *githubRepository) alerts(ctx context.Context) (map[string]map[string]int, []GitHubDependabotAlert) {
	var dependabot []GitHubDependabotAlert
	found := map[string]map[string]int{}
	count := func(name runtime.ReadingPartName, severities []string) {
		counts := map[string]int{}
		for _, severity := range severities {
			counts[strings.ToLower(orDefault(severity, "unknown"))]++
		}
		found[string(name)] = counts
	}
	if alerts, ok := part(ctx, g, runtime.PartCodeScanning, func() ([]githubapi.CodeScanningAlertItems, error) {
		return repoGet[[]githubapi.CodeScanningAlertItems](ctx, g, "/code-scanning/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := []string{}
		for _, alert := range alerts {
			severities = append(severities, orDefault(string(alert.Rule.SecuritySeverityLevel), string(alert.Rule.Severity)))
		}
		count(runtime.PartCodeScanning, severities)
	}
	if alerts, ok := part(ctx, g, runtime.PartDependabot, func() ([]githubapi.DependabotAlert, error) {
		return repoGet[[]githubapi.DependabotAlert](ctx, g, "/dependabot/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := []string{}
		dependabot = []GitHubDependabotAlert{}
		for _, alert := range alerts {
			severities = append(severities, string(alert.SecurityAdvisory.Severity))
			dependabot = append(dependabot, dependabotAlert(alert))
		}
		count(runtime.PartDependabot, severities)
	}
	if alerts, ok := part(ctx, g, runtime.PartLeakedCredentials, func() ([]githubapi.SecretScanningAlert, error) {
		return repoGet[[]githubapi.SecretScanningAlert](ctx, g, "/secret-scanning/alerts?state=open&per_page="+strconv.Itoa(githubListPage))
	}); ok {
		severities := make([]string, len(alerts))
		for i := range alerts {
			severities[i] = "leaked"
		}
		count(runtime.PartLeakedCredentials, severities)
	}
	return found, dependabot
}

func dependabotAlert(alert githubapi.DependabotAlert) GitHubDependabotAlert {
	pkg := alert.Dependency.Package
	if pkg.Name == "" {
		pkg = alert.SecurityVulnerability.Package
	}
	found := GitHubDependabotAlert{
		Number: alert.Number, Package: pkg.Name, Ecosystem: pkg.Ecosystem, ManifestPath: alert.Dependency.ManifestPath,
		Severity: strings.ToLower(orDefault(string(alert.SecurityAdvisory.Severity), "unknown")), URL: alert.HTMLURL,
	}
	if patched := alert.SecurityVulnerability.FirstPatchedVersion.Identifier; patched != "" {
		found.FirstPatchedVersion = &patched
	}
	return found
}

// branchRules is what the default branch requires, from every ruleset that
// applies to it. Each rule is read as the variant its type names.
func branchRules(rules []githubapi.RepositoryRuleDetailed) (GitHubRules, error) {
	found := GitHubRules{RequiredChecks: []string{}}
	checks := map[string]bool{}
	for _, rule := range rules {
		raw, err := rule.MarshalJSON()
		if err != nil {
			return found, err
		}
		var kind struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &kind); err != nil {
			return found, &ProviderError{Message: "decode GitHub branch rule", Err: err}
		}
		switch kind.Type {
		case "pull_request":
			var pull githubapi.RepositoryRulePullRequest
			if err := json.Unmarshal(raw, &pull); err != nil {
				return found, &ProviderError{Message: "decode GitHub pull request rule", Err: err}
			}
			found.PullRequest = true
			found.Reviews = max(found.Reviews, pull.Parameters.RequiredApprovingReviewCount)
		case "required_status_checks":
			var required githubapi.RepositoryRuleRequiredStatusChecks
			if err := json.Unmarshal(raw, &required); err != nil {
				return found, &ProviderError{Message: "decode GitHub status check rule", Err: err}
			}
			for _, check := range required.Parameters.RequiredStatusChecks {
				checks[check.Context] = true
			}
		case "non_fast_forward":
			found.BlocksForcePush = true
		case "deletion":
			found.BlocksDeletion = true
		}
	}
	found.RequiredChecks = sortedKeys(checks)
	return found, nil
}

func githubEnvironments(environments []githubapi.Environment) ([]GitHubEnvironment, error) {
	found := []GitHubEnvironment{}
	for _, environment := range environments {
		reviewers := map[string]bool{}
		wait := 0
		for _, rule := range environment.ProtectionRules {
			typed, err := rule.AsEnvironmentProtectionRules2()
			if err != nil {
				return nil, &ProviderError{Message: "decode GitHub environment rule", Err: err}
			}
			switch typed.Type {
			case "required_reviewers":
				required, err := rule.AsEnvironmentProtectionRules1()
				if err != nil {
					return nil, &ProviderError{Message: "decode GitHub reviewer rule", Err: err}
				}
				for _, entry := range required.Reviewers {
					user, _ := entry.Reviewer.AsSimpleUser()
					team, _ := entry.Reviewer.AsTeam()
					if name := orDefault(user.Login, team.Name); name != "" {
						reviewers[name] = true
					}
				}
			case "wait_timer":
				timer, err := rule.AsEnvironmentProtectionRules0()
				if err != nil {
					return nil, &ProviderError{Message: "decode GitHub wait timer rule", Err: err}
				}
				wait = timer.WaitTimer
			}
		}
		branches := "any"
		switch policy := environment.DeploymentBranchPolicy; {
		case policy.ProtectedBranches:
			branches = "protected"
		case policy.CustomBranchPolicies:
			branches = "custom"
		}
		found = append(found, GitHubEnvironment{Name: environment.Name, Reviewers: sortedKeys(reviewers), WaitMinutes: wait, Branches: branches})
	}
	return found, nil
}

func githubRunners(runners []githubapi.Runner) []GitHubRunner {
	found := []GitHubRunner{}
	for _, runner := range runners {
		labels := []string{}
		for _, label := range runner.Labels {
			labels = append(labels, label.Name)
		}
		slices.Sort(labels)
		found = append(found, GitHubRunner{Name: runner.Name, Status: runner.Status, Busy: runner.Busy, Labels: labels})
	}
	return found
}

// access is who can reach the repository and how its Actions may run.
func (g *githubRepository) access(ctx context.Context, repo githubapi.FullRepository, raw json.RawMessage) (GitHubAccess, error) {
	actions, err := repoGet[githubapi.ActionsRepositoryPermissions](ctx, g, "/actions/permissions")
	if err != nil {
		return GitHubAccess{}, err
	}
	workflow, err := repoGet[githubapi.ActionsGetDefaultWorkflowPermissions](ctx, g, "/actions/permissions/workflow")
	if err != nil {
		return GitHubAccess{}, err
	}
	alerts, err := g.vulnerabilityAlerts(ctx)
	if err != nil {
		return GitHubAccess{}, err
	}
	collaborators, err := repoGet[[]githubapi.Collaborator](ctx, g, "/collaborators?per_page="+strconv.Itoa(githubListPage))
	if err != nil {
		return GitHubAccess{}, err
	}
	keys, err := repoGet[[]githubapi.DeployKey](ctx, g, "/keys?per_page="+strconv.Itoa(githubListPage))
	if err != nil {
		return GitHubAccess{}, err
	}
	// Which security features the plan offers is which keys are present, so
	// they are read as a map, not the struct that cannot tell absent from off.
	security, err := decodeAnswer[struct {
		SecurityAndAnalysis map[string]*struct {
			Status *string `json:"status"`
		} `json:"security_and_analysis"`
	}](raw, "GitHub repository security")
	if err != nil {
		return GitHubAccess{}, err
	}
	visibility := repo.Visibility
	if visibility == "" {
		visibility = map[bool]string{true: "private", false: "public"}[repo.Private]
	}
	access := GitHubAccess{
		Visibility: visibility, Collaborators: []GitHubCollaborator{}, DeployKeys: []GitHubDeployKey{},
		AllowedActions: string(actions.AllowedActions), PinningRequired: actions.ShaPinningRequired,
		Token: string(workflow.DefaultWorkflowPermissions), TokenApprovesReviews: workflow.CanApprovePullRequestReviews,
		VulnerabilityAlerts: alerts,
	}
	for _, collaborator := range collaborators {
		access.Collaborators = append(access.Collaborators, GitHubCollaborator{Login: collaborator.Login, Role: collaborator.RoleName})
	}
	for _, key := range keys {
		access.DeployKeys = append(access.DeployKeys, GitHubDeployKey{Title: key.Title, ReadOnly: key.ReadOnly, LastUsed: key.LastUsed, CreatedAt: key.CreatedAt})
	}
	if len(security.SecurityAndAnalysis) > 0 {
		access.Security = map[string]*string{}
		for feature, setting := range security.SecurityAndAnalysis {
			if setting != nil {
				access.Security[feature] = setting.Status
			} else {
				access.Security[feature] = nil
			}
		}
	}
	return access, nil
}

// vulnerabilityAlerts is GitHub's answer for the setting: 204 on, 404 off.
func (g *githubRepository) vulnerabilityAlerts(ctx context.Context) (bool, error) {
	_, err := g.raw(ctx, "/vulnerability-alerts")
	if httpStatus(err) == 404 {
		return false, nil
	}
	return err == nil, err
}
