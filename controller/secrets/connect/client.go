// Package connect reads a 1Password Connect server on this machine's IPv4
// loopback, and nowhere else.
package connect

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"regexp"
	"strconv"
	"syscall"
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
)

// Bounds on every exchange. A vault item is a few kilobytes; a response this
// large is not one.
const (
	MaxResponseBytes = 4 << 20
	// MaxItems bounds one vault's listing: each item is one more request.
	MaxItems       = 2000
	dialTimeout    = 3 * time.Second
	headerTimeout  = 10 * time.Second
	requestTimeout = 15 * time.Second
	firstBackoff   = 250 * time.Millisecond
	maxBackoff     = 5 * time.Second
)

// Token is a Connect bearer token. It formats, logs and marshals as a
// placeholder, so it cannot reach a log line or an error by accident; only
// the request header reads it.
type Token struct{ value string }

const redacted = "[redacted]"

var tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// NewToken accepts the credential file's bytes: one line, in the JWT alphabet.
func NewToken(raw []byte) (Token, error) {
	value := string(raw)
	for len(value) > 0 && value[len(value)-1] == '\n' {
		value = value[:len(value)-1]
	}
	if value == "" {
		return Token{}, errors.New("empty Connect token")
	}
	if !tokenPattern.MatchString(value) {
		return Token{}, errors.New("malformed Connect token")
	}
	return Token{value: value}, nil
}

func (Token) String() string               { return redacted }
func (Token) GoString() string             { return redacted }
func (Token) Format(f fmt.State, _ rune)   { io.WriteString(f, redacted) }
func (Token) LogValue() slog.Value         { return slog.StringValue(redacted) }
func (Token) MarshalText() ([]byte, error) { return []byte(redacted), nil }

// Failure classes; match with errors.Is.
var (
	ErrEndpoint    = errors.New("Connect requires an explicit IPv4 loopback endpoint")
	ErrNotLoopback = errors.New("refusing to dial an address that is not IPv4 loopback")
	ErrRedirect    = errors.New("Connect answered with a redirect")
	ErrUnavailable = errors.New("Connect is unavailable")
	ErrDenied      = errors.New("Connect refused the token")
	ErrResponse    = errors.New("Connect returned an unusable response")
	ErrIdentifier  = errors.New("not a 1Password identifier")
)

// Error is one failed exchange: what was asked and the class of failure. It
// never carries a URL, a header or a response body.
type Error struct {
	Op     string
	Status int
	Class  error
	Detail string
}

func (e *Error) Error() string {
	message := e.Op + ": " + e.Class.Error()
	if e.Status != 0 {
		message += " (HTTP " + strconv.Itoa(e.Status) + ")"
	}
	if e.Detail != "" {
		message += ": " + e.Detail
	}
	return message
}

func (e *Error) Unwrap() error { return e.Class }

var endpointPattern = regexp.MustCompile(`^http://127\.0\.0\.1:([0-9]{1,5})$`)

// requireLoopback is the rule the dialer enforces on the address it is about
// to connect to, whatever the configured endpoint said.
func requireLoopback(address string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return ErrNotLoopback
	}
	ip, err := netip.ParseAddr(host)
	if err != nil || !ip.Is4() || !ip.IsLoopback() {
		return ErrNotLoopback
	}
	return nil
}

// newHTTPClient is the only client this package makes: no proxy, no redirect,
// IPv4 loopback only, every phase bounded.
func newHTTPClient() *http.Client {
	dialer := &net.Dialer{
		Timeout: dialTimeout,
		// After resolution, on the socket itself: the last word on where it goes.
		Control: func(_, address string, _ syscall.RawConn) error { return requireLoopback(address) },
	}
	return &http.Client{
		Timeout: requestTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return ErrRedirect
		},
		Transport: &http.Transport{
			Proxy: nil,
			DialContext: func(ctx context.Context, _, address string) (net.Conn, error) {
				if err := requireLoopback(address); err != nil {
					return nil, err
				}
				return dialer.DialContext(ctx, "tcp4", address)
			},
			ResponseHeaderTimeout:  headerTimeout,
			MaxResponseHeaderBytes: 64 << 10,
			MaxIdleConns:           2,
			IdleConnTimeout:        30 * time.Second,
		},
	}
}

// Client reads one Connect server.
type Client struct {
	endpoint string
	// authorize sets the bearer header. The token is held in this closure
	// and in no field, so nothing that prints a Client can reach it.
	authorize func(*http.Request)
	http      *http.Client
	// Sleep waits between readiness attempts; tests replace it.
	Sleep func(context.Context, time.Duration) error
}

// CheckEndpoint refuses any endpoint but http://127.0.0.1:PORT.
func CheckEndpoint(endpoint string) error {
	match := endpointPattern.FindStringSubmatch(endpoint)
	if match == nil {
		return ErrEndpoint
	}
	if port, err := strconv.Atoi(match[1]); err != nil || port < 1 || port > 65535 {
		return ErrEndpoint
	}
	return nil
}

// New makes a client for an endpoint CheckEndpoint accepts.
func New(endpoint string, token Token) (*Client, error) {
	if err := CheckEndpoint(endpoint); err != nil {
		return nil, err
	}
	if token.value == "" {
		return nil, errors.New("empty Connect token")
	}
	return &Client{endpoint: endpoint, authorize: bearer(token), http: newHTTPClient(), Sleep: sleep}, nil
}

func bearer(token Token) func(*http.Request) {
	return func(request *http.Request) { request.Header.Set("Authorization", "Bearer "+token.value) }
}

// A Client formats and logs as its endpoint and nothing else.
func (c Client) String() string             { return "connect.Client(" + c.endpoint + ")" }
func (c Client) GoString() string           { return c.String() }
func (c Client) Format(f fmt.State, _ rune) { io.WriteString(f, c.String()) }
func (c Client) LogValue() slog.Value       { return slog.StringValue(c.String()) }

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// get performs one bounded GET and decodes its JSON answer into out.
func (c *Client) get(ctx context.Context, op, path string, authenticated bool, out any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.endpoint+path, nil)
	if err != nil {
		return &Error{Op: op, Class: ErrResponse, Detail: "unbuildable request"}
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "severino-hq-secrets")
	if authenticated {
		c.authorize(request)
	}
	response, err := c.http.Do(request)
	if err != nil {
		// A transport error's text holds the URL; keep the class only.
		switch {
		case errors.Is(err, ErrNotLoopback):
			return &Error{Op: op, Class: ErrNotLoopback}
		case errors.Is(err, ErrRedirect):
			return &Error{Op: op, Class: ErrRedirect}
		case errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded):
			return &Error{Op: op, Class: ErrUnavailable, Detail: "timed out"}
		}
		return &Error{Op: op, Class: ErrUnavailable, Detail: "no answer"}
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, MaxResponseBytes+1))
	switch {
	case response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden:
		return &Error{Op: op, Status: response.StatusCode, Class: ErrDenied}
	case response.StatusCode >= 500 || response.StatusCode == http.StatusTooManyRequests ||
		response.StatusCode == http.StatusRequestTimeout:
		return &Error{Op: op, Status: response.StatusCode, Class: ErrUnavailable}
	case response.StatusCode != http.StatusOK:
		return &Error{Op: op, Status: response.StatusCode, Class: ErrResponse}
	}
	if err != nil {
		return &Error{Op: op, Class: ErrUnavailable, Detail: "the answer was cut short"}
	}
	if len(body) > MaxResponseBytes {
		return &Error{Op: op, Class: ErrResponse, Detail: "larger than the response bound"}
	}
	if kind, _, err := mime.ParseMediaType(response.Header.Get("Content-Type")); err != nil || kind != "application/json" {
		return &Error{Op: op, Class: ErrResponse, Detail: "not JSON"}
	}
	// v2 refuses a repeated member name, invalid UTF-8 and trailing data.
	if err := jsonv2.Unmarshal(body, out); err != nil {
		return &Error{Op: op, Class: ErrResponse, Detail: "not the documented shape"}
	}
	return nil
}

var idPattern = regexp.MustCompile(`^[a-z0-9]{26}$`)

// ValidID reports whether id is a vault or item identifier. Only one that is
// reaches a request path.
func ValidID(id string) bool { return idPattern.MatchString(id) }

// Health is Connect's own account of itself. It needs no token.
type Health struct {
	Name         string                         `json:"name"`
	Version      string                         `json:"version"`
	Dependencies []connectapi.ServiceDependency `json:"dependencies"`
}

func (c *Client) Health(ctx context.Context) (Health, error) {
	var health Health
	err := c.get(ctx, "read health", "/health", false, &health)
	return health, err
}

// Vaults lists the vaults the token reaches.
func (c *Client) Vaults(ctx context.Context) ([]connectapi.Vault, error) {
	var vaults []connectapi.Vault
	if err := c.get(ctx, "list vaults", "/v1/vaults", true, &vaults); err != nil {
		return nil, err
	}
	return vaults, nil
}

// Vault reads one vault's metadata, its content version among it.
func (c *Client) Vault(ctx context.Context, vault string) (connectapi.Vault, error) {
	var found connectapi.Vault
	if !ValidID(vault) {
		return found, &Error{Op: "read vault", Class: ErrIdentifier}
	}
	if err := c.get(ctx, "read vault", "/v1/vaults/"+vault, true, &found); err != nil {
		return found, err
	}
	if found.Id == nil || *found.Id != vault {
		return found, &Error{Op: "read vault", Class: ErrResponse, Detail: "another vault was returned"}
	}
	return found, nil
}

// Items lists a vault's items. Every identifier is checked and none repeats.
func (c *Client) Items(ctx context.Context, vault string) ([]connectapi.Item, error) {
	if !ValidID(vault) {
		return nil, &Error{Op: "list items", Class: ErrIdentifier}
	}
	var items []connectapi.Item
	if err := c.get(ctx, "list items", "/v1/vaults/"+vault+"/items", true, &items); err != nil {
		return nil, err
	}
	if len(items) > MaxItems {
		return nil, &Error{Op: "list items", Class: ErrResponse, Detail: "more items than a host's vault holds"}
	}
	seen := map[string]bool{}
	for _, item := range items {
		if item.Id == nil || !ValidID(*item.Id) {
			return nil, &Error{Op: "list items", Class: ErrResponse, Detail: "an item has no valid identifier"}
		}
		if seen[*item.Id] {
			return nil, &Error{Op: "list items", Class: ErrResponse, Detail: "an item is listed twice"}
		}
		seen[*item.Id] = true
		if item.Vault.Id != vault {
			return nil, &Error{Op: "list items", Class: ErrResponse, Detail: "an item belongs to another vault"}
		}
	}
	return items, nil
}

// Item reads one item in full. The answer must be the item that was asked for.
func (c *Client) Item(ctx context.Context, vault, item string) (connectapi.FullItem, error) {
	var found connectapi.FullItem
	if !ValidID(vault) || !ValidID(item) {
		return found, &Error{Op: "read item", Class: ErrIdentifier}
	}
	if err := c.get(ctx, "read item", "/v1/vaults/"+vault+"/items/"+item, true, &found); err != nil {
		return found, err
	}
	if found.Id == nil || *found.Id != item || found.Vault.Id != vault {
		return found, &Error{Op: "read item", Class: ErrResponse, Detail: "another item was returned"}
	}
	return found, nil
}

// WaitReady lists the vaults until Connect answers or ctx ends. Connect stays
// locked after a restart until an authenticated request arrives, so the probe
// is the authenticated listing itself, never /health. A refused token and a
// malformed answer are final; no answer, a timeout and a 5xx are retried.
func (c *Client) WaitReady(ctx context.Context) ([]connectapi.Vault, int, error) {
	backoff := firstBackoff
	for attempt := 1; ; attempt++ {
		vaults, err := c.Vaults(ctx)
		if err == nil {
			return vaults, attempt, nil
		}
		if !errors.Is(err, ErrUnavailable) || ctx.Err() != nil {
			return nil, attempt, err
		}
		if c.Sleep(ctx, backoff) != nil {
			return nil, attempt, err
		}
		backoff = min(backoff*2, maxBackoff)
	}
}
