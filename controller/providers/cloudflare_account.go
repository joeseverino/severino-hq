package providers

import (
	"context"
	"encoding/json"
	"sort"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What a Cloudflare account holds beyond its DNS, each list read once per sweep.

func (r *Registry) cloudflareAccount(ctx context.Context, ref string) (string, error) {
	raw, err := r.cached(ctx, "cloudflare-account:"+ref, func() (json.RawMessage, error) {
		account, err := r.cloudflareAnalyticsAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		return json.Marshal(account)
	})
	if err != nil {
		return "", err
	}
	var account string
	err = json.Unmarshal(raw, &account)
	return account, err
}

func (r *Registry) cloudflareAccountList(ctx context.Context, ref, path string, perPage int) ([]json.RawMessage, error) {
	account, err := r.cloudflareAccount(ctx, ref)
	if err != nil {
		return nil, err
	}
	raw, err := r.cached(ctx, "cloudflare-account-list:"+ref+":"+path, func() (json.RawMessage, error) {
		items, err := r.cloudflareAPIList(ctx, "/accounts/"+account+path, ref, perPage)
		if err != nil {
			return nil, err
		}
		return json.Marshal(items)
	})
	if err != nil {
		return nil, err
	}
	var items []json.RawMessage
	err = json.Unmarshal(raw, &items)
	return items, err
}

// cloudflareAccountItems decodes one account list into its generated item type.
func cloudflareAccountItems[T any](ctx context.Context, r *Registry, ref, path string, perPage int) ([]T, error) {
	items, err := r.cloudflareAccountList(ctx, ref, path, perPage)
	if err != nil {
		return nil, err
	}
	found := make([]T, 0, len(items))
	for _, item := range items {
		var decoded T
		if err := json.Unmarshal(item, &decoded); err != nil {
			return nil, &ProviderError{Message: "Cloudflare account list returned an invalid result."}
		}
		found = append(found, decoded)
	}
	return found, nil
}

func (r *Registry) cloudflarePagesProjects(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		projects, err := cloudflareAccountItems[cfapi.PagesProject](ctx, r, ref, "/pages/projects", 10)
		if err != nil {
			return nil, err
		}
		for _, project := range projects {
			deployment := project.CanonicalDeployment
			commit := []rune(deployment.DeploymentTrigger.Metadata.CommitHash)
			if len(commit) > 7 {
				commit = commit[:7]
			}
			domains := project.Domains
			if domains == nil {
				domains = []string{}
			}
			found = append(found, CloudflarePagesProjectRecord{
				ConnectionRef: ref, AccountID: account, Name: project.Name, Subdomain: project.Subdomain,
				Domains: domains, ProductionBranch: project.ProductionBranch, DeploymentID: deployment.ID,
				DeploymentCommit: string(commit), DeploymentCreatedOn: deployment.CreatedOn,
			})
		}
	}
	return found, nil
}

// cloudflareD1Databases reads each database once more: the list omits file_size.
func (r *Registry) cloudflareD1Databases(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		databases, err := cloudflareAccountItems[cfapi.D1DatabaseResponse](ctx, r, ref, "/d1/database", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		for _, database := range databases {
			record := CloudflareD1DatabaseRecord{ConnectionRef: ref, AccountID: account, Name: database.Name, UUID: database.UUID, CreatedAt: database.CreatedAt, Version: database.Version}
			detail, err := r.cloudflareAPIResult(ctx, "/accounts/"+account+"/d1/database/"+database.UUID, ref)
			if err != nil {
				refuse(ctx, "file_size", ref, record.Name, err)
			} else {
				// file_size is a number in the spec; HQ keeps it only when Cloudflare sent an integer.
				var size struct {
					FileSize json.RawMessage `json:"file_size"`
				}
				if isJSONObject(detail) && json.Unmarshal(detail, &size) == nil && isJSONInteger(size.FileSize) {
					n := json.Number(strings.TrimSpace(string(size.FileSize)))
					record.FileSize = &n
				}
			}
			found = append(found, record)
		}
	}
	return found, nil
}

func isJSONInteger(raw json.RawMessage) bool {
	text := strings.TrimSpace(string(raw))
	if text == "" || strings.ContainsAny(text, ".eE") {
		return false
	}
	var n json.Number
	return json.Unmarshal(raw, &n) == nil
}

// accessDestinationHosts are hostnames from an application's destinations,
// without paths or CIDRs.
func accessDestinationHosts(raw json.RawMessage) []string {
	hosts := []string{}
	var destinations []json.RawMessage
	if !pyTruthy(raw) || json.Unmarshal(raw, &destinations) != nil {
		return hosts
	}
	for _, item := range destinations {
		var destination struct {
			Type     json.RawMessage `json:"type"`
			URI      json.RawMessage `json:"uri"`
			Hostname json.RawMessage `json:"hostname"`
		}
		if !isJSONObject(item) || json.Unmarshal(item, &destination) != nil {
			continue
		}
		var host string
		if pyStringEquals(destination.Type, "public") {
			uri := pyOrText(destination.URI)
			if at := strings.Index(uri, "://"); at >= 0 {
				uri = uri[at+3:]
			}
			host, _, _ = strings.Cut(uri, "/")
		} else {
			host = pyOrText(destination.Hostname)
		}
		host = hostname(host)
		if host != "" && !contains(hosts, host) {
			hosts = append(hosts, host)
		}
	}
	return hosts
}

func contains(values []string, want string) bool {
	return indexOf(values, want) >= 0
}

func accessPolicies(raw json.RawMessage) []CloudflareNamedRef {
	found := []CloudflareNamedRef{}
	var policies []json.RawMessage
	if !pyTruthy(raw) || json.Unmarshal(raw, &policies) != nil {
		return found
	}
	for _, item := range policies {
		var policy struct {
			ID   json.RawMessage `json:"id"`
			Name json.RawMessage `json:"name"`
		}
		if isJSONObject(item) && json.Unmarshal(item, &policy) == nil {
			found = append(found, CloudflareNamedRef{ID: pyOrText(policy.ID), Name: pyOrText(policy.Name)})
		}
	}
	return found
}

func (r *Registry) cloudflareAccessApps(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		apps, err := cloudflareAccountItems[cfAccessApp](ctx, r, ref, "/access/apps", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		for _, app := range apps {
			found = append(found, CloudflareAccessAppRecord{
				ConnectionRef: ref, AccountID: account, ID: pyGetText(app.ID, ""), Name: pyOrText(app.Name),
				Type: pyOrText(app.Type), Domain: pyOrText(app.Domain), Destinations: accessDestinationHosts(app.Destinations),
				SessionDuration: pyOrText(app.SessionDuration), Policies: accessPolicies(app.Policies),
			})
		}
	}
	return found, nil
}

// appsAdmitting names the applications whose policies include the token.
func appsAdmitting(apps []cfAccessApp, tokenID string) []CloudflareNamedRef {
	found := []CloudflareNamedRef{}
	for _, app := range apps {
		if appAdmits(app, tokenID) {
			found = append(found, CloudflareNamedRef{ID: pyOrText(app.ID), Name: pyOrText(app.Name)})
		}
	}
	return found
}

func appAdmits(app cfAccessApp, tokenID string) bool {
	var policies []json.RawMessage
	if !pyTruthy(app.Policies) || json.Unmarshal(app.Policies, &policies) != nil {
		return false
	}
	for _, item := range policies {
		var policy struct {
			Include json.RawMessage `json:"include"`
		}
		if !isJSONObject(item) || json.Unmarshal(item, &policy) != nil {
			continue
		}
		var rules []json.RawMessage
		if !pyTruthy(policy.Include) || json.Unmarshal(policy.Include, &rules) != nil {
			continue
		}
		for _, rule := range rules {
			if admitsToken(rule, tokenID) {
				return true
			}
		}
	}
	return false
}

func admitsToken(raw json.RawMessage, tokenID string) bool {
	var rule map[string]json.RawMessage
	if !isJSONObject(raw) || json.Unmarshal(raw, &rule) != nil {
		return false
	}
	if _, any := rule["any_valid_service_token"]; any {
		return true
	}
	var token struct {
		TokenID json.RawMessage `json:"token_id"`
	}
	if serviceToken := rule["service_token"]; pyTruthy(serviceToken) && isJSONObject(serviceToken) {
		_ = json.Unmarshal(serviceToken, &token)
	}
	return pyOrText(token.TokenID) == tokenID
}

// cloudflareServiceTokens reads service tokens and the applications that admit
// them. The client ID is never read into a record.
func (r *Registry) cloudflareServiceTokens(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		tokens, err := cloudflareAccountItems[struct {
			ID        json.RawMessage `json:"id"`
			Name      json.RawMessage `json:"name"`
			ExpiresAt json.RawMessage `json:"expires_at"`
			CreatedAt json.RawMessage `json:"created_at"`
		}](ctx, r, ref, "/access/service_tokens", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		apps, appsErr := cloudflareAccountItems[cfAccessApp](ctx, r, ref, "/access/apps", cloudflarePerPage)
		if appsErr != nil {
			refuse(ctx, "apps", ref, "", appsErr)
		}
		for _, token := range tokens {
			tokenID := pyGetText(token.ID, "")
			record := CloudflareServiceTokenRecord{ConnectionRef: ref, ID: tokenID, Name: pyOrText(token.Name), ExpiresAt: pyOrText(token.ExpiresAt), CreatedAt: pyOrText(token.CreatedAt)}
			if appsErr == nil {
				admitting := appsAdmitting(apps, tokenID)
				record.Apps = &admitting
			}
			found = append(found, record)
		}
	}
	return found, nil
}

// cloudflareTunnels reads tunnels, their ingress and live connections; the
// list's connections field is deprecated and is not read.
func (r *Registry) cloudflareTunnels(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		account, err := r.cloudflareAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		tunnels, err := cloudflareAccountItems[cfapi.TunnelCfdTunnel](ctx, r, ref, "/cfd_tunnel?is_deleted=false", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		for _, tunnel := range tunnels {
			base := "/accounts/" + account + "/cfd_tunnel/" + tunnel.ID
			record := CloudflareTunnelRecord{ConnectionRef: ref, AccountID: account, ID: tunnel.ID, Name: tunnel.Name, Status: string(tunnel.Status), CreatedAt: tunnel.CreatedAt, ConnsActiveAt: tunnel.ConnsActiveAt}
			if raw, err := r.cloudflareAPIResult(ctx, base+"/configurations", ref); err != nil {
				refuse(ctx, "configuration", ref, record.Name, err)
			} else {
				var config cfapi.TunnelConfiguration
				if pyTruthy(raw) {
					_ = json.Unmarshal(raw, &config)
				}
				source := string(config.Source)
				ingress := []CloudflareTunnelIngress{}
				for _, rule := range config.Config.Ingress {
					if rule.Hostname != "" {
						ingress = append(ingress, CloudflareTunnelIngress{Hostname: rule.Hostname, Service: rule.Service})
					}
				}
				record.ConfigSource, record.Ingress = &source, &ingress
			}
			if raw, err := r.cloudflareAPIResult(ctx, base+"/connections", ref); err != nil {
				refuse(ctx, "connections", ref, record.Name, err)
			} else {
				var clients []cfapi.TunnelTunnelClient
				if pyTruthy(raw) {
					_ = json.Unmarshal(raw, &clients)
				}
				connections := []CloudflareTunnelConnection{}
				for _, client := range clients {
					for _, connection := range client.Conns {
						version := client.Version
						if version == "" {
							version = connection.ClientVersion
						}
						connections = append(connections, CloudflareTunnelConnection{Version: version, Colo: connection.ColoName, OriginIP: connection.OriginIP})
					}
				}
				record.Connections = &connections
			}
			found = append(found, record)
		}
	}
	return found, nil
}

// cloudflareEdgeCertificates reads certificate packs on every zone the account
// credential sees. A zone whose packs are refused is a refused part on that
// zone; every zone refused is a refused read.
func (r *Registry) cloudflareEdgeCertificates(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		zones, err := r.cloudflareAPIZones(ctx, ref)
		if err != nil {
			return nil, err
		}
		named := namedZones(zones)
		var refused []error
		for _, zone := range named {
			name := hostname(zone.Name)
			items, err := r.cloudflareAPIList(ctx, "/zones/"+zone.ID+"/ssl/certificate_packs?status=all", ref, cloudflareAccountPerPage)
			if err != nil {
				refused = append(refused, unreadError(err))
				refuse(ctx, "", ref, name, err)
				continue
			}
			for _, item := range items {
				var pack cfapi.TLSCertificatesAndHostnamesCertificatePack
				if json.Unmarshal(item, &pack) != nil {
					return nil, &ProviderError{Message: "Cloudflare account list returned an invalid result."}
				}
				hosts := []string(pack.Hosts)
				if hosts == nil {
					hosts = []string{}
				}
				found = append(found, CloudflareEdgeCertificateRecord{
					ConnectionRef: ref, AccountID: zone.Account.ID, Zone: name, ID: pack.ID, Type: string(pack.Type),
					Hosts: hosts, Status: string(pack.Status), CertificateAuthority: string(pack.CertificateAuthority), ExpiresOn: earliestExpiry(pack),
				})
			}
		}
		if len(named) > 0 && len(refused) == len(named) {
			return nil, refused[0]
		}
	}
	return found, nil
}

func namedZones(zones []cfapi.ZonesZone) []cfapi.ZonesZone {
	named := []cfapi.ZonesZone{}
	for _, zone := range zones {
		if zone.Name != "" {
			named = append(named, zone)
		}
	}
	return named
}

func earliestExpiry(pack cfapi.TLSCertificatesAndHostnamesCertificatePack) string {
	dates := []string{}
	for _, certificate := range pack.Certificates {
		if certificate.ExpiresOn != "" {
			dates = append(dates, certificate.ExpiresOn)
		}
	}
	sort.Strings(dates)
	if len(dates) == 0 {
		return ""
	}
	return dates[0]
}

// unreadError is the refused read raised when every zone was refused: the
// short reason, keeping the refusal and its words.
func unreadError(err error) error {
	refused := &ProviderError{Message: runtime.Clip(strings.TrimSpace(err.Error()), runtime.ReasonLimit)}
	if provider, ok := err.(*ProviderError); ok {
		refused.Refusal, refused.Failure, refused.Reason = provider.Refusal, runtime.FailureClass(provider.Refusal), provider.Reason
	}
	return refused
}

func (r *Registry) cloudflareRedirects(ctx context.Context) ([]any, error) {
	return ReadRedirects(ctx, r.cloudflareAPIRefs(), cloudflareZoneReads{r})
}

// cloudflareZoneReads is the Cloudflare API a redirect read goes through.
type cloudflareZoneReads struct{ r *Registry }

func (z cloudflareZoneReads) Zones(ctx context.Context, ref string) ([]RedirectZone, error) {
	listed, err := z.r.cloudflareAPIZones(ctx, ref)
	if err != nil {
		return nil, err
	}
	zones := make([]RedirectZone, 0, len(listed))
	for _, zone := range listed {
		found := RedirectZone{ID: zone.ID, Name: zone.Name}
		found.Account.ID = zone.Account.ID
		zones = append(zones, found)
	}
	return zones, nil
}

func (z cloudflareZoneReads) Listed(ctx context.Context, path, ref string) ([]json.RawMessage, error) {
	return z.r.cloudflareAPIList(ctx, path, ref, cloudflareAccountPerPage)
}

func (z cloudflareZoneReads) Result(ctx context.Context, path, ref string) (json.RawMessage, error) {
	return z.r.cloudflareAPIResult(ctx, path, ref)
}

func (z cloudflareZoneReads) Reason(err error) string { return unreadError(err).Error() }

func (z cloudflareZoneReads) Refuse(ctx context.Context, part string, err error, scope, ref string) {
	refuse(ctx, part, ref, scope, err)
}
