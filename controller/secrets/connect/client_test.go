package connect

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets/connecttest"
)

func token(t *testing.T) Token {
	t.Helper()
	found, err := NewToken([]byte(connecttest.Token + "\n"))
	if err != nil {
		t.Fatal(err)
	}
	return found
}

func client(t *testing.T, fake *connecttest.Fake) *Client {
	t.Helper()
	found, err := New(fake.Server.URL, token(t))
	if err != nil {
		t.Fatal(err)
	}
	found.Sleep = func(context.Context, time.Duration) error { return nil }
	return found
}

func TestEndpointMustBeExplicitIPv4Loopback(t *testing.T) {
	for _, endpoint := range []string{
		"", "https://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080", "http://127.0.0.1",
		"http://127.0.0.1:8080/", "http://127.0.0.1:8080/v1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
		"http://127.0.0.1:999999", "http://192.0.2.10:8080", "http://127.0.0.2:8080", "http://user@127.0.0.1:8080",
		"https://example.com", "http://127.0.0.1:8080@example.com", " http://127.0.0.1:8080", "http://127.0.0.1:8080\n",
	} {
		if _, err := New(endpoint, token(t)); !errors.Is(err, ErrEndpoint) {
			t.Errorf("endpoint %q was accepted: %v", endpoint, err)
		}
	}
	if _, err := New("http://127.0.0.1:8080", Token{}); err == nil {
		t.Error("an empty token was accepted")
	}
}

func TestTokenIsValidatedAndNeverFormats(t *testing.T) {
	for _, raw := range []string{"", "\n", "has space", "tab\there", "line\nbreak", "quote'", "semi;colon", "naïve"} {
		if _, err := NewToken([]byte(raw)); err == nil {
			t.Errorf("token %q was accepted", raw)
		} else if raw != "" && strings.TrimSpace(raw) != "" && strings.Contains(err.Error(), raw) {
			t.Errorf("the refusal quotes the token: %v", err)
		}
	}
	held := token(t)
	var log bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&log, nil))
	logger.Info("token", "token", held, "pointer", &held, "wrapped", fmt.Errorf("with %v", held))
	jsonLogger := slog.New(slog.NewJSONHandler(&log, nil))
	jsonLogger.Info("token", "token", held, "struct", struct{ T Token }{held})
	rendered := log.String() + fmt.Sprintf("%v %s %+v %#v %q %x %d", held, held, held, held, held, held, held) +
		fmt.Sprint(held, &held, []Token{held}, map[string]Token{"t": held}, struct{ T Token }{held})
	if strings.Contains(rendered, connecttest.Token) {
		t.Fatalf("the token was rendered: %s", rendered)
	}
}

func TestAClientNeverFormatsItsToken(t *testing.T) {
	fake := connecttest.New(t)
	held := client(t, fake)
	var log bytes.Buffer
	slog.New(slog.NewTextHandler(&log, nil)).Info("client", "pointer", held, "value", *held, "wrapped", fmt.Errorf("with %v", held))
	slog.New(slog.NewJSONHandler(&log, nil)).Info("client", "pointer", held, "value", *held, "nested", struct{ C *Client }{held})
	rendered := log.String() + fmt.Sprintf("%v %+v %#v %s %q %x", held, held, held, held, held, held) +
		fmt.Sprintf("%v %+v %#v", *held, *held, *held) + fmt.Sprint(held, *held, []*Client{held}, struct{ C Client }{*held})
	if strings.Contains(rendered, connecttest.Token) {
		t.Fatalf("the token was rendered: %s", rendered)
	}
	if !strings.Contains(rendered, "connect.Client(http://127.0.0.1:") {
		t.Fatalf("a client does not say what it is: %s", rendered)
	}
}

func TestAListingLongerThanAVaultIsRefused(t *testing.T) {
	fake := connecttest.New(t)
	fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
		if !strings.HasSuffix(r.URL.Path, "/items") {
			return false
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte("["))
		for n := range MaxItems + 1 {
			if n > 0 {
				w.Write([]byte(","))
			}
			fmt.Fprintf(w, `{"id":%q,"category":"LOGIN","vault":{"id":%q}}`, connecttest.ID(n), connecttest.VaultID)
		}
		w.Write([]byte("]"))
		return true
	}
	if _, err := client(t, fake).Items(context.Background(), connecttest.VaultID); !errors.Is(err, ErrResponse) || !strings.Contains(err.Error(), "more items") {
		t.Fatalf("an unbounded listing was accepted: %v", err)
	}
}

func TestDialerRefusesEverythingButIPv4Loopback(t *testing.T) {
	for _, address := range []string{
		"192.0.2.1:80", "198.51.100.7:8080", "203.0.113.9:443", "[::1]:8080", "localhost:8080",
		"example.com:80", "0.0.0.0:80", "10.0.0.1:80", "[::ffff:127.0.0.1]:80", "127.0.0.1", "",
	} {
		if err := requireLoopback(address); !errors.Is(err, ErrNotLoopback) {
			t.Errorf("address %q was accepted", address)
		}
	}
	for _, address := range []string{"127.0.0.1:8080", "127.0.0.53:1"} {
		if err := requireLoopback(address); err != nil {
			t.Errorf("loopback %q was refused", address)
		}
	}
	// The real client, pointed past its endpoint check at an address that is
	// not loopback: the dialer itself refuses, before any packet.
	inner := newHTTPClient()
	for _, url := range []string{"http://192.0.2.1:8080/v1/vaults", "http://[::1]:8080/v1/vaults", "http://localhost:8080/v1/vaults"} {
		_, err := inner.Get(url)
		if !errors.Is(err, ErrNotLoopback) {
			t.Errorf("%s was dialed: %v", url, err)
		}
	}
	// And through the client's own request path.
	rigged := &Client{endpoint: "http://192.0.2.1:8080", authorize: bearer(token(t)), http: newHTTPClient()}
	if _, err := rigged.Vaults(context.Background()); !errors.Is(err, ErrNotLoopback) {
		t.Fatalf("a non-loopback endpoint was read: %v", err)
	}
}

func TestProxyEnvironmentIsIgnored(t *testing.T) {
	proxied := false
	proxy := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { proxied = true }))
	defer proxy.Close()
	for _, name := range []string{"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY"} {
		t.Setenv(name, proxy.URL)
	}
	t.Setenv("NO_PROXY", "")
	fake := connecttest.New(t)
	if _, err := client(t, fake).Vaults(context.Background()); err != nil {
		t.Fatal(err)
	}
	if proxied {
		t.Fatal("the request went through the environment's proxy")
	}
	if transport := newHTTPClient().Transport.(*http.Transport); transport.Proxy != nil {
		t.Fatal("the transport has a proxy function")
	}
}

func TestRedirectsAreRefusedAndTheTokenIsNotForwarded(t *testing.T) {
	elsewhere := false
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { elsewhere = true }))
	defer target.Close()
	fake := connecttest.New(t)
	for _, status := range []int{http.StatusMovedPermanently, http.StatusFound, http.StatusTemporaryRedirect, http.StatusPermanentRedirect} {
		fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
			http.Redirect(w, r, target.URL+"/v1/vaults", status)
			return true
		}
		_, err := client(t, fake).Vaults(context.Background())
		if !errors.Is(err, ErrRedirect) {
			t.Errorf("redirect %d was followed or misreported: %v", status, err)
		}
	}
	if elsewhere {
		t.Fatal("a redirect was followed")
	}
}

func TestTheTokenTravelsOnlyInTheAuthorizationHeader(t *testing.T) {
	fake := connecttest.New(t)
	var seen []*http.Request
	fake.Intercept = func(_ http.ResponseWriter, r *http.Request) bool {
		seen = append(seen, r.Clone(context.Background()))
		return false
	}
	c := client(t, fake)
	if _, err := c.Vaults(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, request := range seen {
		if strings.Contains(request.URL.String(), connecttest.Token) {
			t.Fatal("the token is in a URL")
		}
		for name, values := range request.Header {
			if name != "Authorization" && strings.Contains(strings.Join(values, " "), connecttest.Token) {
				t.Fatalf("the token is in header %s", name)
			}
		}
		if request.URL.Path == "/health" && request.Header.Get("Authorization") != "" {
			t.Fatal("the unauthenticated health read carried the token")
		}
	}
	if seen[0].Header.Get("Authorization") != "Bearer "+connecttest.Token {
		t.Fatal("the listing did not carry the bearer token")
	}
}

func TestHostileResponsesAreRefused(t *testing.T) {
	body := func(contentType, text string) func(http.ResponseWriter, *http.Request) bool {
		return func(w http.ResponseWriter, _ *http.Request) bool {
			w.Header().Set("Content-Type", contentType)
			w.Write([]byte(text))
			return true
		}
	}
	id := connecttest.ID(1)
	item := func(extra string) string {
		return `{"id":"` + id + `","category":"LOGIN","vault":{"id":"` + connecttest.VaultID + `"}` + extra + `}`
	}
	cases := []struct {
		name      string
		intercept func(http.ResponseWriter, *http.Request) bool
	}{
		{"oversized", body("application/json", `["`+strings.Repeat("a", MaxResponseBytes)+`"]`)},
		{"malformed", body("application/json", `[{"id":`)},
		{"wrong content type", body("text/html", `[]`)},
		{"no content type", func(w http.ResponseWriter, _ *http.Request) bool {
			w.Header()["Content-Type"] = nil
			w.Write([]byte(`[]`))
			return true
		}},
		{"trailing data", body("application/json", `[] []`)},
		{"wrong shape", body("application/json", `{"items":[]}`)},
		{"repeated member", body("application/json", `[`+item(`,"id":"`+connecttest.ID(2)+`"`)+`]`)},
		{"item listed twice", body("application/json", `[`+item("")+`,`+item("")+`]`)},
		{"malformed identifier", body("application/json", `[{"id":"../../v1/vaults","category":"LOGIN","vault":{"id":"`+connecttest.VaultID+`"}}]`)},
		{"missing identifier", body("application/json", `[{"category":"LOGIN","vault":{"id":"`+connecttest.VaultID+`"}}]`)},
		{"another vault's item", body("application/json", `[{"id":"`+id+`","category":"LOGIN","vault":{"id":"wwwwwwwwwwwwwwwwwwwwwwwwww"}}]`)},
		{"invalid utf-8", body("application/json", "[{\"id\":\"\xff\"}]")},
		{"client error", func(w http.ResponseWriter, _ *http.Request) bool { w.WriteHeader(http.StatusTeapot); return true }},
		{"no content", func(w http.ResponseWriter, _ *http.Request) bool { w.WriteHeader(http.StatusNoContent); return true }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			fake := connecttest.New(t)
			fake.Intercept = c.intercept
			_, err := client(t, fake).Items(context.Background(), connecttest.VaultID)
			if !errors.Is(err, ErrResponse) {
				t.Fatalf("accepted or misclassified: %v", err)
			}
			if c.name == "oversized" && !strings.Contains(err.Error(), "larger than the response bound") {
				t.Fatalf("an oversized answer was parsed rather than refused for its size: %v", err)
			}
			if strings.Contains(err.Error(), "127.0.0.1") || strings.Contains(err.Error(), connecttest.Token) {
				t.Fatalf("the error carries the URL or the token: %v", err)
			}
		})
	}
}

func TestAnItemReadMustReturnTheItemAsked(t *testing.T) {
	fake := connecttest.New(t, connecttest.Item{ID: connecttest.ID(1)}, connecttest.Item{ID: connecttest.ID(2)})
	c := client(t, fake)
	fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
		if strings.HasSuffix(r.URL.Path, connecttest.ID(1)) {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"id":%q,"category":"LOGIN","vault":{"id":%q}}`, connecttest.ID(2), connecttest.VaultID)
			return true
		}
		return false
	}
	if _, err := c.Item(context.Background(), connecttest.VaultID, connecttest.ID(1)); !errors.Is(err, ErrResponse) {
		t.Fatalf("a substituted item was accepted: %v", err)
	}
	if _, err := c.Item(context.Background(), connecttest.VaultID, connecttest.ID(2)); err != nil {
		t.Fatal(err)
	}
	// The right item, of another vault.
	other := "wwwwwwwwwwwwwwwwwwwwwwwwww"
	fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, connecttest.ID(1)):
			fmt.Fprintf(w, `{"id":%q,"category":"LOGIN","vault":{"id":%q}}`, connecttest.ID(1), other)
		case strings.HasSuffix(r.URL.Path, connecttest.VaultID):
			fmt.Fprintf(w, `{"id":%q,"name":"Another Vault","contentVersion":1}`, other)
		default:
			return false
		}
		return true
	}
	if _, err := c.Item(context.Background(), connecttest.VaultID, connecttest.ID(1)); !errors.Is(err, ErrResponse) {
		t.Fatalf("an item of another vault was accepted: %v", err)
	}
	if _, err := c.Vault(context.Background(), connecttest.VaultID); !errors.Is(err, ErrResponse) {
		t.Fatalf("another vault was accepted for the one asked: %v", err)
	}
	fake.Intercept = func(w http.ResponseWriter, r *http.Request) bool {
		if !strings.HasSuffix(r.URL.Path, connecttest.VaultID) {
			return false
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"name":"No identifier","contentVersion":1}`))
		return true
	}
	if _, err := c.Vault(context.Background(), connecttest.VaultID); !errors.Is(err, ErrResponse) {
		t.Fatalf("a vault with no identifier was accepted: %v", err)
	}
	fake.Intercept = nil
	before := fake.Count("/")
	for _, bad := range []string{"", "../x", "UPPERCASEUPPERCASEUPPERCAS", "short", connecttest.ID(1) + "/files", "example-item"} {
		if _, err := c.Item(context.Background(), connecttest.VaultID, bad); !errors.Is(err, ErrIdentifier) {
			t.Errorf("item id %q reached a path: %v", bad, err)
		}
		if _, err := c.Items(context.Background(), bad); !errors.Is(err, ErrIdentifier) {
			t.Errorf("vault id %q reached a path: %v", bad, err)
		}
		if _, err := c.Vault(context.Background(), bad); !errors.Is(err, ErrIdentifier) {
			t.Errorf("vault id %q reached a path: %v", bad, err)
		}
	}
	if fake.Count("/") != before {
		t.Fatal("a malformed identifier was requested")
	}
}

func TestDenialIsFinalAndUnavailabilityIsRetriedUnderTheDeadline(t *testing.T) {
	fake := connecttest.New(t)
	denied, err := NewToken([]byte("another.token"))
	if err != nil {
		t.Fatal(err)
	}
	c, _ := New(fake.Server.URL, denied)
	c.Sleep = func(context.Context, time.Duration) error { return nil }
	if _, attempts, err := c.WaitReady(context.Background()); !errors.Is(err, ErrDenied) || attempts != 1 {
		t.Fatalf("a refused token was retried or accepted: %d %v", attempts, err)
	}

	// Locked after a restart: unavailable until the authenticated request lands.
	fake.Unavailable = 3
	var waits []time.Duration
	c = client(t, fake)
	c.Sleep = func(_ context.Context, d time.Duration) error { waits = append(waits, d); return nil }
	vaults, attempts, err := c.WaitReady(context.Background())
	if err != nil || attempts != 4 || len(vaults) != 1 {
		t.Fatalf("readiness: %d attempts, %v", attempts, err)
	}
	if len(waits) != 3 || waits[0] != firstBackoff || waits[1] != 2*firstBackoff || waits[2] != 4*firstBackoff {
		t.Fatalf("backoff: %v", waits)
	}
	if fake.Count("/health") != 0 {
		t.Fatal("readiness was read from /health")
	}

	// Never ready: the context's deadline ends it, with the class intact.
	fake.Unavailable = 1 << 30
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	c = client(t, fake)
	c.Sleep = sleep
	started := time.Now()
	if _, _, err := c.WaitReady(ctx); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("an unavailable Connect was not reported as one: %v", err)
	}
	if time.Since(started) > 5*time.Second {
		t.Fatal("readiness outlived its deadline")
	}
	for _, wait := range []time.Duration{maxBackoff, 2 * maxBackoff} {
		if min(wait*2, maxBackoff) != maxBackoff {
			t.Fatal("backoff is unbounded")
		}
	}
}

func TestNothingListeningIsUnavailable(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	c, err := New("http://"+address, token(t))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Vaults(context.Background()); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("a closed port was not unavailable: %v", err)
	}
}

func TestASlowAnswerIsCutOffByTheContext(t *testing.T) {
	fake := connecttest.New(t)
	release := make(chan struct{})
	fake.Intercept = func(http.ResponseWriter, *http.Request) bool { <-release; return true }
	defer close(release)
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := client(t, fake).Vaults(ctx); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("a stalled answer was not unavailable: %v", err)
	}
}

func TestHealthReportsSyncState(t *testing.T) {
	fake := connecttest.New(t)
	fake.SyncStatus = "TOKEN_NEEDED"
	health, err := client(t, fake).Health(context.Background())
	if err != nil || health.Version != "1.8.1" || len(health.Dependencies) != 2 || *health.Dependencies[0].Status != "TOKEN_NEEDED" {
		t.Fatalf("health: %+v %v", health, err)
	}
}
