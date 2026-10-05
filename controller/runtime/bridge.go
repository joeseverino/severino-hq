package runtime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"os"
	"time"
)

// SocketBridge keeps Django as the transaction and persistence owner. It
// reaches the bridge application HQ's running process serves on a private
// Unix socket, so a call costs the work it asks for: nothing is started.
//
// Requests are built by the client generated from the contract. Every
// connection is made through DialTrusted, which refuses a socket this account
// should not trust; there is no other way to reach HQ and no fallback.
type SocketBridge struct {
	client  *Client
	Timeout time.Duration
}

type BridgeError struct{ Message string }

func (e *BridgeError) Error() string { return e.Message }

// bridgeOrigin is the URL the generated client builds on. The socket decides
// where a request goes; the name only fills the Host header.
const bridgeOrigin = "http://hq-bridge"

// problemLimit bounds what is read of a refusal.
const problemLimit = 64 << 10

// NewSocketBridge is the bridge at a socket path, reached as this account.
func NewSocketBridge(path string) (*SocketBridge, error) {
	if path == "" {
		return nil, &BridgeError{"SEVERINO_BRIDGE_SOCKET must name HQ's bridge socket"}
	}
	transport := &http.Transport{
		DialContext: DialTrusted(path, os.Geteuid()),
		// One connection per call: each is checked as it is made, and none is
		// held idle for HQ to close under a request.
		DisableKeepAlives:  true,
		DisableCompression: true,
		Proxy:              nil,
	}
	client, err := NewClient(bridgeOrigin, WithHTTPClient(&http.Client{
		Transport: transport,
		// The bridge answers; it never sends the controller elsewhere.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}))
	if err != nil {
		return nil, &BridgeError{"bridge client could not be built"}
	}
	return &SocketBridge{client: client}, nil
}

// send is one generated request method, bound to its parameters.
type send func(ctx context.Context, contentType string, body io.Reader) (*http.Response, error)

// call runs one action under the bridge's deadline and decodes its answer.
func (b *SocketBridge) call(ctx context.Context, payload any, result any, request send) error {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("encode bridge payload: %w", err)
		}
		if len(encoded) > MaxBridgeOutput {
			return &BridgeError{"bridge payload is too large"}
		}
		body = bytes.NewReader(encoded)
	}
	timeout := b.Timeout
	if timeout == 0 {
		timeout = BridgeTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	response, err := request(ctx, "application/json", body)
	if err != nil {
		return bridgeFailure(ctx, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return refusal(response)
	}
	if kind, _, _ := mime.ParseMediaType(response.Header.Get("Content-Type")); kind != "application/json" {
		return &BridgeError{"bridge answer does not match the contract: not JSON"}
	}
	answer, err := io.ReadAll(io.LimitReader(response.Body, int64(MaxBridgeOutput)+1))
	if err != nil {
		return bridgeFailure(ctx, err)
	}
	if len(answer) > MaxBridgeOutput {
		return &BridgeError{"bridge returned too much data"}
	}
	if result == nil {
		result = new(any)
	}
	// Strict: a field the contract does not declare is drift, refused here
	// rather than dropped.
	decoder := json.NewDecoder(bytes.NewReader(answer))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(result); err != nil {
		return &BridgeError{"bridge answer does not match the contract: " + err.Error()}
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return &BridgeError{"bridge returned trailing data"}
	}
	return nil
}

// bridgeFailure names why a call got no answer: the deadline, a socket this
// account will not trust, or a bridge that is not there.
func bridgeFailure(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return &BridgeError{"bridge did not complete before its deadline"}
	}
	if refused, ok := errors.AsType[*BridgeError](err); ok {
		return refused
	}
	return &BridgeError{"bridge call failed before HQ answered"}
}

// refusal is HQ's problem document as the error the worker reports.
func refusal(response *http.Response) error {
	var problem Problem
	raw, _ := io.ReadAll(io.LimitReader(response.Body, problemLimit))
	if json.Unmarshal(raw, &problem) != nil || problem.Detail == "" {
		problem.Detail = http.StatusText(response.StatusCode)
	}
	return &BridgeError{"bridge failed: " + Clip(problem.Detail, VerdictLimit)}
}

func (b *SocketBridge) Registry(ctx context.Context) (ControllerRegistry, error) {
	var registry ControllerRegistry
	err := b.call(ctx, nil, &registry, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Registry(ctx)
	})
	return registry, err
}

func (b *SocketBridge) Peek(ctx context.Context, capabilities []string) (Pending, error) {
	var pending Pending
	err := b.call(ctx, nil, &pending, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Peek(ctx, &PeekParams{Capability: capabilities})
	})
	return pending, err
}

func (b *SocketBridge) Claim(ctx context.Context, controllerID string, capabilities []string) (Pending, error) {
	var pending Pending
	err := b.call(ctx, nil, &pending, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Claim(ctx, &ClaimParams{ControllerID: controllerID, LeaseSeconds: ClaimLeaseSeconds, Capability: capabilities})
	})
	return pending, err
}

func (b *SocketBridge) Material(ctx context.Context, resource string) (Material, error) {
	var material Material
	err := b.call(ctx, nil, &material, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Material(ctx, &MaterialParams{Resource: resource})
	})
	return material, err
}

func (b *SocketBridge) Report(ctx context.Context, controllerID, operation string, report ControllerReport) error {
	return b.call(ctx, report, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.ReportWithBody(ctx, &ReportParams{ControllerID: controllerID, Operation: operation}, kind, body)
	})
}

func (b *SocketBridge) Schedule(ctx context.Context, controllerID string) error {
	return b.call(ctx, nil, nil, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Schedule(ctx, &ScheduleParams{ControllerID: controllerID})
	})
}

// Job asks HQ to do one piece of scheduled work and answers how it ended.
func (b *SocketBridge) Job(ctx context.Context, name string) (JobOutcome, error) {
	var outcome JobOutcome
	err := b.call(ctx, nil, &outcome, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.Job(ctx, &JobParams{Name: name})
	})
	return outcome, err
}

func (b *SocketBridge) SweepDue(ctx context.Context, controllerID string) (SweepVerdict, error) {
	var verdict SweepVerdict
	err := b.call(ctx, nil, &verdict, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.SweepDue(ctx, &SweepDueParams{ControllerID: controllerID})
	})
	return verdict, err
}

func (b *SocketBridge) GlancePlan(ctx context.Context, controllerID string) (GlancePlan, error) {
	var plan GlancePlan
	err := b.call(ctx, nil, &plan, func(ctx context.Context, _ string, _ io.Reader) (*http.Response, error) {
		return b.client.GlancePlan(ctx, &GlancePlanParams{ControllerID: controllerID})
	})
	return plan, err
}

func (b *SocketBridge) Glance(ctx context.Context, controllerID string, observations GlanceObservations) error {
	return b.call(ctx, observations, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.GlanceWithBody(ctx, &GlanceParams{ControllerID: controllerID}, kind, body)
	})
}

func (b *SocketBridge) Connections(ctx context.Context, controllerID string, records []ConnectionRecord) error {
	return b.call(ctx, records, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.ConnectionsWithBody(ctx, &ConnectionsParams{ControllerID: controllerID}, kind, body)
	})
}

func (b *SocketBridge) Inventory(ctx context.Context, controllerID string, inventory Inventory) error {
	return b.call(ctx, inventory, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.InventoryWithBody(ctx, &InventoryParams{ControllerID: controllerID}, kind, body)
	})
}

func (b *SocketBridge) AnalyticsPlan(ctx context.Context, sites []AnalyticsSiteIdentity) (AnalyticsPlan, error) {
	var plan AnalyticsPlan
	err := b.call(ctx, sites, &plan, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.AnalyticsPlanWithBody(ctx, kind, body)
	})
	return plan, err
}

func (b *SocketBridge) Analytics(ctx context.Context, controllerID string, readings AnalyticsReadings) error {
	return b.call(ctx, readings, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.AnalyticsWithBody(ctx, &AnalyticsParams{ControllerID: controllerID}, kind, body)
	})
}

func (b *SocketBridge) Steps(ctx context.Context, controllerID string, failures []StepFailure) error {
	return b.call(ctx, failures, nil, func(ctx context.Context, kind string, body io.Reader) (*http.Response, error) {
		return b.client.StepsWithBody(ctx, &StepsParams{ControllerID: controllerID}, kind, body)
	})
}
