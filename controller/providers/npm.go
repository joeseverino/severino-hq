package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"reflect"
	"strings"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
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
		if _, _, _, err := r.npmSession(ctx, ref); err != nil {
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

// npmSession signs in to one NPM connection, once per sweep: the ref the
// connection is held under, NPM's API address, and the token's header.
func (r *Registry) npmSession(ctx context.Context, ref string) (string, string, map[string]string, error) {
	connection, err := r.Supplied.For(runtime.ConnectionProviderNPM, ref)
	if err != nil {
		return "", "", nil, err
	}
	login, err := runtime.Need(connection.Login)
	if err != nil {
		return "", "", nil, err
	}
	base, err := npmBase(login.URL)
	if err != nil {
		return "", "", nil, err
	}
	token, err := cached(ctx, r, "npm-token:"+base+":"+connection.Ref, func() (string, error) {
		signIn := &npmapi.ClientWithResponses{ClientInterface: &npmapi.API{Server: base, Client: vendorDoer{r: r}}}
		answer, err := json200(signIn.RequestTokenWithResponse(ctx, npmapi.RequestTokenJSONRequestBody{Identity: login.Username, Secret: login.Password}))
		if err != nil {
			return "", fmt.Errorf("npm sign-in: %w", err)
		}
		token, err := answer.AsTokenObject()
		if err != nil || token.Token == "" {
			return "", &ProviderError{Message: "npm sign-in returned no token", Err: err}
		}
		return token.Token, nil
	})
	if err != nil {
		return "", "", nil, err
	}
	mask.Learn(token)
	return connection.Ref, base, map[string]string{"Authorization": "Bearer " + token}, nil
}

// npm is the generated client for one NPM connection, signed in. A sweep's
// client reads each list once whichever kinds need it; a write's reads fresh.
// A sign-in that fails fails every call, as npmSessionFailed.
func (r *Registry) npm(ctx context.Context, ref string, sweep bool) *npmapi.ClientWithResponses {
	held, base, headers, err := r.npmSession(ctx, ref)
	doer := vendorDoer{r: r, headers: headers}
	if err != nil {
		doer.err = npmSessionFailed{err}
	}
	if sweep {
		doer.cache = "npm:" + held
	}
	return &npmapi.ClientWithResponses{ClientInterface: &npmapi.API{Server: base, Client: doer}}
}

// npmBase is NPM's API address, under /api whether or not the connection names it.
func npmBase(address string) (string, error) {
	base, err := url.Parse(strings.TrimRight(address, "/"))
	if err != nil {
		return "", &ProviderError{Message: "npm API address is not a URL", Failure: runtime.FailureClassAddress}
	}
	base.Path = strings.TrimRight(base.Path, "/")
	if !strings.HasSuffix(base.Path, "/api") {
		base.Path += "/api"
	}
	return base.String() + "/", nil
}

// npmSource is one NPM list, the call that reads it, and what NPM's
// permission for it is called.
type npmSource[T any] struct {
	list  func(context.Context, *npmapi.ClientWithResponses) ([]T, error)
	needs string
	what  string
}

var (
	npmProxyHosts = npmSource[npmapi.ProxyHostObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.ProxyHostObject, error) {
		return json200(c.GetProxyHostsWithResponse(ctx, nil))
	}, "proxy_hosts: view", "proxy host list"}
	npmRedirectionHosts = npmSource[npmapi.RedirectionHostObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.RedirectionHostObject, error) {
		return json200(c.GetRedirectionHostsWithResponse(ctx, nil))
	}, "redirection_hosts: view", "redirection host list"}
	npmDeadHostsSource = npmSource[npmapi.DeadHostObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.DeadHostObject, error) {
		return json200(c.GetDeadHostsWithResponse(ctx, nil))
	}, "dead_hosts: view", "404 host list"}
	npmStreamsSource = npmSource[npmapi.StreamObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.StreamObject, error) {
		return json200(c.GetStreamsWithResponse(ctx, nil))
	}, "streams: view", "stream list"}
	npmCertificatesSource = npmSource[npmapi.CertificateObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.CertificateObject, error) {
		return json200(c.GetCertificatesWithResponse(ctx, nil))
	}, "certificates: view", "certificate list"}
	npmAccessListsSource = npmSource[npmapi.AccessListObject]{func(ctx context.Context, c *npmapi.ClientWithResponses) ([]npmapi.AccessListObject, error) {
		return json200(c.GetAccessListsWithResponse(ctx, &npmapi.GetAccessListsParams{Expand: &[]npmapi.GetAccessListsParamsExpand{npmapi.GetAccessListsParamsExpandItems, npmapi.GetAccessListsParamsExpandClients}}))
	}, "access_lists: view", "access list list"}
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

// npmSessionFailed marks an error from signing in, as opposed to one from the
// list endpoint, which npmRead words with the list's name.
type npmSessionFailed struct{ error }

func (e npmSessionFailed) Unwrap() error { return e.error }

// npmFetchFailure is a list read's failure as the reader reports it.
func npmFetchFailure(err error, what, needs string) error {
	if session, ok := errors.AsType[npmSessionFailed](err); ok {
		return session.error
	}
	return npmRefused(err, what, needs)
}

// npmRead is one list endpoint's records, read through the sweep's snapshot.
func npmRead[T any](ctx context.Context, r *Registry, ref string, src npmSource[T]) ([]T, error) {
	items, err := src.list(ctx, r.npm(ctx, ref, true))
	if err != nil {
		return nil, npmFetchFailure(err, src.what, src.needs)
	}
	return items, nil
}

// npmProxyHostList reads the proxy hosts through client: the actions that
// write pass a fresh one, deciding from the live list, never the sweep's.
func npmProxyHostList(ctx context.Context, client *npmapi.ClientWithResponses) ([]npmapi.ProxyHostObject, error) {
	hosts, err := npmProxyHosts.list(ctx, client)
	if session, ok := errors.AsType[npmSessionFailed](err); ok {
		return nil, session.error
	}
	if err != nil {
		return nil, fmt.Errorf("npm %s: %w", npmProxyHosts.what, err)
	}
	return hosts, nil
}

// npmID is a record's id; NPM always gives one, so a record without it is malformed.
func npmID(id *npmapi.Id, what string) (int, error) {
	if id == nil {
		return 0, &ProviderError{Message: "npm " + what + " record has no id"}
	}
	return int(*id), nil
}

func matchingHosts(hosts []npmapi.ProxyHostObject, domains []string) []npmapi.ProxyHostObject {
	out := []npmapi.ProxyHostObject{}
	for _, host := range hosts {
		if reflect.DeepEqual(sortedCopy(host.DomainNames), domains) {
			out = append(out, host)
		}
	}
	return out
}

// npmProxyHostRequest is the body NPM takes to create a proxy host, and
// whole, to update one. Locations and Meta are NPM's own, carried forward on
// update; HQ never decides them.
type npmProxyHostRequest = npmapi.CreateProxyHostJSONRequestBody

func npmDesired(spec NPMProxyHostSpec, domains []string) npmProxyHostRequest {
	return npmProxyHostRequest{
		DomainNames:           domains,
		CertificateId:         new(npmapi.CertificateId(spec.CertificateID)),
		Locations:             new(json.RawMessage("[]")),
		Meta:                  new(json.RawMessage("{}")),
		ForwardScheme:         npmapi.ForwardScheme(spec.ForwardScheme),
		ForwardHost:           spec.ForwardHost,
		ForwardPort:           spec.ForwardPort,
		CachingEnabled:        new(npmapi.Flag(spec.CachingEnabled)),
		BlockExploits:         new(npmapi.Flag(spec.BlockExploits)),
		AccessListId:          new(npmapi.AccessListId(spec.AccessListID)),
		HstsEnabled:           new(npmapi.Flag(spec.HSTSEnabled)),
		HstsSubdomains:        new(npmapi.Flag(spec.HSTSSubdomains)),
		TrustForwardedProto:   new(npmapi.Flag(spec.TrustForwardedProto)),
		AdvancedConfig:        new(spec.AdvancedConfig),
		AllowWebsocketUpgrade: new(npmapi.Flag(spec.Websocket)),
		SslForced:             new(npmapi.Flag(spec.ForceSSL)),
		Http2Support:          new(npmapi.Flag(spec.HTTP2)),
		Enabled:               new(npmapi.Flag(spec.Serving)),
	}
}

// npmHeld is the request that would leave a proxy host as NPM holds it.
func npmHeld(host npmapi.ProxyHostObject) npmProxyHostRequest {
	return npmProxyHostRequest{
		DomainNames:           sortedCopy(host.DomainNames),
		CertificateId:         new(host.CertificateId),
		Locations:             new(host.Locations),
		Meta:                  new(host.Meta),
		ForwardScheme:         npmapi.ForwardScheme(host.ForwardScheme),
		ForwardHost:           host.ForwardHost,
		ForwardPort:           host.ForwardPort,
		CachingEnabled:        new(host.CachingEnabled),
		BlockExploits:         new(host.BlockExploits),
		AccessListId:          new(host.AccessListId),
		HstsEnabled:           new(host.HstsEnabled),
		HstsSubdomains:        new(host.HstsSubdomains),
		TrustForwardedProto:   new(host.TrustForwardedProto),
		AdvancedConfig:        new(host.AdvancedConfig),
		AllowWebsocketUpgrade: new(host.AllowWebsocketUpgrade),
		SslForced:             new(host.SslForced),
		Http2Support:          new(host.Http2Support),
		Enabled:               new(host.Enabled),
	}
}

// npmDiffers is whether NPM holds anything HQ decides differently from
// desired. Locations and meta are NPM's own and never compared.
func npmDiffers(held, desired npmProxyHostRequest) bool {
	held.Locations, held.Meta = nil, nil
	desired.Locations, desired.Meta = nil, nil
	return !reflect.DeepEqual(held, desired)
}

// npmUpdate is a create's body as the update that writes every field of it:
// NPM takes the same fields for both.
func npmUpdate(body npmProxyHostRequest) (npmapi.UpdateProxyHostJSONRequestBody, error) {
	var update npmapi.UpdateProxyHostJSONRequestBody
	data, err := json.Marshal(body)
	if err == nil {
		err = json.Unmarshal(data, &update)
	}
	return update, err
}

const npmDuplicateHosts = "npm holds more than one proxy host for the domain set"

func (r *Registry) npmReconcile(ctx context.Context, spec NPMProxyHostSpec, observed NPMProxyHostObserved, apply bool) (Result, error) {
	client := r.npm(ctx, spec.ConnectionRef, false)
	hosts, err := npmProxyHostList(ctx, client)
	if err != nil {
		return Result{}, err
	}
	domains := sortedCopy(spec.DomainNames)
	matches := matchingHosts(hosts, domains)
	if len(matches) == 0 {
		previous := sortedCopy(observed.DomainNames)
		if len(previous) > 0 && !reflect.DeepEqual(previous, domains) {
			matches = matchingHosts(hosts, previous)
		}
	}
	if len(matches) > 1 {
		return Result{}, &ProviderError{Message: npmDuplicateHosts}
	}
	desired := npmDesired(spec, domains)
	changed := true
	id := 0
	if len(matches) == 1 {
		current := matches[0]
		if id, err = npmID(current.Id, "proxy host"); err != nil {
			return Result{}, err
		}
		if spec.ForceSSL && current.CertificateId == 0 {
			return Result{}, &ProviderError{Message: "proxy host forces HTTPS but has no certificate; attach one, then apply again"}
		}
		if spec.CertificateID == 0 {
			desired.CertificateId = new(current.CertificateId)
		}
		if len(current.Locations) > 0 {
			desired.Locations = new(current.Locations)
		}
		if len(current.Meta) > 0 {
			desired.Meta = new(current.Meta)
		}
		changed = npmDiffers(npmHeld(current), desired)
	} else if spec.ForceSSL && spec.CertificateID == 0 {
		return Result{}, &ProviderError{Message: "an HTTPS proxy host needs an issued certificate and none is set yet"}
	}
	if changed && apply {
		if id == 0 {
			_, err = client.CreateProxyHostWithResponse(ctx, desired)
		} else if update, encoded := npmUpdate(desired); encoded != nil {
			err = encoded
		} else {
			_, err = client.UpdateProxyHostWithResponse(ctx, id, update)
		}
		if err != nil {
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
	client := r.npm(ctx, spec.ConnectionRef, false)
	hosts, err := npmProxyHostList(ctx, client)
	if err != nil {
		return Result{}, err
	}
	domains := sortedCopy(spec.DomainNames)
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
		if _, err := client.DeleteProxyHostWithResponse(ctx, id); err != nil {
			return Result{}, fmt.Errorf("npm delete proxy host: %w", err)
		}
	}
	return result(true, status, "Removed", "NPM proxy host was removed.", "NPM proxy host removed."), nil
}

func npmClients(list npmapi.AccessListObject) []NPMAccessRule {
	clients := []NPMAccessRule{}
	for _, rule := range deref(list.Clients) {
		directive, address := string(deref(rule.Directive)), deref(rule.Address)
		if directive != "" && address != "" {
			clients = append(clients, NPMAccessRule{Directive: directive, Address: address})
		}
	}
	return clients
}

// npmEnrichment is an optional list the proxy host inventory joins: a refused
// or unreachable read, which the transport answers as a ProviderError, leaves
// it out; a malformed one fails the inventory.
func npmEnrichment[T any](ctx context.Context, r *Registry, src npmSource[T]) ([]T, error) {
	items, err := src.list(ctx, r.npm(ctx, "", true))
	if _, refused := errors.AsType[*ProviderError](err); refused {
		return []T{}, nil
	}
	if err != nil {
		return nil, &ProviderError{Message: "npm " + src.what + " did not decode", Err: err}
	}
	return items, nil
}

func (r *Registry) npmInventory(ctx context.Context) ([]any, error) {
	hosts, err := npmProxyHostList(ctx, r.npm(ctx, "", true))
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
			AuthorizationCount: len(deref(list.Items)),
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
	return recordsOf(certs, func(cert npmapi.CertificateObject) (NPMCertificateRecord, error) {
		id, err := npmID(cert.Id, "certificate")
		return NPMCertificateRecord{
			ID:        id,
			Name:      cert.NiceName,
			Provider:  cert.Provider,
			Domains:   cert.DomainNames,
			ExpiresOn: deref(cert.ExpiresOn),
			Serves:    distinctNames(serves[id], hostname),
		}, err
	})
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
			Hostnames:   distinctNames(domains, hostname),
			Certificate: names[int(certificate)],
			SSLForced:   bool(sslForced),
			Enabled:     bool(enabled),
		}, err
	}
	return recordsOf(hosts, func(host T) (R, error) { return record(host, base) })
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
	return recordsOf(streams, func(stream npmapi.StreamObject) (NPMStreamRecord, error) {
		id, err := npmID(stream.Id, "stream")
		return NPMStreamRecord{
			ID:             id,
			IncomingPort:   stream.IncomingPort,
			ForwardingHost: stream.ForwardingHost,
			ForwardingPort: optionalPort(stream.ForwardingPort),
			TCP:            bool(stream.TcpForwarding),
			UDP:            bool(stream.UdpForwarding),
			Enabled:        bool(stream.Enabled),
		}, err
	})
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
	return recordsOf(lists, func(list npmapi.AccessListObject) (NPMAccessListRecord, error) {
		id, err := npmID(list.Id, "access list")
		logins := []string{}
		for _, login := range deref(list.Items) {
			if name := deref(login.Username); name != "" {
				logins = append(logins, name)
			}
		}
		return NPMAccessListRecord{
			ID:         id,
			Name:       list.Name,
			SatisfyAny: bool(list.SatisfyAny),
			PassAuth:   bool(list.PassAuth),
			Clients:    npmClients(list),
			Logins:     logins,
			Protects:   distinctNames(protects[id], hostname),
		}, err
	})
}
