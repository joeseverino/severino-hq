package providers

import (
	"context"
	"net/netip"
	"net/url"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/adguardapi"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

type AdGuardPersistentClient struct {
	ConnectionRef     string   `json:"connection_ref"`
	Name              string   `json:"name"`
	Source            string   `json:"source"`
	IDs               []string `json:"ids"`
	Addresses         []string `json:"addresses"`
	UseGlobalSettings *bool    `json:"use_global_settings"`
	FilteringEnabled  *bool    `json:"filtering_enabled"`
}

type AdGuardSeenClient struct {
	ConnectionRef string   `json:"connection_ref"`
	Name          string   `json:"name"`
	Source        string   `json:"source"`
	IDs           []string `json:"ids"`
	Addresses     []string `json:"addresses"`
}

func address(value string) string {
	parsed, err := netip.ParseAddr(strings.TrimSpace(value))
	if err != nil {
		return ""
	}
	return parsed.String()
}

func clientRecords(payload adguardapi.Clients, ref string) []any {
	found := []any{}
	claimed := map[string]bool{}
	for _, client := range deref(payload.Clients) {
		ids := []string{}
		addresses := []string{}
		seen := map[string]bool{}
		for _, value := range deref(client.Ids) {
			id := strings.TrimSpace(value)
			if id == "" {
				continue
			}
			ids = append(ids, id)
			if ip := address(id); ip != "" && !seen[ip] {
				addresses = append(addresses, ip)
				seen[ip] = true
				claimed[ip] = true
			}
		}
		found = append(found, AdGuardPersistentClient{ConnectionRef: ref, Name: deref(client.Name), Source: "persistent", IDs: ids, Addresses: addresses, UseGlobalSettings: client.UseGlobalSettings, FilteringEnabled: client.FilteringEnabled})
	}
	for _, client := range deref(payload.AutoClients) {
		ip := address(deref(client.Ip))
		if ip == "" || claimed[ip] {
			continue
		}
		claimed[ip] = true
		found = append(found, AdGuardSeenClient{ConnectionRef: ref, Name: deref(client.Name), Source: strings.ToLower(deref(client.Source)), IDs: []string{ip}, Addresses: []string{ip}})
	}
	return found
}

func (r *Registry) adguardClients(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs(runtime.ConnectionProviderAdGuard) {
		payload, err := adguardGet[adguardapi.Clients](ctx, r, ref, "/control/clients", "client list")
		if err != nil {
			return nil, err
		}
		found = append(found, clientRecords(payload, ref)...)
	}
	return found, nil
}

type AdGuardUpstream struct {
	Host      string   `json:"host"`
	Transport string   `json:"transport"`
	Domains   []string `json:"domains"`
}

// upstream parses one upstream_dns line, or reports false for a comment, a
// blank line or a scheme AdGuard would not use.
func upstream(value string) (AdGuardUpstream, bool) {
	line := strings.TrimSpace(value)
	domains := []string{}
	if strings.HasPrefix(line, "[/") && strings.Contains(line, "/]") {
		scope, rest, _ := strings.Cut(line[2:], "/]")
		line = strings.TrimSpace(rest)
		for part := range strings.SplitSeq(scope, "/") {
			if name := hostname(part); name != "" {
				domains = append(domains, name)
			}
		}
	}
	if line == "" || strings.HasPrefix(line, "#") {
		return AdGuardUpstream{}, false
	}
	scheme, rest, hasScheme := strings.Cut(line, "://")
	if !hasScheme {
		scheme, rest = "", line
	}
	scheme = strings.ToLower(scheme)
	transport, ok := map[string]string{"": "dns", "udp": "dns", "tcp": "dns", "tls": "tls", "https": "https", "h3": "https", "quic": "quic", "sdns": "dnscrypt"}[scheme]
	if !ok {
		return AdGuardUpstream{}, false
	}
	host := ""
	if scheme != "sdns" {
		parsed, err := url.Parse("dns://" + rest)
		if err != nil {
			return AdGuardUpstream{}, false
		}
		host = parsed.Hostname()
	}
	return AdGuardUpstream{Host: host, Transport: transport, Domains: domains}, true
}

// The parts of an adguard.dns record, each read from its own endpoint. A part
// whose read is refused is left out of the record, not nulled.
type AdGuardUpstreamsPart struct {
	Upstreams     []AdGuardUpstream `json:"upstreams"`
	UpstreamMode  string            `json:"upstream_mode"`
	DNSSECEnabled *bool             `json:"dnssec_enabled"`
}
type AdGuardFilteringPart struct {
	FilteringEnabled *bool `json:"filtering_enabled"`
	FilterLists      int   `json:"filter_lists"`
	FilterRules      int   `json:"filter_rules"`
}
type AdGuardQuerylogPart struct {
	QuerylogEnabled        *bool    `json:"querylog_enabled"`
	QuerylogRetentionHours *float64 `json:"querylog_retention_hours"`
	AnonymizeClientIP      *bool    `json:"anonymize_client_ip"`
}
type AdGuardRewritesPart struct {
	RewritesEnabled *bool `json:"rewrites_enabled"`
}

type AdGuardDNSRecord struct {
	ConnectionRef     string   `json:"connection_ref"`
	Version           string   `json:"version"`
	Running           *bool    `json:"running"`
	ProtectionEnabled *bool    `json:"protection_enabled"`
	DNSAddresses      []string `json:"dns_addresses"`
	*AdGuardUpstreamsPart
	*AdGuardFilteringPart
	*AdGuardQuerylogPart
	*AdGuardRewritesPart
}

func (r *Registry) adguardDNS(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs(runtime.ConnectionProviderAdGuard) {
		status, err := adguardGet[adguardapi.ServerStatus](ctx, r, ref, "/control/status", "status")
		if err != nil {
			return nil, err
		}
		// Null, absent and [] all mean AdGuard listens on no address.
		addresses := deref(status.DnsAddresses)
		if addresses == nil {
			addresses = []string{}
		}
		record := AdGuardDNSRecord{ConnectionRef: ref, Version: deref(status.Version), Running: status.Running, ProtectionEnabled: status.ProtectionEnabled, DNSAddresses: addresses}
		if info, err := adguardGet[adguardapi.DNSConfig](ctx, r, ref, "/control/dns_info", "dns settings"); err != nil {
			refuse(ctx, runtime.PartUpstreams, ref, "", err)
		} else {
			upstreams := []AdGuardUpstream{}
			for _, line := range deref(info.UpstreamDns) {
				if parsed, ok := upstream(line); ok {
					upstreams = append(upstreams, parsed)
				}
			}
			record.AdGuardUpstreamsPart = &AdGuardUpstreamsPart{Upstreams: upstreams, UpstreamMode: string(deref(info.UpstreamMode)), DNSSECEnabled: info.DnssecEnabled}
		}
		if info, err := adguardGet[adguardapi.FilterStatus](ctx, r, ref, "/control/filtering/status", "filtering settings"); err != nil {
			refuse(ctx, runtime.PartFiltering, ref, "", err)
		} else {
			part := &AdGuardFilteringPart{FilteringEnabled: info.Enabled}
			for _, filter := range deref(info.Filters) {
				if deref(filter.Enabled) {
					part.FilterLists++
					part.FilterRules += int(deref(filter.RulesCount))
				}
			}
			record.AdGuardFilteringPart = part
		}
		if info, err := adguardGet[adguardapi.GetQueryLogConfigResponse](ctx, r, ref, "/control/querylog/config", "query log settings"); err != nil {
			refuse(ctx, runtime.PartQueryLog, ref, "", err)
		} else {
			part := &AdGuardQuerylogPart{QuerylogEnabled: info.Enabled, AnonymizeClientIP: info.AnonymizeClientIp}
			if info.Interval != nil {
				hours := roundTo(*info.Interval/float64(time.Hour/time.Millisecond), 2)
				part.QuerylogRetentionHours = &hours
			}
			record.AdGuardQuerylogPart = part
		}
		if info, err := adguardGet[adguardapi.RewriteSettings](ctx, r, ref, "/control/rewrite/settings", "rewrite settings"); err != nil {
			refuse(ctx, runtime.PartRewrites, ref, "", err)
		} else {
			record.AdGuardRewritesPart = &AdGuardRewritesPart{RewritesEnabled: info.Enabled}
		}
		found = append(found, record)
	}
	return found, nil
}
