package providers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
	tsapi "tailscale.com/client/tailscale/v2"
)

// tailnetAPI is the default control server; a connection's <PREFIX>_URL replaces it.
const tailnetAPI = "https://api.tailscale.com/api/v2"

// tailnetClient is one connection's access to the Tailscale API: its base URL
// and the token exchanged for it. Every tailnet request goes through call.
type tailnetClient struct {
	r     *Registry
	base  string
	token string
}

// tailnetBase is where the connection's API lives: its URL, else the public one.
func (r *Registry) tailnetBase(prefix string) string {
	if base := strings.TrimRight(strings.TrimSpace(r.Env[prefix+"_URL"]), "/"); base != "" {
		return base
	}
	return tailnetAPI
}

// tailnetClient exchanges the connection's credential and returns the client.
func (r *Registry) tailnetClient(ctx context.Context, connectionRef string) (tailnetClient, error) {
	token, err := r.tailnetToken(ctx, connectionRef)
	if err != nil {
		return tailnetClient{}, err
	}
	prefix, err := r.Env.Prefix("tailscale", connectionRef)
	if err != nil {
		return tailnetClient{}, err
	}
	return tailnetClient{r: r, base: r.tailnetBase(prefix), token: token}, nil
}

// call sends one request to the tailnet API: base URL and bearer auth are set
// here, extra carries the headers that differ per call.
func (c tailnetClient) call(ctx context.Context, method, path string, extra map[string]string, body any) (json.RawMessage, error) {
	return c.r.HTTP.Request(ctx, c.base+path, method, c.headers(extra), body)
}

// callHeader is a GET that also returns one response header.
func (c tailnetClient) callHeader(ctx context.Context, path string, extra map[string]string, name string) (json.RawMessage, string, error) {
	return c.r.HTTP.RequestHeader(ctx, c.base+path, c.headers(extra), name)
}

func (c tailnetClient) headers(extra map[string]string) map[string]string {
	headers := map[string]string{"Authorization": "Bearer " + c.token}
	for name, value := range extra {
		headers[name] = value
	}
	return headers
}

// tailnet is a path under the tailnet this credential belongs to.
func tailnet(path string) string { return "/tailnet/-/" + path }

// tailnetDevicePath is a path under one device.
func tailnetDevicePath(identifier, tail string) string {
	return "/device/" + url.PathEscape(identifier) + "/" + tail
}

// tailnetNode is one node of `tailscale status --json` (Self, or a Peer).
type tailnetNode struct {
	ID             string   `json:"ID"`
	HostName       string   `json:"HostName"`
	PublicKey      string   `json:"PublicKey"`
	DNSName        string   `json:"DNSName"`
	Online         bool     `json:"Online"`
	LastSeen       string   `json:"LastSeen"`
	KeyExpiry      string   `json:"KeyExpiry"`
	TailscaleIPs   []string `json:"TailscaleIPs"`
	OS             string   `json:"OS"`
	ExitNode       bool     `json:"ExitNode"`
	ExitNodeOption bool     `json:"ExitNodeOption"`
	CurAddr        string   `json:"CurAddr"`
	Addrs          []string `json:"Addrs"`
	Relay          string   `json:"Relay"`
	LastHandshake  string   `json:"LastHandshake"`
	Active         bool     `json:"Active"`
	RxBytes        int64    `json:"RxBytes"`
	TxBytes        int64    `json:"TxBytes"`
}

// tailnetDevice is one device of the API's /devices?fields=all.
type tailnetDevice struct {
	ID                 string   `json:"id"`
	Hostname           string   `json:"hostname"`
	Name               string   `json:"name"`
	NodeKey            string   `json:"nodeKey"`
	ConnectedToControl bool     `json:"connectedToControl"`
	LastSeen           string   `json:"lastSeen"`
	Expires            string   `json:"expires"`
	KeyExpiryDisabled  bool     `json:"keyExpiryDisabled"`
	Addresses          []string `json:"addresses"`
	OS                 string   `json:"os"`
	ClientConnectivity struct {
		Endpoints []string `json:"endpoints"`
	} `json:"clientConnectivity"`
	AdvertisedRoutes []string  `json:"advertisedRoutes"`
	EnabledRoutes    []string  `json:"enabledRoutes"`
	Tags             []string  `json:"tags"`
	User             string    `json:"user"`
	Authorized       pyOptBool `json:"authorized"`
	TailnetLockError string    `json:"tailnetLockError"`
	UpdateAvailable  bool      `json:"updateAvailable"`
	ClientVersion    string    `json:"clientVersion"`
	SSHEnabled       bool      `json:"sshEnabled"`
	BlocksIncoming   bool      `json:"blocksIncomingConnections"`
	IsExternal       bool      `json:"isExternal"`
}

type tailnetRoutes struct {
	AdvertisedRoutes []string `json:"advertisedRoutes"`
	EnabledRoutes    []string `json:"enabledRoutes"`
}
type tailnetRoutesRequest struct {
	Routes []string `json:"routes"`
}
type tailnetKeyRequest struct {
	KeyExpiryDisabled bool `json:"keyExpiryDisabled"`
}
type tailnetToken struct {
	AccessToken string `json:"access_token"`
}

// tailnetResolvers is a nameserver list; Tailscale writes each entry as an
// address string or as {"address": ...}.
type tailnetResolvers []json.RawMessage

func (list tailnetResolvers) addresses() []string {
	found := []string{}
	for _, entry := range list {
		var address string
		if json.Unmarshal(entry, &address) != nil {
			var resolver struct {
				Address string `json:"address"`
			}
			if json.Unmarshal(entry, &resolver) != nil {
				continue
			}
			address = resolver.Address
		}
		if address != "" {
			found = append(found, address)
		}
	}
	return found
}

type tailnetDNSConfiguration struct {
	Nameservers tailnetResolvers                  `json:"nameservers"`
	Preferences tsapi.DNSConfigurationPreferences `json:"preferences"`
	SearchPaths []string                          `json:"searchPaths"`
	SplitDNS    map[string]tailnetResolvers       `json:"splitDNS"`
}

// tailnetSettings carries Tailscale's settings verbatim: HQ stores what
// Tailscale said, and a field Tailscale withholds stays null.
type tailnetSettings struct {
	DevicesApprovalOn           json.RawMessage `json:"devicesApprovalOn"`
	DevicesKeyDurationDays      json.RawMessage `json:"devicesKeyDurationDays"`
	DevicesAutoUpdatesOn        json.RawMessage `json:"devicesAutoUpdatesOn"`
	UsersApprovalOn             json.RawMessage `json:"usersApprovalOn"`
	RegionalRoutingOn           json.RawMessage `json:"regionalRoutingOn"`
	PostureIdentityCollectionOn json.RawMessage `json:"postureIdentityCollectionOn"`
	HTTPSEnabled                json.RawMessage `json:"httpsEnabled"`
	ACLsExternallyManagedOn     json.RawMessage `json:"aclsExternallyManagedOn"`
}

type tailnetUsers struct {
	Users []struct {
		ID          string `json:"id"`
		DisplayName string `json:"displayName"`
		LoginName   string `json:"loginName"`
		Role        string `json:"role"`
		Status      string `json:"status"`
		Created     string `json:"created"`
		LastSeen    string `json:"lastSeen"`
	} `json:"users"`
}

// Records the Tailscale readers report.
type TailnetReachRule struct {
	Who  []string `json:"who"`
	To   []string `json:"to"`
	Line *int     `json:"line"`
}
type TailnetReach struct {
	Port  int                `json:"port"`
	Who   []string           `json:"who"`
	Rules []TailnetReachRule `json:"rules"`
}

// TailscaleDeviceRecord is one machine on the tailnet. KeyExpiryDisabled is
// present only when the API described the device.
type TailscaleDeviceRecord struct {
	Name              string         `json:"name"`
	PublicKey         string         `json:"public_key"`
	DNSName           string         `json:"dns_name"`
	Online            bool           `json:"online"`
	LastSeen          string         `json:"last_seen"`
	KeyExpires        string         `json:"key_expires"`
	Addresses         []string       `json:"addresses"`
	OS                string         `json:"os"`
	ExitNodeInUse     bool           `json:"exit_node_in_use"`
	OffersExitNode    bool           `json:"offers_exit_node"`
	Self              bool           `json:"self"`
	DirectEndpoint    string         `json:"direct_endpoint"`
	Endpoints         []string       `json:"endpoints"`
	Relay             string         `json:"relay"`
	LastHandshake     string         `json:"last_handshake"`
	Active            bool           `json:"active"`
	RxBytes           int64          `json:"rx_bytes"`
	TxBytes           int64          `json:"tx_bytes"`
	Reach             []TailnetReach `json:"reach"`
	User              string         `json:"user"`
	Tags              []string       `json:"tags"`
	AdvertisedRoutes  []string       `json:"advertised_routes"`
	EnabledRoutes     []string       `json:"enabled_routes"`
	Authorized        bool           `json:"authorized"`
	LockError         string         `json:"lock_error"`
	UpdateAvailable   bool           `json:"update_available"`
	ClientVersion     string         `json:"client_version"`
	SSHEnabled        bool           `json:"ssh_enabled"`
	BlocksIncoming    bool           `json:"blocks_incoming"`
	External          bool           `json:"external"`
	KeyExpiryDisabled *bool          `json:"key_expiry_disabled,omitempty"`
	ExitNodeApproved  bool           `json:"exit_node_approved"`
}

type TailscaleDNSRecord struct {
	Record           string              `json:"record"`
	Nameservers      []string            `json:"nameservers"`
	OverrideLocalDNS bool                `json:"override_local_dns"`
	MagicDNS         bool                `json:"magic_dns"`
	SearchPaths      []string            `json:"search_paths"`
	SplitDNS         map[string][]string `json:"split_dns"`
}

type TailscaleSettingsRecord struct {
	Record                      string          `json:"record"`
	DevicesApprovalOn           json.RawMessage `json:"devices_approval_on"`
	DevicesKeyDurationDays      json.RawMessage `json:"devices_key_duration_days"`
	DevicesAutoUpdatesOn        json.RawMessage `json:"devices_auto_updates_on"`
	UsersApprovalOn             json.RawMessage `json:"users_approval_on"`
	RegionalRoutingOn           json.RawMessage `json:"regional_routing_on"`
	PostureIdentityCollectionOn json.RawMessage `json:"posture_identity_collection_on"`
	HTTPSEnabled                json.RawMessage `json:"https_enabled"`
	ACLsExternallyManagedOn     json.RawMessage `json:"acls_externally_managed_on"`
}

type TailscaleUserRecord struct {
	ID          string `json:"id"`
	DisplayName string `json:"display_name"`
	LoginName   string `json:"login_name"`
	Role        string `json:"role"`
	Status      string `json:"status"`
	Created     string `json:"created"`
	LastSeen    string `json:"last_seen"`
}

func (r *Registry) admitTailscale() {
	act(r, runtime.ResourceKindTailscaleDevice, "reconcile", r.tailscaleDeviceReconcile)
	act(r, runtime.ResourceKindTailscaleDevice, "approve-routes", r.tailscaleApproveRoutes)
	r.reader(runtime.ResourceKindTailscaleDevice, r.tailscaleDeviceInventory)

	act(r, runtime.ResourceKindTailscalePolicy, "reconcile", r.tailnetPolicyReconcile)
	r.reader(runtime.ResourceKindTailscalePolicy, r.tailnetPolicyInventory)

	r.reader(runtime.ResourceKindTailscaleDNS, r.tailscaleDNS)
	r.reader(runtime.ResourceKindTailscaleSettings, r.tailscaleSettings)
	r.reader(runtime.ResourceKindTailscaleUser, r.tailscaleUsers)

	r.probe("tailscale", r.tailscaleProbe)
}

func (r *Registry) tailnetToken(ctx context.Context, connectionRef string) (string, error) {
	prefix, err := r.Env.Prefix("tailscale", connectionRef)
	if err != nil {
		return "", err
	}
	raw, err := r.cached(ctx, "tailscale-token:"+prefix, func() (json.RawMessage, error) {
		clientID, err := r.Env.Required(prefix, "CLIENT_ID")
		if err != nil {
			return nil, err
		}
		clientSecret, err := r.Env.Required(prefix, "CLIENT_SECRET")
		if err != nil {
			return nil, err
		}
		data := url.Values{"client_id": {clientID}, "client_secret": {clientSecret}}
		answer, err := r.HTTP.Request(ctx, r.tailnetBase(prefix)+"/oauth/token", "POST", map[string]string{
			"Content-Type": "application/x-www-form-urlencoded",
		}, data)
		if code := httpStatus(err); code != 0 {
			reason := fmt.Sprintf("Tailscale refused the credential for %s (%d). It has to be an OAuth client, not an API key.", connectionRef, code)
			return nil, &ProviderError{Message: reason, Refusal: runtime.RefusalCredential, Failure: runtime.FailureClassCredential, Reason: reason}
		}
		if err != nil || !isObject(answer) {
			return nil, &ProviderError{Message: "Tailscale did not answer the token request.", Failure: networkFailure(err)}
		}
		token, _ := decodeAs[tailnetToken](answer, "")
		if token.AccessToken == "" {
			return nil, &ProviderError{Message: "Tailscale returned no access token."}
		}
		return json.Marshal(token.AccessToken)
	})
	if err != nil {
		return "", err
	}
	var token string
	_ = json.Unmarshal(raw, &token)
	return token, nil
}

// httpStatus is the provider's non-2xx status behind err, or 0.
func httpStatus(err error) int {
	var provider *ProviderError
	if errors.As(err, &provider) {
		return provider.HTTPStatus
	}
	return 0
}

// networkFailure keeps a network failure's classification on the error that wraps it.
func networkFailure(err error) runtime.FailureClass {
	var provider *ProviderError
	if errors.As(err, &provider) && provider.Failure == runtime.FailureClassNetwork {
		return runtime.FailureClassNetwork
	}
	return runtime.FailureClassUnclassified
}

// unreadable names what went wrong with an answer that was not an HTTP
// refusal, in the words the Python controller reports it with.
func unreadable(err error) string {
	if networkFailure(err) != "" {
		return "URLError"
	}
	return "JSONDecodeError"
}

func isObject(raw json.RawMessage) bool {
	var fields map[string]json.RawMessage
	return json.Unmarshal(raw, &fields) == nil && fields != nil
}

// tailnetRefused classifies one refused tailnet read. The token was just
// exchanged, so 403, and 404 on some endpoints, is a missing scope; 401 is the
// token refused.
func tailnetRefused(what, scope string, code int) error {
	switch code {
	case 403, 404:
		return &ProviderError{
			Message: fmt.Sprintf("Tailscale refused %s (%d). The credential needs the %s scope.", what, code, scope),
			Failure: runtime.FailureClassPermission,
			Refusal: runtime.RefusalPermission,
		}
	case 401:
		return &ProviderError{
			Message: fmt.Sprintf("Tailscale refused %s (%d).", what, code),
			Failure: runtime.FailureClassCredential,
			Refusal: runtime.RefusalCredential,
			Reason:  fmt.Sprintf("Tailscale refused the access token (%d).", code),
		}
	}
	return &ProviderError{Message: fmt.Sprintf("Tailscale refused %s (%d).", what, code)}
}

// tailnetGet reads one tailnet endpoint, undecoded.
func (c tailnetClient) tailnetGet(ctx context.Context, path string) (json.RawMessage, error) {
	return c.call(ctx, "GET", tailnet(path), map[string]string{"Accept": "application/json"}, nil)
}

// tailnetRead reads one tailnet endpoint into its response type. A refusal
// names the scope the read needs.
func tailnetRead[T any](ctx context.Context, r *Registry, path, what, scope string) (T, error) {
	var zero T
	client, err := r.tailnetClient(ctx, "")
	if err != nil {
		return zero, err
	}
	raw, err := client.tailnetGet(ctx, path)
	if code := httpStatus(err); code != 0 {
		return zero, tailnetRefused("the "+what+" read", scope, code)
	}
	invalid := fmt.Sprintf("Tailscale did not return readable %s.", what)
	if err != nil || !isObject(raw) {
		return zero, &ProviderError{Message: invalid, Failure: networkFailure(err)}
	}
	return decodeAs[T](raw, invalid)
}

// tailnetPart is one tailnet read for a declared part of a record.
func (c tailnetClient) tailnetPart(ctx context.Context, path string) (map[string]json.RawMessage, error) {
	raw, err := c.call(ctx, "GET", tailnet(path), nil, nil)
	if code := httpStatus(err); code != 0 {
		return nil, &ProviderError{Message: fmt.Sprintf("/%s answered HTTP %d.", path, code)}
	}
	if err != nil || len(raw) == 0 {
		return nil, &ProviderError{Message: fmt.Sprintf("/%s could not be read: %s.", path, unreadable(err))}
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || fields == nil {
		return nil, &ProviderError{Message: fmt.Sprintf("/%s did not answer with an object.", path)}
	}
	return fields, nil
}

func (c tailnetClient) tailnetAPIDevices(ctx context.Context) ([]tailnetDevice, error) {
	raw, err := c.call(ctx, "GET", tailnet("devices?fields=all"), nil, nil)
	if code := httpStatus(err); code != 0 {
		return nil, tailnetRefused("the tailnet device list", "devices:core:read", code)
	}
	if err != nil || len(raw) == 0 {
		return nil, &ProviderError{Message: fmt.Sprintf("The tailnet device list could not be read: %s.", unreadable(err))}
	}
	var answer struct {
		Devices []json.RawMessage `json:"devices"`
	}
	if !isObject(raw) || json.Unmarshal(raw, &answer) != nil {
		return nil, &ProviderError{Message: "The tailnet device list did not answer with an object."}
	}
	devices := []tailnetDevice{}
	for _, entry := range answer.Devices {
		var device tailnetDevice
		if isObject(entry) && json.Unmarshal(entry, &device) == nil {
			devices = append(devices, device)
		}
	}
	return devices, nil
}

// tailnetReading is how reading the local status file went.
type tailnetReading int

const (
	readingOK tailnetReading = iota
	readingAbsent
	readingMissing
	readingInvalid
)

// readTailnetNodes reads the tailnet status file: Self first, then each
// Peer in the order the file lists them.
func (r *Registry) readTailnetNodes() ([]tailnetNode, tailnetReading) {
	statusFile := r.Env["SEVERINO_TAILNET_STATUS"]
	if statusFile == "" {
		return nil, readingAbsent
	}
	data, err := os.ReadFile(statusFile)
	if err != nil {
		return nil, readingMissing
	}
	var status struct {
		Self *tailnetNode    `json:"Self"`
		Peer json.RawMessage `json:"Peer"`
	}
	if err := json.Unmarshal(data, &status); err != nil {
		return nil, readingInvalid
	}
	nodes := []tailnetNode{}
	if status.Self != nil {
		nodes = append(nodes, *status.Self)
	}
	peers, err := orderedValues[tailnetNode](status.Peer)
	if err != nil {
		return nil, readingInvalid
	}
	return append(nodes, peers...), readingOK
}

// localTailnetNodes is the raw reading, for the fields a record does not carry.
func (r *Registry) localTailnetNodes() ([]tailnetNode, error) {
	nodes, reading := r.readTailnetNodes()
	switch reading {
	case readingAbsent:
		return nil, &ProviderError{Message: "This controller was not given a tailnet reading."}
	case readingMissing, readingInvalid:
		return nil, &ProviderError{Message: "The tailnet reading is missing or unreadable."}
	}
	return nodes, nil
}

// orderedValues decodes a JSON object's values in the order they appear.
func orderedValues[T any](raw json.RawMessage) ([]T, error) {
	out := []T{}
	if len(raw) == 0 || string(raw) == "null" {
		return out, nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if token, err := decoder.Token(); err != nil || token != json.Delim('{') {
		return nil, errors.New("not an object")
	}
	for decoder.More() {
		if _, err := decoder.Token(); err != nil {
			return nil, err
		}
		var value T
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		out = append(out, value)
	}
	return out, nil
}

func nonNil(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func sortedCopy(values []string) []string {
	out := append([]string{}, values...)
	sort.Strings(out)
	return out
}

func tailnetRecord(node tailnetNode) (TailscaleDeviceRecord, bool) {
	name := strings.TrimSpace(node.HostName)
	if name == "" {
		return TailscaleDeviceRecord{}, false
	}
	return TailscaleDeviceRecord{
		Name:           name,
		PublicKey:      node.PublicKey,
		DNSName:        strings.TrimRight(node.DNSName, "."),
		Online:         node.Online,
		LastSeen:       node.LastSeen,
		KeyExpires:     node.KeyExpiry,
		Addresses:      nonNil(node.TailscaleIPs),
		OS:             node.OS,
		ExitNodeInUse:  node.ExitNode,
		OffersExitNode: node.ExitNodeOption,
		DirectEndpoint: node.CurAddr,
		Endpoints:      nonNil(node.Addrs),
		Relay:          node.Relay,
		LastHandshake:  node.LastHandshake,
		Active:         node.Active,
		RxBytes:        node.RxBytes,
		TxBytes:        node.TxBytes,
	}, true
}

func apiDeviceRecord(device tailnetDevice) (TailscaleDeviceRecord, bool) {
	name := strings.TrimSpace(device.Hostname)
	if name == "" {
		return TailscaleDeviceRecord{}, false
	}
	keyExpires := device.Expires
	if device.KeyExpiryDisabled {
		keyExpires = ""
	}
	return TailscaleDeviceRecord{
		Name:       name,
		PublicKey:  device.NodeKey,
		DNSName:    strings.TrimRight(device.Name, "."),
		Online:     device.ConnectedToControl,
		LastSeen:   device.LastSeen,
		KeyExpires: keyExpires,
		Addresses:  nonNil(device.Addresses),
		OS:         device.OS,
		Endpoints:  nonNil(device.ClientConnectivity.Endpoints),
	}, true
}

func exitRoute(routes []string) bool {
	for _, route := range routes {
		if route == "0.0.0.0/0" || route == "::/0" {
			return true
		}
	}
	return false
}

func identitiesFrom(devices []tailnetDevice) map[string]tailnetDevice {
	found := map[string]tailnetDevice{}
	for _, device := range devices {
		if device.Hostname != "" {
			found[device.Hostname] = device
		}
	}
	return found
}

// localTailnetDevices is the tailnet as the local daemon sees it.
func (r *Registry) localTailnetDevices() ([]TailscaleDeviceRecord, error) {
	nodes, reading := r.readTailnetNodes()
	switch reading {
	case readingAbsent:
		return nil, &ProviderError{Message: "This controller was not given a tailnet reading, so it cannot say which machines are up."}
	case readingMissing:
		return nil, &ProviderError{Message: "The tailnet reading is missing. It is taken from the local daemon before this container starts, and only when there is one."}
	case readingInvalid:
		return nil, &ProviderError{Message: "The tailnet reading is not readable status."}
	}
	found := []TailscaleDeviceRecord{}
	for _, node := range nodes {
		if record, ok := tailnetRecord(node); ok {
			found = append(found, record)
		}
	}
	if len(found) > 0 {
		found[0].Self = true
	}
	return found, nil
}

func (r *Registry) tailscaleDeviceInventory(ctx context.Context) ([]any, error) {
	devices := []TailscaleDeviceRecord{}
	identities := map[string]tailnetDevice{}
	if r.Env["SEVERINO_TAILNET_STATUS"] != "" {
		local, err := r.localTailnetDevices()
		if err != nil {
			return nil, err
		}
		devices = local
		if client, err := r.tailnetClient(ctx, ""); err == nil {
			if apiDevices, err := client.tailnetAPIDevices(ctx); err == nil {
				identities = identitiesFrom(apiDevices)
			}
		}
	} else {
		client, err := r.tailnetClient(ctx, "")
		if err != nil {
			return nil, &ProviderError{
				Message: "This controller was not given a tailnet reading or a tailnet credential, so it cannot say which machines are up. " + err.Error(),
			}
		}
		apiDevices, err := client.tailnetAPIDevices(ctx)
		if err != nil {
			return nil, err
		}
		for _, device := range apiDevices {
			if record, ok := apiDeviceRecord(device); ok {
				devices = append(devices, record)
			}
		}
		identities = identitiesFrom(apiDevices)
	}

	reach := r.reachByDevice(ctx, devices)
	found := []any{}
	for _, device := range devices {
		device.Reach = reach[device.Name]
		if device.Reach == nil {
			device.Reach = []TailnetReach{}
		}
		identity, known := identities[device.Name]
		device.User = identity.User
		device.Tags = sortedCopy(identity.Tags)
		device.AdvertisedRoutes = sortedCopy(identity.AdvertisedRoutes)
		device.EnabledRoutes = sortedCopy(identity.EnabledRoutes)
		device.Authorized = identity.Authorized.orTrue()
		device.LockError = identity.TailnetLockError
		device.UpdateAvailable = identity.UpdateAvailable
		device.ClientVersion = identity.ClientVersion
		device.SSHEnabled = identity.SSHEnabled
		device.BlocksIncoming = identity.BlocksIncoming
		device.External = identity.IsExternal
		if known {
			disabled := identity.KeyExpiryDisabled
			device.KeyExpiryDisabled = &disabled
			device.OffersExitNode = exitRoute(identity.AdvertisedRoutes)
			device.ExitNodeApproved = exitRoute(identity.EnabledRoutes)
		} else {
			device.ExitNodeApproved = device.OffersExitNode
		}
		found = append(found, device)
	}
	return found, nil
}

// tailnetDeviceID is the device's stable id, from the local reading rather than the API.
func (r *Registry) tailnetDeviceID(name string) (string, error) {
	nodes, err := r.localTailnetNodes()
	if err != nil {
		return "", err
	}
	for _, node := range nodes {
		if strings.TrimSpace(node.HostName) == name && node.ID != "" {
			return node.ID, nil
		}
	}
	return "", &ProviderError{Message: fmt.Sprintf("No device called %s is on the tailnet this machine can see.", pyRepr(name))}
}

// tailnetDeviceState is what the local reading says about one device now.
func (r *Registry) tailnetDeviceState(name string) (TailnetDeviceStatus, error) {
	devices, err := r.localTailnetDevices()
	if err != nil {
		return TailnetDeviceStatus{}, err
	}
	for _, record := range devices {
		if record.Name == name {
			return TailnetDeviceStatus{Name: name, Online: record.Online, KeyExpires: record.KeyExpires, KeyExpiryDisabled: record.KeyExpires == ""}, nil
		}
	}
	return TailnetDeviceStatus{}, &ProviderError{Message: fmt.Sprintf("No device called %s is on the tailnet this machine can see.", pyRepr(name))}
}

func (r *Registry) tailscaleDeviceReconcile(ctx context.Context, spec TailnetDeviceSpec, _ struct{}, apply bool) (Result, error) {
	name := spec.Name
	wanted := spec.KeyExpiryDisabled
	current, err := r.tailnetDeviceState(name)
	if err != nil {
		return Result{}, err
	}
	if current.KeyExpiryDisabled == wanted {
		return result(false, current, "Reconciled", "The device is as declared.", "Tailnet device is current."), nil
	}
	if !apply {
		msg := fmt.Sprintf("Key expiry would be %s for %s.", map[bool]string{true: "disabled", false: "enabled"}[wanted], name)
		return Result{Changed: true, Status: current, Message: msg}, nil
	}
	identifier, err := r.tailnetDeviceID(name)
	if err != nil {
		return Result{}, err
	}
	client, err := r.tailnetClient(ctx, spec.ConnectionRef)
	if err != nil {
		return Result{}, err
	}
	_, err = client.call(ctx, "POST", tailnetDevicePath(identifier, "key"), map[string]string{
		"Content-Type": "application/json",
	}, tailnetKeyRequest{KeyExpiryDisabled: wanted})
	switch code := httpStatus(err); {
	case code == 403:
		return Result{}, &ProviderError{Message: "This Tailscale credential may not change devices. It needs the devices:core scope.", Failure: runtime.FailureClassPermission}
	case code != 0:
		return Result{}, &ProviderError{Message: fmt.Sprintf("Tailscale refused the change to %s (%d).", name, code), Failure: runtime.StatusFailure(code)}
	case err != nil:
		return Result{}, &ProviderError{Message: "Tailscale did not answer the change request.", Failure: networkFailure(err)}
	}
	status := TailnetDeviceStatus{Name: name, Online: current.Online, KeyExpires: "", KeyExpiryDisabled: wanted}
	message := fmt.Sprintf("%s has an expiry date again.", name)
	if wanted {
		message = fmt.Sprintf("%s now stays on the tailnet.", name)
	}
	return result(true, status, "Reconciled", "The device is as declared.", message), nil
}

func (r *Registry) tailscaleApproveRoutes(ctx context.Context, spec TailnetDeviceSpec, _ struct{}, apply bool) (Result, error) {
	name := spec.Name
	identifier, err := r.tailnetDeviceID(name)
	if err != nil {
		return Result{}, err
	}
	client, err := r.tailnetClient(ctx, spec.ConnectionRef)
	if err != nil {
		return Result{}, err
	}
	routesPath := tailnetDevicePath(identifier, "routes")
	raw, err := client.call(ctx, "GET", routesPath, nil, nil)
	unreported := fmt.Sprintf("Tailscale did not report the routes for %s.", name)
	if code := httpStatus(err); code == 401 || code == 403 {
		return Result{}, &ProviderError{Message: "This Tailscale credential may not read routes. It needs the devices:routes:read scope, or devices:routes to approve them.", Failure: runtime.StatusFailure(code)}
	}
	if err != nil || len(raw) == 0 {
		return Result{}, &ProviderError{Message: unreported, Failure: networkFailure(err)}
	}
	current, err := decodeAs[tailnetRoutes](raw, unreported)
	if err != nil {
		return Result{}, err
	}
	advertised := sortedCopy(current.AdvertisedRoutes)
	enabled := sortedCopy(current.EnabledRoutes)
	enabledSet := map[string]bool{}
	for _, route := range enabled {
		enabledSet[route] = true
	}
	pending := []string{}
	for _, route := range advertised {
		if !enabledSet[route] {
			pending = append(pending, route)
		}
	}
	status := TailnetRouteStatus{Name: name, AdvertisedRoutes: advertised, EnabledRoutes: enabled}
	if len(pending) == 0 {
		msg := "Nothing to approve."
		if len(advertised) == 0 {
			msg = fmt.Sprintf("%s advertises no routes.", name)
		}
		return result(false, status, "Reconciled", "Every route this device advertises is approved.", msg), nil
	}
	if !apply {
		return Result{Changed: true, Status: status, Message: fmt.Sprintf("Would approve %s for %s.", strings.Join(pending, ", "), name)}, nil
	}
	answer, err := client.call(ctx, "POST", routesPath, map[string]string{
		"Content-Type": "application/json",
	}, tailnetRoutesRequest{Routes: advertised})
	switch code := httpStatus(err); {
	case code == 401 || code == 403:
		return Result{}, &ProviderError{Message: "This Tailscale credential may not approve routes. It needs the devices:routes scope.", Failure: runtime.StatusFailure(code)}
	case code != 0:
		return Result{}, &ProviderError{Message: fmt.Sprintf("Tailscale refused the route approval for %s.", name)}
	case err != nil || len(answer) == 0:
		return Result{}, &ProviderError{Message: fmt.Sprintf("Tailscale did not answer for %s.", name), Failure: networkFailure(err)}
	}
	approved, err := decodeAs[tailnetRoutes](answer, fmt.Sprintf("Tailscale did not answer for %s.", name))
	if err != nil {
		return Result{}, err
	}
	status.EnabledRoutes = sortedCopy(approved.EnabledRoutes)
	return result(true, status, "Reconciled", "The advertised routes are approved.", fmt.Sprintf("Approved %s for %s.", strings.Join(pending, ", "), name)), nil
}

func (r *Registry) tailscaleDNS(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[tailnetDNSConfiguration](ctx, r, "dns/configuration", "DNS configuration", "dns:read")
	if err != nil {
		return nil, err
	}
	splitDNS := map[string][]string{}
	for domain, resolvers := range found.SplitDNS {
		splitDNS[domain] = resolvers.addresses()
	}
	return []any{TailscaleDNSRecord{
		Record:           "dns",
		Nameservers:      found.Nameservers.addresses(),
		OverrideLocalDNS: found.Preferences.OverrideLocalDNS,
		MagicDNS:         found.Preferences.MagicDNS,
		SearchPaths:      nonNil(found.SearchPaths),
		SplitDNS:         splitDNS,
	}}, nil
}

func withheld(raw json.RawMessage) bool { return len(raw) == 0 || string(raw) == "null" }

func orNull(raw json.RawMessage) json.RawMessage {
	if withheld(raw) {
		return json.RawMessage("null")
	}
	return raw
}

func (r *Registry) tailscaleSettings(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[tailnetSettings](ctx, r, "settings", "settings", "feature_settings:read")
	if err != nil {
		return nil, err
	}
	for _, part := range []struct {
		key, name string
		raw       json.RawMessage
	}{{"httpsEnabled", "https", found.HTTPSEnabled}, {"aclsExternallyManagedOn", "acl_management", found.ACLsExternallyManagedOn}} {
		if withheld(part.raw) {
			refuse(ctx, part.name, "", "", &ProviderError{Message: fmt.Sprintf("Tailscale withheld %s.", part.key), Failure: runtime.FailureClassPermission, Refusal: runtime.RefusalPermission})
		}
	}
	return []any{TailscaleSettingsRecord{
		Record:                      "settings",
		DevicesApprovalOn:           orNull(found.DevicesApprovalOn),
		DevicesKeyDurationDays:      orNull(found.DevicesKeyDurationDays),
		DevicesAutoUpdatesOn:        orNull(found.DevicesAutoUpdatesOn),
		UsersApprovalOn:             orNull(found.UsersApprovalOn),
		RegionalRoutingOn:           orNull(found.RegionalRoutingOn),
		PostureIdentityCollectionOn: orNull(found.PostureIdentityCollectionOn),
		HTTPSEnabled:                orNull(found.HTTPSEnabled),
		ACLsExternallyManagedOn:     orNull(found.ACLsExternallyManagedOn),
	}}, nil
}

func (r *Registry) tailscaleUsers(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[tailnetUsers](ctx, r, "users", "users", "users:read")
	if err != nil {
		return nil, err
	}
	out := []any{}
	for _, user := range found.Users {
		if user.ID == "" {
			continue
		}
		out = append(out, TailscaleUserRecord{ID: user.ID, DisplayName: user.DisplayName, LoginName: user.LoginName, Role: user.Role, Status: user.Status, Created: user.Created, LastSeen: user.LastSeen})
	}
	return out, nil
}

func (r *Registry) tailscaleProbe(ctx context.Context, ref string) (ProbeResult, error) {
	if _, err := r.tailnetToken(ctx, ref); err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: "OAuth credential accepted.", Reaches: []string{}}, nil
}
