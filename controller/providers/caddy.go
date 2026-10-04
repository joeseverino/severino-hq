package providers

import (
	"cmp"
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Caddy routes: read from each edge's running config over SSH, and written as
// the one HQ-owned Caddyfile the edge imports. Routes in the operator's own
// file are read, never written.

// The SSH operations an edge target answers.
const (
	caddyRoutesOperation       = "routes"
	caddyRoutesWriteOperation  = "routes:write"
	caddyCertificatesOperation = "certificates"
	// caddyCertificateOperation is a target older than version 4: the one leaf
	// in its certificate directory.
	caddyCertificateOperation = "certificate"
	caddyRole                 = "caddy"
)

// Why a route carries no certificate, when the edge could not say.
const (
	caddyNoCertificateOperation = "the edge target does not report its certificates; redeploy it at version 4 or later"
	caddySomeUnreported         = "the edge loads %d certificates from files and its target reports %d; redeploy it at version 4 or later"
	caddyManagesCertificate     = "Caddy manages this name's certificate itself, and the edge reports only the certificate it loads from a file"
)

const caddyFileHeader = "# Written by Severino HQ. Edits here are replaced on the next reconcile;\n" +
	"# routes this file does not name are the operator's and are untouched.\n"

// caddyConfig is the part of Caddy's JSON config (GET /config/ on its admin
// API) a route reading needs.
type caddyConfig struct {
	Apps struct {
		HTTP struct {
			Servers map[string]caddyServer `json:"servers"`
		} `json:"http"`
		TLS struct {
			Certificates struct {
				LoadFiles []caddyLoadedFile `json:"load_files"`
			} `json:"certificates"`
		} `json:"tls"`
	} `json:"apps"`
}

type caddyServer struct {
	Routes []caddyRoute `json:"routes"`
}

// caddyRoute is caddyhttp.Route: matcher sets and a handler chain.
type caddyRoute struct {
	Match  []caddyMatch   `json:"match"`
	Handle []caddyHandler `json:"handle"`
}

type caddyMatch struct {
	Host []string `json:"host"`
}

// caddyHandler is one handler module. A subroute nests routes; a reverse
// proxy names upstreams and may route its responses again.
type caddyHandler struct {
	Handler        string                `json:"handler"`
	Upstreams      []caddyUpstream       `json:"upstreams"`
	Routes         []caddyRoute          `json:"routes"`
	HandleResponse []caddyResponseRoutes `json:"handle_response"`
}

type caddyUpstream struct {
	Dial string `json:"dial"`
}

type caddyResponseRoutes struct {
	Routes []caddyRoute `json:"routes"`
}

// caddyLoadedFile is tls.certificates.load_files: a certificate Caddy loads
// from disk rather than obtaining itself.
type caddyLoadedFile struct {
	Certificate string `json:"certificate"`
}

// CaddyRouteRecord is one hostname an edge answers for.
type CaddyRouteRecord struct {
	ConnectionRef string `json:"connection_ref"`
	Domain        string `json:"domain"`
	// Upstream is the one address the route forwards to, a placeholder kept as
	// Caddy holds it; empty when Caddy answers itself or balances several.
	Upstream          string            `json:"upstream"`
	ToRequestedHost   bool              `json:"to_requested_host"`
	Certificate       *CaddyCertificate `json:"certificate,omitempty"`
	CertificateUnread string            `json:"certificate_unread,omitempty"`
}

// CaddyCertificate is a loaded certificate's public facts.
type CaddyCertificate struct {
	Name      string   `json:"name"`
	Provider  string   `json:"provider"`
	ExpiresOn string   `json:"expires_on"`
	Domains   []string `json:"domains"`
}

// CaddyRouteSpec is a resolved caddy.route: the complete set of routes HQ
// declares for one edge.
type CaddyRouteSpec struct {
	ConnectionRef        string             `json:"connection_ref"`
	CertificateDirectory string             `json:"certificate_directory"`
	Routes               []CaddyRouteInFile `json:"routes"`
}

// CaddyRouteInFile is one route of that set.
type CaddyRouteInFile = runtime.CaddyRouteInFile

// CaddyRouteStatus is what a reconcile reports.
type CaddyRouteStatus struct {
	Routes int `json:"routes"`
}

func (r *Registry) admitCaddy() {
	r.reader(runtime.ResourceKindCaddyRoute, r.caddyRoutes)
	act(r, runtime.ResourceKindCaddyRoute, "reconcile", r.caddyReconcile)
}

// caddyRoutes reads the SSH connections declared as Caddy hosts. Only while no
// connection declares a role is every SSH connection asked, so a fleet that
// never declared one still discovers its edges; a declared fleet never logs in
// to a machine to find out it is not an edge. A host that does not answer is
// skipped, its failure recorded as a step, so one down edge is not a blackout.
func (r *Registry) caddyRoutes(ctx context.Context) ([]any, error) {
	refs := r.Env.RoleRefs(caddyRole)
	if len(refs) == 0 {
		refs = r.Env.SSHRefs()
	}
	found := []any{}
	for _, ref := range refs {
		payload, err := r.commands().SSH(ctx, ref, caddyRoutesOperation, nil)
		if err != nil {
			continue
		}
		var config caddyConfig
		if err := json.Unmarshal(payload, &config); err != nil {
			r.commands().record("SSH "+caddyRoutesOperation+" for "+ref, ref, "config did not decode")
			continue
		}
		certificates, unread := r.caddyServed(ctx, ref, config)
		for _, route := range caddyRouteRecords(config, ref) {
			if certificate := coveringCertificate(route.Domain, certificates); certificate != nil {
				route.Certificate = certificate
			} else {
				route.CertificateUnread = orDefault(unread, caddyManagesCertificate)
			}
			found = append(found, route)
		}
	}
	return found, nil
}

// caddyRouteRecords is one record per hostname, the first route naming a host
// deciding it. Servers are read in name order so the result is stable.
func caddyRouteRecords(config caddyConfig, ref string) []CaddyRouteRecord {
	records := []CaddyRouteRecord{}
	seen := map[string]bool{}
	for _, name := range sortedKeys(config.Apps.HTTP.Servers) {
		for _, route := range config.Apps.HTTP.Servers[name].Routes {
			hosts := []string{}
			for _, match := range route.Match {
				for _, host := range match.Host {
					if strings.TrimSpace(host) != "" {
						hosts = append(hosts, hostname(host))
					}
				}
			}
			if len(hosts) == 0 {
				continue
			}
			destinations := caddyUpstreams(route.Handle, nil)
			upstream := ""
			if len(destinations) == 1 {
				upstream = destinations[0]
			}
			for _, host := range hosts {
				if seen[host] {
					continue
				}
				seen[host] = true
				records = append(records, CaddyRouteRecord{
					ConnectionRef: ref, Domain: host, Upstream: upstream, ToRequestedHost: toRequestedHost(upstream),
				})
			}
		}
	}
	return records
}

// caddyUpstreams is every distinct address a handler chain forwards to,
// however deeply Caddy nested it, in the order first named.
func caddyUpstreams(handlers []caddyHandler, found []string) []string {
	for _, handler := range handlers {
		if handler.Handler == "reverse_proxy" {
			for _, upstream := range handler.Upstreams {
				if dial := strings.TrimSpace(upstream.Dial); dial != "" && !slices.Contains(found, dial) {
					found = append(found, dial)
				}
			}
		}
		for _, route := range handler.Routes {
			found = caddyUpstreams(route.Handle, found)
		}
		for _, response := range handler.HandleResponse {
			for _, route := range response.Routes {
				found = caddyUpstreams(route.Handle, found)
			}
		}
	}
	return found
}

// requestedHost is an upstream naming the host each request names, with an
// optional fixed port.
var requestedHost = api.MustPattern("CaddyRequestedHost")

func toRequestedHost(upstream string) bool {
	return requestedHost.MatchString(strings.TrimSpace(upstream))
}

// caddyServed is every certificate this edge loads from a file, and why a
// route none of them covers names no certificate.
func (r *Registry) caddyServed(ctx context.Context, ref string, config caddyConfig) ([]CaddyCertificate, string) {
	loaded := map[string]bool{}
	for _, file := range config.Apps.TLS.Certificates.LoadFiles {
		loaded[file.Certificate] = true
	}
	if len(loaded) == 0 {
		return nil, caddyManagesCertificate
	}
	leaves, err := r.commands().SSH(ctx, ref, caddyCertificatesOperation, nil)
	if err != nil {
		if leaves, err = r.commands().SSH(ctx, ref, caddyCertificateOperation, nil); err != nil {
			return nil, caddyNoCertificateOperation
		}
	}
	certificates, err := pemCertificates(leaves)
	if err != nil {
		return nil, "its certificate did not parse: " + err.Error()
	}
	switch {
	case len(certificates) == 0:
		return nil, caddyNoCertificateOperation
	case len(certificates) < len(loaded):
		return certificates, fmt.Sprintf(caddySomeUnreported, len(loaded), len(certificates))
	}
	return certificates, ""
}

// pemCertificates is every certificate in a PEM bundle, in the order given.
// Only public facts are kept; any other block is skipped, never recorded.
func pemCertificates(data []byte) ([]CaddyCertificate, error) {
	found := []CaddyCertificate{}
	for {
		var block *pem.Block
		block, data = pem.Decode(data)
		if block == nil {
			return found, nil
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		leaf, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, err
		}
		found = append(found, caddyCertificateFacts(leaf))
	}
}

func caddyCertificateFacts(leaf *x509.Certificate) CaddyCertificate {
	domains := []string{}
	for _, name := range leaf.DNSNames {
		if name = hostname(name); name != "" && !slices.Contains(domains, name) {
			domains = append(domains, name)
		}
	}
	name := leaf.Subject.CommonName
	if name == "" && len(domains) > 0 {
		name = domains[0]
	}
	return CaddyCertificate{Name: name, Provider: issuerName(leaf), ExpiresOn: stamp(leaf.NotAfter), Domains: domains}
}

// coveringCertificate is the loaded certificate a name is served with: one
// naming the host over one covering it by wildcard, then the one valid
// longest, which is how Caddy chooses among the certificates it holds.
func coveringCertificate(domain string, certificates []CaddyCertificate) *CaddyCertificate {
	name := hostname(domain)
	var best *CaddyCertificate
	bestExact := false
	for i := range certificates {
		certificate := &certificates[i]
		names := nameSet(certificate.Domains)
		if !certificateCovers(name, names) {
			continue
		}
		exact := names[name]
		if best == nil || (exact && !bestExact) || (exact == bestExact && certificate.ExpiresOn > best.ExpiresOn) {
			best, bestExact = certificate, exact
		}
	}
	if best == nil {
		return nil
	}
	chosen := *best
	return &chosen
}

// What may reach the Caddyfile. The file is text, so a value carrying a
// newline or a brace would become directives of its own; each is one token.
// The patterns are the contract's, which HQ's caddy.route declaration validates
// with, and they are checked again here, on the line that writes the file.
var (
	caddyDomainPattern    = api.MustPattern("CaddyRouteInFile", "properties", "domain")
	caddyUpstreamPattern  = api.MustPattern("CaddyRouteInFile", "properties", "upstream")
	caddyDirectoryPattern = api.MustPattern("CaddyCertificateDirectory")
)

func caddyToken(value string, pattern *regexp.Regexp, what string) error {
	if !pattern.MatchString(value) {
		return &ProviderError{Message: fmt.Sprintf("caddy route %s %q is not one plain value", what, value)}
	}
	return nil
}

// renderCaddyRoutes is the complete HQ-owned file for one edge: every declared
// route with somewhere to forward, by hostname.
func renderCaddyRoutes(routes []CaddyRouteInFile, certificateDirectory string) (string, error) {
	if certificateDirectory != "" {
		if err := caddyToken(certificateDirectory, caddyDirectoryPattern, "certificate directory"); err != nil {
			return "", err
		}
	}
	kept := []CaddyRouteInFile{}
	for _, route := range routes {
		if route.Domain != "" && route.Upstream != "" {
			kept = append(kept, route)
		}
	}
	slices.SortStableFunc(kept, func(a, b CaddyRouteInFile) int { return cmp.Compare(a.Domain, b.Domain) })
	blocks := make([]string, 0, len(kept))
	for _, route := range kept {
		if err := caddyToken(route.Domain, caddyDomainPattern, "hostname"); err != nil {
			return "", err
		}
		if err := caddyToken(route.Upstream, caddyUpstreamPattern, "upstream"); err != nil {
			return "", err
		}
		lines := []string{route.Domain + " {"}
		if certificateDirectory != "" {
			directory := strings.TrimRight(certificateDirectory, "/")
			lines = append(lines, "\ttls "+directory+"/fullchain.pem "+directory+"/privkey.pem")
		}
		lines = append(lines, "\treverse_proxy "+route.Upstream, "}")
		blocks = append(blocks, strings.Join(lines, "\n"))
	}
	return caddyFileHeader + "\n" + strings.Join(blocks, "\n\n") + "\n", nil
}

// caddyReconcile converges the complete route file one resolved resource
// stands for. The file is rendered, and so checked, in a plan as well.
func (r *Registry) caddyReconcile(ctx context.Context, spec CaddyRouteSpec, _ Object, apply bool) (Result, error) {
	rendered, err := renderCaddyRoutes(spec.Routes, spec.CertificateDirectory)
	if err != nil {
		return Result{}, err
	}
	status := CaddyRouteStatus{Routes: len(spec.Routes)}
	if !apply {
		return Result{Status: status, Conditions: []Condition{}, Message: "Would write the routes this edge serves."}, nil
	}
	if _, err := r.commands().SSH(ctx, spec.ConnectionRef, caddyRoutesWriteOperation, []byte(rendered)); err != nil {
		return Result{}, err
	}
	return result(true, status, "Written", "Caddy reloaded with these routes.", "Routes written and Caddy reloaded."), nil
}
