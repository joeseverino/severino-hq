package providers

import "encoding/json"

// Cloudflare kind payloads: spec and observed arrive opaque in the bridge
// contract; statuses and records are what HQ stores for the kind.

type CloudflareDNSRecordSpec struct {
	Zone       string      `json:"zone"`
	Name       string      `json:"name"`
	RecordType string      `json:"record_type"`
	Content    string      `json:"content"`
	TTL        json.Number `json:"ttl"`
	Priority   json.Number `json:"priority"`
	Proxied    bool        `json:"proxied"`
}

type CloudflareDNSRecordObserved struct {
	RecordID string `json:"record_id"`
}

// CloudflareDNSRecordStatus carries the live record. Priority and TTL are the
// values Cloudflare returned: absent priority is null, as HQ stores it.
type CloudflareDNSRecordStatus struct {
	Zone       string          `json:"zone"`
	RecordID   string          `json:"record_id"`
	Name       string          `json:"name"`
	RecordType string          `json:"record_type"`
	Content    string          `json:"content"`
	Priority   json.RawMessage `json:"priority"`
	Proxied    bool            `json:"proxied"`
	TTL        json.RawMessage `json:"ttl"`
}

type CloudflareDNSDeleteStatus struct {
	Zone    string `json:"zone"`
	Name    string `json:"name"`
	Removed bool   `json:"removed"`
}

type CloudflareZoneRecord struct {
	Zone          string                  `json:"zone"`
	ConnectionRef string                  `json:"connection_ref"`
	AccountID     string                  `json:"account_id"`
	Status        string                  `json:"status"`
	Plan          string                  `json:"plan"`
	Posture       map[string]string       `json:"posture"`
	Registration  *CloudflareRegistration `json:"registration"`
}

// CloudflareRegistration marshals as {} when the registrar holds nothing.
type CloudflareRegistration struct {
	ExpiresAt string `json:"expires_at,omitempty"`
	AutoRenew bool   `json:"auto_renew,omitempty"`
	Locked    bool   `json:"locked,omitempty"`
	Status    string `json:"status,omitempty"`
	Registrar string `json:"registrar,omitempty"`
	known     bool
}

func (c CloudflareRegistration) MarshalJSON() ([]byte, error) {
	if !c.known {
		return []byte("{}"), nil
	}
	return json.Marshal(struct {
		ExpiresAt string `json:"expires_at"`
		AutoRenew bool   `json:"auto_renew"`
		Locked    bool   `json:"locked"`
		Status    string `json:"status"`
		Registrar string `json:"registrar"`
	}{c.ExpiresAt, c.AutoRenew, c.Locked, c.Status, c.Registrar})
}

type CloudflarePagesProjectRecord struct {
	ConnectionRef       string   `json:"connection_ref"`
	AccountID           string   `json:"account_id"`
	Name                string   `json:"name"`
	Subdomain           string   `json:"subdomain"`
	Domains             []string `json:"domains"`
	ProductionBranch    string   `json:"production_branch"`
	DeploymentID        string   `json:"deployment_id"`
	DeploymentCommit    string   `json:"deployment_commit"`
	DeploymentCreatedOn string   `json:"deployment_created_on"`
}

type CloudflareD1DatabaseRecord struct {
	ConnectionRef string       `json:"connection_ref"`
	AccountID     string       `json:"account_id"`
	Name          string       `json:"name"`
	UUID          string       `json:"uuid"`
	CreatedAt     string       `json:"created_at"`
	Version       string       `json:"version"`
	FileSize      *json.Number `json:"file_size,omitempty"`
}

type CloudflareNamedRef struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type CloudflareAccessAppRecord struct {
	ConnectionRef   string               `json:"connection_ref"`
	AccountID       string               `json:"account_id"`
	ID              string               `json:"id"`
	Name            string               `json:"name"`
	Type            string               `json:"type"`
	Domain          string               `json:"domain"`
	Destinations    []string             `json:"destinations"`
	SessionDuration string               `json:"session_duration"`
	Policies        []CloudflareNamedRef `json:"policies"`
}

type CloudflareServiceTokenRecord struct {
	ConnectionRef string                `json:"connection_ref"`
	ID            string                `json:"id"`
	Name          string                `json:"name"`
	ExpiresAt     string                `json:"expires_at"`
	CreatedAt     string                `json:"created_at"`
	Apps          *[]CloudflareNamedRef `json:"apps,omitempty"` // absent when the apps were refused
}

type CloudflareTunnelIngress struct {
	Hostname string `json:"hostname"`
	Service  string `json:"service"`
}

type CloudflareTunnelConnection struct {
	Version  string `json:"version"`
	Colo     string `json:"colo"`
	OriginIP string `json:"origin_ip"`
}

type CloudflareTunnelRecord struct {
	ConnectionRef string                        `json:"connection_ref"`
	AccountID     string                        `json:"account_id"`
	ID            string                        `json:"id"`
	Name          string                        `json:"name"`
	Status        string                        `json:"status"`
	CreatedAt     string                        `json:"created_at"`
	ConnsActiveAt string                        `json:"conns_active_at"`
	ConfigSource  *string                       `json:"config_source,omitempty"`
	Ingress       *[]CloudflareTunnelIngress    `json:"ingress,omitempty"`
	Connections   *[]CloudflareTunnelConnection `json:"connections,omitempty"`
}

type CloudflareEdgeCertificateRecord struct {
	ConnectionRef        string   `json:"connection_ref"`
	AccountID            string   `json:"account_id"`
	Zone                 string   `json:"zone"`
	ID                   string   `json:"id"`
	Type                 string   `json:"type"`
	Hosts                []string `json:"hosts"`
	Status               string   `json:"status"`
	CertificateAuthority string   `json:"certificate_authority"`
	ExpiresOn            string   `json:"expires_on"`
}

type CloudflareRedirectRecord struct {
	ConnectionRef       string          `json:"connection_ref"`
	AccountID           string          `json:"account_id"`
	Zone                string          `json:"zone"`
	Source              string          `json:"source"`
	ID                  string          `json:"id"`
	Description         *string         `json:"description,omitempty"` // rules only
	Hostnames           []string        `json:"hostnames"`
	Target              string          `json:"target"`
	TargetHost          string          `json:"target_host"`
	StatusCode          json.RawMessage `json:"status_code"`
	PreserveQueryString *bool           `json:"preserve_query_string,omitempty"` // rules only
	Enabled             bool            `json:"enabled"`
}

type CloudflareAnalyticsSite struct {
	SiteTag       string `json:"site_tag"`
	Host          string `json:"host"`
	Account       string `json:"account,omitempty"`
	ConnectionRef string `json:"connection_ref,omitempty"`
}

// Presence-aware views of Cloudflare answers the generated cfapi types cannot
// carry faithfully. Each names why.

// cfRecordFields: the spec models a DNS record as a 20-way oneOf whose
// priority and ttl are plain numbers, so an absent priority would read as 0;
// HQ stores the value Cloudflare returned, null included.
type cfRecordFields struct {
	ID       string          `json:"id"`
	Type     string          `json:"type"`
	Name     string          `json:"name"`
	Content  json.RawMessage `json:"content"`
	Priority json.RawMessage `json:"priority"`
	TTL      json.RawMessage `json:"ttl"`
	Proxied  json.RawMessage `json:"proxied"`
	Data     *struct {
		Flags json.RawMessage `json:"flags"`
		Tag   json.RawMessage `json:"tag"`
		Value json.RawMessage `json:"value"`
	} `json:"data"`
}

// cfSettingValue: a zone setting's value is a 60-way oneOf of strings,
// numbers, booleans and objects, carried as the text Python's str() gives it.
type cfSettingValue struct {
	Value json.RawMessage `json:"value"`
}

// cfAccessApp: the spec's application list is 11 anonymous oneOf variants with
// no discriminator; these are the fields every variant shares.
type cfAccessApp struct {
	ID              json.RawMessage `json:"id"`
	Name            json.RawMessage `json:"name"`
	Type            json.RawMessage `json:"type"`
	Domain          json.RawMessage `json:"domain"`
	SessionDuration json.RawMessage `json:"session_duration"`
	Destinations    json.RawMessage `json:"destinations"`
	Policies        json.RawMessage `json:"policies"`
}

// cfRule: ruleset rules are a oneOf over every action, and the redirect
// variant's enabled is a plain bool, but an absent enabled means enabled.
type cfRule struct {
	ID               json.RawMessage `json:"id"`
	Action           json.RawMessage `json:"action"`
	Description      json.RawMessage `json:"description"`
	Expression       json.RawMessage `json:"expression"`
	Enabled          json.RawMessage `json:"enabled"`
	ActionParameters json.RawMessage `json:"action_parameters"`
}

// cfPageRule: page rule actions are a oneOf of about thirty settings with no
// accessor for the id that tells them apart.
type cfPageRule struct {
	ID      json.RawMessage `json:"id"`
	Status  json.RawMessage `json:"status"`
	Actions json.RawMessage `json:"actions"`
	Targets json.RawMessage `json:"targets"`
}
