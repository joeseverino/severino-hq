package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
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

// cloudflareAccountItems reads one account list once per sweep and decodes
// each entry into T.
func cloudflareAccountItems[T any](ctx context.Context, r *Registry, ref, path string, perPage int) ([]T, error) {
	account, err := r.cloudflareAccount(ctx, ref)
	if err != nil {
		return nil, err
	}
	items, err := r.cloudflareCachedList(ctx, "cloudflare-account-list:"+ref+":"+path, func() ([]json.RawMessage, error) {
		return r.cloudflareList(ctx, runtime.ConnectionProviderCloudflareAPI, "/accounts/"+account+path, ref, perPage)
	})
	if err != nil {
		return nil, err
	}
	return cloudflareItems[T](items, path+" entry")
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
			size, err := r.cloudflareD1Size(ctx, account, database.UUID, ref)
			if err != nil {
				refuse(ctx, runtime.PartFileSize, ref, record.Name, err)
			}
			record.FileSize = size
			found = append(found, record)
		}
	}
	return found, nil
}

// cloudflareD1Size is a database's size in bytes; the list omits it.
func (r *Registry) cloudflareD1Size(ctx context.Context, account, uuid, ref string) (*int64, error) {
	raw, err := r.cloudflareAPIResult(ctx, "/accounts/"+account+"/d1/database/"+uuid, ref)
	if err != nil || !present(raw) {
		return nil, err
	}
	detail, err := cloudflareDecode[struct {
		FileSize *int64 `json:"file_size"`
	}](raw, "D1 database")
	return detail.FileSize, err
}

// accessDestinationHosts are hostnames from an application's destinations,
// without paths or CIDRs.
func accessDestinationHosts(app cfAccessApp) []string {
	hosts := []string{}
	for _, destination := range app.Destinations {
		host := destination.Hostname
		if destination.Type == "public" {
			uri := destination.URI
			if _, after, found := strings.Cut(uri, "://"); found {
				uri = after
			}
			host, _, _ = strings.Cut(uri, "/")
		}
		if host = hostname(host); host != "" && !slices.Contains(hosts, host) {
			hosts = append(hosts, host)
		}
	}
	return hosts
}

func accessPolicies(app cfAccessApp) []CloudflareNamedRef {
	found := []CloudflareNamedRef{}
	for _, policy := range app.Policies {
		found = append(found, CloudflareNamedRef{ID: policy.ID, Name: policy.Name})
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
				ConnectionRef: ref, AccountID: account, ID: app.ID, Name: app.Name, Type: app.Type, Domain: app.Domain,
				Destinations: accessDestinationHosts(app), SessionDuration: app.SessionDuration, Policies: accessPolicies(app),
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
			found = append(found, CloudflareNamedRef{ID: app.ID, Name: app.Name})
		}
	}
	return found
}

func appAdmits(app cfAccessApp, tokenID string) bool {
	for _, policy := range app.Policies {
		for _, rule := range policy.Include {
			if rule.AnyValidServiceToken != nil || (rule.ServiceToken != nil && rule.ServiceToken.TokenID == tokenID) {
				return true
			}
		}
	}
	return false
}

// cloudflareServiceTokens reads service tokens and the applications that admit
// them. The client ID is never read into a record.
func (r *Registry) cloudflareServiceTokens(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.cloudflareAPIRefs() {
		tokens, err := cloudflareAccountItems[cfapi.AccessServiceTokens](ctx, r, ref, "/access/service_tokens", cloudflarePerPage)
		if err != nil {
			return nil, err
		}
		apps, appsErr := cloudflareAccountItems[cfAccessApp](ctx, r, ref, "/access/apps", cloudflarePerPage)
		if appsErr != nil {
			refuse(ctx, runtime.PartApps, ref, "", appsErr)
		}
		for _, token := range tokens {
			record := CloudflareServiceTokenRecord{ConnectionRef: ref, ID: token.ID, Name: token.Name, ExpiresAt: token.ExpiresAt, CreatedAt: token.CreatedAt}
			if appsErr == nil {
				admitting := appsAdmitting(apps, token.ID)
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
			if config, err := cloudflareOptional[cfapi.TunnelConfiguration](r.cloudflareAPIResult(ctx, base+"/configurations", ref)); err != nil {
				refuse(ctx, runtime.PartConfiguration, ref, record.Name, err)
			} else {
				source := string(config.Source)
				ingress := []CloudflareTunnelIngress{}
				for _, rule := range config.Config.Ingress {
					if rule.Hostname != "" {
						ingress = append(ingress, CloudflareTunnelIngress{Hostname: rule.Hostname, Service: rule.Service})
					}
				}
				record.ConfigSource, record.Ingress = &source, &ingress
			}
			if clients, err := cloudflareOptional[[]cfapi.TunnelTunnelClient](r.cloudflareAPIResult(ctx, base+"/connections", ref)); err != nil {
				refuse(ctx, runtime.PartConnections, ref, record.Name, err)
			} else {
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
			packs, err := r.cloudflareCertificatePacks(ctx, zone.ID, ref)
			if err != nil {
				refused = append(refused, err)
				refuse(ctx, runtime.PartWhole, ref, name, err)
				continue
			}
			for _, pack := range packs {
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
			return nil, fmt.Errorf("every zone refused its certificate packs: %w", refused[0])
		}
	}
	return found, nil
}

func (r *Registry) cloudflareCertificatePacks(ctx context.Context, zoneID, ref string) ([]cfapi.TLSCertificatesAndHostnamesCertificatePack, error) {
	items, err := r.cloudflareList(ctx, runtime.ConnectionProviderCloudflareAPI, "/zones/"+zoneID+"/ssl/certificate_packs?status=all", ref, cloudflareAccountPerPage)
	if err != nil {
		return nil, err
	}
	return cloudflareItems[cfapi.TLSCertificatesAndHostnamesCertificatePack](items, "certificate pack")
}

// cloudflareOptional decodes a result that may be absent or null, which reads
// as the zero value.
func cloudflareOptional[T any](raw json.RawMessage, err error) (T, error) {
	var zero T
	if err != nil || !present(raw) {
		return zero, err
	}
	return cloudflareDecode[T](raw, "result")
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
	slices.Sort(dates)
	if len(dates) == 0 {
		return ""
	}
	return dates[0]
}
