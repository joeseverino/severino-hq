package providers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// TestParityChild is invoked only by the differential harness. All I/O uses
// synthetic responses; this test executable cannot contact a real provider.
func TestParityChild(t *testing.T) {
	if os.Getenv("HQ_CONTROLLER_PARITY_CHILD") != "1" {
		return
	}
	var input struct {
		Provider      string                     `json:"provider"`
		Surface       string                     `json:"surface"`
		Spec          Object                     `json:"spec"`
		Observed      Object                     `json:"observed"`
		Apply         bool                       `json:"apply"`
		Routes        map[string]json.RawMessage `json:"routes"`
		Failures      map[string]string          `json:"failures"`
		Statuses      map[string]int             `json:"statuses"`
		WriteStatuses map[string]int             `json:"write_statuses"`
		Answers       map[string]json.RawMessage `json:"answers"`
		Headers       map[string]map[string]any  `json:"headers"`
		TailnetStatus string                     `json:"tailnet_status"`
		TailnetLock   string                     `json:"tailnet_lock"`
		Portainer     bool                       `json:"portainer"`
		ControllerID  *string                    `json:"controller_id"`
		Run           string                     `json:"run"`
		Resolves      string                     `json:"resolves"`
		ErrorBodies   map[string]json.RawMessage `json:"error_bodies"`
		Network       []string                   `json:"network"`
		Windows       []runtime.AnalyticsWindow  `json:"windows"`
	}
	raw, readErr := io.ReadAll(os.Stdin)
	if readErr != nil {
		os.Exit(2)
	}
	if localParity(raw) {
		os.Exit(0)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&input); err != nil {
		fmt.Fprintln(os.Stderr, "invalid fixture")
		os.Exit(2)
	}
	if input.Provider == "tls" {
		runTLSParity(raw)
	}
	// The tailnet readings are configuration, read through the registry's Env.
	fileEnv := map[string]string{}
	for name, content := range map[string]string{"SEVERINO_TAILNET_STATUS": input.TailnetStatus, "SEVERINO_TAILNET_LOCK": input.TailnetLock} {
		if content == "" {
			continue
		}
		if file, err := os.CreateTemp("", "parity-tailnet-*.json"); err == nil {
			file.WriteString(content)
			file.Close()
			defer os.Remove(file.Name())
			fileEnv[name] = file.Name()
		}
	}
	portainerEnv := runtime.Environment{
		"PORTAINER_CONNECTION_REF": "portainer-example",
		"PORTAINER_URL":            "https://example.invalid",
		"PORTAINER_API_TOKEN":      "synthetic",
		"HQ_CONTROLLER_ID":         "hq-node",
	}
	if input.ControllerID != nil {
		portainerEnv["HQ_CONTROLLER_ID"] = *input.ControllerID
	}
	if input.Run != "" {
		portainerEnv["HQ_CONTROLLER_RUN"] = input.Run
	}
	var r *Registry
	if input.Provider == "portainer" {
		r = New(portainerEnv, &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
	} else if input.Provider == "npm" {
		r = New(runtime.Environment{
			"NPM_CONNECTION_REF": "example",
			"NPM_URL":            "https://example.invalid",
			"NPM_USERNAME":       "user",
			"NPM_PASSWORD":       "synthetic",
		}, &fakeHTTP{routes: map[string]any{"/api/tokens": Object{"token": "synthetic"}}, fail: map[string]error{}})
	} else if input.Provider == "tailscale" {
		env := runtime.Environment{
			"TAILSCALE_CONNECTION_REF": "example",
			"TAILSCALE_CLIENT_ID":      "synthetic",
			"TAILSCALE_CLIENT_SECRET":  "synthetic",
		}
		if input.Portainer {
			for key, value := range portainerEnv {
				env[key] = value
			}
		}
		r = New(env, &fakeHTTP{
			routes: map[string]any{
				"/oauth/token": Object{"access_token": "synthetic"},
			},
			fail: map[string]error{},
		})
	} else if input.Provider == "cloudflare" {
		r = New(runtime.Environment{
			"CLOUDFLARE_DNS_CONNECTION_REF": "example",
			"CLOUDFLARE_DNS_URL":            "https://example.invalid",
			"CLOUDFLARE_DNS_API_TOKEN":      "synthetic",
			"CLOUDFLARE_API_CONNECTION_REF": "account",
			"CLOUDFLARE_API_URL":            "https://example.invalid",
			"CLOUDFLARE_API_API_TOKEN":      "synthetic",
		}, &fakeHTTP{routes: map[string]any{}, fail: map[string]error{}})
	} else {
		r = adguardFixture()
	}
	for name, value := range fileEnv {
		r.Env[name] = value
	}
	r.Resolve = func(string) (string, error) {
		if input.Resolves != "" {
			return input.Resolves, nil
		}
		return "", errors.New("does not resolve")
	}
	cleanup := r.BeginSnapshot()
	defer cleanup()
	h := r.HTTP.(*fakeHTTP)
	for path, val := range input.Routes {
		h.routes[path] = val
	}
	h.answers, h.headers, h.writeFail = map[string]any{}, input.Headers, map[string]error{}
	for path, val := range input.Answers {
		h.answers[path] = val
	}
	refusal := func(path string, code int) error {
		err := runtime.HTTPRefusal(code)
		if body, ok := input.ErrorBodies[path]; ok {
			err.Body = body
		} else if input.Provider == "cloudflare" {
			err.Body = []byte("{}")
		}
		return err
	}
	for path, code := range input.Statuses {
		h.fail[path] = refusal(path, code)
	}
	for path, code := range input.WriteStatuses {
		h.writeFail[path] = refusal(path, code)
	}
	for _, path := range input.Network {
		h.fail[path] = &ProviderError{Message: "Provider request failed.", Failure: "network"}
	}
	for path, message := range input.Failures {
		if input.Provider == "npm" || input.Provider == "tailscale" {
			h.fail[path] = &ProviderError{Message: message, Failure: "permission", Refusal: "permission"}
		} else {
			h.fail[path] = &ProviderError{Message: message}
		}
	}
	r.Now = func() time.Time { return time.Date(2026, 1, 2, 12, 0, 0, 0, time.UTC) }
	ledger := &refusals{}
	ctx := context.WithValue(context.Background(), refusalKey{}, ledger)
	var value any
	var err error
	if input.Provider == "portainer" {
		readings := map[string]string{
			"inventory": "portainer.container", "environments": "portainer.environment", "networks": "portainer.network",
			"volumes": "portainer.volume", "images": "portainer.image", "runtime": "portainer.runtime", "stacks": "portainer.compose_project",
		}
		actions := map[string]actionKey{
			"reconcile": {portainerStackKind, "reconcile"}, "delete": {portainerStackKind, "delete"},
			"restart": {portainerContainerKind, "restart"}, "start": {portainerContainerKind, "start"}, "stop": {portainerContainerKind, "stop"},
		}
		if key, ok := actions[input.Surface]; ok {
			value, err = r.actions[key](ctx, input.Spec, input.Observed, input.Apply)
		} else if kind, ok := readings[input.Surface]; ok {
			value, err = r.readers[kind](ctx)
		} else if input.Surface == "probe" {
			value, err = r.probes["portainer"](ctx, "portainer-example")
		} else {
			fmt.Fprintln(os.Stderr, "unknown fixture surface")
			os.Exit(2)
		}
	} else if input.Provider == "npm" {
		switch input.Surface {
		case "reconcile":
			value, err = r.npmReconcile(ctx, input.Spec, input.Observed, input.Apply)
		case "delete":
			value, err = r.npmDelete(ctx, input.Spec, input.Observed, input.Apply)
		case "inventory":
			value, err = r.npmInventory(ctx)
		case "certificates":
			value, err = r.npmCertificates(ctx, "example")
		case "redirects":
			value, err = r.npmRedirects(ctx, "example")
		case "dead_hosts":
			value, err = r.npmDeadHosts(ctx, "example")
		case "streams":
			value, err = r.npmStreams(ctx, "example")
		case "access_lists":
			value, err = r.npmAccessLists(ctx, "example")
		case "probe":
			probeFn := r.probes["npm"]
			value, err = probeFn(ctx, "example")
		default:
			fmt.Fprintln(os.Stderr, "unknown fixture surface")
			os.Exit(2)
		}
	} else if input.Provider == "tailscale" {
		switch input.Surface {
		case "reconcile_device":
			value, err = r.tailscaleDeviceReconcile(ctx, input.Spec, input.Observed, input.Apply)
		case "approve_routes":
			value, err = r.tailscaleApproveRoutes(ctx, input.Spec, input.Observed, input.Apply)
		case "reconcile_policy":
			value, err = r.tailnetPolicyReconcile(ctx, input.Spec, input.Observed, input.Apply)
		case "device_inventory":
			value, err = r.tailscaleDeviceInventory(ctx)
		case "policy_inventory":
			value, err = r.tailnetPolicyInventory(ctx)
		case "dns":
			value, err = r.tailscaleDNS(ctx)
		case "settings":
			value, err = r.tailscaleSettings(ctx)
		case "users":
			value, err = r.tailscaleUsers(ctx)
		case "probe":
			probeFn := r.probes["tailscale"]
			value, err = probeFn(ctx, "example")
		default:
			fmt.Fprintln(os.Stderr, "unknown fixture surface")
			os.Exit(2)
		}
	} else if input.Provider == "cloudflare" {
		value, err = cloudflareParitySurface(ctx, r, input.Surface, input.Spec, input.Observed, input.Apply, input.Windows)
	} else {
		switch input.Surface {
		case "reconcile":
			value, err = r.adguardReconcile(ctx, input.Spec, input.Observed, input.Apply)
		case "delete":
			value, err = r.adguardDelete(ctx, input.Spec, input.Observed, input.Apply)
		case "inventory":
			value, err = r.adguardInventory(ctx)
		case "clients":
			value, err = r.adguardClients(ctx)
		case "dns":
			value, err = r.adguardDNS(ctx)
		case "queries":
			value, err = r.adguardQueries(ctx)
		case "probe":
			value, err = r.adguardProbe(ctx, "example")
		default:
			fmt.Fprintln(os.Stderr, "unknown fixture surface")
			os.Exit(2)
		}
	}
	requests := []Object{}
	for _, request := range h.requests {
		entry := Object{"path": request.path, "method": request.method, "payload": request.payload}
		if request.ifMatch != "" {
			entry["if_match"] = request.ifMatch
		}
		requests = append(requests, entry)
	}
	output := Object{"result": value, "error": "", "requests": requests, "refused_parts": ledger.entries}
	if ledger.entries == nil {
		output["refused_parts"] = []Object{}
	}
	if err != nil {
		output["result"] = nil
		output["error"] = err.Error()
	}
	if result, ok := value.(runtime.Result); ok && err == nil {
		output["result"] = result
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		os.Exit(2)
	}
	os.Exit(0)
}

func cloudflareParitySurface(ctx context.Context, r *Registry, surface string, spec, observed Object, apply bool, windows []runtime.AnalyticsWindow) (any, error) {
	readers := map[string]func(context.Context) ([]any, error){
		"zones":             r.cloudflareZoneInventory,
		"records":           r.cloudflareRecordInventory,
		"pages":             r.cloudflarePagesProjects,
		"d1":                r.cloudflareD1Databases,
		"access_apps":       r.cloudflareAccessApps,
		"service_tokens":    r.cloudflareServiceTokens,
		"tunnels":           r.cloudflareTunnels,
		"edge_certificates": r.cloudflareEdgeCertificates,
		"redirects":         r.cloudflareRedirects,
	}
	switch surface {
	case "reconcile":
		return r.cloudflareRecordReconcile(ctx, spec, observed, apply)
	case "delete":
		return r.cloudflareRecordDelete(ctx, spec, observed, apply)
	case "probe_dns":
		return r.probes["cloudflare_dns"](ctx, "example")
	case "probe_api":
		return r.probes["cloudflare_api"](ctx, "account")
	case "analytics":
		sites, err := r.AnalyticsSites(ctx)
		if err != nil {
			return nil, err
		}
		return r.Analytics(ctx, sites, windows)
	}
	if read, ok := readers[surface]; ok {
		return read(ctx)
	}
	fmt.Fprintln(os.Stderr, "unknown fixture surface")
	os.Exit(2)
	return nil, nil
}
