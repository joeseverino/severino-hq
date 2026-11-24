package providers

import (
	"cmp"
	"context"
	"fmt"
	"maps"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/adguardapi"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

type AdGuardQueryClient struct {
	Address string `json:"address"`
	Name    string `json:"name"`
	Queries int    `json:"queries"`
}

// AdGuardQuerySummary is one name's last day of queries. Clients is absent
// when AdGuard anonymizes client addresses.
type AdGuardQuerySummary struct {
	ConnectionRef string                `json:"connection_ref"`
	Domain        string                `json:"domain"`
	Queries       int                   `json:"queries"`
	Blocked       int                   `json:"blocked"`
	ClientCount   int                   `json:"client_count"`
	LastSeen      string                `json:"last_seen"`
	WindowHours   float64               `json:"window_hours"`
	Clients       *[]AdGuardQueryClient `json:"clients,omitempty"`
}

type queryClient struct {
	name  string
	count int
}
type queryTally struct {
	queries, blocked int
	last             time.Time
	clients          map[string]*queryClient
}

func newTally() *queryTally { return &queryTally{clients: map[string]*queryClient{}} }
func (t *queryTally) add(entry adguardapi.QueryLogItem, when time.Time) {
	t.queries++
	if strings.HasPrefix(string(deref(entry.Reason)), "Filtered") {
		t.blocked++
	}
	if when.After(t.last) {
		t.last = when
	}
	id := address(deref(entry.Client))
	if id == "" {
		id = deref(entry.ClientId)
	}
	if id == "" {
		return
	}
	name := ""
	if entry.ClientInfo != nil {
		name = deref(entry.ClientInfo.Name)
	}
	if t.clients[id] == nil {
		t.clients[id] = &queryClient{name: name}
	}
	client := t.clients[id]
	client.count++
	if client.name == "" {
		client.name = name
	}
}

func (t *queryTally) record(domain, ref string, hours float64, anonymized bool) AdGuardQuerySummary {
	last := ""
	if !t.last.IsZero() {
		last = t.last.Format(time.RFC3339Nano)
	}
	record := AdGuardQuerySummary{ConnectionRef: ref, Domain: domain, Queries: t.queries, Blocked: t.blocked, ClientCount: len(t.clients), LastSeen: last, WindowHours: roundTo(hours, 1)}
	if !anonymized {
		keys := slices.AppendSeq([]string{}, maps.Keys(t.clients))
		slices.SortFunc(keys, func(a, b string) int {
			return cmp.Or(cmp.Compare(t.clients[b].count, t.clients[a].count), cmp.Compare(a, b))
		})
		clients := []AdGuardQueryClient{}
		for _, key := range keys[:min(10, len(keys))] {
			client := t.clients[key]
			clients = append(clients, AdGuardQueryClient{Address: key, Name: client.name, Queries: client.count})
		}
		record.Clients = &clients
	}
	return record
}

type queryNames struct {
	exact map[string]bool
	zones []string
}

func (n queryNames) covers(name string) bool {
	return n.exact[name] || slices.ContainsFunc(n.zones, func(zone string) bool {
		return name != zone && strings.HasSuffix(name, "."+zone)
	})
}

func summarizeQueries(read func(olderThan string) (adguardapi.QueryLog, error), domains []string, ref string, now time.Time, anonymized bool) ([]any, error) {
	names := queryNames{exact: map[string]bool{}}
	tallies := map[string]*queryTally{}
	for _, domain := range domains {
		name := hostname(domain)
		if name == "" {
			continue
		}
		if strings.HasPrefix(name, "*.") {
			names.zones = append(names.zones, name[2:])
		} else {
			names.exact[name] = true
			tallies[name] = newTally()
		}
	}
	oldest := now
	cutoff := now.Add(-adguardQueryWindow)
	full := false
	cursor := ""
	for page := 0; page < 10 && !full; page++ {
		answer, err := read(cursor)
		if err != nil {
			return nil, fmt.Errorf("adguard query log: %w", err)
		}
		for _, entry := range deref(answer.Data) {
			when, err := time.Parse(time.RFC3339Nano, deref(entry.Time))
			if err != nil {
				return nil, &ProviderError{Message: "adguard query log entry has no valid time", Err: err}
			}
			if when.Before(cutoff) {
				full = true
				break
			}
			if when.Before(oldest) {
				oldest = when
			}
			name := hostname(deref(deref(entry.Question).Name))
			if name != "" && names.covers(name) {
				if tallies[name] == nil {
					tallies[name] = newTally()
				}
				tallies[name].add(entry, when)
			}
		}
		cursor = deref(answer.Oldest)
		if len(deref(answer.Data)) < adguardQueryPage || cursor == "" {
			break
		}
	}
	hours := max(0, now.Sub(oldest).Hours())
	if full {
		hours = 24
	}
	keys := []string{}
	for name := range tallies {
		keys = append(keys, name)
	}
	slices.Sort(keys)
	records := []any{}
	for _, name := range keys {
		records = append(records, tallies[name].record(name, ref, hours, anonymized))
	}
	return records, nil
}

func (r *Registry) adguardQueries(ctx context.Context) ([]any, error) {
	found := []any{}
	now := r.Now().UTC()
	for _, ref := range r.refs(runtime.ConnectionProviderAdGuard) {
		client := r.adguard(ref, true)
		page := func(olderThan string) (adguardapi.QueryLog, error) {
			params := adguardapi.QueryLogParams{Limit: new(adguardQueryPage)}
			if olderThan != "" {
				params.OlderThan = &olderThan
			}
			return json200(client.QueryLogWithResponse(ctx, &params))
		}
		config, err := json200(client.GetQueryLogConfigWithResponse(ctx))
		if err != nil {
			return nil, fmt.Errorf("adguard query log settings: %w", err)
		}
		if config.Enabled != nil && !*config.Enabled {
			return nil, &ProviderError{Message: "adguard query log is off, so which names are used is unknown"}
		}
		rewrites, err := adguardRewrites(ctx, client)
		if err != nil {
			return nil, err
		}
		domains := []string{}
		for _, rewrite := range rewrites {
			if rewriteEnabled(rewrite) {
				domains = append(domains, deref(rewrite.Domain))
			}
		}
		anonymized := config.AnonymizeClientIp != nil && *config.AnonymizeClientIp
		if anonymized {
			refuse(ctx, runtime.PartClients, ref, "", &ProviderError{Message: "adguard anonymizes client addresses"})
		}
		records, err := summarizeQueries(page, domains, ref, now, anonymized)
		if err != nil {
			return nil, err
		}
		found = append(found, records...)
	}
	return found, nil
}

// roundTo rounds x to places decimal places, halves away from zero.
func roundTo(x float64, places int) float64 {
	scale := math.Pow(10, float64(places))
	return math.Round(x*scale) / scale
}
