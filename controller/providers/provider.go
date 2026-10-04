// Package providers contains native controller integrations. It has no database
// access; desired state and policy arrive through HQ's bridge contract.
package providers

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

type Object = runtime.Object
type Result = runtime.Result
type Condition = runtime.Condition
type ProviderError = runtime.ProviderError

// Transport answers with validated, undecoded JSON; each provider decodes it
// into its own response type. The payload is a provider request struct, url.Values or a string.
type Transport interface {
	Request(context.Context, string, string, map[string]string, any) (json.RawMessage, error)
	// Header GETs an address for one response header, such as an ETag.
	Header(ctx context.Context, address string, headers map[string]string, name string) (string, error)
}

// Action receives the resource's spec and observed payloads, opaque in the
// bridge contract, and decodes them into its kind's types.
type Action func(context.Context, Object, Object, bool) (Result, error)

// Reader returns one kind's records, each a kind-specific struct.
type Reader func(context.Context) ([]any, error)
type Probe func(context.Context, string) (ProbeResult, error)

type ProbeResult struct {
	Detail    string   `json:"detail"`
	Reaches   []string `json:"reaches"`
	ExpiresAt *string  `json:"expires_at,omitempty"` // credential expiry, where the probe reads one
}

type Coverage struct {
	Actions []string `json:"actions"`
	Readers []string `json:"readers"`
	Probes  []string `json:"probes"`
}

type actionKey struct{ kind, action string }

type Registry struct {
	Env      runtime.Environment
	HTTP     Transport
	Now      func() time.Time
	Commands *Commands
	TLS      TLSDialer
	// Monotonic times a verification deadline, apart from the wall clock.
	Monotonic func() time.Time
	// Sleep waits between verification attempts; injected in tests.
	Sleep func(context.Context, time.Duration) error
	// Resolve returns a name's IPv4 address; nil resolves through DNS.
	Resolve func(host string) (string, error)
	// Dial reports whether a TCP connection to address:port is accepted.
	Dial func(ctx context.Context, address string, port int) bool
	// PublishedContainers is the containers the sweep found, with their ports.
	PublishedContainers func(context.Context) ([]PublishedContainer, error)
	// Portainer is the Portainer access the dashboard glance reads containers through.
	Portainer PortainerSource
	// ControllerID names this installation; empty uses HQ_CONTROLLER_ID or the host name.
	ControllerID string
	actions      map[actionKey]Action
	readers      map[string]Reader
	probes       map[string]Probe
	snapshotMu   sync.Mutex
	snapshot     map[string]*cachedRead
	// refusedCredentials holds, for this sweep, each Cloudflare credential prefix
	// refused outright and why; nil outside a sweep.
	refusedCredentials map[string]string
	// zoneIDs maps a Cloudflare zone name to its id for the controller's life.
	zoneIDs map[string]string
	// analyticsSites keeps each measured site's account and host between
	// AnalyticsSites and Analytics.
	analyticsSites map[runtime.AnalyticsSiteIdentity]CloudflareAnalyticsSite
}
type cachedRead struct {
	ready chan struct{}
	value json.RawMessage
	err   error
}

func New(env runtime.Environment, transport Transport) *Registry {
	r := &Registry{
		Env: env, HTTP: transport, Commands: &Commands{Env: env}, TLS: NetTLSDialer{CAFile: env["HQ_CONTROLLER_CA_FILE"]},
		Now: time.Now, Monotonic: time.Now, Sleep: sleepContext,
		actions: map[actionKey]Action{}, readers: map[string]Reader{}, probes: map[string]Probe{}, zoneIDs: map[string]string{},
	}
	r.admitAdGuard()
	r.admitNPM()
	r.admitTailscale()
	r.admitHostReadings()
	r.admitPortainer()
	r.admitCloudflare()
	r.admitTLS()
	return r
}

func (r *Registry) commands() *Commands {
	if r.Commands == nil {
		r.Commands = &Commands{Env: r.Env}
	}
	return r.Commands
}

func (r *Registry) action(kind, action string, handler Action) {
	key := actionKey{kind, action}
	if _, exists := r.actions[key]; exists {
		panic("duplicate native controller action: " + kind + "/" + action)
	}
	r.actions[key] = handler
}
func (r *Registry) reader(kind string, handler Reader) {
	if _, exists := r.readers[kind]; exists {
		panic("duplicate native controller reader: " + kind)
	}
	r.readers[kind] = handler
}
func (r *Registry) probe(provider string, handler Probe) {
	if _, exists := r.probes[provider]; exists {
		panic("duplicate native controller probe: " + provider)
	}
	r.probes[provider] = handler
}

// Coverage is deliberately separate from admission: unsupported work must never
// disappear silently when migrating from the host's declared controller set.
func (r *Registry) Coverage() Coverage {
	actions := []string{}
	readers := []string{}
	probes := []string{}
	for key := range r.actions {
		actions = append(actions, key.kind+":"+key.action)
	}
	for kind := range r.readers {
		readers = append(readers, kind)
	}
	for provider := range r.probes {
		probes = append(probes, provider)
	}
	sort.Strings(actions)
	sort.Strings(readers)
	sort.Strings(probes)
	return Coverage{Actions: actions, Readers: readers, Probes: probes}
}

func (r *Registry) BeginSnapshot() func() {
	r.snapshotMu.Lock()
	r.snapshot = map[string]*cachedRead{}
	r.refusedCredentials = map[string]string{}
	r.snapshotMu.Unlock()
	return func() {
		r.snapshotMu.Lock()
		r.snapshot, r.refusedCredentials = nil, nil
		r.snapshotMu.Unlock()
	}
}
func (r *Registry) cached(ctx context.Context, key string, load func() (json.RawMessage, error)) (json.RawMessage, error) {
	r.snapshotMu.Lock()
	if r.snapshot == nil {
		r.snapshotMu.Unlock()
		return load()
	}
	cache := r.snapshot
	if found := cache[key]; found != nil {
		r.snapshotMu.Unlock()
		select {
		case <-found.ready:
			return found.value, found.err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	found := &cachedRead{ready: make(chan struct{})}
	cache[key] = found
	r.snapshotMu.Unlock()
	found.value, found.err = load()
	r.snapshotMu.Lock()
	if found.err != nil {
		delete(cache, key)
	}
	close(found.ready)
	r.snapshotMu.Unlock()
	return found.value, found.err
}

type refusalKey struct{}
type refusals struct{ entries []runtime.RefusedPart }

func refuse(ctx context.Context, part, ref, scope string, err error) {
	refuseAt(ctx, part, ref, scope, "", err)
}

// refuseAt reports a refused part whose scope is a machine, with its address.
func refuseAt(ctx context.Context, part, ref, scope, address string, err error) {
	ledger, _ := ctx.Value(refusalKey{}).(*refusals)
	if ledger == nil {
		return
	}
	failure := ""
	var provider *ProviderError
	if errors.As(err, &provider) {
		failure = provider.Failure
		if provider.Refusal != "" {
			failure = provider.Refusal
		}
	}
	reason := []rune(err.Error())
	if len(reason) > 200 {
		reason = reason[:200]
	}
	ledger.entries = append(ledger.entries, runtime.RefusedPart{Part: part, ConnectionRef: ref, Scope: scope, Refusal: failure, Reason: string(reason), Address: address})
}

func sleepContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// decodePayload decodes a resource's opaque spec or observed payload into its kind's type.
func decodePayload[T any](src Object) (T, error) {
	var target T
	if src == nil {
		return target, nil
	}
	data, err := json.Marshal(src)
	if err != nil {
		return target, err
	}
	err = json.Unmarshal(data, &target)
	return target, err
}

// decodeAs decodes one provider answer into its response type. An empty or
// null answer is the zero value; one of the wrong shape fails with invalid.
func decodeAs[T any](raw json.RawMessage, invalid string) (T, error) {
	var target T
	if len(raw) == 0 || string(raw) == "null" {
		return target, nil
	}
	if err := json.Unmarshal(raw, &target); err != nil {
		return target, &ProviderError{Message: invalid}
	}
	return target, nil
}

func hostname(value string) string {
	return strings.TrimRight(strings.ToLower(strings.TrimSpace(value)), ".")
}
func condition(kind, reason, message string) Condition {
	return Condition{Type: kind, Status: true, Reason: reason, Message: message}
}
func result(changed bool, status any, reason, detail, message string) Result {
	return Result{Changed: changed, Status: status, Conditions: []Condition{condition("Ready", reason, detail)}, Message: message}
}
func (r *Registry) refs(provider string) []string {
	refs := r.Env.Refs(provider)
	if len(refs) == 0 {
		return []string{""}
	}
	return refs
}
