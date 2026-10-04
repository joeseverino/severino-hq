package runtime

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRedirectDropsCredentialsOnSameOriginRead(t *testing.T) {
	seen := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/first" {
			if r.Header.Get("Authorization") != "Bearer example" {
				t.Error("initial credential missing")
			}
			http.Redirect(w, r, "/last", http.StatusFound)
			return
		}
		seen = true
		if r.Header.Get("Authorization") != "" || r.Header.Get("X-Api-Key") != "" {
			t.Error("redirect received credentials")
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()
	h := &HTTPClient{}
	_, err := h.Request(context.Background(), server.URL+"/first", "GET", map[string]string{"Authorization": "Bearer example", "X-Api-Key": "example"}, nil)
	if err != nil || !seen {
		t.Fatalf("%v reached=%v", err, seen)
	}
}
func TestRedirectRefusesDifferentOriginAndAllWrites(t *testing.T) {
	for _, method := range []string{"GET", "POST", "PUT", "DELETE"} {
		t.Run(method, func(t *testing.T) {
			landed := false
			target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { landed = true }))
			defer target.Close()
			origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
			}))
			defer origin.Close()
			_, err := (&HTTPClient{}).Request(context.Background(), origin.URL, method, nil, nil)
			var provider *ProviderError
			if landed || !errors.As(err, &provider) || provider.Failure != "address" {
				t.Fatalf("landed=%v error=%v", landed, err)
			}
		})
	}
}
func TestProviderResponseFailures(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		failure    FailureClass
		status     int
	}{
		{"credential", "", "credential", 401}, {"permission", "", "permission", 403},
		{"html", "<html>login</html>", "address", 200}, {"malformed", "{", "", 200},
		{"trailing", "{} {}", "", 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			_, err := (&HTTPClient{}).Request(context.Background(), server.URL, "GET", nil, nil)
			var provider *ProviderError
			if !errors.As(err, &provider) || provider.Failure != tc.failure {
				t.Fatalf("%v", err)
			}
			if want := map[bool]int{true: tc.status}[tc.status != 200]; provider.HTTPStatus != want {
				t.Fatalf("status %d, want %d", provider.HTTPStatus, want)
			}
		})
	}
}
func TestRequestHeaderReadsBodyAndHeaderFromOneAnswer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/gone":
			w.WriteHeader(412)
			return
		case "/broken":
			w.Header().Set("ETag", `"v2"`)
			_, _ = w.Write([]byte(`{"a": 1} trailing`))
			return
		}
		w.Header().Set("ETag", `"v1"`)
		_, _ = w.Write([]byte(`{"read": true}`))
	}))
	defer server.Close()
	data, etag, err := (&HTTPClient{}).RequestHeader(context.Background(), server.URL, nil, "etag")
	if err != nil || etag != `"v1"` || string(data) != `{"read": true}` {
		t.Fatalf("%s %q %v", data, etag, err)
	}
	// A body that fails its JSON check still says which version answered.
	if _, etag, err := (&HTTPClient{}).RequestHeader(context.Background(), server.URL+"/broken", nil, "etag"); err == nil || etag != `"v2"` {
		t.Fatalf("%q %v", etag, err)
	}
	var provider *ProviderError
	if _, _, err := (&HTTPClient{}).RequestHeader(context.Background(), server.URL+"/gone", nil, "etag"); !errors.As(err, &provider) || provider.HTTPStatus != 412 {
		t.Fatalf("%v", err)
	}
}
func TestResponseSizeIsBounded(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chunk := strings.Repeat("x", 1<<20)
		for range 33 {
			if _, err := w.Write([]byte(chunk)); err != nil {
				return
			}
		}
	}))
	defer server.Close()
	_, err := (&HTTPClient{}).Request(context.Background(), server.URL, "GET", nil, nil)
	if err == nil || !strings.Contains(err.Error(), "32 MB") {
		t.Fatal(err)
	}
}
func TestMissingCABundleFailsClosed(t *testing.T) {
	if _, err := NewHTTPClient(t.TempDir() + "/missing.pem"); err == nil {
		t.Fatal("missing CA accepted")
	}
}

func TestRefusalKeepsItsBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"success":false,"errors":[{"message":"Content for A record must be a valid IPv4 address"}]}`))
	}))
	defer server.Close()
	_, err := (&HTTPClient{Transport: server.Client().Transport}).Request(context.Background(), server.URL, "GET", nil, nil)
	var refused *ProviderError
	if !errors.As(err, &refused) || refused.HTTPStatus != 400 || !strings.Contains(string(refused.Body), "valid IPv4 address") {
		t.Fatalf("refusal = %#v", err)
	}
}
