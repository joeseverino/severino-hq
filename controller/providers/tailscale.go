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
	"time"

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

var jsonBody = map[string]string{"Content-Type": "application/json"}

// tailnet is a path under the tailnet this credential belongs to.
func tailnet(path string) string { return "/tailnet/-/" + path }

// tailnetDevicePath is a path under one device.
func tailnetDevicePath(identifier, tail string) string {
	return "/device/" + url.PathEscape(identifier) + "/" + tail
}

// tailnetNode is one node of the local daemon's status (Self, or a Peer). The
// official API client does not model the daemon's local API.
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

type tailnetToken struct {
	AccessToken string `json:"access_token"`
}

// tailnetResolver is one nameserver: Tailscale writes it as an address string
// or as the official resolver object.
type tailnetResolver tsapi.DNSConfigurationResolver

func (r *tailnetResolver) UnmarshalJSON(data []byte) error {
	var address string
	if json.Unmarshal(data, &address) == nil {
		*r = tailnetResolver{Address: address}
		return nil
	}
	var resolver tsapi.DNSConfigurationResolver
	if err := json.Unmarshal(data, &resolver); err != nil {
		return fmt.Errorf("a resolver is an address or an object: %w", err)
	}
	*r = tailnetResolver(resolver)
	return nil
}

func resolverAddresses(list []tailnetResolver) []string {
	found := []string{}
	for _, resolver := range list {
		if resolver.Address != "" {
			found = append(found, resolver.Address)
		}
	}
	return found
}

// tailnetDNSConfiguration is tsapi.DNSConfiguration with resolvers that accept
// either shape.
type tailnetDNSConfiguration struct {
	Nameservers []tailnetResolver                 `json:"nameservers"`
	Preferences tsapi.DNSConfigurationPreferences `json:"preferences"`
	SearchPaths []string                          `json:"searchPaths"`
	SplitDNS    map[string][]tailnetResolver      `json:"splitDNS"`
}

// tailnetSettings is tsapi.TailnetSettings with every field optional: a
// setting the credential may not see is withheld (null or absent), not false.
type tailnetSettings struct {
	DevicesApprovalOn           *bool `json:"devicesApprovalOn"`
	DevicesKeyDurationDays      *int  `json:"devicesKeyDurationDays"`
	DevicesAutoUpdatesOn        *bool `json:"devicesAutoUpdatesOn"`
	UsersApprovalOn             *bool `json:"usersApprovalOn"`
	RegionalRoutingOn           *bool `json:"regionalRoutingOn"`
	PostureIdentityCollectionOn *bool `json:"postureIdentityCollectionOn"`
	HTTPSEnabled                *bool `json:"httpsEnabled"`
	ACLsExternallyManagedOn     *bool `json:"aclsExternallyManagedOn"`
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

// TailscaleSettingsRecord is the tailnet's settings; a withheld one is null.
type TailscaleSettingsRecord struct {
	Record                      string `json:"record"`
	DevicesApprovalOn           *bool  `json:"devices_approval_on"`
	DevicesKeyDurationDays      *int   `json:"devices_key_duration_days"`
	DevicesAutoUpdatesOn        *bool  `json:"devices_auto_updates_on"`
	UsersApprovalOn             *bool  `json:"users_approval_on"`
	RegionalRoutingOn           *bool  `json:"regional_routing_on"`
	PostureIdentityCollectionOn *bool  `json:"posture_identity_collection_on"`
	HTTPSEnabled                *bool  `json:"https_enabled"`
	ACLsExternallyManagedOn     *bool  `json:"acls_externally_managed_on"`
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
			reason := fmt.Sprintf("tailscale refused the credential for %s (%d): it must be an OAuth client, not an API key", connectionRef, code)
			return nil, &ProviderError{Message: reason, Failure: runtime.FailureClassCredential, Reason: reason, HTTPStatus: code}
		}
		if err != nil {
			return nil, fmt.Errorf("tailscale token request: %w", err)
		}
		token, err := decodeTailnet[tailnetToken](answer, "tailscale token answer")
		if err != nil {
			return nil, err
		}
		if token.AccessToken == "" {
			return nil, &ProviderError{Message: "tailscale returned no access token"}
		}
		return json.Marshal(token.AccessToken)
	})
	if err != nil {
		return "", err
	}
	var token string
	if err := json.Unmarshal(raw, &token); err != nil {
		return "", err
	}
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

// decodeTailnet decodes one answer strictly: an empty answer or one of the
// wrong shape is an error naming what was read.
func decodeTailnet[T any](raw json.RawMessage, what string) (T, error) {
	var target T
	if len(raw) == 0 || string(raw) == "null" {
		return target, &ProviderError{Message: what + " is empty"}
	}
	if err := json.Unmarshal(raw, &target); err != nil {
		return target, &ProviderError{Message: what + " is unreadable", Err: err}
	}
	return target, nil
}

// tailnetRefused classifies one refused tailnet request. The token was just
// exchanged, so 403, and 404 on some endpoints, is a missing scope; 401 is the
// token refused.
func tailnetRefused(what, scope string, code int) error {
	switch code {
	case 403, 404:
		return &ProviderError{
			Message:    fmt.Sprintf("tailscale refused %s (%d): the credential needs the %s scope", what, code, scope),
			Failure:    runtime.FailureClassPermission,
			HTTPStatus: code,
		}
	case 401:
		return &ProviderError{
			Message:    fmt.Sprintf("tailscale refused %s (%d)", what, code),
			Failure:    runtime.FailureClassCredential,
			Reason:     fmt.Sprintf("tailscale refused the access token (%d)", code),
			HTTPStatus: code,
		}
	}
	return &ProviderError{Message: fmt.Sprintf("tailscale refused %s (%d)", what, code), HTTPStatus: code}
}

// tailnetGet reads one endpoint of the client's tailnet into its response
// type. A refusal names the scope the read needs.
func tailnetGet[T any](ctx context.Context, c tailnetClient, path, what, scope string) (T, error) {
	raw, err := c.call(ctx, "GET", tailnet(path), map[string]string{"Accept": "application/json"}, nil)
	if code := httpStatus(err); code != 0 {
		var zero T
		return zero, tailnetRefused(what, scope, code)
	}
	if err != nil {
		var zero T
		return zero, fmt.Errorf("read %s: %w", what, err)
	}
	return decodeTailnet[T](raw, what)
}

// tailnetRead is tailnetGet through the default tailscale connection.
func tailnetRead[T any](ctx context.Context, r *Registry, path, what, scope string) (T, error) {
	client, err := r.tailnetClient(ctx, "")
	if err != nil {
		var zero T
		return zero, err
	}
	return tailnetGet[T](ctx, client, path, what, scope)
}

func (c tailnetClient) tailnetAPIDevices(ctx context.Context) ([]tsapi.Device, error) {
	answer, err := tailnetGet[struct {
		Devices []tsapi.Device `json:"devices"`
	}](ctx, c, "devices?fields=all", "the tailnet device list", "devices:core:read")
	if err != nil {
		return nil, err
	}
	return answer.Devices, nil
}

// errNoTailnetReading is a controller started without the local daemon's status.
var errNoTailnetReading = &ProviderError{Message: "this controller was not given a tailnet reading"}

// readTailnetNodes reads the tailnet status file: Self first, then each Peer
// in the order the file lists them.
func (r *Registry) readTailnetNodes() ([]tailnetNode, error) {
	statusFile := r.Env["SEVERINO_TAILNET_STATUS"]
	if statusFile == "" {
		return nil, errNoTailnetReading
	}
	data, err := os.ReadFile(statusFile)
	if err != nil {
		return nil, &ProviderError{Message: "the tailnet reading is missing; it is taken from the local daemon before this container starts", Err: err}
	}
	var status struct {
		Self *tailnetNode    `json:"Self"`
		Peer json.RawMessage `json:"Peer"`
	}
	if err := json.Unmarshal(data, &status); err != nil {
		return nil, &ProviderError{Message: "the tailnet reading is not readable status", Err: err}
	}
	nodes := []tailnetNode{}
	if status.Self != nil {
		nodes = append(nodes, *status.Self)
	}
	peers, err := orderedValues[tailnetNode](status.Peer)
	if err != nil {
		return nil, &ProviderError{Message: "the tailnet reading is not readable status", Err: err}
	}
	return append(nodes, peers...), nil
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

// stamp is a moment as a record carries it: RFC 3339 in UTC, or "" for none.
func stamp(moment time.Time) string {
	if moment.IsZero() {
		return ""
	}
	return moment.UTC().Format(time.RFC3339)
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

func apiDeviceRecord(device tsapi.Device) (TailscaleDeviceRecord, bool) {
	name := strings.TrimSpace(device.Hostname)
	if name == "" {
		return TailscaleDeviceRecord{}, false
	}
	keyExpires := stamp(device.Expires.Time)
	if device.KeyExpiryDisabled {
		keyExpires = ""
	}
	lastSeen := ""
	if device.LastSeen != nil {
		lastSeen = stamp(device.LastSeen.Time)
	}
	endpoints := []string{}
	if device.ClientConnectivity != nil {
		endpoints = nonNil(device.ClientConnectivity.Endpoints)
	}
	return TailscaleDeviceRecord{
		Name:       name,
		PublicKey:  device.NodeKey,
		DNSName:    strings.TrimRight(device.Name, "."),
		Online:     device.ConnectedToControl,
		LastSeen:   lastSeen,
		KeyExpires: keyExpires,
		Addresses:  nonNil(device.Addresses),
		OS:         device.OS,
		Endpoints:  endpoints,
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

func identitiesFrom(devices []tsapi.Device) map[string]tsapi.Device {
	found := map[string]tsapi.Device{}
	for _, device := range devices {
		if device.Hostname != "" {
			found[device.Hostname] = device
		}
	}
	return found
}

// localTailnetDevices is the tailnet as the local daemon sees it; the first
// record is this machine.
func (r *Registry) localTailnetDevices() ([]TailscaleDeviceRecord, error) {
	nodes, err := r.readTailnetNodes()
	if err != nil {
		return nil, err
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

// tailscaleDeviceInventory reads the tailnet from the local daemon when this
// controller has its status, else from the API. The API, when it answers,
// adds each device's identity and routes either way.
func (r *Registry) tailscaleDeviceInventory(ctx context.Context) ([]any, error) {
	devices := []TailscaleDeviceRecord{}
	identities := map[string]tsapi.Device{}
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
			return nil, fmt.Errorf("no tailnet reading and no tailnet credential: %w", err)
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
		device.LockError = identity.TailnetLockError
		device.UpdateAvailable = identity.UpdateAvailable
		device.ClientVersion = identity.ClientVersion
		device.SSHEnabled = identity.SSHEnabled
		device.BlocksIncoming = identity.BlocksIncomingConnections
		device.External = identity.IsExternal
		if known {
			disabled := identity.KeyExpiryDisabled
			device.KeyExpiryDisabled = &disabled
			device.Authorized = identity.Authorized
			device.OffersExitNode = exitRoute(identity.AdvertisedRoutes)
			device.ExitNodeApproved = exitRoute(identity.EnabledRoutes)
		} else {
			// The local daemon lists only devices the tailnet admitted.
			device.Authorized = true
			device.ExitNodeApproved = device.OffersExitNode
		}
		found = append(found, device)
	}
	return found, nil
}

func noDevice(name string) error {
	return &ProviderError{Message: fmt.Sprintf("no device called %q is on the tailnet this machine can see", name)}
}

// tailnetDeviceID is the device's stable id, from the local reading rather than the API.
func (r *Registry) tailnetDeviceID(name string) (string, error) {
	nodes, err := r.readTailnetNodes()
	if err != nil {
		return "", err
	}
	for _, node := range nodes {
		if strings.TrimSpace(node.HostName) == name && node.ID != "" {
			return node.ID, nil
		}
	}
	return "", noDevice(name)
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
	return TailnetDeviceStatus{}, noDevice(name)
}

// tailnetWriteFailed classifies a refused or unanswered device change.
func tailnetWriteFailed(what, scope string, err error) error {
	switch code := httpStatus(err); {
	case code == 401 || code == 403:
		return &ProviderError{
			Message:    fmt.Sprintf("tailscale refused %s (%d): the credential needs the %s scope", what, code, scope),
			Failure:    runtime.StatusFailure(code),
			HTTPStatus: code,
		}
	case code != 0:
		return &ProviderError{Message: fmt.Sprintf("tailscale refused %s (%d)", what, code), HTTPStatus: code}
	}
	return fmt.Errorf("%s: %w", what, err)
}

func (r *Registry) tailscaleDeviceReconcile(ctx context.Context, spec TailnetDeviceSpec, _ struct{}, apply bool) (Result, error) {
	name, wanted := spec.Name, spec.KeyExpiryDisabled
	current, err := r.tailnetDeviceState(name)
	if err != nil {
		return Result{}, err
	}
	if current.KeyExpiryDisabled == wanted {
		return result(false, current, "Reconciled", "The device is as declared.", "Tailnet device is current."), nil
	}
	if !apply {
		verb := "enabled"
		if wanted {
			verb = "disabled"
		}
		return Result{Changed: true, Status: current, Message: fmt.Sprintf("Key expiry would be %s for %s.", verb, name)}, nil
	}
	identifier, err := r.tailnetDeviceID(name)
	if err != nil {
		return Result{}, err
	}
	client, err := r.tailnetClient(ctx, spec.ConnectionRef)
	if err != nil {
		return Result{}, err
	}
	if _, err := client.call(ctx, "POST", tailnetDevicePath(identifier, "key"), jsonBody, tsapi.DeviceKey{KeyExpiryDisabled: wanted}); err != nil {
		return Result{}, tailnetWriteFailed("the key change for "+name, "devices:core", err)
	}
	status := TailnetDeviceStatus{Name: name, Online: current.Online, KeyExpiryDisabled: wanted}
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
	if err != nil {
		return Result{}, tailnetWriteFailed("the route read for "+name, "devices:routes:read", err)
	}
	current, err := decodeTailnet[tsapi.DeviceRoutes](raw, "the routes of "+name)
	if err != nil {
		return Result{}, err
	}
	advertised := sortedCopy(current.Advertised)
	enabled := sortedCopy(current.Enabled)
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
	answer, err := client.call(ctx, "POST", routesPath, jsonBody, map[string][]string{"routes": advertised})
	if err != nil {
		return Result{}, tailnetWriteFailed("the route approval for "+name, "devices:routes", err)
	}
	approved, err := decodeTailnet[tsapi.DeviceRoutes](answer, "the approved routes of "+name)
	if err != nil {
		return Result{}, err
	}
	status.EnabledRoutes = sortedCopy(approved.Enabled)
	return result(true, status, "Reconciled", "The advertised routes are approved.", fmt.Sprintf("Approved %s for %s.", strings.Join(pending, ", "), name)), nil
}

func (r *Registry) tailscaleDNS(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[tailnetDNSConfiguration](ctx, r, "dns/configuration", "the DNS configuration", "dns:read")
	if err != nil {
		return nil, err
	}
	splitDNS := map[string][]string{}
	for domain, resolvers := range found.SplitDNS {
		splitDNS[domain] = resolverAddresses(resolvers)
	}
	return []any{TailscaleDNSRecord{
		Record:           "dns",
		Nameservers:      resolverAddresses(found.Nameservers),
		OverrideLocalDNS: found.Preferences.OverrideLocalDNS,
		MagicDNS:         found.Preferences.MagicDNS,
		SearchPaths:      nonNil(found.SearchPaths),
		SplitDNS:         splitDNS,
	}}, nil
}

func (r *Registry) tailscaleSettings(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[tailnetSettings](ctx, r, "settings", "the settings", "feature_settings:read")
	if err != nil {
		return nil, err
	}
	for _, part := range []struct {
		key, name string
		value     *bool
	}{{"httpsEnabled", "https", found.HTTPSEnabled}, {"aclsExternallyManagedOn", "acl_management", found.ACLsExternallyManagedOn}} {
		if part.value == nil {
			refuse(ctx, part.name, "", "", &ProviderError{Message: "tailscale withheld " + part.key, Failure: runtime.FailureClassPermission})
		}
	}
	return []any{TailscaleSettingsRecord{
		Record:                      "settings",
		DevicesApprovalOn:           found.DevicesApprovalOn,
		DevicesKeyDurationDays:      found.DevicesKeyDurationDays,
		DevicesAutoUpdatesOn:        found.DevicesAutoUpdatesOn,
		UsersApprovalOn:             found.UsersApprovalOn,
		RegionalRoutingOn:           found.RegionalRoutingOn,
		PostureIdentityCollectionOn: found.PostureIdentityCollectionOn,
		HTTPSEnabled:                found.HTTPSEnabled,
		ACLsExternallyManagedOn:     found.ACLsExternallyManagedOn,
	}}, nil
}

func (r *Registry) tailscaleUsers(ctx context.Context) ([]any, error) {
	found, err := tailnetRead[struct {
		Users []tsapi.User `json:"users"`
	}](ctx, r, "users", "the users", "users:read")
	if err != nil {
		return nil, err
	}
	out := []any{}
	for _, user := range found.Users {
		if user.ID == "" {
			continue
		}
		out = append(out, TailscaleUserRecord{
			ID: user.ID, DisplayName: user.DisplayName, LoginName: user.LoginName,
			Role: string(user.Role), Status: string(user.Status),
			Created: stamp(user.Created), LastSeen: stamp(user.LastSeen),
		})
	}
	return out, nil
}

func (r *Registry) tailscaleProbe(ctx context.Context, ref string) (ProbeResult, error) {
	if _, err := r.tailnetToken(ctx, ref); err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: "OAuth credential accepted.", Reaches: []string{}}, nil
}
