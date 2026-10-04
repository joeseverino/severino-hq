package providers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

// npmRecord is one record from an NPM list endpoint, field by field and
// undecoded. NPM is not consistent about types across releases (flags come
// as true or 1, ids as numbers or strings), so each field is read through an
// accessor below that states how it is interpreted.
type npmRecord map[string]json.RawMessage

// scalar decodes one field for the accessors; json.Number keeps ids exact.
func (n npmRecord) scalar(key string) any {
	raw, ok := n[key]
	if !ok || len(raw) == 0 {
		return nil
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&value) != nil {
		return nil
	}
	return value
}

// raw is the field verbatim, or null when absent.
func (n npmRecord) raw(key string) json.RawMessage {
	if value, ok := n[key]; ok && len(value) > 0 {
		return value
	}
	return json.RawMessage("null")
}

func (n npmRecord) present(key string) bool { return n.scalar(key) != nil }

// text is a string field as written; a number or flag as its literal.
func (n npmRecord) text(key string) string {
	switch value := n.scalar(key).(type) {
	case nil:
		return ""
	case string:
		return value
	default:
		return fmt.Sprint(value)
	}
}

// integer reports a whole-number field.
func (n npmRecord) integer(key string) bool {
	number, ok := n.scalar(key).(json.Number)
	if !ok {
		return false
	}
	_, err := number.Int64()
	return err == nil
}

// int reads a whole number, or a numeric string; anything else is 0.
func (n npmRecord) int(key string) int {
	switch value := n.scalar(key).(type) {
	case json.Number:
		i, _ := value.Int64()
		return int(i)
	case string:
		i, _ := strconv.Atoi(value)
		return i
	}
	return 0
}

// flag reads NPM's booleans: true/false, 1/0, "1"/"0"/"false", or a
// non-empty list or object.
func (n npmRecord) flag(key string) bool {
	switch value := n.scalar(key).(type) {
	case nil:
		return false
	case bool:
		return value
	case string:
		return value != "" && value != "0" && strings.ToLower(value) != "false"
	case json.Number:
		if i, err := value.Int64(); err == nil {
			return i != 0
		}
		f, _ := value.Float64()
		return f != 0
	case []any:
		return len(value) > 0
	case map[string]any:
		return len(value) > 0
	}
	return true
}

// flagOr is flag, with fallback for an absent or null field.
func (n npmRecord) flagOr(key string, fallback bool) bool {
	if !n.present(key) {
		return fallback
	}
	return n.flag(key)
}

// names reads a list of host names, each as text.
func (n npmRecord) names(key string) []string {
	out := []string{}
	list, _ := n.scalar(key).([]any)
	for _, item := range list {
		switch value := item.(type) {
		case nil:
			out = append(out, "")
		case string:
			out = append(out, value)
		default:
			out = append(out, fmt.Sprint(value))
		}
	}
	return out
}

// children decodes a nested list of records (access list clients and logins).
// An entry that is not an object reads as an empty record, so counts hold.
func (n npmRecord) children(key string) []npmRecord {
	var items []json.RawMessage
	if json.Unmarshal(n.raw(key), &items) != nil {
		return []npmRecord{}
	}
	out := make([]npmRecord, 0, len(items))
	for _, item := range items {
		var record npmRecord
		if json.Unmarshal(item, &record) != nil || record == nil {
			record = npmRecord{}
		}
		out = append(out, record)
	}
	return out
}

// canonical re-encodes a JSON value the way json.Marshal writes it, so two
// spellings of one value compare equal.
func canonical(raw json.RawMessage) string {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&value) != nil {
		return string(raw)
	}
	out, _ := json.Marshal(value)
	return string(out)
}

type npmTokenRequest struct {
	Identity string `json:"identity"`
	Secret   string `json:"secret"`
}
type npmTokenAnswer struct {
	Token string `json:"token"`
}

// npmProxyHostRequest is the body NPM takes to create or update a proxy host.
// CertificateID, Locations and Meta carry NPM's own values forward on update.
type npmProxyHostRequest struct {
	DomainNames           []string        `json:"domain_names"`
	CertificateID         json.RawMessage `json:"certificate_id"`
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

// NPMProxyHostRecord carries NPM's own values for the host verbatim; HQ
// compares and adopts them as NPM wrote them.
type NPMProxyHostRecord struct {
	DomainNames           json.RawMessage   `json:"domain_names"`
	ForwardScheme         json.RawMessage   `json:"forward_scheme"`
	ForwardHost           json.RawMessage   `json:"forward_host"`
	ForwardPort           json.RawMessage   `json:"forward_port"`
	SSLForced             json.RawMessage   `json:"ssl_forced"`
	HTTP2Support          json.RawMessage   `json:"http2_support"`
	AllowWebsocketUpgrade json.RawMessage   `json:"allow_websocket_upgrade"`
	CachingEnabled        json.RawMessage   `json:"caching_enabled"`
	BlockExploits         json.RawMessage   `json:"block_exploits"`
	AccessListID          json.RawMessage   `json:"access_list_id"`
	AdvancedConfig        json.RawMessage   `json:"advanced_config"`
	HSTSEnabled           json.RawMessage   `json:"hsts_enabled"`
	HSTSSubdomains        json.RawMessage   `json:"hsts_subdomains"`
	TrustForwardedProto   json.RawMessage   `json:"trust_forwarded_proto"`
	Enabled               json.RawMessage   `json:"enabled"`
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

func (r *Registry) admitNPM() {
	r.action("npm.proxy_host", "reconcile", r.npmReconcile)
	r.action("npm.proxy_host", "delete", r.npmDelete)
	r.reader("npm.proxy_host", r.npmInventory)
	r.reader("npm.certificate", npmEachConnection(r, r.npmCertificates, func(rec *NPMCertificateRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader("npm.redirect", npmEachConnection(r, r.npmRedirects, func(rec *NPMRedirectRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader("npm.dead_host", npmEachConnection(r, r.npmDeadHosts, func(rec *NPMDeadHostRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader("npm.stream", npmEachConnection(r, r.npmStreams, func(rec *NPMStreamRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader("npm.access_list", npmEachConnection(r, r.npmAccessLists, func(rec *NPMAccessListRecord, ref *string) { rec.ConnectionRef = ref }))
	r.probe("npm", func(ctx context.Context, ref string) (ProbeResult, error) {
		if _, _, err := r.npmSession(ctx, ref); err != nil {
			return ProbeResult{}, err
		}
		return ProbeResult{Detail: "Authenticated.", Reaches: []string{}}, nil
	})
}

// npmEachConnection gathers one kind across every NPM connection, stamping
// each record with the connection it came from.
func npmEachConnection[T any](r *Registry, read func(context.Context, string) ([]T, error), stamp func(*T, *string)) Reader {
	return func(ctx context.Context) ([]any, error) {
		found := []any{}
		for _, ref := range r.refs("npm") {
			records, err := read(ctx, ref)
			if err != nil {
				return nil, err
			}
			for i := range records {
				connection := ref
				stamp(&records[i], &connection)
				found = append(found, records[i])
			}
		}
		return found, nil
	}
}

func (r *Registry) npmSession(ctx context.Context, ref string) (string, map[string]string, error) {
	prefix, err := r.Env.Prefix("npm", ref)
	if err != nil {
		return "", nil, err
	}
	configured, err := r.Env.Required(prefix, "URL")
	if err != nil {
		return "", nil, err
	}
	base, err := url.Parse(strings.TrimRight(configured, "/"))
	if err != nil {
		return "", nil, &ProviderError{Message: "Invalid NPM API address."}
	}
	base.Path = strings.TrimRight(base.Path, "/")
	if !strings.HasSuffix(base.Path, "/api") {
		base.Path += "/api"
	}
	raw, err := r.cached(ctx, "npm-token:"+base.String()+":"+prefix, func() (json.RawMessage, error) {
		user, err := r.Env.Required(prefix, "USERNAME")
		if err != nil {
			return nil, err
		}
		password, err := r.Env.Required(prefix, "PASSWORD")
		if err != nil {
			return nil, err
		}
		answer, err := r.HTTP.Request(ctx, base.String()+"/tokens", "POST", nil, npmTokenRequest{Identity: user, Secret: password})
		if err != nil {
			return nil, err
		}
		token, _ := decodeAs[npmTokenAnswer](answer, "")
		if token.Token == "" {
			return nil, &ProviderError{Message: "NPM authentication did not return a token."}
		}
		return json.Marshal(token.Token)
	})
	if err != nil {
		return "", nil, err
	}
	var token string
	_ = json.Unmarshal(raw, &token)
	return base.String(), map[string]string{"Authorization": "Bearer " + token}, nil
}

type npmSource struct {
	path  string
	needs string
	what  string
}

var (
	npmProxyHosts         = npmSource{path: "/nginx/proxy-hosts", needs: "proxy_hosts: view", what: "The proxy host list"}
	npmRedirectionHosts   = npmSource{path: "/nginx/redirection-hosts", needs: "redirection_hosts: view", what: "The redirection host list"}
	npmDeadHostsSource    = npmSource{path: "/nginx/dead-hosts", needs: "dead_hosts: view", what: "The 404 host list"}
	npmStreamsSource      = npmSource{path: "/nginx/streams", needs: "streams: view", what: "The stream list"}
	npmCertificatesSource = npmSource{path: "/nginx/certificates", needs: "certificates: view", what: "The certificate list"}
	npmAccessListsSource  = npmSource{path: "/nginx/access-lists?expand=items,clients", needs: "access_lists: view", what: "The access list list"}
)

func npmRefused(err error, what, needs string) error {
	var provider *ProviderError
	if errors.As(err, &provider) {
		if provider.Refusal == "credential" || provider.Failure == "credential" {
			return &ProviderError{
				Message: what + ": the credential was refused.",
				Failure: "credential",
				Refusal: "credential",
				Reason:  "The credential was refused.",
			}
		}
		if provider.Refusal == "permission" || provider.Failure == "permission" {
			return &ProviderError{
				Message: what + " needs " + needs + ".",
				Failure: "permission",
				Refusal: "permission",
			}
		}
		return provider
	}
	return &ProviderError{Message: what + " failed."}
}

// npmRecords decodes an NPM list answer.
func npmRecords(raw json.RawMessage) ([]npmRecord, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return []npmRecord{}, nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil {
		return nil, &ProviderError{Message: "Provider returned an invalid record list."}
	}
	out := make([]npmRecord, 0, len(items))
	for _, item := range items {
		var record npmRecord
		if json.Unmarshal(item, &record) != nil || record == nil {
			return nil, &ProviderError{Message: "Provider returned an invalid record."}
		}
		out = append(out, record)
	}
	return out, nil
}

func (r *Registry) npmListed(ctx context.Context, ref string, src npmSource) ([]npmRecord, error) {
	raw, err := r.cached(ctx, "npm-list:"+ref+":"+src.path, func() (json.RawMessage, error) {
		base, headers, err := r.npmSession(ctx, ref)
		if err != nil {
			return nil, err
		}
		found, err := r.HTTP.Request(ctx, base+src.path, "GET", headers, nil)
		if err != nil {
			return nil, npmRefused(err, src.what, src.needs)
		}
		if _, err := npmRecords(found); err != nil {
			return nil, err
		}
		return found, nil
	})
	if err != nil {
		return nil, err
	}
	return npmRecords(raw)
}

func (r *Registry) npmProxyHostList(ctx context.Context, base string, headers map[string]string) ([]npmRecord, error) {
	raw, err := r.HTTP.Request(ctx, base+"/nginx/proxy-hosts", "GET", headers, nil)
	if err != nil {
		return nil, err
	}
	return npmRecords(raw)
}

func sortedNames(names []string) []string {
	out := append([]string{}, names...)
	sort.Strings(out)
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func matchingHosts(hosts []npmRecord, domains []string) []npmRecord {
	out := []npmRecord{}
	for _, host := range hosts {
		if equalStrings(sortedNames(host.names("domain_names")), domains) {
			out = append(out, host)
		}
	}
	return out
}

func npmDesired(spec NPMProxyHostSpec, domains []string) npmProxyHostRequest {
	certificate, _ := json.Marshal(spec.CertificateID)
	return npmProxyHostRequest{
		DomainNames:           domains,
		CertificateID:         certificate,
		Locations:             json.RawMessage("[]"),
		Meta:                  json.RawMessage("{}"),
		ForwardScheme:         spec.ForwardScheme,
		ForwardHost:           spec.ForwardHost,
		ForwardPort:           spec.ForwardPort,
		CachingEnabled:        spec.CachingEnabled,
		BlockExploits:         spec.BlockExploits,
		AccessListID:          spec.AccessListID,
		HSTSEnabled:           spec.HSTSEnabled,
		HSTSSubdomains:        spec.HSTSSubdomains,
		TrustForwardedProto:   spec.TrustForwardedProto,
		AdvancedConfig:        spec.AdvancedConfig,
		AllowWebsocketUpgrade: spec.Websocket,
		SSLForced:             spec.ForceSSL,
		HTTP2Support:          spec.HTTP2,
		Enabled:               spec.Serving,
	}
}

// npmDiffers compares the desired body with what NPM holds, field by field
// as JSON, so a value NPM writes differently (1 for true) counts as a change.
func npmDiffers(current npmRecord, desired npmProxyHostRequest) bool {
	body, _ := json.Marshal(desired)
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(body, &fields)
	for key, want := range fields {
		if canonical(current.raw(key)) != canonical(want) {
			return true
		}
	}
	return false
}

func (r *Registry) npmReconcile(ctx context.Context, rawSpec, rawObserved Object, apply bool) (Result, error) {
	spec, err := decodePayload[NPMProxyHostSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
	observed, _ := decodePayload[NPMProxyHostObserved](rawObserved)
	base, headers, err := r.npmSession(ctx, namedRef(rawSpec))
	if err != nil {
		return Result{}, err
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return Result{}, err
	}
	domains := sortedNames(spec.DomainNames)
	matches := matchingHosts(hosts, domains)
	if len(matches) == 0 {
		previous := sortedNames(observed.DomainNames)
		if len(previous) > 0 && !equalStrings(previous, domains) {
			matches = matchingHosts(hosts, previous)
		}
	}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: "NPM contains duplicate proxy hosts for the domain set."}
	}
	desired := npmDesired(spec, domains)
	changed := true
	path, method := "/nginx/proxy-hosts", "POST"
	if len(matches) == 1 {
		current := matches[0]
		if spec.ForceSSL && current.int("certificate_id") == 0 {
			return Result{}, &ProviderError{Message: "The proxy host forces HTTPS but has no certificate. Attach one, then reconcile."}
		}
		if spec.CertificateID == 0 {
			desired.CertificateID = json.RawMessage("0")
			if current.present("certificate_id") {
				desired.CertificateID = current.raw("certificate_id")
			}
		}
		if _, ok := current["locations"]; ok {
			desired.Locations = current.raw("locations")
		}
		if _, ok := current["meta"]; ok {
			desired.Meta = current.raw("meta")
		}
		changed = npmDiffers(current, desired)
		path += "/" + current.text("id")
		method = "PUT"
	} else if spec.ForceSSL && spec.CertificateID == 0 {
		return Result{}, &ProviderError{Message: "An HTTPS proxy host needs an issued certificate. None is set yet."}
	}
	if changed && apply {
		if _, err := r.HTTP.Request(ctx, base+path, method, headers, desired); err != nil {
			return Result{}, err
		}
	}
	message := "NPM proxy host unchanged."
	if changed {
		message = "NPM proxy host updated."
	}
	return result(changed, NPMProxyHostStatus{
		DomainNames: domains,
		Forward:     fmt.Sprintf("%s://%s:%d", spec.ForwardScheme, spec.ForwardHost, spec.ForwardPort),
	}, "Reconciled", "NPM proxy host is current.", message), nil
}

func (r *Registry) npmDelete(ctx context.Context, rawSpec, _ Object, apply bool) (Result, error) {
	spec, err := decodePayload[NPMProxyHostSpec](rawSpec)
	if err != nil {
		return Result{}, err
	}
	base, headers, err := r.npmSession(ctx, namedRef(rawSpec))
	if err != nil {
		return Result{}, err
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return Result{}, err
	}
	domains := sortedNames(spec.DomainNames)
	matches := matchingHosts(hosts, domains)
	status := NPMProxyHostDeleteStatus{DomainNames: domains, Removed: true}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: "NPM contains duplicate proxy hosts for the domain set."}
	}
	if len(matches) == 0 {
		return result(false, status, "Absent", "No such proxy host in NPM.", "NPM proxy host was already absent."), nil
	}
	if apply {
		if _, err := r.HTTP.Request(ctx, base+"/nginx/proxy-hosts/"+matches[0].text("id"), "DELETE", headers, nil); err != nil {
			return Result{}, err
		}
	}
	return result(true, status, "Removed", "NPM proxy host was removed.", "NPM proxy host removed."), nil
}

func npmClients(record npmRecord) []NPMAccessRule {
	clients := []NPMAccessRule{}
	for _, rule := range record.children("clients") {
		directive, address := rule.text("directive"), rule.text("address")
		if directive != "" && address != "" {
			clients = append(clients, NPMAccessRule{Directive: directive, Address: address})
		}
	}
	return clients
}

func (r *Registry) npmInventory(ctx context.Context) ([]any, error) {
	base, headers, err := r.npmSession(ctx, "")
	if err != nil {
		return nil, err
	}
	hosts, err := r.npmProxyHostList(ctx, base, headers)
	if err != nil {
		return nil, err
	}
	policies := map[string]*NPMAccessPolicy{}
	certificates := map[string]*NPMCertificateSummary{}
	if raw, err := r.HTTP.Request(ctx, base+"/nginx/access-lists?expand=items,clients", "GET", headers, nil); err == nil {
		items, _ := npmRecords(raw)
		for _, item := range items {
			if !item.integer("id") {
				continue
			}
			clients := npmClients(item)
			policies[item.text("id")] = &NPMAccessPolicy{
				Name:               item.text("name"),
				SatisfyAny:         item.flag("satisfy_any"),
				PassAuth:           item.flag("pass_auth"),
				AuthorizationCount: len(item.children("items")),
				Clients:            clients,
				ImplicitDeny:       len(clients) > 0,
			}
		}
	}
	if raw, err := r.HTTP.Request(ctx, base+"/nginx/certificates", "GET", headers, nil); err == nil {
		items, _ := npmRecords(raw)
		for _, item := range items {
			if item.flag("id") {
				certificates[item.text("id")] = &NPMCertificateSummary{
					Name:      item.text("nice_name"),
					Domains:   item.names("domain_names"),
					ExpiresOn: item.text("expires_on"),
					Provider:  item.text("provider"),
				}
			}
		}
	}
	found := []any{}
	for _, host := range hosts {
		if !host.flag("domain_names") {
			continue
		}
		found = append(found, NPMProxyHostRecord{
			DomainNames:           host.raw("domain_names"),
			ForwardScheme:         host.raw("forward_scheme"),
			ForwardHost:           host.raw("forward_host"),
			ForwardPort:           host.raw("forward_port"),
			SSLForced:             host.raw("ssl_forced"),
			HTTP2Support:          host.raw("http2_support"),
			AllowWebsocketUpgrade: host.raw("allow_websocket_upgrade"),
			CachingEnabled:        host.raw("caching_enabled"),
			BlockExploits:         host.raw("block_exploits"),
			AccessListID:          host.raw("access_list_id"),
			AdvancedConfig:        host.raw("advanced_config"),
			HSTSEnabled:           host.raw("hsts_enabled"),
			HSTSSubdomains:        host.raw("hsts_subdomains"),
			TrustForwardedProto:   host.raw("trust_forwarded_proto"),
			Enabled:               host.raw("enabled"),
			Certificate:           npmCertificateRef{certificates[host.text("certificate_id")]},
			AccessPolicy:          policies[host.text("access_list_id")],
		})
	}
	return found, nil
}

func npmNames(values []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, value := range values {
		name := hostname(value)
		if name != "" && !seen[name] {
			seen[name] = true
			out = append(out, name)
		}
	}
	return out
}

func (r *Registry) npmCertificateNames(ctx context.Context, ref string) map[int]string {
	names := map[int]string{}
	certs, err := r.npmListed(ctx, ref, npmCertificatesSource)
	if err != nil {
		return names
	}
	for _, item := range certs {
		if item.integer("id") {
			names[item.int("id")] = item.text("nice_name")
		}
	}
	return names
}

func (r *Registry) npmServes(ctx context.Context, ref string) map[int][]string {
	serves := map[int][]string{}
	servingSources := []struct {
		src  npmSource
		part string
	}{
		{npmProxyHosts, "proxy_hosts"},
		{npmRedirectionHosts, "redirection_hosts"},
		{npmDeadHostsSource, "dead_hosts"},
		{npmStreamsSource, "streams"},
	}
	for _, s := range servingSources {
		hosts, err := r.npmListed(ctx, ref, s.src)
		if err != nil {
			refuse(ctx, s.part, ref, "", err)
			continue
		}
		for _, host := range hosts {
			certID := host.int("certificate_id")
			if certID > 0 && host.flagOr("enabled", true) {
				serves[certID] = append(serves[certID], host.names("domain_names")...)
			}
		}
	}
	return serves
}

func (r *Registry) npmCertificates(ctx context.Context, ref string) ([]NPMCertificateRecord, error) {
	listedCerts, err := r.npmListed(ctx, ref, npmCertificatesSource)
	if err != nil {
		return nil, err
	}
	serves := r.npmServes(ctx, ref)
	out := []NPMCertificateRecord{}
	for _, item := range listedCerts {
		if !item.integer("id") {
			continue
		}
		id := item.int("id")
		out = append(out, NPMCertificateRecord{
			ID:        id,
			Name:      item.text("nice_name"),
			Provider:  item.text("provider"),
			Domains:   item.names("domain_names"),
			ExpiresOn: item.text("expires_on"),
			Serves:    npmNames(serves[id]),
		})
	}
	return out, nil
}

func npmTarget(scheme, host string) string {
	if host == "" {
		return ""
	}
	if scheme == "http" || scheme == "https" {
		return scheme + "://" + host
	}
	return host
}

// optionalInt is a whole-number field, or nil when it is absent or not whole.
func optionalInt(record npmRecord, key string) *int {
	if !record.integer(key) {
		return nil
	}
	value := record.int(key)
	return &value
}

func (r *Registry) npmRedirects(ctx context.Context, ref string) ([]NPMRedirectRecord, error) {
	names := r.npmCertificateNames(ctx, ref)
	hosts, err := r.npmListed(ctx, ref, npmRedirectionHosts)
	if err != nil {
		return nil, err
	}
	out := []NPMRedirectRecord{}
	for _, item := range hosts {
		if !item.integer("id") {
			continue
		}
		out = append(out, NPMRedirectRecord{
			ID:           item.int("id"),
			Hostnames:    npmNames(item.names("domain_names")),
			Target:       npmTarget(item.text("forward_scheme"), item.text("forward_domain_name")),
			TargetHost:   hostname(item.text("forward_domain_name")),
			StatusCode:   optionalInt(item, "forward_http_code"),
			PreservePath: item.flag("preserve_path"),
			SSLForced:    item.flag("ssl_forced"),
			Certificate:  names[item.int("certificate_id")],
			Enabled:      item.flagOr("enabled", true),
		})
	}
	return out, nil
}

func (r *Registry) npmDeadHosts(ctx context.Context, ref string) ([]NPMDeadHostRecord, error) {
	names := r.npmCertificateNames(ctx, ref)
	hosts, err := r.npmListed(ctx, ref, npmDeadHostsSource)
	if err != nil {
		return nil, err
	}
	out := []NPMDeadHostRecord{}
	for _, item := range hosts {
		if !item.integer("id") {
			continue
		}
		out = append(out, NPMDeadHostRecord{
			ID:          item.int("id"),
			Hostnames:   npmNames(item.names("domain_names")),
			Certificate: names[item.int("certificate_id")],
			SSLForced:   item.flag("ssl_forced"),
			Enabled:     item.flagOr("enabled", true),
		})
	}
	return out, nil
}

func (r *Registry) npmStreams(ctx context.Context, ref string) ([]NPMStreamRecord, error) {
	hosts, err := r.npmListed(ctx, ref, npmStreamsSource)
	if err != nil {
		return nil, err
	}
	out := []NPMStreamRecord{}
	for _, item := range hosts {
		if !item.integer("id") || !item.integer("incoming_port") {
			continue
		}
		out = append(out, NPMStreamRecord{
			ID:             item.int("id"),
			IncomingPort:   item.int("incoming_port"),
			ForwardingHost: item.text("forwarding_host"),
			ForwardingPort: optionalInt(item, "forwarding_port"),
			TCP:            item.flag("tcp_forwarding"),
			UDP:            item.flag("udp_forwarding"),
			Enabled:        item.flagOr("enabled", true),
		})
	}
	return out, nil
}

func (r *Registry) npmAccessLists(ctx context.Context, ref string) ([]NPMAccessListRecord, error) {
	listedLists, err := r.npmListed(ctx, ref, npmAccessListsSource)
	if err != nil {
		return nil, err
	}
	protects := map[int][]string{}
	proxyHosts, err := r.npmListed(ctx, ref, npmProxyHosts)
	if err != nil {
		refuse(ctx, "proxy_hosts", ref, "", err)
	} else {
		for _, host := range proxyHosts {
			if id := host.int("access_list_id"); id > 0 {
				protects[id] = append(protects[id], host.names("domain_names")...)
			}
		}
	}
	out := []NPMAccessListRecord{}
	for _, item := range listedLists {
		if !item.integer("id") {
			continue
		}
		id := item.int("id")
		logins := []string{}
		for _, login := range item.children("items") {
			if name := login.text("username"); name != "" {
				logins = append(logins, name)
			}
		}
		out = append(out, NPMAccessListRecord{
			ID:         id,
			Name:       item.text("name"),
			SatisfyAny: item.flag("satisfy_any"),
			PassAuth:   item.flag("pass_auth"),
			Clients:    npmClients(item),
			Logins:     logins,
			Protects:   npmNames(protects[id]),
		})
	}
	return out, nil
}
