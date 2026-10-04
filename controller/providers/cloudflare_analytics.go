package providers

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Site analytics read through Cloudflare's GraphQL API.

// analyticsDimensions: which Cloudflare dimension answers each breakdown HQ
// stores, keyed by the contract's dimension. The query and the payload are
// both generated from this.
var analyticsDimensions = []struct {
	name  runtime.AnalyticsRowDimension
	field string
}{
	{runtime.Path, "requestPath"},
	{runtime.Referrer, "refererHost"},
	{runtime.Country, "countryName"},
	{runtime.Device, "deviceType"},
	{runtime.Browser, "userAgentBrowser"},
	{runtime.Os, "userAgentOS"},
}

// analyticsVitals: the p75 percentiles HQ keeps, p75 being where Core Web
// Vitals is judged.
var analyticsVitals = []struct {
	field  string
	column func(*runtime.AnalyticsVitals) **int
}{
	{"largestContentfulPaintP75", func(v *runtime.AnalyticsVitals) **int { return &v.LargestContentfulPaintMs }},
	{"interactionToNextPaintP75", func(v *runtime.AnalyticsVitals) **int { return &v.InteractionToNextPaintMs }},
	{"firstContentfulPaintP75", func(v *runtime.AnalyticsVitals) **int { return &v.FirstContentfulPaintMs }},
	{"timeToFirstByteP75", func(v *runtime.AnalyticsVitals) **int { return &v.TimeToFirstByteMs }},
}

const cumulativeLayoutShiftP75 = "cumulativeLayoutShiftP75"

// analyticsBuckets: how many page loads fell in each Core Web Vitals band.
var analyticsBuckets = []struct {
	field  string
	column func(*runtime.AnalyticsVitals) *int
}{
	{"lcpGood", func(v *runtime.AnalyticsVitals) *int { return &v.LcpGood }},
	{"lcpNeedsImprovement", func(v *runtime.AnalyticsVitals) *int { return &v.LcpNeedsImprovement }},
	{"lcpPoor", func(v *runtime.AnalyticsVitals) *int { return &v.LcpPoor }},
	{"inpGood", func(v *runtime.AnalyticsVitals) *int { return &v.InpGood }},
	{"inpNeedsImprovement", func(v *runtime.AnalyticsVitals) *int { return &v.InpNeedsImprovement }},
	{"inpPoor", func(v *runtime.AnalyticsVitals) *int { return &v.InpPoor }},
	{"clsGood", func(v *runtime.AnalyticsVitals) *int { return &v.ClsGood }},
	{"clsNeedsImprovement", func(v *runtime.AnalyticsVitals) *int { return &v.ClsNeedsImprovement }},
	{"clsPoor", func(v *runtime.AnalyticsVitals) *int { return &v.ClsPoor }},
}

// analyticsVitalsAlias names the vitals group in the query's answer.
const analyticsVitalsAlias = "vitals"

// maxQueryDays is Cloudflare's widest accepted query (13w2d). HQ plans its
// windows inside the same number (MAX_QUERY_DAYS, hq/domains/analytics/contracts.py);
// Cloudflare publishes no description of this API to take it from.
const maxQueryDays = 93

// analyticsGroup is one row of an adaptive-groups answer.
type analyticsGroup struct {
	Count int            `json:"count"`
	Sum   map[string]int `json:"sum"`
	Avg   struct {
		SampleInterval float64 `json:"sampleInterval"`
	} `json:"avg"`
	Quantiles  map[string]*float64 `json:"quantiles"`
	Dimensions map[string]string   `json:"dimensions"`
}

// analyticsAccount is one account's answer: each alias in the query, a list of groups.
type analyticsAccount map[string][]analyticsGroup

type analyticsAnswer struct {
	Viewer struct {
		Accounts []analyticsAccount `json:"accounts"`
	} `json:"viewer"`
}

// cloudflareGraphQL answers 200 with an errors array rather than a status, so a
// failed query must not read as a site nobody visited.
func (r *Registry) cloudflareGraphQL(ctx context.Context, query string, variables any, ref string) (json.RawMessage, error) {
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderCloudflareAPI, ref)
	if err != nil {
		return nil, err
	}
	if err := r.cloudflareBreaker(prefix); err != nil {
		return nil, err
	}
	base, err := r.cloudflareURL(runtime.ConnectionProviderCloudflareAPI, ref)
	if err != nil {
		return nil, err
	}
	headers, err := r.cloudflareHeaders(runtime.ConnectionProviderCloudflareAPI, ref)
	if err != nil {
		return nil, err
	}
	delete(headers, "Accept")
	raw, err := r.HTTP.Request(ctx, base+"/graphql", "POST", headers, struct {
		Query     string `json:"query"`
		Variables any    `json:"variables"`
	}{query, variables})
	if err != nil {
		var answered *ProviderError
		if errors.As(err, &answered) && answered.HTTPStatus != 0 {
			return nil, r.cloudflareRefused(prefix, fmt.Sprintf("cloudflare analytics refused the query (HTTP %d)", answered.HTTPStatus), cloudflareErrors(answered.Body), answered.HTTPStatus, func() bool {
				return r.cloudflareVerified(ctx, runtime.ConnectionProviderCloudflareAPI, ref)
			})
		}
		return nil, fmt.Errorf("cloudflare analytics: %w", err)
	}
	if !present(raw) {
		return nil, &ProviderError{Message: "cloudflare analytics answered with no body"}
	}
	payload, err := cloudflareDecode[struct {
		Errors []cfMessage     `json:"errors"`
		Data   json.RawMessage `json:"data"`
	}](raw, "analytics answer")
	if err != nil {
		return nil, err
	}
	if len(payload.Errors) > 0 {
		message := payload.Errors[0].Message
		return nil, r.cloudflareRefused(prefix, "cloudflare analytics rejected the query", message, 0, nil)
	}
	return payload.Data, nil
}

// cloudflareAnalyticsAccount is the one account this credential reads,
// discovered rather than configured.
func (r *Registry) cloudflareAnalyticsAccount(ctx context.Context, ref string) (string, error) {
	items, err := r.cloudflareList(ctx, runtime.ConnectionProviderCloudflareAPI, "/accounts", ref, cloudflareAccountPerPage)
	if err != nil {
		return "", err
	}
	accounts, err := cloudflareItems[cfapi.IamAccount](items, "account")
	if err != nil {
		return "", err
	}
	if len(accounts) != 1 || accounts[0].ID == "" {
		return "", &ProviderError{Message: fmt.Sprintf("the cloudflare credential sees %d accounts; it has to see one", len(accounts))}
	}
	return accounts[0].ID, nil
}

// cloudflareAccountSites are Web Analytics sites that still describe something:
// a site whose ruleset names no hostname measures nothing.
func (r *Registry) cloudflareAccountSites(ctx context.Context, account, ref string) ([]CloudflareAnalyticsSite, error) {
	items, err := r.cloudflareList(ctx, runtime.ConnectionProviderCloudflareAPI, "/accounts/"+account+"/rum/site_info/list", ref, cloudflarePerPage)
	if err != nil {
		return nil, err
	}
	listed, err := cloudflareItems[cfapi.RumSite](items, "analytics site")
	if err != nil {
		return nil, err
	}
	sites := []CloudflareAnalyticsSite{}
	for _, site := range listed {
		if site.SiteTag == "" {
			continue
		}
		host := strings.ToLower(strings.TrimRight(strings.TrimSpace(site.Ruleset.ZoneName), "."))
		if host != "" {
			sites = append(sites, CloudflareAnalyticsSite{SiteTag: site.SiteTag, Host: host})
		}
	}
	slices.SortStableFunc(sites, func(a, b CloudflareAnalyticsSite) int { return cmp.Compare(a.Host, b.Host) })
	return sites, nil
}

// analyticsQuery carries every breakdown in one query: they share a filter and
// a window, and asking six times would spend six times the quota.
func analyticsQuery() string {
	var query strings.Builder
	query.WriteString(`query($account: String!, $filter: ZoneRumPageloadEventsAdaptiveGroupsFilter_InputObject!,
      $vitalsFilter: ZoneRumWebVitalsEventsAdaptiveGroupsFilter_InputObject!) {
  viewer {
    accounts(filter: { accountTag: $account }) {
`)
	for _, dimension := range analyticsDimensions {
		fmt.Fprintf(&query, `      %s: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 5000, orderBy: [count_DESC]) {
        count
        sum { visits }
        avg { sampleInterval }
        dimensions { date %s }
      }
`, dimension.name, dimension.field)
	}
	quantiles := []string{}
	for _, vital := range analyticsVitals {
		quantiles = append(quantiles, vital.field)
	}
	buckets := []string{}
	for _, bucket := range analyticsBuckets {
		buckets = append(buckets, bucket.field)
	}
	fmt.Fprintf(&query, `      %s: rumWebVitalsEventsAdaptiveGroups(filter: $vitalsFilter, limit: 5000, orderBy: [date_ASC]) {
        count
        avg { sampleInterval }
        quantiles { %s %s }
        sum { %s }
        dimensions { date }
      }
    }
  }
}
`, analyticsVitalsAlias, strings.Join(quantiles, " "), cumulativeLayoutShiftP75, strings.Join(buckets, " "))
	return query.String()
}

// milliseconds is Cloudflare's microseconds as milliseconds; its -1 for no
// samples is absence, not a negative load time.
func milliseconds(micros *float64) *int {
	if micros == nil || *micros < 0 {
		return nil
	}
	ms := int(math.Round(*micros / 1000))
	return &ms
}

// sampleInterval is a group's average sampling interval as the whole number HQ
// stores; at least 1, which is every event counted.
func sampleInterval(group analyticsGroup) int {
	return max(1, int(math.Round(group.Avg.SampleInterval)))
}

func groupDate(group analyticsGroup) *string {
	if date := group.Dimensions["date"]; date != "" {
		return &date
	}
	return nil
}

func analyticsRows(account analyticsAccount) []runtime.AnalyticsRow {
	rows := []runtime.AnalyticsRow{}
	for _, dimension := range analyticsDimensions {
		for _, group := range account[string(dimension.name)] {
			value := strings.TrimSpace(group.Dimensions[dimension.field])
			if value == "" {
				continue
			}
			rows = append(rows, runtime.AnalyticsRow{
				Dimension: dimension.name, Value: runtime.Clip(value, runtime.AnalyticsValueMax), Date: groupDate(group),
				Pageviews: group.Count, Visits: group.Sum["visits"], SampleInterval: sampleInterval(group),
			})
		}
	}
	return rows
}

func analyticsVitalsReadings(account analyticsAccount) []runtime.AnalyticsVitals {
	vitals := []runtime.AnalyticsVitals{}
	for _, group := range account[analyticsVitalsAlias] {
		reading := runtime.AnalyticsVitals{Date: groupDate(group), SampleInterval: sampleInterval(group)}
		reading.CumulativeLayoutShift = group.Quantiles[cumulativeLayoutShiftP75]
		for _, vital := range analyticsVitals {
			*vital.column(&reading) = milliseconds(group.Quantiles[vital.field])
		}
		for _, bucket := range analyticsBuckets {
			*bucket.column(&reading) = group.Sum[bucket.field]
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
	var answer analyticsAnswer
	if present(data) {
		if answer, err = cloudflareDecode[analyticsAnswer](data, "analytics answer"); err != nil {
			return runtime.AnalyticsSiteReading{}, err
		}
	}
	if len(answer.Viewer.Accounts) != 1 {
		return runtime.AnalyticsSiteReading{}, &ProviderError{Message: "cloudflare analytics returned no matching account"}
	}
	account := answer.Viewer.Accounts[0]
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
	for _, ref := range r.Env.Refs(runtime.ConnectionProviderCloudflareAPI) {
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
