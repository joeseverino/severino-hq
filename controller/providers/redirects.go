package providers

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"regexp"
	"strings"
)

// Cloudflare redirects as records: which names a rule matches and where it sends
// them. A rule's names come from its expression (http.host comparisons and URL
// literals); a page rule's from its URL pattern. A target is a static URL or an
// expression; its host is the first URL literal it names.

// RedirectPhase is the dynamic redirect phase: zone Single Redirects.
const RedirectPhase = "http_request_dynamic_redirect"

var (
	redirectHostField = regexp.MustCompile(`http\.host\s*(?:eq|==|in|contains|wildcard|strict\s+wildcard)\s*(\{[^}]*\}|r?"[^"]*")`)
	redirectQuoted    = regexp.MustCompile(`"([^"]*)"`)
	redirectURL       = regexp.MustCompile(`https?://([A-Za-z0-9*.-]+)`)
)

// ZoneReads is the Cloudflare calls a redirect read makes, supplied by the caller.
type ZoneReads interface {
	Zones(ctx context.Context, ref string) ([]RedirectZone, error)
	Listed(ctx context.Context, path, ref string) ([]json.RawMessage, error)
	Result(ctx context.Context, path, ref string) (json.RawMessage, error)
	// Reason is what an error that is not a provider refusal says.
	Reason(err error) string
	// Refuse reports one declared part refused on a zone.
	Refuse(ctx context.Context, part string, err error, scope, ref string)
}

type RedirectZone struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Account struct {
		ID string `json:"id"`
	} `json:"account"`
}

// RedirectRuleRecord is one Single Redirect rule.
type RedirectRuleRecord struct {
	ConnectionRef       string          `json:"connection_ref"`
	AccountID           string          `json:"account_id"`
	Zone                string          `json:"zone"`
	Source              string          `json:"source"`
	ID                  string          `json:"id"`
	Description         string          `json:"description"`
	Hostnames           []string        `json:"hostnames"`
	Target              string          `json:"target"`
	TargetHost          string          `json:"target_host"`
	StatusCode          json.RawMessage `json:"status_code"`
	PreserveQueryString bool            `json:"preserve_query_string"`
	Enabled             bool            `json:"enabled"`
}

// RedirectPageRuleRecord is one forwarding page rule.
type RedirectPageRuleRecord struct {
	ConnectionRef string          `json:"connection_ref"`
	AccountID     string          `json:"account_id"`
	Zone          string          `json:"zone"`
	Source        string          `json:"source"`
	ID            string          `json:"id"`
	Hostnames     []string        `json:"hostnames"`
	Target        string          `json:"target"`
	TargetHost    string          `json:"target_host"`
	StatusCode    json.RawMessage `json:"status_code"`
	Enabled       bool            `json:"enabled"`
}

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
	name := normalizedHostname(value)
	if name == "" || strings.Contains(name, "*") {
		return ""
	}
	if wildcard {
		return "*." + name
	}
	return name
}

// normalizedHostname is control_plane.names.normalized_hostname.
func normalizedHostname(name string) string {
	return strings.TrimRight(strings.ToLower(strings.TrimSpace(name)), ".")
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

func rawOrNull(value *pyValue) json.RawMessage {
	if value == nil {
		return json.RawMessage("null")
	}
	var out strings.Builder
	value.write(&out, 0, false, 0)
	return json.RawMessage(out.String())
}

// strField is str(d.get(key).opt() or "").
func strField(v *pyValue, key string) string {
	if v == nil {
		return ""
	}
	field := v.get(key).opt()
	if field == nil || !field.truthy() {
		return ""
	}
	return field.str()
}

func objectField(v *pyValue, key string) *pyValue {
	if v == nil {
		return nil
	}
	field := v.get(key).opt()
	if field == nil || !field.truthy() {
		return nil
	}
	return field
}

func ruleRecord(rule pyValue, zone string) (RedirectRuleRecord, bool) {
	if action := rule.get("action").opt(); action == nil || action.text == nil || *action.text != "redirect" {
		return RedirectRuleRecord{}, false
	}
	parameters := objectField(objectField(&rule, "action_parameters"), "from_value")
	targetURL := objectField(parameters, "target_url")
	target := strField(targetURL, "value")
	if target == "" {
		target = strField(targetURL, "expression")
	}
	var status *pyValue
	if parameters != nil {
		status = parameters.get("status_code").opt()
	}
	preserve := false
	if parameters != nil {
		if field := parameters.get("preserve_query_string").opt(); field != nil {
			preserve = field.truthy()
		}
	}
	enabled := true
	if field := rule.get("enabled").opt(); field != nil {
		enabled = field.truthy()
	}
	return RedirectRuleRecord{
		Zone: zone, Source: "rule",
		ID:                  strField(&rule, "id"),
		Description:         strField(&rule, "description"),
		Hostnames:           expressionHosts(strField(&rule, "expression")),
		Target:              target,
		TargetHost:          targetHost(target),
		StatusCode:          rawOrNull(status),
		PreserveQueryString: preserve,
		Enabled:             enabled,
	}, true
}

func pageRuleRecord(rule pyValue, zone string) (RedirectPageRuleRecord, bool) {
	var forwarding *pyValue
	if actions := rule.get("actions").opt(); actions != nil && actions.truthy() {
		for i := range actions.array {
			action := &actions.array[i]
			if id := action.get("id").opt(); id != nil && id.text != nil && *id.text == "forwarding_url" {
				if value := objectField(action, "value"); value != nil {
					forwarding = value
				} else {
					forwarding = &pyValue{object: true}
				}
				break
			}
		}
	}
	if forwarding == nil {
		return RedirectPageRuleRecord{}, false
	}
	patterns := []string{}
	if targets := rule.get("targets").opt(); targets != nil && targets.truthy() {
		for i := range targets.array {
			target := &targets.array[i]
			if kind := target.get("target").opt(); kind != nil && kind.text != nil && *kind.text == "url" {
				patterns = append(patterns, strField(objectField(target, "constraint"), "value"))
			}
		}
	}
	hosts := []string{}
	for _, pattern := range patterns {
		hosts = append(hosts, redirectHost(pattern))
	}
	target := strField(forwarding, "url")
	status := "active"
	if field := rule.get("status").opt(); field != nil && field.truthy() {
		status = field.str()
	}
	return RedirectPageRuleRecord{
		Zone: zone, Source: "page_rule",
		ID:         strField(&rule, "id"),
		Hostnames:  uniqueNames(hosts),
		Target:     target,
		TargetHost: targetHost(target),
		StatusCode: rawOrNull(forwarding.get("status_code").opt()),
		Enabled:    status == "active",
	}, true
}

// redirectRules is the redirect rules in the zone's dynamic redirect rulesets.
func redirectRules(ctx context.Context, api ZoneReads, zoneID, zone, ref string) ([]any, error) {
	found := []any{}
	rulesets, err := api.Listed(ctx, "/zones/"+zoneID+"/rulesets", ref)
	if err != nil {
		return nil, err
	}
	for _, raw := range rulesets {
		ruleset, err := parsePy(raw)
		if err != nil {
			return nil, err
		}
		if phase := ruleset.get("phase").opt(); phase == nil || phase.text == nil || *phase.text != RedirectPhase {
			continue
		}
		id := ""
		if field := ruleset.get("id").opt(); field != nil {
			id = field.str()
		}
		detailRaw, err := api.Result(ctx, "/zones/"+zoneID+"/rulesets/"+id, ref)
		if err != nil {
			return nil, err
		}
		detail, err := parsePy(orEmpty(detailRaw))
		if err != nil {
			return nil, err
		}
		rules := objectField(&detail, "rules")
		if rules == nil {
			continue
		}
		for _, rule := range rules.array {
			if record, ok := ruleRecord(rule, zone); ok {
				found = append(found, record)
			}
		}
	}
	return found, nil
}

func orEmpty(raw json.RawMessage) []byte {
	if len(raw) == 0 {
		return []byte("null")
	}
	return raw
}

// redirectPageRules is the zone's forwarding page rules.
func redirectPageRules(ctx context.Context, api ZoneReads, zoneID, zone, ref string) ([]any, error) {
	raw, err := api.Result(ctx, "/zones/"+zoneID+"/pagerules", ref)
	if err != nil {
		return nil, err
	}
	rules, err := parsePy(orEmpty(raw))
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, rule := range rules.array {
		if !rule.object {
			continue
		}
		if record, ok := pageRuleRecord(rule, zone); ok {
			found = append(found, record)
		}
	}
	return found, nil
}

type redirectPart struct {
	name string
	read func(context.Context, ZoneReads, string, string, string) ([]any, error)
}

// redirectParts is the reading's declared parts (cloudflare.redirect).
var redirectParts = []redirectPart{{"rules", redirectRules}, {"page_rules", redirectPageRules}}

// ReadRedirects is every redirect on every zone each credential sees. A part
// refused on one zone is reported through Refuse on that zone; every part
// refused on every zone is a refused read.
func ReadRedirects(ctx context.Context, refs []string, api ZoneReads) ([]any, error) {
	found := []any{}
	for _, ref := range refs {
		listed, err := api.Zones(ctx, ref)
		if err != nil {
			return nil, err
		}
		zones := []RedirectZone{}
		for _, zone := range listed {
			if zone.Name != "" {
				zones = append(zones, zone)
			}
		}
		refused := []error{}
		for _, zone := range zones {
			name := normalizedHostname(zone.Name)
			for _, part := range redirectParts {
				records, err := part.read(ctx, api, zone.ID, name, ref)
				if err != nil {
					api.Refuse(ctx, part.name, err, name, ref)
					var provider *ProviderError
					if !errors.As(err, &provider) {
						err = &ProviderError{Message: api.Reason(err)}
					}
					refused = append(refused, err)
					continue
				}
				for _, record := range records {
					switch r := record.(type) {
					case RedirectRuleRecord:
						r.ConnectionRef, r.AccountID = ref, zone.Account.ID
						found = append(found, r)
					case RedirectPageRuleRecord:
						r.ConnectionRef, r.AccountID = ref, zone.Account.ID
						found = append(found, r)
					}
				}
			}
		}
		if len(zones) > 0 && len(refused) == len(redirectParts)*len(zones) {
			return nil, refused[0]
		}
	}
	return found, nil
}
