package providers

// Tailscale action payloads: what HQ declares and what an action reports.

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
