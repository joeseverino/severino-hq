package providers

import (
	"context"
	"math"
	"net/netip"
	"net/url"
	"strings"
)

// adguardClients is /control/clients: the clients configured in AdGuard and
// the ones it only saw on the wire.
type adguardClients struct {
	Clients []struct {
		Name              string   `json:"name"`
		IDs               []string `json:"ids"`
		UseGlobalSettings *bool    `json:"use_global_settings"`
		FilteringEnabled  *bool    `json:"filtering_enabled"`
	} `json:"clients"`
	AutoClients []struct {
		IP     string `json:"ip"`
		Name   string `json:"name"`
		Source string `json:"source"`
	} `json:"auto_clients"`
}

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

func clientRecords(payload adguardClients, ref string) []any {
	found := []any{}
	claimed := map[string]bool{}
	for _, client := range payload.Clients {
		ids := []string{}
		addresses := []string{}
		seen := map[string]bool{}
		for _, value := range client.IDs {
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
		found = append(found, AdGuardPersistentClient{ConnectionRef: ref, Name: client.Name, Source: "persistent", IDs: ids, Addresses: addresses, UseGlobalSettings: client.UseGlobalSettings, FilteringEnabled: client.FilteringEnabled})
	}
	for _, client := range payload.AutoClients {
		ip := address(client.IP)
		if ip == "" || claimed[ip] {
			continue
		}
		claimed[ip] = true
		found = append(found, AdGuardSeenClient{ConnectionRef: ref, Name: client.Name, Source: strings.ToLower(client.Source), IDs: []string{ip}, Addresses: []string{ip}})
	}
	return found
}

func (r *Registry) adguardClients(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs("adguard") {
		payload, err := adguardGet[adguardClients](ctx, r, ref, "/control/clients", "AdGuard returned an invalid client list.")
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
		for _, part := range strings.Split(scope, "/") {
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

type adguardDNSInfo struct {
	UpstreamDNS   []string `json:"upstream_dns"`
	UpstreamMode  string   `json:"upstream_mode"`
	DNSSECEnabled *bool    `json:"dnssec_enabled"`
}
type adguardFilteringStatus struct {
	Enabled *bool `json:"enabled"`
	Filters []struct {
		Enabled    bool    `json:"enabled"`
		RulesCount float64 `json:"rules_count"`
	} `json:"filters"`
}
type adguardQuerylogConfig struct {
	Enabled           *bool    `json:"enabled"`
	Interval          *float64 `json:"interval"`
	AnonymizeClientIP *bool    `json:"anonymize_client_ip"`
}
type adguardRewriteSettings struct {
	Enabled *bool `json:"enabled"`
}

func (r *Registry) adguardDNS(ctx context.Context) ([]any, error) {
	found := []any{}
	for _, ref := range r.refs("adguard") {
		status, err := adguardGet[adguardStatus](ctx, r, ref, "/control/status", "AdGuard did not return a status.")
		if err != nil {
			return nil, err
		}
		addresses, _ := decodeAs[[]string](status.DNSAddresses, "")
		if addresses == nil {
			addresses = []string{}
		}
		record := AdGuardDNSRecord{ConnectionRef: ref, Version: status.Version, Running: status.Running, ProtectionEnabled: status.ProtectionEnabled, DNSAddresses: addresses}
		const invalid = "AdGuard returned an invalid setting."
		if info, err := adguardGet[adguardDNSInfo](ctx, r, ref, "/control/dns_info", invalid); err != nil {
			refuse(ctx, "upstreams", ref, "", err)
		} else {
			upstreams := []AdGuardUpstream{}
			for _, line := range info.UpstreamDNS {
				if parsed, ok := upstream(line); ok {
					upstreams = append(upstreams, parsed)
				}
			}
			record.AdGuardUpstreamsPart = &AdGuardUpstreamsPart{Upstreams: upstreams, UpstreamMode: info.UpstreamMode, DNSSECEnabled: info.DNSSECEnabled}
		}
		if info, err := adguardGet[adguardFilteringStatus](ctx, r, ref, "/control/filtering/status", invalid); err != nil {
			refuse(ctx, "filtering", ref, "", err)
		} else {
			part := &AdGuardFilteringPart{FilteringEnabled: info.Enabled}
			for _, filter := range info.Filters {
				if filter.Enabled {
					part.FilterLists++
					part.FilterRules += int(filter.RulesCount)
				}
			}
			record.AdGuardFilteringPart = part
		}
		if info, err := adguardGet[adguardQuerylogConfig](ctx, r, ref, "/control/querylog/config", invalid); err != nil {
			refuse(ctx, "querylog", ref, "", err)
		} else {
			part := &AdGuardQuerylogPart{QuerylogEnabled: info.Enabled, AnonymizeClientIP: info.AnonymizeClientIP}
			if info.Interval != nil {
				hours := math.RoundToEven(*info.Interval/3600000*100) / 100
				part.QuerylogRetentionHours = &hours
			}
			record.AdGuardQuerylogPart = part
		}
		if info, err := adguardGet[adguardRewriteSettings](ctx, r, ref, "/control/rewrite/settings", invalid); err != nil {
			refuse(ctx, "rewrites", ref, "", err)
		} else {
			record.AdGuardRewritesPart = &AdGuardRewritesPart{RewritesEnabled: info.Enabled}
		}
		found = append(found, record)
	}
	return found, nil
}
