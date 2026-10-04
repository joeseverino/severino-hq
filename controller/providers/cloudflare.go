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

// cloudflareAutoTTL is the TTL Cloudflare reads as automatic.
const cloudflareAutoTTL = 1

func (r *Registry) cloudflareZones(ctx context.Context) ([]cfapi.ZonesZone, error) {
	items, err := r.cloudflareCachedList(ctx, "cloudflare-zones", func() ([]json.RawMessage, error) {
		return r.cloudflareList(ctx, "cloudflare_dns", "/zones", "", cloudflarePerPage)
	})
	if err != nil {
		return nil, err
	}
	return cloudflareItems[cfapi.ZonesZone](items, "zone")
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
	return "", &ProviderError{Message: fmt.Sprintf("the cloudflare credential cannot see zone %q", wanted)}
}

func (r *Registry) cloudflareRecords(ctx context.Context, zoneID string) ([]cfRecord, error) {
	items, err := r.cloudflareList(ctx, "cloudflare_dns", "/zones/"+zoneID+"/dns_records", "", cloudflarePerPage)
	if err != nil {
		return nil, err
	}
	return cloudflareItems[cfRecord](items, "DNS record")
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
	ttl := spec.TTL
	if ttl == 0 {
		ttl = cloudflareAutoTTL
	}
	payload := cfRecordPayload{Type: recordType, Name: hostname(spec.Name), TTL: ttl}
	if recordType == "CAA" {
		flags, tag, value, ok := caaParts(spec.Content)
		if !ok {
			return payload, &ProviderError{Message: `a CAA value must look like: 0 issue "letsencrypt.org"`}
		}
		payload.Data = &cfCAAData{Flags: flags, Tag: tag, Value: value}
	} else {
		content := normalizedRecordContent(recordType, spec.Content)
		payload.Content = &content
	}
	if recordType == "MX" {
		if spec.Priority == nil {
			return payload, &ProviderError{Message: "an MX record needs a priority"}
		}
		payload.Priority = spec.Priority
	}
	if recordType == "A" || recordType == "AAAA" || recordType == "CNAME" {
		proxied := spec.Proxied
		payload.Proxied = &proxied
	}
	return payload, nil
}

func recordMatches(live cfRecord, spec CloudflareDNSRecordSpec) bool {
	recordType := strings.ToUpper(spec.RecordType)
	return strings.ToUpper(live.Type) == recordType &&
		hostname(live.Name) == hostname(spec.Name) &&
		normalizedRecordContent(recordType, live.Content) == normalizedRecordContent(recordType, spec.Content)
}

func recordStatus(zone string, live *cfRecord) CloudflareDNSRecordStatus {
	if live == nil {
		return CloudflareDNSRecordStatus{Zone: zone, TTL: cloudflareAutoTTL}
	}
	return CloudflareDNSRecordStatus{
		Zone: zone, RecordID: live.ID, Name: live.Name, RecordType: strings.ToUpper(live.Type),
		Content: live.Content, Priority: live.Priority, Proxied: live.Proxied, TTL: live.TTL,
	}
}

// findRecord is identity: the recorded id first, then name/type/value. A zone
// apex commonly holds several records of one type.
func findRecord(records []cfRecord, observed CloudflareDNSRecordObserved, spec CloudflareDNSRecordSpec) *cfRecord {
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

func (r *Registry) cloudflareRecordTarget(ctx context.Context, spec CloudflareDNSRecordSpec, observed CloudflareDNSRecordObserved) (string, string, *cfRecord, error) {
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

// writtenRecord is the record a create or update answered with.
func writtenRecord(raw json.RawMessage, err error) (*cfRecord, error) {
	if err != nil {
		return nil, err
	}
	record, err := cloudflareDecode[cfRecord](raw, "DNS record")
	if err != nil {
		return nil, err
	}
	return &record, nil
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
			if live, err = writtenRecord(r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records", "POST", desired)); err != nil {
				return Result{}, err
			}
		}
		return result(true, recordStatus(zone, live), "Created", "DNS record was created.", "Public DNS record created."), nil
	}
	if recordCurrent(*live, desired) {
		return result(false, recordStatus(zone, live), "Reconciled", "DNS record is current.", "Public DNS record unchanged."), nil
	}
	if apply {
		if live, err = writtenRecord(r.cloudflareRequest(ctx, "/zones/"+zoneID+"/dns_records/"+live.ID, "PUT", desired)); err != nil {
			return Result{}, err
		}
	}
	return result(true, recordStatus(zone, live), "Reconciled", "DNS record was updated.", "Public DNS record updated."), nil
}

// recordCurrent compares the live record in the shape of the desired payload.
func recordCurrent(live cfRecord, desired cfRecordPayload) bool {
	if strings.ToUpper(live.Type) != desired.Type || hostname(live.Name) != desired.Name || live.TTL != desired.TTL {
		return false
	}
	if desired.Data != nil {
		if live.Data == nil || *live.Data != *desired.Data {
			return false
		}
	} else if normalizedRecordContent(desired.Type, live.Content) != *desired.Content {
		return false
	}
	if desired.Priority != nil && (live.Priority == nil || *live.Priority != *desired.Priority) {
		return false
	}
	return desired.Proxied == nil || live.Proxied == *desired.Proxied
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
	domains, err := r.cloudflareRegistrations(ctx)
	if err != nil {
		refuse(ctx, "registration", "", "", err)
		return map[string]CloudflareRegistration{}
	}
	found := map[string]CloudflareRegistration{}
	for _, domain := range domains {
		if name := hostname(domain.DomainName); name != "" {
			found[name] = CloudflareRegistration{
				ExpiresAt: runtime.ISODate(domain.ExpiresAt), AutoRenew: domain.AutoRenew, Locked: domain.Locked,
				Status: string(domain.Status), Registrar: "Cloudflare", known: true,
			}
		}
	}
	return found
}

func (r *Registry) cloudflareRegistrations(ctx context.Context) ([]cfapi.RegistrarAPIRegistration, error) {
	account, err := r.cloudflareAnalyticsAccount(ctx, "")
	if err != nil {
		return nil, err
	}
	items, err := r.cloudflareAPICursorList(ctx, "/accounts/"+account+"/registrar/registrations", "", cloudflareAccountPerPage)
	if err != nil {
		return nil, err
	}
	return cloudflareItems[cfapi.RegistrarAPIRegistration](items, "registration")
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
		raw, err := r.cloudflareAPIResult(ctx, "/zones/"+zoneID+"/settings/"+setting, "")
		var item cfStringSetting
		if err == nil && present(raw) {
			item, err = cloudflareDecode[cfStringSetting](raw, setting+" setting")
		}
		if err != nil {
			refuse(ctx, "posture", "", zone, err)
			return map[string]string{}
		}
		if item.Value != "" {
			found[setting] = item.Value
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
	zones, err := cloudflareItems[cfapi.ZonesZone](items, "zone")
	if err != nil {
		return ProbeResult{}, err
	}
	names := []string{}
	for _, zone := range namedZones(zones) {
		names = append(names, zone.Name)
	}
	sort.Strings(names)
	expires, err := tokenExpiry(verification)
	if err != nil {
		return ProbeResult{}, err
	}
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
	expires, err := tokenExpiry(verification)
	if err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: fmt.Sprintf("%d analytics %s.", len(hosts), measured), Reaches: hosts, ExpiresAt: &expires}, nil
}

// tokenExpiry is when a verified credential expires; empty when it does not.
func tokenExpiry(verification cfEnvelope) (string, error) {
	if !present(verification.Result) {
		return "", nil
	}
	result, err := cloudflareDecode[struct {
		ExpiresOn string `json:"expires_on"`
	}](verification.Result, "token verification")
	return result.ExpiresOn, err
}
