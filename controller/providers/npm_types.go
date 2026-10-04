package providers

import (
	"encoding/json"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
)

// What HQ declares for a proxy host, what it last observed, and what an action reports.

type NPMProxyHostSpec struct {
	ConnectionRef       string   `json:"connection_ref"`
	DomainNames         []string `json:"domain_names"`
	ForwardScheme       string   `json:"forward_scheme"`
	ForwardHost         string   `json:"forward_host"`
	ForwardPort         int      `json:"forward_port"`
	ForceSSL            bool     `json:"force_ssl"`
	HTTP2               bool     `json:"http2"`
	Websocket           bool     `json:"websocket"`
	CachingEnabled      bool     `json:"caching_enabled"`
	BlockExploits       bool     `json:"block_exploits"`
	AccessListID        int      `json:"access_list_id"`
	CertificateID       int      `json:"certificate_id"`
	AdvancedConfig      string   `json:"advanced_config"`
	HSTSEnabled         bool     `json:"hsts_enabled"`
	HSTSSubdomains      bool     `json:"hsts_subdomains"`
	TrustForwardedProto bool     `json:"trust_forwarded_proto"`
	Serving             bool     `json:"serving"`
}

type NPMProxyHostObserved struct {
	ID          int      `json:"id,omitempty"`
	DomainNames []string `json:"domain_names,omitempty"`
}

type NPMProxyHostStatus struct {
	DomainNames []string `json:"domain_names"`
	Forward     string   `json:"forward"`
}

type NPMProxyHostDeleteStatus struct {
	DomainNames []string `json:"domain_names"`
	Removed     bool     `json:"removed"`
}

// npmProxyHostRequest is the body NPM takes to create or update a proxy host.
// Locations and Meta are NPM's own, carried forward on update; HQ never
// decides them.
type npmProxyHostRequest struct {
	DomainNames           []string        `json:"domain_names"`
	CertificateID         int             `json:"certificate_id"`
	Locations             json.RawMessage `json:"locations"`
	Meta                  json.RawMessage `json:"meta"`
	ForwardScheme         string          `json:"forward_scheme"`
	ForwardHost           string          `json:"forward_host"`
	ForwardPort           int             `json:"forward_port"`
	CachingEnabled        bool            `json:"caching_enabled"`
	BlockExploits         bool            `json:"block_exploits"`
	AccessListID          int             `json:"access_list_id"`
	HSTSEnabled           bool            `json:"hsts_enabled"`
	HSTSSubdomains        bool            `json:"hsts_subdomains"`
	TrustForwardedProto   bool            `json:"trust_forwarded_proto"`
	AdvancedConfig        string          `json:"advanced_config"`
	AllowWebsocketUpgrade bool            `json:"allow_websocket_upgrade"`
	SSLForced             bool            `json:"ssl_forced"`
	HTTP2Support          bool            `json:"http2_support"`
	Enabled               bool            `json:"enabled"`
}

// npmAccessList is an access list read with expand=items,clients.
type npmAccessList struct {
	npmapi.AccessListObject
	Items   npmapi.AccessItems   `json:"items"`
	Clients npmapi.AccessClients `json:"clients"`
}

// Records the NPM readers report.

type NPMCertificateSummary struct {
	Name      string   `json:"name"`
	Domains   []string `json:"domains"`
	ExpiresOn string   `json:"expires_on"`
	Provider  string   `json:"provider"`
}

// npmCertificateRef is a proxy host's certificate: its summary, or {} when
// NPM lists none for the host.
type npmCertificateRef struct{ *NPMCertificateSummary }

func (c npmCertificateRef) MarshalJSON() ([]byte, error) {
	if c.NPMCertificateSummary == nil {
		return []byte("{}"), nil
	}
	return json.Marshal(c.NPMCertificateSummary)
}

type NPMAccessRule struct {
	Directive string `json:"directive"`
	Address   string `json:"address"`
}

type NPMAccessPolicy struct {
	Name               string          `json:"name"`
	SatisfyAny         bool            `json:"satisfy_any"`
	PassAuth           bool            `json:"pass_auth"`
	AuthorizationCount int             `json:"authorization_count"`
	Clients            []NPMAccessRule `json:"clients"`
	ImplicitDeny       bool            `json:"implicit_deny"`
}

// NPMProxyHostRecord is a proxy host as HQ adopts it.
type NPMProxyHostRecord struct {
	DomainNames           []string          `json:"domain_names"`
	ForwardScheme         string            `json:"forward_scheme"`
	ForwardHost           string            `json:"forward_host"`
	ForwardPort           int               `json:"forward_port"`
	SSLForced             bool              `json:"ssl_forced"`
	HTTP2Support          bool              `json:"http2_support"`
	AllowWebsocketUpgrade bool              `json:"allow_websocket_upgrade"`
	CachingEnabled        bool              `json:"caching_enabled"`
	BlockExploits         bool              `json:"block_exploits"`
	AccessListID          int               `json:"access_list_id"`
	AdvancedConfig        string            `json:"advanced_config"`
	HSTSEnabled           bool              `json:"hsts_enabled"`
	HSTSSubdomains        bool              `json:"hsts_subdomains"`
	TrustForwardedProto   bool              `json:"trust_forwarded_proto"`
	Enabled               bool              `json:"enabled"`
	Certificate           npmCertificateRef `json:"certificate"`
	AccessPolicy          *NPMAccessPolicy  `json:"access_policy"`
}

// The per-connection readers below leave ConnectionRef unset; the kind's
// reader sets it when it gathers every connection.

type NPMCertificateRecord struct {
	ConnectionRef *string  `json:"connection_ref,omitempty"`
	ID            int      `json:"id"`
	Name          string   `json:"name"`
	Provider      string   `json:"provider"`
	Domains       []string `json:"domains"`
	ExpiresOn     string   `json:"expires_on"`
	Serves        []string `json:"serves"`
}

type NPMRedirectRecord struct {
	ConnectionRef *string  `json:"connection_ref,omitempty"`
	ID            int      `json:"id"`
	Hostnames     []string `json:"hostnames"`
	Target        string   `json:"target"`
	TargetHost    string   `json:"target_host"`
	StatusCode    *int     `json:"status_code"`
	PreservePath  bool     `json:"preserve_path"`
	SSLForced     bool     `json:"ssl_forced"`
	Certificate   string   `json:"certificate"`
	Enabled       bool     `json:"enabled"`
}

type NPMDeadHostRecord struct {
	ConnectionRef *string  `json:"connection_ref,omitempty"`
	ID            int      `json:"id"`
	Hostnames     []string `json:"hostnames"`
	Certificate   string   `json:"certificate"`
	SSLForced     bool     `json:"ssl_forced"`
	Enabled       bool     `json:"enabled"`
}

type NPMStreamRecord struct {
	ConnectionRef  *string `json:"connection_ref,omitempty"`
	ID             int     `json:"id"`
	IncomingPort   int     `json:"incoming_port"`
	ForwardingHost string  `json:"forwarding_host"`
	ForwardingPort *int    `json:"forwarding_port"`
	TCP            bool    `json:"tcp"`
	UDP            bool    `json:"udp"`
	Enabled        bool    `json:"enabled"`
}

type NPMAccessListRecord struct {
	ConnectionRef *string         `json:"connection_ref,omitempty"`
	ID            int             `json:"id"`
	Name          string          `json:"name"`
	SatisfyAny    bool            `json:"satisfy_any"`
	PassAuth      bool            `json:"pass_auth"`
	Clients       []NPMAccessRule `json:"clients"`
	Logins        []string        `json:"logins"`
	Protects      []string        `json:"protects"`
}
