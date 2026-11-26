package runtime

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/secret"
)

// carriedForms is a secret as a refusal or a child's stderr may carry it,
// each beside the part that must not survive. A base64 run keeps the edge
// characters it can share with its neighbors.
func carriedForms(sentinel string) map[string][2]string {
	encoded := base64.StdEncoding.EncodeToString([]byte(sentinel))
	quoted := strconv.Quote(sentinel)
	return map[string][2]string{
		"plain":        {"provider refused " + sentinel, sentinel},
		"base64":       {"refused Basic " + encoded, encoded[2 : len(encoded)-2]},
		"url-encoded":  {"refused /login?password=" + url.QueryEscape(sentinel), url.QueryEscape(sentinel)},
		"quote-escape": {"refused " + quoted, quoted[1 : len(quoted)-1]},
	}
}

func assertStruck(t *testing.T, where, text, leak string) {
	t.Helper()
	if strings.Contains(text, leak) {
		t.Errorf("%s carries %q: %q", where, leak, text)
	}
}

// A secret setting of a connection the run holds never reaches a report,
// however the error that carries it is worded or wrapped.
func TestAHeldSecretSettingNeverReachesAReport(t *testing.T) {
	sentinel := `sentinel "held" pass/word+0123`
	NewConnections(connections.Connection{
		Ref: "example", Provider: "example",
		Login: &connections.Login{URL: "https://a.example.com", Username: "reader", Password: secret.New(sentinel)},
	})
	for name, form := range carriedForms(sentinel) {
		wrapped := fmt.Errorf("read failed: %w", &ProviderError{Message: "login", Err: errors.New(form[0])})
		assertStruck(t, name+" report text", ReportText(wrapped.Error()), form[1])
		assertStruck(t, name+" reason", Clip(wrapped.Error(), ReasonLimit), form[1])
	}
}

// A cut that falls inside a secret leaves none of it behind.
func TestAClipNeverLeavesPartOfASecret(t *testing.T) {
	sentinel := "sentinel-clipped-secret-0123456789"
	NewConnections(connections.Connection{Ref: "example", Provider: "example", ServiceAccount: &connections.ServiceAccount{APIToken: secret.New(sentinel)}})
	text := strings.Repeat("x", ReasonLimit-10) + sentinel
	assertStruck(t, "a clipped reason", Clip(text, ReasonLimit), sentinel[:10])
}

// A token this process sends a vendor is struck from the refusal that echoes
// it back, whoever minted it.
func TestATokenSentIsStruckFromTheRefusal(t *testing.T) {
	token := "sentinel-sent-token-0123456789"
	var echoed string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		echoed = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"message":"token `+token+` is not valid"}`)
	}))
	defer server.Close()
	_, err := (&HTTPClient{Transport: server.Client().Transport}).Request(t.Context(), server.URL, "GET", map[string]string{"Authorization": "Bearer " + token}, nil)
	if echoed != "Bearer "+token {
		t.Fatalf("the vendor was sent %q", echoed)
	}
	refused, ok := errors.AsType[*ProviderError](err)
	if !ok {
		t.Fatalf("not a refusal: %v", err)
	}
	for name, form := range carriedForms(token) {
		assertStruck(t, name+" refusal", ReportText(string(refused.Body)+" "+form[0]), form[1])
	}
}

// A value opened from its seal is struck from what is reported.
func TestASealedValueOpenedIsStruck(t *testing.T) {
	v := readVector(t)
	if _, err := vectorOpener(t).Open(v.Sealed, v.ResourceKey, v.Slot); err != nil {
		t.Fatal(err)
	}
	for name, form := range carriedForms(v.Plaintext) {
		assertStruck(t, name+" report text", ReportText(form[0]), form[1])
	}
}

// What the bridge sends HQ carries no learned secret, in a field nothing
// clipped too.
func TestTheBridgeSendsNoLearnedSecret(t *testing.T) {
	sentinel := `sentinel "bridged" secret/0123+&`
	NewConnections(connections.Connection{Ref: "example", Provider: "example", ServiceAccount: &connections.ServiceAccount{APIToken: secret.New(sentinel)}})
	var mu sync.Mutex
	var sent string
	bridge := served(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		sent = string(body)
		mu.Unlock()
		answer(`{"ok":true}`)(w, r)
	})
	report := ControllerReport{Message: "failed with " + sentinel, Status: map[string]any{"said": sentinel}}
	if err := bridge.Report(context.Background(), "example", "1", report); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	quoted := strconv.Quote(sentinel)
	if sent == "" || strings.Contains(sent, sentinel) || strings.Contains(sent, quoted[1:len(quoted)-1]) || strings.Contains(sent, `bridged\"`) {
		t.Fatalf("the bridge sent %s", sent)
	}
}
