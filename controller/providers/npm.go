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

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// npmRecord is one record from an NPM list endpoint, field by field and
// undecoded. NPM is not consistent about types across releases (flags come
// as true or 1, ids as numbers or strings), so each field is read through an
// accessor below that states how it is interpreted. The field names are the
// ones the vendored spec gives the record (npm_spec_test.go holds them to it).
type npmRecord map[string]json.RawMessage

// npmKind is the JSON type of one field.
type npmKind int

const (
	npmAbsent npmKind = iota
	npmString
	npmNumber
	npmBool
	npmList
	npmObject
)

// kindOf classifies a field's JSON text; null reads as absent.
func kindOf(raw json.RawMessage) npmKind {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 {
		return npmAbsent
	}
	switch raw[0] {
	case '"':
		return npmString
	case 't', 'f':
		return npmBool
	case '[':
		return npmList
	case '{':
		return npmObject
	case 'n':
		return npmAbsent
	}
	return npmNumber
}

// raw is the field verbatim, or null when absent.
func (n npmRecord) raw(key string) json.RawMessage {
	if value, ok := n[key]; ok && len(value) > 0 {
		return value
	}
	return json.RawMessage("null")
}

func (n npmRecord) kind(key string) npmKind { return kindOf(n[key]) }

func (n npmRecord) present(key string) bool { return n.kind(key) != npmAbsent }

// literal is a JSON value as text: a string as written, anything else as its
// JSON spelling (a number keeps its digits, a flag reads true or false).
func literal(raw json.RawMessage) string {
	switch kindOf(raw) {
	case npmAbsent:
		return ""
	case npmString:
		var text string
		_ = json.Unmarshal(raw, &text)
		return text
	}
	return string(bytes.TrimSpace(raw))
}

// text is a string field as written; a number or flag as its literal.
func (n npmRecord) text(key string) string { return literal(n[key]) }

// integer reports a whole-number field.
func (n npmRecord) integer(key string) bool {
	if n.kind(key) != npmNumber {
		return false
	}
	_, err := strconv.ParseInt(n.text(key), 10, 64)
	return err == nil
}

// int reads a whole number, or a numeric string; anything else is 0.
func (n npmRecord) int(key string) int {
	switch n.kind(key) {
	case npmNumber:
		i, _ := strconv.ParseInt(n.text(key), 10, 64)
		return int(i)
	case npmString:
		i, _ := strconv.Atoi(n.text(key))
		return i
	}
	return 0
}

// flag reads NPM's booleans: true/false, 1/0, "1"/"0"/"false", or a
// non-empty list or object.
func (n npmRecord) flag(key string) bool {
	value := n.text(key)
	switch n.kind(key) {
	case npmAbsent:
		return false
	case npmBool:
		return value == "true"
	case npmString:
		return value != "" && value != "0" && strings.ToLower(value) != "false"
	case npmNumber:
		if i, err := strconv.ParseInt(value, 10, 64); err == nil {
			return i != 0
		}
		f, _ := strconv.ParseFloat(value, 64)
		return f != 0
	case npmList:
		var items []json.RawMessage
		_ = json.Unmarshal(n[key], &items)
		return len(items) > 0
	case npmObject:
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(n[key], &fields)
		return len(fields) > 0
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
	if n.kind(key) != npmList {
		return out
	}
	var items []json.RawMessage
	_ = json.Unmarshal(n[key], &items)
	for _, item := range items {
		out = append(out, literal(item))
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

// canonical re-encodes a JSON value with sorted keys and json.Marshal's string
// escaping, so two spellings of one value compare equal. Numbers keep their
// digits.
func canonical(raw json.RawMessage) string {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	out, err := canonicalValue(decoder)
	if err != nil || decoder.More() {
		return string(raw)
	}
	return out
}

func canonicalValue(decoder *json.Decoder) (string, error) {
	token, err := decoder.Token()
	if err != nil {
		return "", err
	}
	switch token := token.(type) {
	case json.Delim:
		if token == '[' {
			items := []string{}
			for decoder.More() {
				item, err := canonicalValue(decoder)
				if err != nil {
					return "", err
				}
				items = append(items, item)
			}
			_, err := decoder.Token()
			return "[" + strings.Join(items, ",") + "]", err
		}
		fields := map[string]string{}
		for decoder.More() {
			key, err := decoder.Token()
			if err != nil {
				return "", err
			}
			value, err := canonicalValue(decoder)
			if err != nil {
				return "", err
			}
			fields[key.(string)] = value
		}
		_, err := decoder.Token()
		keys := make([]string, 0, len(fields))
		for key := range fields {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			name, _ := json.Marshal(key)
			parts = append(parts, string(name)+":"+fields[key])
		}
		return "{" + strings.Join(parts, ",") + "}", err
	case json.Number:
		return token.String(), nil
	}
	out, err := json.Marshal(token)
	return string(out), err
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
	act(r, runtime.ResourceKindNPMProxyHost, "reconcile", r.npmReconcile)
	act(r, runtime.ResourceKindNPMProxyHost, "delete", r.npmDelete)
	r.reader(runtime.ResourceKindNPMProxyHost, r.npmInventory)
	r.reader(runtime.ResourceKindNPMCertificate, npmEachConnection(r, r.npmCertificates, func(rec *NPMCertificateRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMRedirect, npmEachConnection(r, r.npmRedirects, func(rec *NPMRedirectRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMDeadHost, npmEachConnection(r, r.npmDeadHosts, func(rec *NPMDeadHostRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMStream, npmEachConnection(r, r.npmStreams, func(rec *NPMStreamRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMAccessList, npmEachConnection(r, r.npmAccessLists, func(rec *NPMAccessListRecord, ref *string) { rec.ConnectionRef = ref }))
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
		answer, err := r.HTTP.Request(ctx, base.String()+"/tokens", "POST", nil, npmapi.RequestTokenJSONBody{Identity: user, Secret: password})
		if err != nil {
			return nil, err
		}
		token, _ := decodeAs[npmapi.TokenObject](answer, "")
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
		if provider.Refusal == runtime.RefusalCredential || provider.Failure == runtime.FailureClassCredential {
			return &ProviderError{
				Message: what + ": the credential was refused.",
				Failure: runtime.FailureClassCredential,
				Refusal: runtime.RefusalCredential,
				Reason:  "The credential was refused.",
			}
		}
		if provider.Refusal == runtime.RefusalPermission || provider.Failure == runtime.FailureClassPermission {
			return &ProviderError{
				Message: what + " needs " + needs + ".",
				Failure: runtime.FailureClassPermission,
				Refusal: runtime.RefusalPermission,
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

// npmSessionFailed marks an error from signing in, as opposed to one from the
// list endpoint, which npmListed words differently.
type npmSessionFailed struct{ error }

func (e npmSessionFailed) Unwrap() error { return e.error }

// npmFetched is one NPM list endpoint's answer, read once per sweep whichever
// kinds need it. A sign-in failure comes back as npmSessionFailed.
func (r *Registry) npmFetched(ctx context.Context, ref, path string) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix("npm", ref)
	if err != nil {
		return nil, npmSessionFailed{err}
	}
	return r.cached(ctx, "npm-list:"+prefix+":"+path, func() (json.RawMessage, error) {
		base, headers, err := r.npmSession(ctx, ref)
		if err != nil {
			return nil, npmSessionFailed{err}
		}
		return r.HTTP.Request(ctx, base+path, "GET", headers, nil)
	})
}

func (r *Registry) npmListed(ctx context.Context, ref string, src npmSource) ([]npmRecord, error) {
	raw, err := r.npmFetched(ctx, ref, src.path)
	if err != nil {
		var session npmSessionFailed
		if errors.As(err, &session) {
			return nil, session.error
		}
		return nil, npmRefused(err, src.what, src.needs)
	}
	return npmRecords(raw)
}

// npmProxyHostList reads the proxy hosts fresh: the actions that write decide
// from the live list, never from the sweep's snapshot.
func (r *Registry) npmProxyHostList(ctx context.Context, base string, headers map[string]string) ([]npmRecord, error) {
	raw, err := r.HTTP.Request(ctx, base+npmProxyHosts.path, "GET", headers, nil)
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

func (r *Registry) npmReconcile(ctx context.Context, spec NPMProxyHostSpec, observed NPMProxyHostObserved, apply bool) (Result, error) {
	base, headers, err := r.npmSession(ctx, string(spec.ConnectionRef))
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

func (r *Registry) npmDelete(ctx context.Context, spec NPMProxyHostSpec, _ struct{}, apply bool) (Result, error) {
	base, headers, err := r.npmSession(ctx, string(spec.ConnectionRef))
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
	hosts, err := r.npmFetched(ctx, "", npmProxyHosts.path)
	if err != nil {
		var session npmSessionFailed
		if errors.As(err, &session) {
			return nil, session.error
		}
		return nil, err
	}
	hostRecords, err := npmRecords(hosts)
	if err != nil {
		return nil, err
	}
	policies := map[string]*NPMAccessPolicy{}
	certificates := map[string]*NPMCertificateSummary{}
	if raw, err := r.npmFetched(ctx, "", npmAccessListsSource.path); err == nil {
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
	if raw, err := r.npmFetched(ctx, "", npmCertificatesSource.path); err == nil {
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
	for _, host := range hostRecords {
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
