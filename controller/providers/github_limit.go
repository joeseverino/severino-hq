package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
)

// GitHub allows each App installation an hour's worth of requests, shared by
// everything that runs under it: this controller's readings, the deploy
// pipeline's reports and the dependency update run. The controller spends its
// share of each through a fuse. It counts every answer GitHub counts against
// the installation (a 304 and a GraphQL query are not), and once a sweep's
// count passes githubSweepCeiling, or an answer says the installation has fewer
// than githubFloor requests left, it reads nothing more under that installation
// this sweep, and every reading still to come says until when. A request
// authenticated as the App itself (minting a token, listing installations) is
// on the App's own allowance and passes untouched; so does a write, which the
// pipeline's report of a deploy waits on.

const (
	// githubSweepCeiling is a few times what a sweep spends when every covered
	// repository changed at once; a sweep that reaches it is a bug.
	githubSweepCeiling = 150
	// githubFloor is what the controller leaves an installation's other users:
	// the deploy pipeline needs a few requests a deploy, the dependency update
	// run some hundreds a day.
	githubFloor = 1000
	// githubCore is the allowance REST requests spend, as GitHub names it in
	// X-RateLimit-Resource.
	githubCore = "core"
)

// githubLedger is this sweep's budget for each installation, and which
// installation each token it minted spends.
type githubLedger struct {
	mu      sync.Mutex
	budgets map[int]*githubBudget
	tokens  map[string]int
}

// githubBudget is what this sweep has spent of one installation's allowance,
// what GitHub last said is left, and why the fuse tripped, if it did.
type githubBudget struct {
	mu        sync.Mutex
	spent     int
	remaining int
	known     bool
	resets    time.Time
	tripped   error
}

// githubSpend is the sweep's ledger, made on first use.
func (r *Registry) githubSpend() *githubLedger {
	r.snapshotMu.Lock()
	defer r.snapshotMu.Unlock()
	if r.githubLedger == nil {
		r.githubLedger = &githubLedger{budgets: map[int]*githubBudget{}, tokens: map[string]int{}}
	}
	return r.githubLedger
}

// minted records which installation a token spends.
func (l *githubLedger) minted(token string, installation int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.tokens[token] = installation
}

// of is the budget a token spends; a token this sweep did not mint shares one
// budget with every other such token.
func (l *githubLedger) of(token string) *githubBudget {
	l.mu.Lock()
	defer l.mu.Unlock()
	installation := l.tokens[token]
	if l.budgets[installation] == nil {
		l.budgets[installation] = &githubBudget{}
	}
	return l.budgets[installation]
}

// GitHubSpent is how many requests this sweep has spent of its installations'
// allowances, by GitHub's own count of each answer.
func (r *Registry) GitHubSpent() int {
	ledger := r.githubSpend()
	ledger.mu.Lock()
	defer ledger.mu.Unlock()
	spent := 0
	for _, budget := range ledger.budgets {
		budget.mu.Lock()
		spent += budget.spent
		budget.mu.Unlock()
	}
	return spent
}

// admit refuses a read once the fuse has tripped.
func (b *githubBudget) admit() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tripped
}

// note counts one answer and trips the fuse when it passes a bound. GitHub
// names the allowance an answer spent in its headers; an answer without them
// is counted as REST, so a missing header never spends free.
func (b *githubBudget) note(header http.Header, err error, now time.Time) {
	if header == nil {
		return
	}
	resource := strings.ToLower(orDefault(header.Get("X-RateLimit-Resource"), githubCore))
	if resource != githubCore || httpStatus(err) == http.StatusNotModified {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.spent++
	if left, parsed := strconv.Atoi(header.Get("X-RateLimit-Remaining")); parsed == nil {
		b.remaining, b.known, b.resets = left, true, githubResets(header, now)
	}
	b.check(now)
}

// left records what /rate_limit says the installation has, which costs
// nothing to ask.
func (b *githubBudget) left(remaining int, resets, now time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.remaining, b.known, b.resets = remaining, true, resets
	b.check(now)
}

func (b *githubBudget) check(now time.Time) {
	if b.tripped != nil {
		return
	}
	switch {
	case b.known && b.remaining < githubFloor:
		b.tripped = githubLimited(fmt.Sprintf("GitHub allows the app's installation %d more requests, under the %d the controller leaves its other users", b.remaining, githubFloor), b.resets, now)
	case b.spent >= githubSweepCeiling:
		b.tripped = &ProviderError{Message: fmt.Sprintf("this sweep spent %d GitHub requests, the controller's ceiling for one sweep: rate limited until the next sweep, the last reading stands", b.spent)}
	}
}

// githubResets is when an answer says to ask again: now plus the wait a
// secondary limit asks for, which outranks the hour's reset beside it, or
// that reset.
func githubResets(header http.Header, now time.Time) time.Time {
	if seconds, err := strconv.Atoi(header.Get("Retry-After")); err == nil && seconds >= 0 {
		return now.Add(time.Duration(seconds) * time.Second).UTC()
	}
	if seconds, err := strconv.ParseInt(header.Get("X-RateLimit-Reset"), 10, 64); err == nil && seconds > 0 {
		return time.Unix(seconds, 0).UTC()
	}
	return time.Time{}
}

// githubLimited is a read refused for the allowance, saying until when. It is
// never a permission to grant, so it carries no refusal class.
func githubLimited(why string, resets, now time.Time) error {
	until := "rate limited until GitHub's hour starts over"
	switch {
	case resets.IsZero():
	case resets.UTC().Format(time.DateOnly) == now.UTC().Format(time.DateOnly):
		until = "rate limited until " + resets.UTC().Format("15:04 UTC")
	default:
		until = "rate limited until " + resets.UTC().Format("2006-01-02 15:04 UTC")
	}
	return &ProviderError{Message: why + ": " + until + ", the last reading stands"}
}

// githubRateLimited is a refusal GitHub gave for an allowance, primary or
// secondary, as the limit and when it lifts; any other answer passes as it
// came. GitHub answers a spent allowance with 403 or 429, and says which by an
// allowance left at zero, a wait to keep, or its message.
func githubRateLimited(header http.Header, err error, now time.Time) error {
	answered, ok := errors.AsType[*ProviderError](err)
	if !ok || (answered.HTTPStatus != http.StatusForbidden && answered.HTTPStatus != http.StatusTooManyRequests) {
		return err
	}
	var said struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(answered.Body, &said)
	spent := header.Get("X-RateLimit-Remaining") == "0"
	if !spent && header.Get("Retry-After") == "" && !strings.Contains(strings.ToLower(said.Message), "rate limit") {
		return err
	}
	why := "GitHub refused the request for a secondary rate limit"
	if spent {
		why = "GitHub's hourly allowance for the app's installation is spent"
	}
	return githubLimited(why, githubResets(header, now), now)
}

// githubExchange is one request to GitHub's API. Under a token, a read passes
// the fuse of the installation it spends, and every answer is counted there;
// a refusal for a limit is named as one either way.
func (r *Registry) githubExchange(ctx context.Context, method, path string, headers map[string]string, payload any, token string) (json.RawMessage, error) {
	var budget *githubBudget
	if token != "" && path != githubRateLimitRoute {
		budget = r.githubSpend().of(token)
	}
	if budget != nil && (method == http.MethodGet || path == githubGraphQL) {
		if err := budget.admit(); err != nil {
			return nil, err
		}
	}
	raw, header, err := r.HTTP.Exchange(ctx, githubAPI+path, method, headers, payload)
	if budget != nil {
		budget.note(header, err, r.Now())
	}
	return raw, githubRateLimited(header, err, r.Now())
}

// githubRateLimitRoute answers what the allowance has left without spending any of it.
const githubRateLimitRoute = "/rate_limit"

// githubAllowance asks what a token's installation has left of its REST
// allowance, and holds that installation's fuse to it before the sweep spends any.
func (r *Registry) githubAllowance(ctx context.Context, token string) error {
	raw, err := r.githubExchange(ctx, http.MethodGet, githubRateLimitRoute, githubHeaders(token), nil, token)
	if err != nil {
		return err
	}
	limit, err := decodeAnswer[githubapi.RateLimitOverview](raw, "GitHub rate limit")
	if err != nil {
		return err
	}
	budget := r.githubSpend().of(token)
	core := limit.Resources.Core
	budget.left(core.Remaining, time.Unix(int64(core.Reset), 0).UTC(), r.Now())
	return budget.admit()
}
