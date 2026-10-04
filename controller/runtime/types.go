// Package runtime implements the controller's one-shot execution contract.
package runtime

import (
	"context"
	"encoding/json"
)

type Object = map[string]any

type Result struct {
	Changed    bool        `json:"changed"`
	Status     any         `json:"status"` // kind-specific; marshaled as the report's status
	Conditions []Condition `json:"conditions"`
	Message    string      `json:"message"`
}

// MarshalJSON writes no conditions as [], as the Python controller reports them.
func (r Result) MarshalJSON() ([]byte, error) {
	type plain Result
	if r.Conditions == nil {
		r.Conditions = []Condition{}
	}
	return json.Marshal(plain(r))
}

type ProviderError struct {
	Message    string
	Status     any
	Failure    FailureClass
	Refusal    Refusal
	Reason     string
	HTTPStatus int    // the provider's non-2xx answer, when it gave one
	Body       []byte // that answer's body, bounded; some providers explain a refusal there
}

func (e *ProviderError) Error() string { return e.Message }

// HTTPRefusal is a provider's non-2xx answer: 401 refuses the credential, 403 permission.
func HTTPRefusal(code int) *ProviderError {
	failure := StatusFailure(code)
	return &ProviderError{Message: "Provider request failed: HTTPError.", Failure: failure, Refusal: Refusal(failure), HTTPStatus: code}
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

// Bridge calls only the host's declared management-command actions. Payloads
// travel over stdin, never process arguments.
type Bridge interface {
	Call(context.Context, []string, any, any) error
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

func decodeObject(value any, into any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, into)
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
