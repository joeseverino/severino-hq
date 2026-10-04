package providers

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Controller is the native runtime.Providers: the registry's handlers,
// dispatched by HQ's provider declarations, which arrive through the bridge.
type Controller struct {
	*Registry
	Declared runtime.ControllerRegistry
	Log      *slog.Logger
}

var _ runtime.Providers = (*Controller)(nil)

// Why Execute refused; match with errors.Is.
var (
	ErrUnsupported = errors.New("no handler for this kind and action")
	// ErrUndeclared fails closed: a write nothing declares a connection for
	// has no manages switch an operator could have set.
	ErrUndeclared        = errors.New("nothing declares which connections act for this kind")
	ErrForeignConnection = errors.New("not a connection for this kind")
	ErrNoManager         = errors.New("no connection this controller holds acts for this kind")
	ErrObserveOnly       = errors.New("only observes; set manages on the connection to act through it")
)

func NewController(r *Registry, declared runtime.ControllerRegistry) *Controller {
	r.Extensions = declared.Extensions
	return &Controller{Registry: r, Declared: declared}
}

func (c *Controller) logger() *slog.Logger {
	if c.Log != nil {
		return c.Log
	}
	return slog.Default()
}

// Capabilities is the declared actions this controller implements. An action it
// has no handler for is left for a controller that does, never claimed and failed.
func (c *Controller) Capabilities() []runtime.Capability {
	found := []runtime.Capability{}
	for _, capability := range c.Declared.Capabilities {
		if _, ok := c.actions[actionKey{capability.Kind, capability.Action}]; ok {
			found = append(found, capability)
		}
	}
	return found
}

// Uncovered is the declared actions this controller has no handler for.
func (c *Controller) Uncovered() []string {
	found := []string{}
	for _, capability := range c.Declared.Capabilities {
		if _, ok := c.actions[actionKey{capability.Kind, capability.Action}]; !ok {
			found = append(found, string(capability.Kind)+":"+capability.Action)
		}
	}
	return found
}

func (c *Controller) NeedsMaterial(kind runtime.ResourceKind) bool {
	for _, found := range c.Declared.MaterialKinds {
		if found == kind {
			return true
		}
	}
	return false
}

func (c *Controller) locked(kind runtime.ResourceKind, action string) (string, bool) {
	for _, entry := range c.Declared.Locked {
		if entry.Kind == kind && entry.Action == action {
			return entry.Reason, true
		}
	}
	return "", false
}

// Execute runs one action. A locked action is refused with the registry's own
// reason; a write goes only through connections that declare manages.
func (c *Controller) Execute(ctx context.Context, resource runtime.Resource, action string, apply bool) (runtime.Result, error) {
	if reason, locked := c.locked(resource.Kind, action); locked {
		return runtime.Result{}, &ProviderError{Message: reason}
	}
	handler, ok := c.actions[actionKey{resource.Kind, action}]
	if !ok {
		return runtime.Result{}, &ProviderError{Message: string(resource.Kind) + "/" + action, Err: ErrUnsupported}
	}
	if apply {
		if err := c.refuseUnlessManaged(resource.Kind, resource.Spec); err != nil {
			return runtime.Result{}, err
		}
	}
	observed := resource.Observed
	if observed == nil {
		observed = Object{}
	}
	return handler(ctx, resource.Spec, observed, apply)
}

func (c *Controller) sshRefs() map[string]bool {
	refs := map[string]bool{}
	for _, ref := range c.Env.SSHRefs() {
		refs[ref] = true
	}
	return refs
}

// effectiveProvider is the provider a connection acts as: its own when it has
// a probe, else ssh (or its declared provider) for a transport.
func (c *Controller) effectiveProvider(ref string, ssh map[string]bool) string {
	provider := c.Env.Provider(ref)
	if _, ok := c.probes[provider]; ok {
		return provider
	}
	if ssh[ref] {
		if declared := strings.TrimSpace(c.Env[c.Env.Prefixes()[ref]+"_PROVIDER"]); declared != "" {
			return declared
		}
		return "ssh"
	}
	return provider
}

func sortedRefs(prefixes map[string]string) []string {
	refs := make([]string, 0, len(prefixes))
	for ref := range prefixes {
		refs = append(refs, ref)
	}
	sort.Strings(refs)
	return refs
}

// namedRef is the connection a spec names, or "" for the kind's default. The
// manages gate checks this one and the write uses it.
func namedRef(spec Object) string {
	value, _ := spec["connection_ref"].(string)
	return value
}

func (c *Controller) refuseUnlessManaged(kind runtime.ResourceKind, spec Object) error {
	providers := c.Declared.ConnectionProviders[string(kind)]
	if len(providers) == 0 {
		return &ProviderError{Message: string(kind), Err: ErrUndeclared}
	}
	named := namedRef(spec)
	refs := []string{}
	ssh := c.sshRefs()
	if named != "" {
		if !slices.Contains(providers, c.effectiveProvider(named, ssh)) {
			return &ProviderError{Message: named, Err: ErrForeignConnection}
		}
		refs = append(refs, named)
	} else {
		for _, ref := range sortedRefs(c.Env.Prefixes()) {
			for _, provider := range providers {
				if c.effectiveProvider(ref, ssh) == provider {
					refs = append(refs, ref)
					break
				}
			}
		}
	}
	if len(refs) == 0 {
		return &ProviderError{Message: string(kind), Err: ErrNoManager}
	}
	observing := []string{}
	for _, ref := range refs {
		if !c.Env.Manages(ref) {
			observing = append(observing, ref)
		}
	}
	if len(observing) > 0 {
		return &ProviderError{Message: strings.Join(observing, ", "), Err: ErrObserveOnly}
	}
	return nil
}

var defaultEndpoints = map[string]string{"tailscale": tailnetAPI}

// endpoint is where a connection points: a URL or a host, never a secret.
func (c *Controller) endpoint(prefix, provider string) string {
	for _, name := range []string{"URL", "DIRECTORY_URL"} {
		if url := strings.TrimSpace(c.Env[prefix+"_"+name]); url != "" {
			return url
		}
	}
	host, port := strings.TrimSpace(c.Env[prefix+"_HOST"]), strings.TrimSpace(c.Env[prefix+"_PORT"])
	if host != "" {
		if port != "" {
			return host + ":" + port
		}
		return host
	}
	return defaultEndpoints[provider]
}

// store is where the connection's credential is kept: references only.
func (c *Controller) store(prefix string) map[string]string {
	found := map[string]string{}
	for key, name := range map[string]string{"vault": "STORE_VAULT", "item": "STORE_ITEM", "bootstrap": "BOOTSTRAP"} {
		if value := strings.TrimSpace(c.Env[prefix+"_"+name]); value != "" {
			found[key] = value
		}
	}
	return found
}

func (c *Controller) probeSSH(ctx context.Context, ref string) (ProbeResult, error) {
	if _, err := c.commands().SSH(ctx, ref, "preflight", nil); err != nil {
		return ProbeResult{}, err
	}
	target, err := c.Env.SSH(ref)
	if err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: fmt.Sprintf("%s@%s:%d", target.User, target.Host, target.Port), Reaches: []string{target.Host}}, nil
}

func failureOf(err error) runtime.FailureClass {
	failure, _, _ := runtime.Classify(err)
	return failure
}

// Connections is every connection the environment carries and whether it
// answers. One failure is that connection's failure, reported rather than raised.
// An SSH connection HQ says is recently good is carried, not logged in to again.
func (c *Controller) Connections(ctx context.Context, carry []string) ([]runtime.ConnectionRecord, error) {
	carried := map[string]bool{}
	for _, ref := range carry {
		carried[ref] = true
	}
	ssh := c.sshRefs()
	prefixes := c.Env.Prefixes()
	reported := []runtime.ConnectionRecord{}
	for _, ref := range sortedRefs(prefixes) {
		prefix := prefixes[ref]
		provider := c.effectiveProvider(ref, ssh)
		probe := c.probes[provider]
		viaSSH := false
		if probe == nil && ssh[ref] {
			probe, viaSSH = c.probeSSH, true
		}
		connection := runtime.ConnectionRecord{
			ConnectionRef: ref, Provider: provider, Endpoint: c.endpoint(prefix, provider),
			Manages: c.Env.Manages(ref), Probed: probe != nil, OK: true, Reaches: []string{},
		}
		if store := c.store(prefix); len(store) > 0 {
			connection.Store = store
		}
		switch {
		case viaSSH && carried[ref]:
			connection.Carried, connection.Probed, connection.Detail = true, false, "Not asked again this sweep."
		case probe == nil:
			connection.Detail = "No probe for this kind of connection."
		default:
			result, err := probe(ctx, ref)
			if err != nil {
				connection.OK, connection.Detail, connection.Failure = false, runtime.ReportText(err.Error()), failureOf(err)
			} else {
				connection.Detail, connection.Reaches = result.Detail, result.Reaches
				if result.ExpiresAt != nil {
					connection.ExpiresAt = *result.ExpiresAt
				}
				if connection.Reaches == nil {
					connection.Reaches = []string{}
				}
			}
		}
		reported = append(reported, connection)
	}
	return reported, nil
}

// providerOf is whose kind this is, so one provider's kinds are read in turn:
// every kind read by logging in to a machine is the ssh group.
func (c *Controller) providerOf(kind string) string {
	if c.Declared.Observations[kind] == "ssh" {
		return "ssh"
	}
	for _, provider := range c.Declared.ConnectionProviders[kind] {
		if provider == "ssh" {
			return "ssh"
		}
	}
	vendor, _, _ := strings.Cut(kind, ".")
	return vendor
}

// hasSource is whether anything this controller holds can read the kind.
func (c *Controller) hasSource(kind string, connected map[string]bool) bool {
	var needs []string
	if provider, ok := c.Declared.Observations[kind]; ok {
		needs = []string{provider}
	} else if providers, ok := c.Declared.ConnectionProviders[kind]; ok {
		needs = providers
	} else {
		return true
	}
	credentials := map[string]bool{}
	for _, provider := range c.Declared.ConnectionCredentials {
		credentials[provider] = true
	}
	for _, provider := range needs {
		if credentials[provider] && connected[provider] {
			return true
		}
	}
	switch runtime.ResourceKind(kind) {
	case runtime.ResourceKindTailscaleDevice:
		return c.Env["SEVERINO_TAILNET_STATUS"] != ""
	case runtime.ResourceKindHostFirewall:
		return c.Env["SEVERINO_HOST_FIREWALL"] != ""
	}
	return false
}

// readKind is one kind's report: its records and any parts refused while they
// were read, or the refusal of the whole read.
func (c *Controller) readKind(ctx context.Context, reader Reader) runtime.KindReport {
	ledger := &refusals{}
	records, err := reader(context.WithValue(ctx, refusalKey{}, ledger))
	if err != nil {
		_, refusal, reason := runtime.Classify(err)
		report := runtime.KindReport{OK: false, Records: []any{}, Error: runtime.ReportText(err.Error()), Refusal: refusal}
		if refusal == runtime.RefusalCredential && reason != "" {
			report.Error = runtime.ReportText(reason)
		}
		return report
	}
	if records == nil {
		records = []any{}
	}
	return runtime.KindReport{OK: true, Records: records, RefusedParts: ledger.entries}
}

// Inventory is everything each provider holds; with only, just those kinds.
// Providers are read at once, one provider's kinds in turn, so a credential it
// refuses is refused once and not by every kind asking at the same moment.
// An unreachable provider reports as unreachable rather than failing the sweep.
func (c *Controller) Inventory(ctx context.Context, only []runtime.ResourceKind) (runtime.Inventory, error) {
	wantedOnly := map[string]bool{}
	for _, kind := range only {
		wantedOnly[string(kind)] = true
	}
	ssh := c.sshRefs()
	connected := map[string]bool{}
	for ref := range c.Env.Prefixes() {
		connected[c.effectiveProvider(ref, ssh)] = true
	}
	found := runtime.Inventory{}
	groups := map[string][]string{}
	for _, kind := range sortedKeys(c.readers) {
		if len(wantedOnly) > 0 && !wantedOnly[kind] {
			continue
		}
		if !c.hasSource(kind, connected) {
			unconnected := false
			found[kind] = runtime.KindReport{OK: true, Records: []any{}, Connected: &unconnected}
			continue
		}
		group := c.providerOf(kind)
		groups[group] = append(groups[group], kind)
	}
	started := time.Now()
	took := map[string]time.Duration{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, kinds := range groups {
		wg.Add(1)
		go func(kinds []string) {
			defer wg.Done()
			for _, kind := range kinds {
				began := time.Now()
				report := c.readKind(ctx, c.readers[kind])
				mu.Lock()
				found[kind], took[kind] = report, time.Since(began)
				mu.Unlock()
			}
		}(kinds)
	}
	wg.Wait()
	c.sayIfSlow(took, time.Since(started))
	return found, nil
}

// sayIfSlow logs the slowest readers when a sweep ran long: kinds, never records.
func (c *Controller) sayIfSlow(took map[string]time.Duration, elapsed time.Duration) {
	if elapsed < runtime.SlowSweep {
		return
	}
	kinds := sortedKeys(took)
	sort.SliceStable(kinds, func(i, j int) bool { return took[kinds[i]] > took[kinds[j]] })
	if len(kinds) > 5 {
		kinds = kinds[:5]
	}
	parts := make([]string, len(kinds))
	for i, kind := range kinds {
		parts[i] = fmt.Sprintf("%s %.0fs", kind, took[kind].Seconds())
	}
	c.logger().Warn(fmt.Sprintf("sweep read %d kinds in %.0fs; slowest: %s", len(took), elapsed.Seconds(), strings.Join(parts, ", ")),
		slog.String("event", "controller.sweep.slow"))
}

func (c *Controller) StepFailures() []runtime.StepFailure { return c.commands().StepFailures() }
