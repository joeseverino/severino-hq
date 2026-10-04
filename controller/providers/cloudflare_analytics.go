package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Site analytics read through Cloudflare's GraphQL API.

// analyticsDimensions: which Cloudflare dimension answers each breakdown HQ
// stores. The query and the payload are both generated from this.
var analyticsDimensions = []struct{ name, field string }{
	{"path", "requestPath"},
	{"referrer", "refererHost"},
	{"country", "countryName"},
	{"device", "deviceType"},
	{"browser", "userAgentBrowser"},
	{"os", "userAgentOS"},
}

// analyticsVitals: the p75 percentiles HQ keeps, p75 being where Core Web
// Vitals is judged.
var analyticsVitals = []struct{ column, field string }{
	{"largest_contentful_paint_ms", "largestContentfulPaintP75"},
	{"interaction_to_next_paint_ms", "interactionToNextPaintP75"},
	{"first_contentful_paint_ms", "firstContentfulPaintP75"},
	{"time_to_first_byte_ms", "timeToFirstByteP75"},
}

var analyticsBuckets = []string{"lcp", "inp", "cls"}

var analyticsBucketSuffixes = []struct{ suffix, column string }{{"Good", "good"}, {"NeedsImprovement", "needs_improvement"}, {"Poor", "poor"}}

// maxQueryDays is Cloudflare's widest accepted query (13w2d).
const maxQueryDays = 93

// cloudflareGraphQL answers 200 with an errors array rather than a status, so a
// failed query must not read as a site nobody visited.
func (r *Registry) cloudflareGraphQL(ctx context.Context, query string, variables any, ref string) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix("cloudflare_api", ref)
	if err != nil {
		return nil, err
	}
	if err := r.cloudflareBreaker(prefix); err != nil {
		return nil, err
	}
	base, err := r.cloudflareURL("cloudflare_api", ref)
	if err != nil {
		return nil, err
	}
	headers, err := r.cloudflareHeaders("cloudflare_api", ref)
	if err != nil {
		return nil, err
	}
	delete(headers, "Accept")
	raw, err := r.HTTP.Request(ctx, base+"/graphql", "POST", headers, struct {
		Query     string `json:"query"`
		Variables any    `json:"variables"`
	}{query, variables})
	if err != nil {
		var refused *ProviderError
		if errors.As(err, &refused) && refused.HTTPStatus != 0 {
			detail := cloudflareErrors(refused.Body)
			return nil, r.cloudflareRefused(prefix, fmt.Sprintf("Cloudflare analytics refused the query: HTTP %d.", refused.HTTPStatus), detail, refused.HTTPStatus, func() bool {
				return r.cloudflareVerified(ctx, "cloudflare_api", ref)
			})
		}
		if errors.As(err, &refused) && refused.Failure == runtime.FailureClassNetwork {
			return nil, &ProviderError{Message: "Cloudflare analytics was unreachable: URLError.", Failure: runtime.FailureClassNetwork}
		}
		return nil, cloudflareTransportError(err, "Cloudflare analytics was unreachable", "Cloudflare analytics returned invalid JSON.")
	}
	var payload struct {
		Errors []json.RawMessage `json:"errors"`
		Data   json.RawMessage   `json:"data"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &payload) != nil {
		return nil, &ProviderError{Message: "Cloudflare analytics returned invalid JSON."}
	}
	if len(payload.Errors) > 0 {
		var first struct {
			Message json.RawMessage `json:"message"`
		}
		message := ""
		if isJSONObject(payload.Errors[0]) && json.Unmarshal(payload.Errors[0], &first) == nil {
			message = pyGetText(first.Message, "")
		}
		return nil, r.cloudflareRefused(prefix, "Cloudflare analytics rejected the query: "+message, message, 0, nil)
	}
	if !pyTruthy(payload.Data) {
		return json.RawMessage("{}"), nil
	}
	return payload.Data, nil
}

// cloudflareAnalyticsAccount is the one account this credential reads,
// discovered rather than configured.
func (r *Registry) cloudflareAnalyticsAccount(ctx context.Context, ref string) (string, error) {
	items, err := r.cloudflareAPIList(ctx, "/accounts", ref, cloudflareAccountPerPage)
	if err != nil {
		return "", err
	}
	tags := []string{}
	for _, item := range items {
		var account cfapi.IamAccount
		if json.Unmarshal(item, &account) == nil && account.ID != "" {
			tags = append(tags, account.ID)
		}
	}
	if len(tags) != 1 {
		return "", &ProviderError{Message: fmt.Sprintf("The Cloudflare credential sees %d accounts; it has to see one.", len(tags))}
	}
	return tags[0], nil
}

// cloudflareAccountSites are Web Analytics sites that still describe something:
// a site whose ruleset names no hostname measures nothing.
func (r *Registry) cloudflareAccountSites(ctx context.Context, account, ref string) ([]CloudflareAnalyticsSite, error) {
	items, err := r.cloudflareAPIList(ctx, "/accounts/"+account+"/rum/site_info/list", ref, cloudflarePerPage)
	if err != nil {
		return nil, err
	}
	sites := []CloudflareAnalyticsSite{}
	for _, item := range items {
		var site cfapi.RumSite
		if json.Unmarshal(item, &site) != nil || site.SiteTag == "" {
			continue
		}
		host := strings.ToLower(strings.TrimRight(strings.TrimSpace(site.Ruleset.ZoneName), "."))
		if host != "" {
			sites = append(sites, CloudflareAnalyticsSite{SiteTag: site.SiteTag, Host: host})
		}
	}
	sort.SliceStable(sites, func(a, b int) bool { return sites[a].Host < sites[b].Host })
	return sites, nil
}

// analyticsQuery carries every breakdown in one query: they share a filter and
// a window, and asking six times would spend six times the quota.
func analyticsQuery() string {
	breakdowns := make([]string, len(analyticsDimensions))
	for i, dimension := range analyticsDimensions {
		breakdowns[i] = dimension.name + ": rumPageloadEventsAdaptiveGroups(\n" +
			"             filter: $filter, limit: 5000, orderBy: [count_DESC]\n" +
			"           ) {\n" +
			"             count\n" +
			"             sum { visits }\n" +
			"             avg { sampleInterval }\n" +
			"             dimensions { date " + dimension.field + " }\n" +
			"           }"
	}
	quantiles := make([]string, len(analyticsVitals))
	for i, vital := range analyticsVitals {
		quantiles[i] = vital.field
	}
	buckets := []string{}
	for _, metric := range analyticsBuckets {
		for _, bucket := range analyticsBucketSuffixes {
			buckets = append(buckets, metric+bucket.suffix)
		}
	}
	return "\n      query($account: String!, $filter: ZoneRumPageloadEventsAdaptiveGroupsFilter_InputObject!,\n" +
		"            $vitalsFilter: ZoneRumWebVitalsEventsAdaptiveGroupsFilter_InputObject!) {\n" +
		"        viewer {\n" +
		"          accounts(filter: { accountTag: $account }) {\n" +
		"            " + strings.Join(breakdowns, "\n") + "\n" +
		"            vitals: rumWebVitalsEventsAdaptiveGroups(\n" +
		"              filter: $vitalsFilter, limit: 5000, orderBy: [date_ASC]\n" +
		"            ) {\n" +
		"              count\n" +
		"              avg { sampleInterval }\n" +
		"              quantiles { " + strings.Join(quantiles, " ") + " cumulativeLayoutShiftP75 }\n" +
		"              sum { " + strings.Join(buckets, " ") + " }\n" +
		"              dimensions { date }\n" +
		"            }\n" +
		"          }\n" +
		"        }\n" +
		"      }\n" +
		"    "
}

// milliseconds is Cloudflare's microseconds as milliseconds; its -1 for no
// samples is absence, not a negative load time.
func milliseconds(raw json.RawMessage) *int {
	var n json.Number
	if len(raw) == 0 || string(raw) == "null" || json.Unmarshal(raw, &n) != nil {
		var s string
		if json.Unmarshal(raw, &s) != nil {
			return nil
		}
		n = json.Number(strings.TrimSpace(s))
	}
	micros, err := n.Float64()
	if err != nil || micros < 0 {
		return nil
	}
	ms := int(math.RoundToEven(micros / 1000))
	return &ms
}

type analyticsGroup struct {
	Count json.RawMessage            `json:"count"`
	Sum   map[string]json.RawMessage `json:"sum"`
	Avg   struct {
		SampleInterval json.RawMessage `json:"sampleInterval"`
	} `json:"avg"`
	Quantiles  map[string]json.RawMessage `json:"quantiles"`
	Dimensions map[string]json.RawMessage `json:"dimensions"`
}

func dateOrNull(raw json.RawMessage) *string {
	var date string
	if json.Unmarshal(raw, &date) != nil {
		return nil
	}
	return &date
}

func analyticsRows(account map[string]json.RawMessage) []runtime.AnalyticsRow {
	rows := []runtime.AnalyticsRow{}
	for _, dimension := range analyticsDimensions {
		var groups []analyticsGroup
		if pyTruthy(account[dimension.name]) {
			_ = json.Unmarshal(account[dimension.name], &groups)
		}
		for _, group := range groups {
			value := strings.TrimSpace(pyOrText(group.Dimensions[dimension.field]))
			if value == "" {
				continue
			}
			value = runtime.Clip(value, runtime.AnalyticsValueMax)
			rows = append(rows, runtime.AnalyticsRow{
				Dimension: runtime.AnalyticsRowDimension(dimension.name), Value: value, Date: dateOrNull(group.Dimensions["date"]),
				Pageviews: rawInt(group.Count), Visits: rawInt(group.Sum["visits"]), SampleInterval: intOr(group.Avg.SampleInterval, 1),
			})
		}
	}
	return rows
}

func intOr(raw json.RawMessage, fallback int) int {
	if n := rawInt(raw); n != 0 {
		return n
	}
	return fallback
}

func analyticsVitalsReadings(account map[string]json.RawMessage) []runtime.AnalyticsVitals {
	vitals := []runtime.AnalyticsVitals{}
	var groups []analyticsGroup
	if pyTruthy(account["vitals"]) {
		_ = json.Unmarshal(account["vitals"], &groups)
	}
	for _, group := range groups {
		reading := runtime.AnalyticsVitals{Date: dateOrNull(group.Dimensions["date"]), SampleInterval: intOr(group.Avg.SampleInterval, 1)}
		var shift float64
		if raw := group.Quantiles["cumulativeLayoutShiftP75"]; len(raw) > 0 && string(raw) != "null" && json.Unmarshal(raw, &shift) == nil {
			reading.CumulativeLayoutShift = &shift
		}
		columns := map[string]**int{
			"largest_contentful_paint_ms":  &reading.LargestContentfulPaintMs,
			"interaction_to_next_paint_ms": &reading.InteractionToNextPaintMs,
			"first_contentful_paint_ms":    &reading.FirstContentfulPaintMs,
			"time_to_first_byte_ms":        &reading.TimeToFirstByteMs,
		}
		for _, vital := range analyticsVitals {
			*columns[vital.column] = milliseconds(group.Quantiles[vital.field])
		}
		buckets := map[string]*int{
			"lcpGood": &reading.LcpGood, "lcpNeedsImprovement": &reading.LcpNeedsImprovement, "lcpPoor": &reading.LcpPoor,
			"inpGood": &reading.InpGood, "inpNeedsImprovement": &reading.InpNeedsImprovement, "inpPoor": &reading.InpPoor,
			"clsGood": &reading.ClsGood, "clsNeedsImprovement": &reading.ClsNeedsImprovement, "clsPoor": &reading.ClsPoor,
		}
		for field, target := range buckets {
			*target = rawInt(group.Sum[field])
		}
		vitals = append(vitals, reading)
	}
	return vitals
}

func (r *Registry) analyticsSiteReading(ctx context.Context, site CloudflareAnalyticsSite, start, end time.Time, query string) (runtime.AnalyticsSiteReading, error) {
	window := map[string]string{"siteTag": site.SiteTag, "date_geq": start.Format(time.DateOnly), "date_leq": end.Format(time.DateOnly)}
	data, err := r.cloudflareGraphQL(ctx, query, struct {
		Account      string            `json:"account"`
		Filter       map[string]string `json:"filter"`
		VitalsFilter map[string]string `json:"vitalsFilter"`
	}{site.Account, window, window}, site.ConnectionRef)
	if err != nil {
		return runtime.AnalyticsSiteReading{}, err
	}
	var answer struct {
		Viewer *struct {
			Accounts []json.RawMessage `json:"accounts"`
		} `json:"viewer"`
	}
	_ = json.Unmarshal(data, &answer)
	var accounts []json.RawMessage
	if answer.Viewer != nil {
		accounts = answer.Viewer.Accounts
	}
	var account map[string]json.RawMessage
	if len(accounts) != 1 || !isJSONObject(accounts[0]) || json.Unmarshal(accounts[0], &account) != nil {
		return runtime.AnalyticsSiteReading{}, &ProviderError{Message: "Cloudflare analytics returned no matching account."}
	}
	return runtime.AnalyticsSiteReading{
		SiteTag: site.SiteTag, Host: site.Host, ConnectionRef: site.ConnectionRef,
		Start: start.Format(time.DateOnly), End: end.Format(time.DateOnly),
		Rows: analyticsRows(account), Vitals: analyticsVitalsReadings(account),
	}, nil
}

// cloudflareAnalyticsSites discovers measured sites once so HQ can plan their
// missing windows. Unlike the account readings, no declared connection reads nothing.
func (r *Registry) cloudflareAnalyticsSites(ctx context.Context) ([]CloudflareAnalyticsSite, error) {
	found := []CloudflareAnalyticsSite{}
	for _, ref := range r.Env.Refs("cloudflare_api") {
		account, err := r.cloudflareAnalyticsAccount(ctx, ref)
		if err != nil {
			return nil, err
		}
		sites, err := r.cloudflareAccountSites(ctx, account, ref)
		if err != nil {
			return nil, err
		}
		for _, site := range sites {
			site.Account, site.ConnectionRef = account, ref
			found = append(found, site)
		}
	}
	return found, nil
}

// AnalyticsSites returns the measured sites as the identities HQ plans with,
// keeping their accounts and hosts for the read that follows.
func (r *Registry) AnalyticsSites(ctx context.Context) ([]runtime.AnalyticsSiteIdentity, error) {
	sites, err := r.cloudflareAnalyticsSites(ctx)
	if err != nil {
		return nil, err
	}
	r.snapshotMu.Lock()
	r.analyticsSites = map[runtime.AnalyticsSiteIdentity]CloudflareAnalyticsSite{}
	identities := make([]runtime.AnalyticsSiteIdentity, 0, len(sites))
	for _, site := range sites {
		identity := runtime.AnalyticsSiteIdentity{ConnectionRef: site.ConnectionRef, SiteTag: site.SiteTag}
		r.analyticsSites[identity] = site
		identities = append(identities, identity)
	}
	r.snapshotMu.Unlock()
	return identities, nil
}

// Analytics reads every site's recent traffic and vitals. Whole UTC days only:
// the current day is still accumulating, and a closed day does not change, so
// the default window is the last three completed days.
func (r *Registry) Analytics(ctx context.Context, identities []runtime.AnalyticsSiteIdentity, windows []runtime.AnalyticsWindow) (runtime.AnalyticsReadings, error) {
	r.snapshotMu.Lock()
	sites := make([]CloudflareAnalyticsSite, 0, len(identities))
	for _, identity := range identities {
		if site, ok := r.analyticsSites[identity]; ok {
			sites = append(sites, site)
		}
	}
	r.snapshotMu.Unlock()
	return r.cloudflareAnalytics(ctx, sites, windows, 3)
}

func (r *Registry) cloudflareAnalytics(ctx context.Context, sites []CloudflareAnalyticsSite, windows []runtime.AnalyticsWindow, days int) (runtime.AnalyticsReadings, error) {
	readings := runtime.AnalyticsReadings{Sites: []runtime.AnalyticsSiteReading{}}
	if len(sites) == 0 {
		return readings, nil
	}
	today := r.Now().UTC()
	completed := time.Date(today.Year(), today.Month(), today.Day(), 0, 0, 0, 0, time.UTC).AddDate(0, 0, -1)
	defaultStart := completed.AddDate(0, 0, -(max(days, 1) - 1))
	query := analyticsQuery()
	planned := map[runtime.AnalyticsSiteIdentity]runtime.AnalyticsWindow{}
	for _, window := range windows {
		planned[runtime.AnalyticsSiteIdentity{ConnectionRef: window.ConnectionRef, SiteTag: window.SiteTag}] = window
	}
	for _, site := range sites {
		start, end := defaultStart, completed
		if window, ok := planned[runtime.AnalyticsSiteIdentity{ConnectionRef: site.ConnectionRef, SiteTag: site.SiteTag}]; ok {
			parsedStart, startErr := time.Parse(time.DateOnly, window.Start)
			parsedEnd, endErr := time.Parse(time.DateOnly, window.End)
			if startErr == nil && endErr == nil {
				start, end = parsedStart, parsedEnd
			}
		}
		if start.After(end) || end.After(completed) || int(end.Sub(start).Hours()/24) >= maxQueryDays {
			start, end = defaultStart, completed
		}
		reading, err := r.analyticsSiteReading(ctx, site, start, end, query)
		if err != nil {
			return runtime.AnalyticsReadings{}, err
		}
		readings.Sites = append(readings.Sites, reading)
	}
	return readings, nil
}
