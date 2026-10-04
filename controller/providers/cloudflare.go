package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The zone settings worth carrying: how a domain answers over TLS.
var zonePostureSettings = []string{"ssl", "min_tls_version", "tls_1_3", "always_use_https", "automatic_https_rewrites"}

var caaValueParts = regexp.MustCompile(`^\s*(\d{1,3})\s+(issue|issuewild|iodef)\s+"([^"]*)"\s*$`)

func (r *Registry) admitCloudflare() {
	act(r, runtime.ResourceKindCloudflareDNSRecord, "reconcile", r.cloudflareRecordReconcile)
	act(r, runtime.ResourceKindCloudflareDNSRecord, "delete", r.cloudflareRecordDelete)
	r.reader(runtime.ResourceKindCloudflareZone, r.cloudflareZoneInventory)
	r.reader(runtime.ResourceKindCloudflareDNSRecord, r.cloudflareRecordInventory)
	r.reader(runtime.ResourceKindCloudflarePagesProject, r.cloudflarePagesProjects)
	r.reader(runtime.ResourceKindCloudflareD1Database, r.cloudflareD1Databases)
	r.reader(runtime.ResourceKindCloudflareAccessApp, r.cloudflareAccessApps)
	r.reader(runtime.ResourceKindCloudflareAccessServiceToken, r.cloudflareServiceTokens)
	r.reader(runtime.ResourceKindCloudflareTunnel, r.cloudflareTunnels)
	r.reader(runtime.ResourceKindCloudflareEdgeCertificate, r.cloudflareEdgeCertificates)
	r.reader(runtime.ResourceKindCloudflareRedirect, r.cloudflareRedirects)
	r.probe("cloudflare_dns", r.cloudflareDNSProbe)
	r.probe("cloudflare_api", r.cloudflareAPIProbe)
}

func (r *Registry) cloudflareZones(ctx context.Context) ([]cfapi.ZonesZone, error) {
	raw, err := r.cached(ctx, "cloudflare-zones", func() (json.RawMessage, error) {
		items, err := r.cloudflarePaged(ctx, "/zones")
		if err != nil {
			return nil, err
		}
		return json.Marshal(items)
	})
	if err != nil {
		return nil, err
	}
	return decodeAs[[]cfapi.ZonesZone](raw, "Cloudflare returned an invalid zone list.")
}

// cloudflareZoneID resolves a zone name once per controller; ids do not change.
func (r *Registry) cloudflareZoneID(ctx context.Context, zone string) (string, error) {
	wanted := hostname(zone)
	r.snapshotMu.Lock()
	id, found := r.zoneIDs[wanted]
	r.snapshotMu.Unlock()
	if found {
		return id, nil
	}
	zones, err := r.cloudflareZones(ctx)
	if err != nil {
		return "", err
	}
	r.snapshotMu.Lock()
	defer r.snapshotMu.Unlock()
	for _, candidate := range zones {
		if name := strings.ToLower(strings.TrimSpace(candidate.Name)); name != "" {
			r.zoneIDs[name] = candidate.ID
		}
	}
	if id, found := r.zoneIDs[wanted]; found {
		return id, nil
	}
	return "", &ProviderError{Message: fmt.Sprintf("The Cloudflare credential cannot see a zone called %s.", pyRepr(wanted))}
}

func (r *Registry) cloudflareRecords(ctx context.Context, zoneID string) ([]cfRecordFields, error) {
	items, err := r.cloudflarePaged(ctx, "/zones/"+zoneID+"/dns_records")
	if err != nil {
		return nil, err
	}
	records := make([]cfRecordFields, 0, len(items))
	for _, item := range items {
		var record cfRecordFields
		if err := json.Unmarshal(item, &record); err != nil {
			return nil, &ProviderError{Message: "Cloudflare returned an invalid record list."}
		}
		records = append(records, record)
	}
	return records, nil
}

// caaParts splits a CAA value by the rule the spec validates with.
func caaParts(content string) (int, string, string, bool) {
	match := caaValueParts.FindStringSubmatch(content)
	if match == nil {
		return 0, "", "", false
	}
	flags, _ := strconv.Atoi(match[1])
	return flags, match[2], match[3], true
}

// normalizedRecordContent is one spelling of a value, so desired and observed
// compare: a TXT value comes back quoted, a hostname lowercased, a CAA value
// re-emitted with single spaces.
func normalizedRecordContent(recordType, content string) string {
	value := strings.TrimSpace(content)
	if recordType == "TXT" && !(strings.HasPrefix(value, `"`) && strings.HasSuffix(value, `"`)) {
		value = `"` + value + `"`
	}
	if recordType == "CNAME" || recordType == "MX" {
		value = hostname(value)
	}
	if recordType == "CAA" {
		if flags, tag, target, ok := caaParts(value); ok {
			value = fmt.Sprintf(`%d %s "%s"`, flags, tag, target)
		}
	}
	return value
}

type cfCAAData struct {
	Flags int    `json:"flags"`
	Tag   string `json:"tag"`
	Value string `json:"value"`
}

// cfRecordPayload is what a create or update sends. proxied goes only with the
// types that carry it: Cloudflare rejects it on a TXT or MX record.
type cfRecordPayload struct {
	Type     string     `json:"type"`
	Name     string     `json:"name"`
	TTL      int        `json:"ttl"`
	Data     *cfCAAData `json:"data,omitempty"`
	Content  *string    `json:"content,omitempty"`
	Priority *int       `json:"priority,omitempty"`
	Proxied  *bool      `json:"proxied,omitempty"`
}

func cloudflarePayload(spec CloudflareDNSRecordSpec) (cfRecordPayload, error) {
	recordType := strings.ToUpper(spec.RecordType)
	payload := cfRecordPayload{Type: recordType, Name: hostname(spec.Name), TTL: numberOr(spec.TTL, 1)}
	if recordType == "CAA" {
		flags, tag, value, ok := caaParts(spec.Content)
		if !ok {
			return payload, &ProviderError{Message: `A CAA value must look like: 0 issue "letsencrypt.org".`}
		}
		payload.Data = &cfCAAData{Flags: flags, Tag: tag, Value: value}
	} else {
		content := normalizedRecordContent(recordType, spec.Content)
		payload.Content = &content
	}
	if recordType == "MX" {
		priority := numberOr(spec.Priority, 0)
		payload.Priority = &priority
	}
	if recordType == "A" || recordType == "AAAA" || recordType == "CNAME" {
		proxied := spec.Proxied
		payload.Proxied = &proxied
	}
	return payload, nil
}

// numberOr is int(value or fallback).
func numberOr(value json.Number, fallback int) int {
	if f, err := value.Float64(); err == nil && f != 0 {
		return int(f)
	}
	return fallback
}

func rawNumberOr(raw json.RawMessage, fallback int) int {
	var n json.Number
	if !pyTruthy(raw) || json.Unmarshal(raw, &n) != nil {
		return fallback
	}
	return numberOr(n, fallback)
}

func recordMatches(live cfRecordFields, spec CloudflareDNSRecordSpec) bool {
	recordType := strings.ToUpper(spec.RecordType)
	return strings.ToUpper(live.Type) == recordType &&
		hostname(live.Name) == hostname(spec.Name) &&
		normalizedRecordContent(recordType, pyGetText(live.Content, "")) == normalizedRecordContent(recordType, spec.Content)
}

func recordStatus(zone string, live *cfRecordFields) CloudflareDNSRecordStatus {
	status := CloudflareDNSRecordStatus{Zone: zone, Content: "", Priority: json.RawMessage("null"), TTL: json.RawMessage("1")}
	if live == nil {
		return status
	}
	status.RecordID = live.ID
	status.Name = live.Name
	status.RecordType = strings.ToUpper(live.Type)
	status.Content = pyGetText(live.Content, "")
	if len(live.Priority) > 0 {
		status.Priority = live.Priority
	}
	status.Proxied = pyTruthy(live.Proxied)
	if len(live.TTL) > 0 {
		status.TTL = live.TTL
	}
	return status
}

// findRecord is identity: the recorded id first, then name/type/value. A zone
// apex commonly holds several records of one type.
func findRecord(records []cfRecordFields, observed CloudflareDNSRecordObserved, spec CloudflareDNSRecordSpec) *cfRecordFields {
	recordID := strings.TrimSpace(observed.RecordID)
	for i := range records {
		if recordID != "" && records[i].ID == recordID {
			return &records[i]
		}
	}
	for i := range records {
		if recordMatches(records[i], spec) {
			return &records[i]
		}
	}
	return nil
}

func (r *Registry) cloudflareRecordTarget(ctx context.Context, spec CloudflareDNSRecordSpec, observed CloudflareDNSRecordObserved) (string, string, *cfRecordFields, error) {
	zone := hostname(spec.Zone)
	zoneID, err := r.cloudflareZoneID(ctx, zone)
	if err != nil {
		return zone, "", nil, err
	}
	records, err := r.cloudflareRecords(ctx, zoneID)
	if err != nil {
		return zone, zoneID, nil, err
	}
	return zone, zoneID, findRecord(records, observed, spec), nil
}

func decodeRecord(raw json.RawMessage) *cfRecordFields {
	var record cfRecordFields
	if !pyTruthy(raw) || json.Unmarshal(raw, &record) != nil {
		return nil
	}
	return &record
}

// cloudflareRecordReconcile makes one public DNS record match its declaration.
func (r *Registry) cloudflareRecordReconcile(ctx context.Context, spec CloudflareDNSRecordSpec, observed CloudflareDNSRecordObserved, apply bool) (Result, error) {
	zone, zoneID, live, err := r.cloudflareRecordTarget(ctx, spec, observed)
	if err != nil {
		return Result{}, err
	}
	desired, err := cloudflarePayload(spec)
	if err != nil {
		return Result{}, err
	}
	if live == nil {
		if apply {
			created, err := r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records", "POST", desired)
			if err != nil {
				return Result{}, err
			}
			live = decodeRecord(created)
		}
		return result(true, recordStatus(zone, live), "Created", "DNS record was created.", "Public DNS record created."), nil
	}
	if recordCurrent(*live, desired) {
		return result(false, recordStatus(zone, live), "Reconciled", "DNS record is current.", "Public DNS record unchanged."), nil
	}
	if apply {
		updated, err := r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records/"+live.ID, "PUT", desired)
		if err != nil {
			return Result{}, err
		}
		live = decodeRecord(updated)
	}
	return result(true, recordStatus(zone, live), "Reconciled", "DNS record was updated.", "Public DNS record updated."), nil
}

// recordCurrent compares the live record in the shape of the desired payload.
func recordCurrent(live cfRecordFields, desired cfRecordPayload) bool {
	if strings.ToUpper(live.Type) != desired.Type || hostname(live.Name) != desired.Name || rawNumberOr(live.TTL, 1) != desired.TTL {
		return false
	}
	if desired.Data != nil {
		if live.Data == nil || !pyNumberEquals(live.Data.Flags, desired.Data.Flags) || !pyStringEquals(live.Data.Tag, desired.Data.Tag) || !pyStringEquals(live.Data.Value, desired.Data.Value) {
			return false
		}
	} else if normalizedRecordContent(desired.Type, pyGetText(live.Content, "")) != *desired.Content {
		return false
	}
	if desired.Priority != nil && rawNumberOr(live.Priority, 0) != *desired.Priority {
		return false
	}
	if desired.Proxied != nil && pyTruthy(live.Proxied) != *desired.Proxied {
		return false
	}
	return true
}

func pyNumberEquals(raw json.RawMessage, want int) bool {
	var n json.Number
	if json.Unmarshal(raw, &n) != nil {
		return false
	}
	f, err := n.Float64()
	return err == nil && f == float64(want)
}

func pyStringEquals(raw json.RawMessage, want string) bool {
	var s string
	return json.Unmarshal(raw, &s) == nil && s == want
}

// cloudflareRecordDelete removes only the record this declaration owns, by id.
func (r *Registry) cloudflareRecordDelete(ctx context.Context, spec CloudflareDNSRecordSpec, observed CloudflareDNSRecordObserved, apply bool) (Result, error) {
	zone, zoneID, live, err := r.cloudflareRecordTarget(ctx, spec, observed)
	if err != nil {
		return Result{}, err
	}
	status := CloudflareDNSDeleteStatus{Zone: zone, Name: spec.Name, Removed: true}
	if live == nil {
		return result(false, status, "Absent", "No such record in Cloudflare.", "Public DNS record was already absent."), nil
	}
	if apply {
		if _, err := r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records/"+live.ID, "DELETE", nil); err != nil {
			return Result{}, err
		}
	}
	return result(true, status, "Removed", "DNS record was removed.", "Public DNS record removed."), nil
}

// cloudflareRegistrarDomains is what the registrar holds for every domain on the
// account, by name. A refusal is the zone sweep's refused registration part.
func (r *Registry) cloudflareRegistrarDomains(ctx context.Context) map[string]CloudflareRegistration {
	account, err := r.cloudflareAnalyticsAccount(ctx, "")
	var items []json.RawMessage
	if err == nil {
		items, err = r.cloudflareAPICursorList(ctx, "/accounts/"+account+"/registrar/registrations", "", cloudflareAccountPerPage)
	}
	if err != nil {
		refuse(ctx, "registration", "", "", err)
		return map[string]CloudflareRegistration{}
	}
	found := map[string]CloudflareRegistration{}
	for _, item := range items {
		var domain cfapi.RegistrarAPIRegistration
		if json.Unmarshal(item, &domain) != nil {
			continue
		}
		name := hostname(domain.DomainName)
		if name == "" {
			continue
		}
		expires := domain.ExpiresAt
		expires = runtime.ISODate(expires)
		found[name] = CloudflareRegistration{ExpiresAt: expires, AutoRenew: domain.AutoRenew, Locked: domain.Locked, Status: string(domain.Status), Registrar: "Cloudflare", known: true}
	}
	return found
}

// cloudflareZonePosture reads how a zone answers over TLS through the account
// credential; the DNS token cannot. A refused setting is the zone's refused
// posture part, and the zone still reports.
func (r *Registry) cloudflareZonePosture(ctx context.Context, zoneID, zone string) map[string]string {
	found := map[string]string{}
	if zoneID == "" {
		return found
	}
	for _, setting := range zonePostureSettings {
		envelope, err := r.cloudflareAPIRequest(ctx, "/zones/"+zoneID+"/settings/"+setting, "")
		if err != nil {
			refuse(ctx, "posture", "", zone, err)
			return map[string]string{}
		}
		var item cfSettingValue
		if !isJSONObject(envelope.Result) || json.Unmarshal(envelope.Result, &item) != nil {
			continue
		}
		if len(item.Value) > 0 && string(item.Value) != "null" && string(item.Value) != `""` {
			found[setting] = rawText(item.Value)
		}
	}
	return found
}

// cloudflareZoneInventory reports every zone the credential can see, declared
// or not: which of them HQ manages is an operator's decision.
func (r *Registry) cloudflareZoneInventory(ctx context.Context) ([]any, error) {
	prefix, err := r.Env.Prefix("cloudflare_dns", "")
	if err != nil {
		return nil, err
	}
	connectionRef, err := r.Env.Required(prefix, "CONNECTION_REF")
	if err != nil {
		return nil, err
	}
	registrars := r.cloudflareRegistrarDomains(ctx)
	zones, err := r.cloudflareZones(ctx)
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, zone := range zones {
		if zone.Name == "" {
			continue
		}
		name := hostname(zone.Name)
		record := CloudflareZoneRecord{
			Zone: zone.Name, ConnectionRef: connectionRef, AccountID: zone.Account.ID,
			Status: string(zone.Status), Plan: zone.Plan.Name,
			Posture: r.cloudflareZonePosture(ctx, zone.ID, name),
		}
		registration := registrars[name]
		record.Registration = &registration
		found = append(found, record)
	}
	return found, nil
}

func (r *Registry) cloudflareRecordInventory(ctx context.Context) ([]any, error) {
	zones, err := r.cloudflareZones(ctx)
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, zone := range zones {
		if zone.Name == "" {
			continue
		}
		records, err := r.cloudflareRecords(ctx, zone.ID)
		if err != nil {
			return nil, err
		}
		for i := range records {
			if records[i].Type == "" || records[i].Name == "" {
				continue
			}
			found = append(found, recordStatus(zone.Name, &records[i]))
		}
	}
	return found, nil
}

// A probe answers whether the credential still works and what it reaches.

func (r *Registry) cloudflareDNSProbe(ctx context.Context, ref string) (ProbeResult, error) {
	verification, err := r.cloudflareEnvelope(ctx, "cloudflare_dns", ref, "/user/tokens/verify", "GET", nil)
	if err != nil {
		return ProbeResult{}, err
	}
	items, err := r.cloudflareList(ctx, "cloudflare_dns", "/zones", ref, cloudflarePerPage)
	if err != nil {
		return ProbeResult{}, err
	}
	names := []string{}
	for _, item := range items {
		var zone cfapi.ZonesZone
		if json.Unmarshal(item, &zone) == nil && zone.Name != "" {
			names = append(names, zone.Name)
		}
	}
	sort.Strings(names)
	expires := tokenExpiry(verification)
	return ProbeResult{Detail: fmt.Sprintf("%d zones.", len(names)), Reaches: names, ExpiresAt: &expires}, nil
}

// cloudflareAPIProbe reports the analytics sites the account credential
// observes: both credentials see the same zones, so the sites tell them apart.
func (r *Registry) cloudflareAPIProbe(ctx context.Context, ref string) (ProbeResult, error) {
	verification, err := r.cloudflareAPIRequest(ctx, "/user/tokens/verify", ref)
	if err != nil {
		return ProbeResult{}, err
	}
	account, err := r.cloudflareAnalyticsAccount(ctx, ref)
	if err != nil {
		return ProbeResult{}, err
	}
	sites, err := r.cloudflareAccountSites(ctx, account, ref)
	if err != nil {
		return ProbeResult{}, err
	}
	hosts := make([]string, 0, len(sites))
	for _, site := range sites {
		hosts = append(hosts, site.Host)
	}
	sort.Strings(hosts)
	measured := "sites"
	if len(hosts) == 1 {
		measured = "site"
	}
	expires := tokenExpiry(verification)
	return ProbeResult{Detail: fmt.Sprintf("%d analytics %s.", len(hosts), measured), Reaches: hosts, ExpiresAt: &expires}, nil
}

func tokenExpiry(verification cfEnvelope) string {
	var result struct {
		ExpiresOn json.RawMessage `json:"expires_on"`
	}
	if !isJSONObject(verification.Result) || json.Unmarshal(verification.Result, &result) != nil {
		return ""
	}
	return pyOrText(result.ExpiresOn)
}
