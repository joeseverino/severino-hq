package providers

import (
	"context"
	"encoding/json"
	"math"
	"net/url"
	"sort"
	"strings"
	"time"
)

// adguardQuerylogPage is one page of /control/querylog, newest first.
type adguardQuerylogPage struct {
	Data   []adguardQuery `json:"data"`
	Oldest string         `json:"oldest"`
}

type adguardQuery struct {
	Time     string `json:"time"`
	Reason   string `json:"reason"`
	Client   string `json:"client"`
	ClientID string `json:"client_id"`
	Question struct {
		Name string `json:"name"`
	} `json:"question"`
	ClientInfo struct {
		Name string `json:"name"`
	} `json:"client_info"`
}

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
func (t *queryTally) add(entry adguardQuery, when time.Time) {
	t.queries++
	if strings.HasPrefix(entry.Reason, "Filtered") {
		t.blocked++
	}
	if when.After(t.last) {
		t.last = when
	}
	id := address(entry.Client)
	if id == "" {
		id = entry.ClientID
	}
	if id == "" {
		return
	}
	name := entry.ClientInfo.Name
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
		last = t.last.Format("2006-01-02T15:04:05.999999-07:00")
	}
	record := AdGuardQuerySummary{ConnectionRef: ref, Domain: domain, Queries: t.queries, Blocked: t.blocked, ClientCount: len(t.clients), LastSeen: last, WindowHours: math.RoundToEven(hours*10) / 10}
	if !anonymized {
		keys := []string{}
		for key := range t.clients {
			keys = append(keys, key)
		}
		sort.Slice(keys, func(i, j int) bool {
			a, b := t.clients[keys[i]], t.clients[keys[j]]
			if a.count != b.count {
				return a.count > b.count
			}
			return keys[i] < keys[j]
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
	if n.exact[name] {
		return true
	}
	for _, zone := range n.zones {
		if name != zone && strings.HasSuffix(name, "."+zone) {
			return true
		}
	}
	return false
}

func summarizeQueries(get func(string) (json.RawMessage, error), domains []string, ref string, now time.Time, anonymized bool) ([]any, error) {
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
	cutoff := now.Add(-24 * time.Hour)
	full := false
	cursor := ""
	for page := 0; page < 10 && !full; page++ {
		path := "/control/querylog?limit=500"
		if cursor != "" {
			path += "&older_than=" + url.QueryEscape(cursor)
		}
		raw, err := get(path)
		if err != nil {
			return nil, err
		}
		answer, err := decodeAs[adguardQuerylogPage](raw, "AdGuard returned an invalid query log.")
		if err != nil {
			return nil, err
		}
		for _, entry := range answer.Data {
			when, err := time.Parse(time.RFC3339Nano, entry.Time)
			if err != nil {
				continue
			}
			if when.Before(cutoff) {
				full = true
				break
			}
			if when.Before(oldest) {
				oldest = when
			}
			name := hostname(entry.Question.Name)
			if name != "" && names.covers(name) {
				if tallies[name] == nil {
					tallies[name] = newTally()
				}
				tallies[name].add(entry, when)
			}
		}
		cursor = answer.Oldest
		if len(answer.Data) < 500 || cursor == "" {
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
	sort.Strings(keys)
	records := []any{}
	for _, name := range keys {
		records = append(records, tallies[name].record(name, ref, hours, anonymized))
	}
	return records, nil
}

func (r *Registry) adguardQueries(ctx context.Context) ([]any, error) {
	found := []any{}
	now := r.Now().UTC()
	for _, ref := range r.refs("adguard") {
		get := func(path string) (json.RawMessage, error) { return r.adguardRequest(ctx, ref, path, "GET", nil) }
		config, err := adguardGet[adguardQuerylogConfig](ctx, r, ref, "/control/querylog/config", "AdGuard returned an invalid setting.")
		if err != nil {
			return nil, err
		}
		if config.Enabled != nil && !*config.Enabled {
			return nil, &ProviderError{Message: "AdGuard's query log is off, so HQ cannot tell which names are used."}
		}
		rewrites, err := r.adguardRewrites(ctx, ref, true)
		if err != nil {
			return nil, err
		}
		domains := []string{}
		for _, rewrite := range rewrites {
			if rewrite.enabled() {
				domains = append(domains, rewrite.Domain)
			}
		}
		anonymized := config.AnonymizeClientIP != nil && *config.AnonymizeClientIP
		if anonymized {
			refuse(ctx, "clients", ref, "", &ProviderError{Message: "AdGuard anonymizes client addresses."})
		}
		records, err := summarizeQueries(get, domains, ref, now, anonymized)
		if err != nil {
			return nil, err
		}
		found = append(found, records...)
	}
	return found, nil
}
