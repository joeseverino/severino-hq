// Package runtime implements the controller's one-shot execution contract.
package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

type Object = map[string]any

type Result struct {
	Changed    bool        `json:"changed"`
	Status     any         `json:"status"` // kind-specific; marshaled as the report's status
	Conditions []Condition `json:"conditions"`
	Message    string      `json:"message"`
}

// MarshalJSON writes no conditions as [], never null.
func (r Result) MarshalJSON() ([]byte, error) {
	type plain Result
	if r.Conditions == nil {
		r.Conditions = []Condition{}
	}
	return json.Marshal(plain(r))
}

// ProviderError is a failure a provider answered with, or one met reaching it.
// Failure is the contract's class; a credential or permission failure is also
// a refusal. Wrap it with context (fmt.Errorf("...: %w", err)); Classify reads
// it back at the report boundary.
type ProviderError struct {
	Message    string
	Failure    FailureClass
	Reason     string // the provider's own words for a refused credential
	Status     any    // a partial status an action reports with its failure
	HTTPStatus int    // the provider's non-2xx answer, when it gave one
	Body       []byte // that answer's body, bounded; some providers explain a refusal there
	Err        error  // the cause, when there is one
}

func (e *ProviderError) Error() string {
	switch {
	case e.Err == nil:
		return e.Message
	case e.Message == "":
		return e.Err.Error()
	}
	return e.Message + ": " + e.Err.Error()
}

func (e *ProviderError) Unwrap() error { return e.Err }

// Refusal is the failure as a refusal, or unclassified when it is not one.
func (e *ProviderError) Refusal() Refusal {
	switch e.Failure {
	case FailureClassCredential:
		return RefusalCredential
	case FailureClassPermission:
		return RefusalPermission
	}
	return RefusalUnclassified
}

// Classify is the one place an error becomes the contract's failure fields:
// its class, its refusal, and the provider's reason for a refused credential.
// An error with no ProviderError in its chain is unclassified.
func Classify(err error) (FailureClass, Refusal, string) {
	var provider *ProviderError
	if !errors.As(err, &provider) {
		return FailureClassUnclassified, RefusalUnclassified, ""
	}
	return provider.Failure, provider.Refusal(), provider.Reason
}

// HTTPRefusal is a provider's non-2xx answer: 401 refuses the credential, 403 permission.
func HTTPRefusal(code int) *ProviderError {
	return &ProviderError{Message: fmt.Sprintf("provider answered %d", code), Failure: StatusFailure(code), HTTPStatus: code}
}

// StatusFailure is what a 401 or 403 from a provider says went wrong: 401
// refuses the credential, 403 the permission. Other statuses say nothing.
func StatusFailure(code int) FailureClass {
	switch code {
	case 401:
		return FailureClassCredential
	case 403:
		return FailureClassPermission
	}
	return FailureClassUnclassified
}

// Bridge is HQ as the worker sees it: one method per action of the contract
// the worker calls, and nothing else.
type Bridge interface {
	Peek(ctx context.Context, capabilities []string) (Pending, error)
	Claim(ctx context.Context, controllerID string, capabilities []string) (Pending, error)
	Material(ctx context.Context, resource string) (Material, error)
	Report(ctx context.Context, controllerID, operation string, report ControllerReport) error
	Schedule(ctx context.Context, controllerID string) error
	SweepDue(ctx context.Context, controllerID string) (SweepVerdict, error)
	GlancePlan(ctx context.Context, controllerID string) (GlancePlan, error)
	Glance(ctx context.Context, controllerID string, observations GlanceObservations) error
	Connections(ctx context.Context, controllerID string, records []ConnectionRecord) error
	Inventory(ctx context.Context, controllerID string, inventory Inventory) error
	AnalyticsPlan(ctx context.Context, sites []AnalyticsSiteIdentity) (AnalyticsPlan, error)
	Analytics(ctx context.Context, controllerID string, readings AnalyticsReadings) error
	Steps(ctx context.Context, controllerID string, failures []StepFailure) error
}

// Providers owns external I/O. The worker owns order, claims and reports.
// Implementations must not write during Execute when apply is false.
type Providers interface {
	Capabilities() []Capability
	NeedsMaterial(ResourceKind) bool
	Execute(context.Context, Resource, string, bool) (Result, error)
	Connections(context.Context, []string) ([]ConnectionRecord, error)
	Inventory(context.Context, []ResourceKind) (Inventory, error)
	AnalyticsSites(context.Context) ([]AnalyticsSiteIdentity, error)
	Analytics(context.Context, []AnalyticsSiteIdentity, []AnalyticsWindow) (AnalyticsReadings, error)
	Glance(context.Context, GlancePlan) (GlanceObservations, error)
	StepFailures() []StepFailure
	BeginSnapshot() func()
}

func isNetworkFailure(c ConnectionRecord) bool { return !c.OK && c.Failure == FailureClassNetwork }

func planHealth(connections []ConnectionRecord) (bool, []string) {
	tried, network := 0, 0
	warnings := []string{}
	for _, connection := range connections {
		if !connection.Probed {
			continue
		}
		tried++
		if connection.OK {
			continue
		}
		if !isNetworkFailure(connection) {
			return false, []string{}
		}
		network++
		warnings = append(warnings, connection.ConnectionRef+": "+connection.Detail)
	}
	if network > 0 && network == tried {
		return false, []string{}
	}
	return true, warnings
}
