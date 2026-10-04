package providers

import (
	"context"
	"encoding/base64"
	"encoding/json"
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
	r.probe("adguard", r.adguardProbe)
}

func (r *Registry) adguardRequest(ctx context.Context, ref, path, method string, payload any) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix("adguard", ref)
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

// adguardGet reads one AdGuard endpoint into its response type. A sweep reads
// each endpoint once, whichever kinds need it.
func adguardGet[T any](ctx context.Context, r *Registry, ref, path, invalid string) (T, error) {
	raw, err := r.cached(ctx, "adguard-get:"+ref+":"+path, func() (json.RawMessage, error) {
		return r.adguardRequest(ctx, ref, path, "GET", nil)
	})
	if err != nil {
		var zero T
		return zero, err
	}
	return decodeAs[T](raw, invalid)
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
		return nil, err
	}
	rewrites, err := decodeAs[[]adguardapi.RewriteEntry](raw, "Provider returned an invalid record list.")
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
		return Result{}, &ProviderError{Message: "AdGuard contains duplicate rewrites for the domain."}
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
			return Result{}, err
		}
	}
	status := AdGuardRewriteStatus{Domain: spec.Domain, Answer: spec.Answer, Enabled: rewriteEnabled(live)}
	if !status.Enabled {
		return Result{Changed: changed, Status: status, Conditions: []Condition{condition("Degraded", "Disabled", "The rewrite is disabled in AdGuard, so the name does not resolve. Enable it in AdGuard.")}, Message: "AdGuard rewrite is present but disabled."}, nil
	}
	message := "AdGuard rewrite unchanged."
	if changed {
		message = "AdGuard rewrite updated."
	}
	return result(changed, status, "Reconciled", "AdGuard rewrite is current.", message), nil
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
		return result(false, status, "Absent", "No such rewrite in AdGuard.", "AdGuard rewrite was already absent."), nil
	}
	if apply {
		for _, match := range matches {
			if _, err := r.adguardRequest(ctx, "", "/control/rewrite/delete", "POST", rewritePair(deref(match.Domain), deref(match.Answer))); err != nil {
				return Result{}, err
			}
		}
	}
	return result(true, status, "Removed", "AdGuard rewrite was removed.", "AdGuard rewrite removed."), nil
}

func (r *Registry) adguardInventory(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs("adguard") {
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

func (r *Registry) adguardProbe(ctx context.Context, ref string) (ProbeResult, error) {
	status, err := adguardGet[adguardapi.ServerStatus](ctx, r, ref, "/control/status", "AdGuard did not return a status.")
	if err != nil {
		return ProbeResult{}, err
	}
	if status.DnsAddresses == nil {
		return ProbeResult{}, &ProviderError{Message: "AdGuard did not return a status."}
	}
	return ProbeResult{Detail: strings.TrimSpace("AdGuard " + deref(status.Version)), Reaches: []string{}}, nil
}
