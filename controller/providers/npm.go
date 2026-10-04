package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"reflect"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/npmapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// NPM's records decode into the vendored spec's models (npmapi), corrected to
// NPM's wire types by api/vendor/npm/overlay.yaml. A record that does not
// decode fails the read; nothing is coerced.

func (r *Registry) admitNPM() {
	act(r, runtime.ResourceKindNPMProxyHost, "reconcile", r.npmReconcile)
	act(r, runtime.ResourceKindNPMProxyHost, "delete", r.npmDelete)
	r.reader(runtime.ResourceKindNPMProxyHost, r.npmInventory)
	r.reader(runtime.ResourceKindNPMCertificate, npmEachConnection(r, r.npmCertificates, func(rec *NPMCertificateRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMRedirect, npmEachConnection(r, r.npmRedirects, func(rec *NPMRedirectRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMDeadHost, npmEachConnection(r, r.npmDeadHosts, func(rec *NPMDeadHostRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMStream, npmEachConnection(r, r.npmStreams, func(rec *NPMStreamRecord, ref *string) { rec.ConnectionRef = ref }))
	r.reader(runtime.ResourceKindNPMAccessList, npmEachConnection(r, r.npmAccessLists, func(rec *NPMAccessListRecord, ref *string) { rec.ConnectionRef = ref }))
	r.probe(runtime.ConnectionProviderNPM, func(ctx context.Context, ref string) (ProbeResult, error) {
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
		for _, ref := range r.refs(runtime.ConnectionProviderNPM) {
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
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderNPM, ref)
	if err != nil {
		return "", nil, err
	}
	configured, err := r.Env.Required(prefix, "URL")
	if err != nil {
		return "", nil, err
	}
	base, err := url.Parse(strings.TrimRight(configured, "/"))
	if err != nil {
		return "", nil, &ProviderError{Message: "npm API address is not a URL", Failure: runtime.FailureClassAddress}
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
			return nil, fmt.Errorf("npm sign-in: %w", err)
		}
		var token npmapi.TokenObject
		if err := json.Unmarshal(answer, &token); err != nil {
			return nil, &ProviderError{Message: "npm sign-in answer did not decode", Err: err}
		}
		if token.Token == "" {
			return nil, &ProviderError{Message: "npm sign-in returned no token"}
		}
		return json.Marshal(token.Token)
	})
	if err != nil {
		return "", nil, err
	}
	var token string
	if err := json.Unmarshal(raw, &token); err != nil {
		return "", nil, err
	}
	return base.String(), map[string]string{"Authorization": "Bearer " + token}, nil
}

// npmSource is one NPM list endpoint, the model its records decode into, and
// what NPM's permission for it is called.
type npmSource[T any] struct {
	path  string
	needs string
	what  string
}

var (
	npmProxyHosts         = npmSource[npmapi.ProxyHostObject]{path: "/nginx/proxy-hosts", needs: "proxy_hosts: view", what: "proxy host list"}
	npmRedirectionHosts   = npmSource[npmapi.RedirectionHostObject]{path: "/nginx/redirection-hosts", needs: "redirection_hosts: view", what: "redirection host list"}
	npmDeadHostsSource    = npmSource[npmapi.DeadHostObject]{path: "/nginx/dead-hosts", needs: "dead_hosts: view", what: "404 host list"}
	npmStreamsSource      = npmSource[npmapi.StreamObject]{path: "/nginx/streams", needs: "streams: view", what: "stream list"}
	npmCertificatesSource = npmSource[npmapi.CertificateObject]{path: "/nginx/certificates", needs: "certificates: view", what: "certificate list"}
	npmAccessListsSource  = npmSource[npmAccessList]{path: "/nginx/access-lists?expand=items,clients", needs: "access_lists: view", what: "access list list"}
)

// npmRefused says which list NPM refused and, for a missing permission, which
// one the credential needs.
func npmRefused(err error, what, needs string) error {
	failure, _, _ := runtime.Classify(err)
	switch failure {
	case runtime.FailureClassCredential:
		return &ProviderError{Message: what + ": credential refused", Failure: failure, Reason: "credential refused"}
	case runtime.FailureClassPermission:
		return &ProviderError{Message: what + " needs " + needs, Failure: failure}
	}
	if _, ok := errors.AsType[*ProviderError](err); ok {
		return fmt.Errorf("%s: %w", what, err)
	}
	return &ProviderError{Message: what, Err: err}
}

// npmDecode decodes one NPM list answer; an empty answer is an empty list.
func npmDecode[T any](raw json.RawMessage, what string) ([]T, error) {
	items := []T{}
	if len(raw) == 0 {
		return items, nil
	}
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, &ProviderError{Message: what + " did not decode", Err: err}
	}
	if items == nil {
		items = []T{}
	}
	return items, nil
}

// npmSessionFailed marks an error from signing in, as opposed to one from the
// list endpoint, which npmRead words with the list's name.
type npmSessionFailed struct{ error }

func (e npmSessionFailed) Unwrap() error { return e.error }

// npmFetched is one NPM list endpoint's answer, read once per sweep whichever
// kinds need it. A sign-in failure comes back as npmSessionFailed.
func (r *Registry) npmFetched(ctx context.Context, ref, path string) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderNPM, ref)
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

// npmFetchFailure is a list read's failure as the reader reports it.
func npmFetchFailure(err error, what, needs string) error {
	if session, ok := errors.AsType[npmSessionFailed](err); ok {
		return session.error
	}
	return npmRefused(err, what, needs)
}

// npmRead is one list endpoint's records, read through the sweep's snapshot.
func npmRead[T any](ctx context.Context, r *Registry, ref string, src npmSource[T]) ([]T, error) {
	raw, err := r.npmFetched(ctx, ref, src.path)
	if err != nil {
		return nil, npmFetchFailure(err, src.what, src.needs)
	}
	return npmDecode[T](raw, "npm "+src.what)
}

// npmProxyHostList reads the proxy hosts fresh: the actions that write decide
// from the live list, never from the sweep's snapshot.
func (r *Registry) npmProxyHostList(ctx context.Context, base string, headers map[string]string) ([]npmapi.ProxyHostObject, error) {
	raw, err := r.HTTP.Request(ctx, base+npmProxyHosts.path, "GET", headers, nil)
	if err != nil {
		return nil, fmt.Errorf("npm %s: %w", npmProxyHosts.what, err)
	}
	return npmDecode[npmapi.ProxyHostObject](raw, "npm "+npmProxyHosts.what)
}

// npmID is a record's id; NPM always gives one, so a record without it is malformed.
func npmID(id *npmapi.Id, what string) (int, error) {
	if id == nil {
		return 0, &ProviderError{Message: "npm " + what + " record has no id"}
	}
	return int(*id), nil
}

func sortedNames(names []string) []string {
	out := append([]string{}, names...)
	slices.Sort(out)
	return out
}

func matchingHosts(hosts []npmapi.ProxyHostObject, domains []string) []npmapi.ProxyHostObject {
	out := []npmapi.ProxyHostObject{}
	for _, host := range hosts {
		if reflect.DeepEqual(sortedNames(host.DomainNames), domains) {
			out = append(out, host)
		}
	}
	return out
}

func npmDesired(spec NPMProxyHostSpec, domains []string) npmProxyHostRequest {
	return npmProxyHostRequest{
		DomainNames:           domains,
		CertificateID:         spec.CertificateID,
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

// npmHeld is the request that would leave a proxy host as NPM holds it.
func npmHeld(host npmapi.ProxyHostObject) npmProxyHostRequest {
	return npmProxyHostRequest{
		DomainNames:           sortedNames(host.DomainNames),
		CertificateID:         int(host.CertificateId),
		Locations:             host.Locations,
		Meta:                  host.Meta,
		ForwardScheme:         string(host.ForwardScheme),
		ForwardHost:           host.ForwardHost,
		ForwardPort:           host.ForwardPort,
		CachingEnabled:        bool(host.CachingEnabled),
		BlockExploits:         bool(host.BlockExploits),
		AccessListID:          int(host.AccessListId),
		HSTSEnabled:           bool(host.HstsEnabled),
		HSTSSubdomains:        bool(host.HstsSubdomains),
		TrustForwardedProto:   bool(host.TrustForwardedProto),
		AdvancedConfig:        host.AdvancedConfig,
		AllowWebsocketUpgrade: bool(host.AllowWebsocketUpgrade),
		SSLForced:             bool(host.SslForced),
		HTTP2Support:          bool(host.Http2Support),
		Enabled:               bool(host.Enabled),
	}
}

// npmDiffers is whether NPM holds anything HQ decides differently from
// desired. Locations and meta are NPM's own and never compared.
func npmDiffers(held, desired npmProxyHostRequest) bool {
	held.Locations, held.Meta = nil, nil
	desired.Locations, desired.Meta = nil, nil
	return !reflect.DeepEqual(held, desired)
}

const npmDuplicateHosts = "npm holds more than one proxy host for the domain set"

func (r *Registry) npmReconcile(ctx context.Context, spec NPMProxyHostSpec, observed NPMProxyHostObserved, apply bool) (Result, error) {
	base, headers, err := r.npmSession(ctx, spec.ConnectionRef)
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
		if len(previous) > 0 && !reflect.DeepEqual(previous, domains) {
			matches = matchingHosts(hosts, previous)
		}
	}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: npmDuplicateHosts}
	}
	desired := npmDesired(spec, domains)
	changed := true
	path, method := npmProxyHosts.path, "POST"
	if len(matches) == 1 {
		current := matches[0]
		id, err := npmID(current.Id, "proxy host")
		if err != nil {
			return Result{}, err
		}
		if spec.ForceSSL && current.CertificateId == 0 {
			return Result{}, &ProviderError{Message: "proxy host forces HTTPS but has no certificate; attach one, then reconcile"}
		}
		if spec.CertificateID == 0 {
			desired.CertificateID = int(current.CertificateId)
		}
		if len(current.Locations) > 0 {
			desired.Locations = current.Locations
		}
		if len(current.Meta) > 0 {
			desired.Meta = current.Meta
		}
		changed = npmDiffers(npmHeld(current), desired)
		path, method = fmt.Sprintf("%s/%d", path, id), "PUT"
	} else if spec.ForceSSL && spec.CertificateID == 0 {
		return Result{}, &ProviderError{Message: "an HTTPS proxy host needs an issued certificate and none is set yet"}
	}
	if changed && apply {
		if _, err := r.HTTP.Request(ctx, base+path, method, headers, desired); err != nil {
			return Result{}, fmt.Errorf("npm write proxy host: %w", err)
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
	base, headers, err := r.npmSession(ctx, spec.ConnectionRef)
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
		return Result{}, &ProviderError{Message: npmDuplicateHosts}
	}
	if len(matches) == 0 {
		return result(false, status, "Absent", "No such proxy host in NPM.", "NPM proxy host was already absent."), nil
	}
	id, err := npmID(matches[0].Id, "proxy host")
	if err != nil {
		return Result{}, err
	}
	if apply {
		if _, err := r.HTTP.Request(ctx, fmt.Sprintf("%s%s/%d", base, npmProxyHosts.path, id), "DELETE", headers, nil); err != nil {
			return Result{}, fmt.Errorf("npm delete proxy host: %w", err)
		}
	}
	return result(true, status, "Removed", "NPM proxy host was removed.", "NPM proxy host removed."), nil
}

func npmClients(list npmAccessList) []NPMAccessRule {
	clients := []NPMAccessRule{}
	for _, rule := range list.Clients {
		directive, address := string(deref(rule.Directive)), deref(rule.Address)
		if directive != "" && address != "" {
			clients = append(clients, NPMAccessRule{Directive: directive, Address: address})
		}
	}
	return clients
}

// npmEnrichment is an optional list the proxy host inventory joins: a refused
// or unreachable read leaves it out, a malformed one fails the inventory.
func npmEnrichment[T any](ctx context.Context, r *Registry, src npmSource[T]) ([]T, error) {
	raw, err := r.npmFetched(ctx, "", src.path)
	if err != nil {
		return []T{}, nil
	}
	return npmDecode[T](raw, "npm "+src.what)
}

func (r *Registry) npmInventory(ctx context.Context) ([]any, error) {
	raw, err := r.npmFetched(ctx, "", npmProxyHosts.path)
	if err != nil {
		if session, ok := errors.AsType[npmSessionFailed](err); ok {
			return nil, session.error
		}
		return nil, fmt.Errorf("npm %s: %w", npmProxyHosts.what, err)
	}
	hosts, err := npmDecode[npmapi.ProxyHostObject](raw, "npm "+npmProxyHosts.what)
	if err != nil {
		return nil, err
	}
	lists, err := npmEnrichment(ctx, r, npmAccessListsSource)
	if err != nil {
		return nil, err
	}
	policies := map[int]*NPMAccessPolicy{}
	for _, list := range lists {
		id, err := npmID(list.Id, "access list")
		if err != nil {
			return nil, err
		}
		clients := npmClients(list)
		policies[id] = &NPMAccessPolicy{
			Name:               list.Name,
			SatisfyAny:         bool(list.SatisfyAny),
			PassAuth:           bool(list.PassAuth),
			AuthorizationCount: len(list.Items),
			Clients:            clients,
			ImplicitDeny:       len(clients) > 0,
		}
	}
	certs, err := npmEnrichment(ctx, r, npmCertificatesSource)
	if err != nil {
		return nil, err
	}
	certificates := map[int]*NPMCertificateSummary{}
	for _, cert := range certs {
		id, err := npmID(cert.Id, "certificate")
		if err != nil {
			return nil, err
		}
		certificates[id] = &NPMCertificateSummary{Name: cert.NiceName, Domains: cert.DomainNames, ExpiresOn: deref(cert.ExpiresOn), Provider: cert.Provider}
	}
	found := []any{}
	for _, host := range hosts {
		if len(host.DomainNames) == 0 {
			continue
		}
		found = append(found, NPMProxyHostRecord{
			DomainNames:           host.DomainNames,
			ForwardScheme:         string(host.ForwardScheme),
			ForwardHost:           host.ForwardHost,
			ForwardPort:           host.ForwardPort,
			SSLForced:             bool(host.SslForced),
			HTTP2Support:          bool(host.Http2Support),
			AllowWebsocketUpgrade: bool(host.AllowWebsocketUpgrade),
			CachingEnabled:        bool(host.CachingEnabled),
			BlockExploits:         bool(host.BlockExploits),
			AccessListID:          int(host.AccessListId),
			AdvancedConfig:        host.AdvancedConfig,
			HSTSEnabled:           bool(host.HstsEnabled),
			HSTSSubdomains:        bool(host.HstsSubdomains),
			TrustForwardedProto:   bool(host.TrustForwardedProto),
			Enabled:               bool(host.Enabled),
			Certificate:           npmCertificateRef{certificates[int(host.CertificateId)]},
			AccessPolicy:          policies[int(host.AccessListId)],
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

// npmCertificateNames names each certificate by id for the readers that show
// one. Best effort: the certificate kind reports its own read's failure.
func (r *Registry) npmCertificateNames(ctx context.Context, ref string) map[int]string {
	names := map[int]string{}
	certs, err := npmRead(ctx, r, ref, npmCertificatesSource)
	if err != nil {
		return names
	}
	for _, cert := range certs {
		if cert.Id != nil {
			names[int(*cert.Id)] = cert.NiceName
		}
	}
	return names
}

// npmServes is the names each certificate serves through an enabled host. A
// refused list is a refused part; the others still count.
func (r *Registry) npmServes(ctx context.Context, ref string) map[int][]string {
	serves := map[int][]string{}
	add := func(certificate npmapi.CertificateId, enabled npmapi.Flag, names []string) {
		if certificate > 0 && bool(enabled) {
			serves[int(certificate)] = append(serves[int(certificate)], names...)
		}
	}
	if hosts, err := npmRead(ctx, r, ref, npmProxyHosts); err != nil {
		refuse(ctx, runtime.PartProxyHosts, ref, "", err)
	} else {
		for _, host := range hosts {
			add(host.CertificateId, host.Enabled, host.DomainNames)
		}
	}
	if hosts, err := npmRead(ctx, r, ref, npmRedirectionHosts); err != nil {
		refuse(ctx, runtime.PartRedirectionHosts, ref, "", err)
	} else {
		for _, host := range hosts {
			add(host.CertificateId, host.Enabled, host.DomainNames)
		}
	}
	if hosts, err := npmRead(ctx, r, ref, npmDeadHostsSource); err != nil {
		refuse(ctx, runtime.PartDeadHosts, ref, "", err)
	} else {
		for _, host := range hosts {
			add(host.CertificateId, host.Enabled, host.DomainNames)
		}
	}
	return serves
}

func (r *Registry) npmCertificates(ctx context.Context, ref string) ([]NPMCertificateRecord, error) {
	certs, err := npmRead(ctx, r, ref, npmCertificatesSource)
	if err != nil {
		return nil, err
	}
	serves := r.npmServes(ctx, ref)
	out := []NPMCertificateRecord{}
	for _, cert := range certs {
		id, err := npmID(cert.Id, "certificate")
		if err != nil {
			return nil, err
		}
		out = append(out, NPMCertificateRecord{
			ID:        id,
			Name:      cert.NiceName,
			Provider:  cert.Provider,
			Domains:   cert.DomainNames,
			ExpiresOn: deref(cert.ExpiresOn),
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

// optionalPort is a port or status code, nil when NPM gave none.
func optionalPort(value int) *int {
	if value == 0 {
		return nil
	}
	return &value
}

// npmHostRecords is one host list as its records. record gets each host and
// base, which resolves what every host kind shows the same way and fails on a
// malformed host.
func npmHostRecords[T, R any](ctx context.Context, r *Registry, ref string, src npmSource[T], what string, record func(host T, base npmHostBase) (R, error)) ([]R, error) {
	names := r.npmCertificateNames(ctx, ref)
	hosts, err := npmRead(ctx, r, ref, src)
	if err != nil {
		return nil, err
	}
	base := func(id *npmapi.Id, domains []string, certificate npmapi.CertificateId, sslForced, enabled npmapi.Flag) (NPMHostRecord, error) {
		resolved, err := npmID(id, what)
		return NPMHostRecord{
			ID:          resolved,
			Hostnames:   npmNames(domains),
			Certificate: names[int(certificate)],
			SSLForced:   bool(sslForced),
			Enabled:     bool(enabled),
		}, err
	}
	out := []R{}
	for _, host := range hosts {
		rec, err := record(host, base)
		if err != nil {
			return nil, err
		}
		out = append(out, rec)
	}
	return out, nil
}

type npmHostBase func(id *npmapi.Id, domains []string, certificate npmapi.CertificateId, sslForced, enabled npmapi.Flag) (NPMHostRecord, error)

func (r *Registry) npmRedirects(ctx context.Context, ref string) ([]NPMRedirectRecord, error) {
	return npmHostRecords(ctx, r, ref, npmRedirectionHosts, "redirection host", func(host npmapi.RedirectionHostObject, base npmHostBase) (NPMRedirectRecord, error) {
		shared, err := base(host.Id, host.DomainNames, host.CertificateId, host.SslForced, host.Enabled)
		return NPMRedirectRecord{
			NPMHostRecord: shared,
			Target:        npmTarget(string(host.ForwardScheme), host.ForwardDomainName),
			TargetHost:    hostname(host.ForwardDomainName),
			StatusCode:    optionalPort(host.ForwardHttpCode),
			PreservePath:  bool(host.PreservePath),
		}, err
	})
}

func (r *Registry) npmDeadHosts(ctx context.Context, ref string) ([]NPMDeadHostRecord, error) {
	return npmHostRecords(ctx, r, ref, npmDeadHostsSource, "404 host", func(host npmapi.DeadHostObject, base npmHostBase) (NPMDeadHostRecord, error) {
		shared, err := base(host.Id, host.DomainNames, host.CertificateId, host.SslForced, host.Enabled)
		return NPMDeadHostRecord{shared}, err
	})
}

func (r *Registry) npmStreams(ctx context.Context, ref string) ([]NPMStreamRecord, error) {
	streams, err := npmRead(ctx, r, ref, npmStreamsSource)
	if err != nil {
		return nil, err
	}
	out := []NPMStreamRecord{}
	for _, stream := range streams {
		id, err := npmID(stream.Id, "stream")
		if err != nil {
			return nil, err
		}
		out = append(out, NPMStreamRecord{
			ID:             id,
			IncomingPort:   stream.IncomingPort,
			ForwardingHost: stream.ForwardingHost,
			ForwardingPort: optionalPort(stream.ForwardingPort),
			TCP:            bool(stream.TcpForwarding),
			UDP:            bool(stream.UdpForwarding),
			Enabled:        bool(stream.Enabled),
		})
	}
	return out, nil
}

func (r *Registry) npmAccessLists(ctx context.Context, ref string) ([]NPMAccessListRecord, error) {
	lists, err := npmRead(ctx, r, ref, npmAccessListsSource)
	if err != nil {
		return nil, err
	}
	protects := map[int][]string{}
	if hosts, err := npmRead(ctx, r, ref, npmProxyHosts); err != nil {
		refuse(ctx, runtime.PartProxyHosts, ref, "", err)
	} else {
		for _, host := range hosts {
			if id := int(host.AccessListId); id > 0 {
				protects[id] = append(protects[id], host.DomainNames...)
			}
		}
	}
	out := []NPMAccessListRecord{}
	for _, list := range lists {
		id, err := npmID(list.Id, "access list")
		if err != nil {
			return nil, err
		}
		logins := []string{}
		for _, login := range list.Items {
			if name := deref(login.Username); name != "" {
				logins = append(logins, name)
			}
		}
		out = append(out, NPMAccessListRecord{
			ID:         id,
			Name:       list.Name,
			SatisfyAny: bool(list.SatisfyAny),
			PassAuth:   bool(list.PassAuth),
			Clients:    npmClients(list),
			Logins:     logins,
			Protects:   npmNames(protects[id]),
		})
	}
	return out, nil
}
