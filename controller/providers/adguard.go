package providers

import (
	"cmp"
	"context"
	"encoding/base64"
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

// adguard is the generated client for one AdGuard connection. A sweep's
// client reads each endpoint once, whichever kinds need it; a write's reads
// fresh. A connection that is missing or holds no login fails every call.
func (r *Registry) adguard(ref string, sweep bool) *adguardapi.ClientWithResponses {
	connection, err := r.Supplied.For(runtime.ConnectionProviderAdGuard, ref)
	login, missing := runtime.Need(connection.Login)
	doer := vendorDoer{r: r, err: cmp.Or(err, missing), headers: map[string]string{"Authorization": "Basic " + base64.StdEncoding.EncodeToString([]byte(login.Username+":"+login.Password.Reveal()))}}
	if sweep {
		doer.cache = "adguard:" + ref
	}
	return &adguardapi.ClientWithResponses{ClientInterface: &adguardapi.API{Server: strings.TrimRight(login.URL, "/") + adguardapi.ServerUrlControl + "/", Client: doer}}
}

func adguardRewrites(ctx context.Context, client *adguardapi.ClientWithResponses) (adguardapi.RewriteList, error) {
	rewrites, err := json200(client.RewriteListWithResponse(ctx))
	if err != nil {
		return nil, fmt.Errorf("adguard rewrite list: %w", err)
	}
	return rewrites, nil
}

func (r *Registry) adguardReconcile(ctx context.Context, spec AdGuardRewriteSpec, observed AdGuardRewriteObserved, apply bool) (Result, error) {
	client := r.adguard("", false)
	rewrites, err := adguardRewrites(ctx, client)
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
		if len(matches) == 1 {
			target := rewritePair(deref(live.Domain), deref(live.Answer))
			_, err = client.RewriteUpdateWithResponse(ctx, adguardapi.RewriteUpdate{Target: &target, Update: &desired})
		} else {
			_, err = client.RewriteAddWithResponse(ctx, desired)
		}
		if err != nil {
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
	client := r.adguard("", false)
	rewrites, err := adguardRewrites(ctx, client)
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
			if _, err := client.RewriteDeleteWithResponse(ctx, rewritePair(deref(match.Domain), deref(match.Answer))); err != nil {
				return Result{}, fmt.Errorf("adguard delete rewrite: %w", err)
			}
		}
	}
	return result(true, status, "Removed", "The AdGuard record was removed.", "AdGuard record removed."), nil
}

func (r *Registry) adguardInventory(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs(runtime.ConnectionProviderAdGuard) {
		client := r.adguard(ref, true)
		rewrites, err := adguardRewrites(ctx, client)
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
	client := r.adguard(ref, true)
	status, err := json200(client.StatusWithResponse(ctx))
	if err != nil {
		return ProbeResult{}, fmt.Errorf("adguard status: %w", err)
	}
	if deref(status.Version) == "" {
		return ProbeResult{}, &ProviderError{Message: "adguard status has no version, so it is not AdGuard's answer", Failure: runtime.FailureClassAddress}
	}
	return ProbeResult{Detail: "AdGuard " + *status.Version, Reaches: []string{}}, nil
}
