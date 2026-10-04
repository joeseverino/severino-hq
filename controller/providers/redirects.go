package providers

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
)

// Cloudflare redirects as records: which names a rule matches and where it sends
// them. A rule's names come from its expression (http.host comparisons and URL
// literals); a page rule's from its URL pattern. A target is a static URL or an
// expression; its host is the first URL literal it names.

// redirectPhase is the dynamic redirect phase: zone Single Redirects.
const redirectPhase = "http_request_dynamic_redirect"

const (
	redirectSourceRule     = "rule"
	redirectSourcePageRule = "page_rule"
	pageRuleForwarding     = "forwarding_url"
	pageRuleActive         = "active"
	redirectAction         = "redirect"
)

var (
	redirectHostField = regexp.MustCompile(`http\.host\s*(?:eq|==|in|contains|wildcard|strict\s+wildcard)\s*(\{[^}]*\}|r?"[^"]*")`)
	redirectQuoted    = regexp.MustCompile(`"([^"]*)"`)
	redirectURL       = regexp.MustCompile(`https?://([A-Za-z0-9*.-]+)`)
)

// redirectHost is a hostname from a pattern: scheme, path, port and a leading
// "*" or "*." removed, kept as "*.name" when it was a wildcard.
func redirectHost(text string) string {
	value := strings.TrimSpace(text)
	if _, after, ok := strings.Cut(value, "://"); ok {
		value = after
	}
	value, _, _ = strings.Cut(value, "/")
	value, _, _ = strings.Cut(value, ":")
	wildcard := strings.HasPrefix(value, "*.")
	value = strings.TrimLeft(strings.TrimLeft(value, "*"), ".")
	name := hostname(value)
	if name == "" || strings.Contains(name, "*") {
		return ""
	}
	if wildcard {
		return "*." + name
	}
	return name
}

func uniqueNames(values []string) []string {
	out := []string{}
	seen := map[string]bool{}
	for _, value := range values {
		if value != "" && !seen[value] {
			seen[value] = true
			out = append(out, value)
		}
	}
	return out
}

// expressionHosts is the hostnames a rule expression matches, where it names them.
func expressionHosts(expression string) []string {
	found := []string{}
	for _, match := range redirectHostField.FindAllStringSubmatch(expression, -1) {
		for _, name := range redirectQuoted.FindAllStringSubmatch(match[1], -1) {
			found = append(found, redirectHost(name[1]))
		}
	}
	for _, match := range redirectURL.FindAllStringSubmatch(expression, -1) {
		found = append(found, redirectHost(match[1]))
	}
	return uniqueNames(found)
}

// targetHost is the host a redirect target names: a URL's, or an expression's
// first URL literal.
func targetHost(target string) string {
	if strings.HasPrefix(target, "http://") || strings.HasPrefix(target, "https://") {
		parsed, err := url.Parse(target)
		if err != nil {
			return ""
		}
		return redirectHost(parsed.Hostname())
	}
	if match := redirectURL.FindStringSubmatch(target); match != nil {
		return redirectHost(match[1])
	}
	return ""
}

// ruleRecord is a Single Redirect rule as a record; false for any other action.
func ruleRecord(rule cfRulesetRule, zone string) (CloudflareRedirectRecord, bool, error) {
	if rule.Action != redirectAction {
		return CloudflareRedirectRecord{}, false, nil
	}
	parameters, err := cloudflareOptional[struct {
		FromValue *cfapi.RulesetsRedirectFromValue `json:"from_value"`
	}](rule.ActionParameters, nil)
	if err != nil {
		return CloudflareRedirectRecord{}, false, err
	}
	var from cfapi.RulesetsRedirectFromValue
	var status *int
	if parameters.FromValue != nil {
		from = *parameters.FromValue
		if from.StatusCode != 0 {
			code := int(from.StatusCode)
			status = &code
		}
	}
	target := from.TargetURL.Value
	if target == "" {
		target = from.TargetURL.Expression
	}
	description, preserve, enabled := rule.Description, from.PreserveQueryString, rule.Enabled == nil || *rule.Enabled
	return CloudflareRedirectRecord{
		Zone: zone, Source: redirectSourceRule, ID: rule.ID, Description: &description,
		Hostnames: expressionHosts(rule.Expression), Target: target, TargetHost: targetHost(target),
		StatusCode: status, PreserveQueryString: &preserve, Enabled: enabled,
	}, true, nil
}

// pageRuleRecord is a forwarding page rule as a record; false for any other.
func pageRuleRecord(rule cfPageRule, zone string) (CloudflareRedirectRecord, bool, error) {
	index := slices.IndexFunc(rule.Actions, func(action cfPageRuleAction) bool { return action.ID == pageRuleForwarding })
	if index < 0 {
		return CloudflareRedirectRecord{}, false, nil
	}
	forwarding, err := cloudflareOptional[cfForwardingURL](rule.Actions[index].Value, nil)
	if err != nil {
		return CloudflareRedirectRecord{}, false, err
	}
	hosts := []string{}
	for _, target := range rule.Targets {
		if target.Target == "url" {
			hosts = append(hosts, redirectHost(target.Constraint.Value))
		}
	}
	status := rule.Status
	if status == "" {
		status = pageRuleActive
	}
	return CloudflareRedirectRecord{
		Zone: zone, Source: redirectSourcePageRule, ID: rule.ID, Hostnames: uniqueNames(hosts),
		Target: forwarding.URL, TargetHost: targetHost(forwarding.URL), StatusCode: forwarding.StatusCode,
		Enabled: status == pageRuleActive,
	}, true, nil
}

// cloudflareRedirectRules is the redirect rules in the zone's dynamic redirect rulesets.
func (r *Registry) cloudflareRedirectRules(ctx context.Context, zoneID, zone, ref string) ([]CloudflareRedirectRecord, error) {
	items, err := r.cloudflareList(ctx, "cloudflare_api", "/zones/"+zoneID+"/rulesets", ref, cloudflareAccountPerPage)
	if err != nil {
		return nil, err
	}
	rulesets, err := cloudflareItems[cfRuleset](items, "ruleset")
	if err != nil {
		return nil, err
	}
	found := []CloudflareRedirectRecord{}
	for _, listed := range rulesets {
		if listed.Phase != redirectPhase {
			continue
		}
		ruleset, err := cloudflareOptional[cfRuleset](r.cloudflareAPIResult(ctx, "/zones/"+zoneID+"/rulesets/"+listed.ID, ref))
		if err != nil {
			return nil, err
		}
		for _, rule := range ruleset.Rules {
			record, ok, err := ruleRecord(rule, zone)
			if err != nil {
				return nil, err
			}
			if ok {
				found = append(found, record)
			}
		}
	}
	return found, nil
}

// cloudflarePageRedirects is the zone's forwarding page rules.
func (r *Registry) cloudflarePageRedirects(ctx context.Context, zoneID, zone, ref string) ([]CloudflareRedirectRecord, error) {
	rules, err := cloudflareOptional[[]cfPageRule](r.cloudflareAPIResult(ctx, "/zones/"+zoneID+"/pagerules", ref))
	if err != nil {
		return nil, err
	}
	found := []CloudflareRedirectRecord{}
	for _, rule := range rules {
		record, ok, err := pageRuleRecord(rule, zone)
		if err != nil {
			return nil, err
		}
		if ok {
			found = append(found, record)
		}
	}
	return found, nil
}

// cloudflareRedirects is every redirect on every zone each credential sees. A
// part refused on one zone is a refused part on that zone; every part refused
// on every zone is a refused read.
func (r *Registry) cloudflareRedirects(ctx context.Context) ([]any, error) {
	parts := []struct {
		name string
		read func(context.Context, string, string, string) ([]CloudflareRedirectRecord, error)
	}{{"rules", r.cloudflareRedirectRules}, {"page_rules", r.cloudflarePageRedirects}}
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		zones, err := r.cloudflareAPIZones(ctx, ref)
		if err != nil {
			return nil, err
		}
		named := namedZones(zones)
		var refused []error
		for _, zone := range named {
			name := hostname(zone.Name)
			for _, part := range parts {
				records, err := part.read(ctx, zone.ID, name, ref)
				if err != nil {
					refuse(ctx, part.name, ref, name, err)
					refused = append(refused, err)
					continue
				}
				for _, record := range records {
					record.ConnectionRef, record.AccountID = ref, zone.Account.ID
					found = append(found, record)
				}
			}
		}
		if len(named) > 0 && len(refused) == len(parts)*len(named) {
			return nil, fmt.Errorf("every zone refused its redirects: %w", refused[0])
		}
	}
	return found, nil
}
