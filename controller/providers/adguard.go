package providers

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
)

// adguardRewrite is one entry of /control/rewrite/list. Enabled is absent on
// AdGuard releases that predate per-rewrite toggles, which means enabled.
type adguardRewrite struct {
	Domain  string `json:"domain"`
	Answer  string `json:"answer"`
	Enabled *bool  `json:"enabled"`
}

func (w adguardRewrite) enabled() bool { return w.Enabled == nil || *w.Enabled }

type adguardRewritePair struct {
	Domain string `json:"domain"`
	Answer string `json:"answer"`
}

type adguardRewriteUpdate struct {
	Target adguardRewritePair `json:"target"`
	Update adguardRewritePair `json:"update"`
}

// adguardStatus is /control/status. DNSAddresses is raw so a present but
// empty field still proves the answer came from AdGuard.
type adguardStatus struct {
	Version           string          `json:"version"`
	Running           *bool           `json:"running"`
	ProtectionEnabled *bool           `json:"protection_enabled"`
	DNSAddresses      json.RawMessage `json:"dns_addresses"`
}

type AdGuardRewriteInventoryRecord struct {
	ConnectionRef string `json:"connection_ref"`
	Domain        string `json:"domain"`
	Answer        string `json:"answer"`
	Enabled       bool   `json:"enabled"`
}

func (r *Registry) admitAdGuard() {
	r.action("adguard.rewrite", "reconcile", r.adguardReconcile)
	r.action("adguard.rewrite", "delete", r.adguardDelete)
	r.reader("adguard.rewrite", r.adguardInventory)
	r.reader("adguard.client", r.adguardClients)
	r.reader("adguard.dns", r.adguardDNS)
	r.reader("adguard.query_summary", r.adguardQueries)
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

// adguardGet reads one AdGuard endpoint into its response type.
func adguardGet[T any](ctx context.Context, r *Registry, ref, path, invalid string) (T, error) {
	raw, err := r.adguardRequest(ctx, ref, path, "GET", nil)
	if err != nil {
		var zero T
		return zero, err
	}
	return decodeAs[T](raw, invalid)
}

func (r *Registry) adguardRewrites(ctx context.Context, ref string, cached bool) ([]adguardRewrite, error) {
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
	rewrites, err := decodeAs[[]adguardRewrite](raw, "Provider returned an invalid record list.")
	if err != nil {
		return nil, err
	}
	if rewrites == nil {
		rewrites = []adguardRewrite{}
	}
	return rewrites, nil
}

func (r *Registry) adguardReconcile(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	spec, err := decodePayload[AdGuardRewriteSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
	observed, _ := decodePayload[AdGuardRewriteObserved](rawObserved)
	rewrites, err := r.adguardRewrites(ctx, "", false)
	if err != nil {
		return Result{}, err
	}
	desired := adguardRewritePair{Domain: spec.Domain, Answer: spec.Answer}
	matches := matchingRewrites(rewrites, spec.Domain)
	if len(matches) == 0 && observed.Domain != "" && observed.Domain != spec.Domain {
		matches = matchingRewrites(rewrites, observed.Domain)
	}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: "AdGuard contains duplicate rewrites for the domain."}
	}
	var live adguardRewrite
	if len(matches) == 1 {
		live = matches[0]
	}
	changed := len(matches) == 0 || live.Domain != desired.Domain || live.Answer != desired.Answer
	if changed && apply {
		path, method, payload := "/control/rewrite/add", "POST", any(desired)
		if len(matches) == 1 {
			path, method = "/control/rewrite/update", "PUT"
			payload = adguardRewriteUpdate{Target: adguardRewritePair{Domain: live.Domain, Answer: live.Answer}, Update: desired}
		}
		if _, err := r.adguardRequest(ctx, "", path, method, payload); err != nil {
			return Result{}, err
		}
	}
	status := AdGuardRewriteStatus{Domain: spec.Domain, Answer: spec.Answer, Enabled: live.enabled()}
	if !status.Enabled {
		return Result{Changed: changed, Status: status, Conditions: []Condition{condition("Degraded", "Disabled", "The rewrite is disabled in AdGuard, so the name does not resolve. Enable it in AdGuard.")}, Message: "AdGuard rewrite is present but disabled."}, nil
	}
	message := "AdGuard rewrite unchanged."
	if changed {
		message = "AdGuard rewrite updated."
	}
	return result(changed, status, "Reconciled", "AdGuard rewrite is current.", message), nil
}

func matchingRewrites(rewrites []adguardRewrite, domain string) []adguardRewrite {
	found := []adguardRewrite{}
	for _, rewrite := range rewrites {
		if rewrite.Domain == domain {
			found = append(found, rewrite)
		}
	}
	return found
}

func (r *Registry) adguardDelete(ctx context.Context, rawSpec, _ Object, apply bool) (Result, error) {
	spec, err := decodePayload[AdGuardRewriteSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
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
			if _, err := r.adguardRequest(ctx, "", "/control/rewrite/delete", "POST", adguardRewritePair{Domain: match.Domain, Answer: match.Answer}); err != nil {
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
			if item.Domain == "" || item.Answer == "" {
				continue
			}
			found = append(found, AdGuardRewriteInventoryRecord{ConnectionRef: ref, Domain: item.Domain, Answer: item.Answer, Enabled: item.enabled()})
		}
	}
	return found, nil
}

func (r *Registry) adguardProbe(ctx context.Context, ref string) (ProbeResult, error) {
	status, err := adguardGet[adguardStatus](ctx, r, ref, "/control/status", "AdGuard did not return a status.")
	if err != nil {
		return ProbeResult{}, err
	}
	if len(status.DNSAddresses) == 0 {
		return ProbeResult{}, &ProviderError{Message: "AdGuard did not return a status."}
	}
	return ProbeResult{Detail: strings.TrimSpace("AdGuard " + status.Version), Reaches: []string{}}, nil
}
