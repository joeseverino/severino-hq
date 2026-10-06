package providers

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/adguardapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// rewriteEnabled reads a rewrite's toggle; it is absent on AdGuard releases
// that predate per-rewrite toggles, which means enabled.
func rewriteEnabled(w adguardapi.RewriteEntry) bool { return w.Enabled == nil || *w.Enabled }

// rewritePair is the target or the update of a rewrite call: domain and
// answer only, so the toggle is left as AdGuard holds it.
func rewritePair(domain, answer string) adguardapi.RewriteEntry {
	return adguardapi.RewriteEntry{Domain: &domain, Answer: &answer}
}

type AdGuardRewriteInventoryRecord struct {
	ConnectionRef string `json:"connection_ref"`
	Domain        string `json:"domain"`
	Answer        string `json:"answer"`
	Enabled       bool   `json:"enabled"`
}

func (r *Registry) admitAdGuard() {
	act(r, runtime.ResourceKindAdGuardRewrite, "reconcile", r.adguardReconcile)
	act(r, runtime.ResourceKindAdGuardRewrite, "delete", r.adguardDelete)
	r.reader(runtime.ResourceKindAdGuardRewrite, r.adguardInventory)
	r.reader(runtime.ResourceKindAdGuardClient, r.adguardClients)
	r.reader(runtime.ResourceKindAdGuardDNS, r.adguardDNS)
	r.reader(runtime.ResourceKindAdGuardQuerySummary, r.adguardQueries)
	r.probe(runtime.ConnectionProviderAdGuard, r.adguardProbe)
}

func (r *Registry) adguardRequest(ctx context.Context, ref, path, method string, payload any) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderAdGuard, ref)
	if err != nil {
		return nil, err
	}
	base, err := r.Env.Required(prefix, "URL")
	if err != nil {
		return nil, err
	}
	user, err := r.Env.Required(prefix, "USERNAME")
	if err != nil {
		return nil, err
	}
	password, err := r.Env.Required(prefix, "PASSWORD")
	if err != nil {
		return nil, err
	}
	headers := map[string]string{"Authorization": "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"+password))}
	return r.HTTP.Request(ctx, strings.TrimRight(base, "/")+path, method, headers, payload)
}

// adguardDecode decodes one AdGuard answer into its generated type; an empty
// answer is the zero value, a malformed one an error.
func adguardDecode[T any](raw json.RawMessage, what string) (T, error) {
	var answer T
	if len(raw) == 0 {
		return answer, nil
	}
	if err := json.Unmarshal(raw, &answer); err != nil {
		return answer, &ProviderError{Message: "adguard " + what + " did not decode", Err: err}
	}
	return answer, nil
}

// adguardGet reads one AdGuard endpoint into its response type. A sweep reads
// each endpoint once, whichever kinds need it.
func adguardGet[T any](ctx context.Context, r *Registry, ref, path, what string) (T, error) {
	raw, err := r.cached(ctx, "adguard-get:"+ref+":"+path, func() (json.RawMessage, error) {
		return r.adguardRequest(ctx, ref, path, "GET", nil)
	})
	if err != nil {
		var zero T
		return zero, fmt.Errorf("adguard %s: %w", what, err)
	}
	return adguardDecode[T](raw, what)
}

func (r *Registry) adguardRewrites(ctx context.Context, ref string, cached bool) ([]adguardapi.RewriteEntry, error) {
	load := func() (json.RawMessage, error) {
		return r.adguardRequest(ctx, ref, "/control/rewrite/list", "GET", nil)
	}
	var raw json.RawMessage
	var err error
	if cached {
		raw, err = r.cached(ctx, "adguard-rewrites:"+ref, load)
	} else {
		raw, err = load()
	}
	if err != nil {
		return nil, fmt.Errorf("adguard rewrite list: %w", err)
	}
	rewrites, err := adguardDecode[[]adguardapi.RewriteEntry](raw, "rewrite list")
	if err != nil {
		return nil, err
	}
	if rewrites == nil {
		rewrites = []adguardapi.RewriteEntry{}
	}
	return rewrites, nil
}

func (r *Registry) adguardReconcile(ctx context.Context, spec AdGuardRewriteSpec, observed AdGuardRewriteObserved, apply bool) (Result, error) {
	rewrites, err := r.adguardRewrites(ctx, "", false)
	if err != nil {
		return Result{}, err
	}
	desired := rewritePair(spec.Domain, spec.Answer)
	matches := matchingRewrites(rewrites, spec.Domain)
	if len(matches) == 0 && observed.Domain != "" && observed.Domain != spec.Domain {
		matches = matchingRewrites(rewrites, observed.Domain)
	}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: "adguard holds more than one rewrite for " + spec.Domain}
	}
	var live adguardapi.RewriteEntry
	if len(matches) == 1 {
		live = matches[0]
	}
	changed := len(matches) == 0 || deref(live.Domain) != spec.Domain || deref(live.Answer) != spec.Answer
	if changed && apply {
		path, method, payload := "/control/rewrite/add", "POST", any(desired)
		if len(matches) == 1 {
			path, method = "/control/rewrite/update", "PUT"
			target := rewritePair(deref(live.Domain), deref(live.Answer))
			payload = adguardapi.RewriteUpdate{Target: &target, Update: &desired}
		}
		if _, err := r.adguardRequest(ctx, "", path, method, payload); err != nil {
			return Result{}, fmt.Errorf("adguard write rewrite: %w", err)
		}
	}
	status := AdGuardRewriteStatus{Domain: spec.Domain, Answer: spec.Answer, Enabled: rewriteEnabled(live)}
	if !status.Enabled {
		return Result{Changed: changed, Status: status, Conditions: []Condition{condition(runtime.ConditionDegraded, "Disabled", "The record is switched off in AdGuard, so the name does not resolve. Switch it on in AdGuard.")}, Message: "The AdGuard record is switched off."}, nil
	}
	message := "AdGuard record unchanged."
	if changed {
		message = "AdGuard record updated."
	}
	return result(changed, status, "Reconciled", "The AdGuard record is as HQ set it.", message), nil
}

func matchingRewrites(rewrites []adguardapi.RewriteEntry, domain string) []adguardapi.RewriteEntry {
	found := []adguardapi.RewriteEntry{}
	for _, rewrite := range rewrites {
		if deref(rewrite.Domain) == domain {
			found = append(found, rewrite)
		}
	}
	return found
}

func (r *Registry) adguardDelete(ctx context.Context, spec AdGuardRewriteSpec, _ struct{}, apply bool) (Result, error) {
	rewrites, err := r.adguardRewrites(ctx, "", false)
	if err != nil {
		return Result{}, err
	}
	matches := matchingRewrites(rewrites, spec.Domain)
	status := AdGuardDeleteStatus{Domain: spec.Domain, Removed: true}
	if len(matches) == 0 {
		return result(false, status, "Absent", "No such record in AdGuard.", "AdGuard record was already gone."), nil
	}
	if apply {
		for _, match := range matches {
			if _, err := r.adguardRequest(ctx, "", "/control/rewrite/delete", "POST", rewritePair(deref(match.Domain), deref(match.Answer))); err != nil {
				return Result{}, fmt.Errorf("adguard delete rewrite: %w", err)
			}
		}
	}
	return result(true, status, "Removed", "The AdGuard record was removed.", "AdGuard record removed."), nil
}

func (r *Registry) adguardInventory(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs(runtime.ConnectionProviderAdGuard) {
		rewrites, err := r.adguardRewrites(ctx, ref, true)
		if err != nil {
			return nil, err
		}
		for _, item := range rewrites {
			if deref(item.Domain) == "" || deref(item.Answer) == "" {
				continue
			}
			found = append(found, AdGuardRewriteInventoryRecord{ConnectionRef: ref, Domain: *item.Domain, Answer: *item.Answer, Enabled: rewriteEnabled(item)})
		}
	}
	return found, nil
}

// adguardProbe reaches AdGuard with the connection's credential. An answer
// with a version is AdGuard's status; dns_addresses may be null, which is
// AdGuard listening on nothing yet, and says nothing about the credential.
func (r *Registry) adguardProbe(ctx context.Context, ref string) (ProbeResult, error) {
	status, err := adguardGet[adguardapi.ServerStatus](ctx, r, ref, "/control/status", "status")
	if err != nil {
		return ProbeResult{}, err
	}
	if deref(status.Version) == "" {
		return ProbeResult{}, &ProviderError{Message: "adguard status has no version, so it is not AdGuard's answer", Failure: runtime.FailureClassAddress}
	}
	return ProbeResult{Detail: "AdGuard " + *status.Version, Reaches: []string{}}, nil
}
