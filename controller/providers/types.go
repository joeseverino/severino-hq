package providers

import "encoding/json"

// AdGuard types

type AdGuardRewriteSpec struct {
	Domain string `json:"domain"`
	Answer string `json:"answer"`
}

type AdGuardRewriteObserved struct {
	Domain  string `json:"domain,omitempty"`
	Answer  string `json:"answer,omitempty"`
	Enabled bool   `json:"enabled,omitempty"`
}

type AdGuardRewriteStatus struct {
	Domain  string `json:"domain"`
	Answer  string `json:"answer"`
	Enabled bool   `json:"enabled"`
}

type AdGuardRewriteRecord struct {
	Domain  string `json:"domain"`
	Answer  string `json:"answer"`
	Enabled bool   `json:"enabled,omitempty"`
}

type AdGuardDeleteStatus struct {
	Domain  string `json:"domain"`
	Removed bool   `json:"removed"`
}

// NPM (Nginx Proxy Manager) types

// namedConnection is the connection a declaration names ("connection_ref"); a
// value that is not text names none.
type namedConnection string

func (n *namedConnection) UnmarshalJSON(data []byte) error {
	var text string
	if json.Unmarshal(data, &text) != nil {
		text = ""
	}
	*n = namedConnection(text)
	return nil
}

type NPMProxyHostSpec struct {
	ConnectionRef       namedConnection `json:"connection_ref"`
	DomainNames         []string        `json:"domain_names"`
	ForwardScheme       string          `json:"forward_scheme"`
	ForwardHost         string          `json:"forward_host"`
	ForwardPort         int             `json:"forward_port"`
	ForceSSL            bool            `json:"force_ssl"`
	HTTP2               bool            `json:"http2"`
	Websocket           bool            `json:"websocket"`
	CachingEnabled      bool            `json:"caching_enabled"`
	BlockExploits       bool            `json:"block_exploits"`
	AccessListID        int             `json:"access_list_id"`
	CertificateID       int             `json:"certificate_id"`
	AdvancedConfig      string          `json:"advanced_config"`
	HSTSEnabled         bool            `json:"hsts_enabled"`
	HSTSSubdomains      bool            `json:"hsts_subdomains"`
	TrustForwardedProto bool            `json:"trust_forwarded_proto"`
	Serving             bool            `json:"serving"`
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

// Tailscale types

type TailnetDeviceSpec struct {
	ConnectionRef     string `json:"connection_ref,omitempty"`
	Name              string `json:"name"`
	KeyExpiryDisabled bool   `json:"key_expiry_disabled"`
}

type TailnetDeviceObserved struct {
	Name string `json:"name,omitempty"`
}

type TailnetDeviceStatus struct {
	Name              string `json:"name"`
	Online            bool   `json:"online"`
	KeyExpires        string `json:"key_expires"`
	KeyExpiryDisabled bool   `json:"key_expiry_disabled"`
}

type TailnetRouteStatus struct {
	Name             string   `json:"name"`
	AdvertisedRoutes []string `json:"advertised_routes"`
	EnabledRoutes    []string `json:"enabled_routes"`
}

type TailnetPolicySpec struct {
	ConnectionRef string `json:"connection_ref,omitempty"`
	Document      string `json:"document"`
}

type TailnetPolicyStatus struct {
	Applied  bool   `json:"applied"`
	Document string `json:"document"`
}
