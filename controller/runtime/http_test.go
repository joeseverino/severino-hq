package runtime

import (
	"errors"
	"io"
	"mime"
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
	_, err := h.Request(t.Context(), server.URL+"/first", "GET", map[string]string{"Authorization": "Bearer example", "X-Api-Key": "example"}, nil)
	if err != nil || !seen {
		t.Fatalf("%v reached=%v", err, seen)
	}
}
func TestRedirectRefusesDifferentOriginAndAllWrites(t *testing.T) {
	for _, method := range []string{"GET", "POST", "PUT", "DELETE"} {
		t.Run(method, func(t *testing.T) {
			landed := false
			target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { landed = true }))
			defer target.Close()
			origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
			}))
			defer origin.Close()
			_, err := (&HTTPClient{}).Request(t.Context(), origin.URL, method, nil, nil)
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
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			_, err := (&HTTPClient{}).Request(t.Context(), server.URL, "GET", nil, nil)
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
func TestExchangeReadsBodyAndHeadersFromOneAnswer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/gone":
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.WriteHeader(http.StatusPreconditionFailed)
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
	answer, err := (&HTTPClient{}).Exchange(t.Context(), server.URL, "GET", nil, nil)
	if err != nil || answer.Status != http.StatusOK || answer.Header.Get("etag") != `"v1"` || string(answer.Body) != `{"read": true}` {
		t.Fatalf("%+v %v", answer, err)
	}
	// A body that fails its JSON check still says which version answered.
	if answer, err := (&HTTPClient{}).Exchange(t.Context(), server.URL+"/broken", "GET", nil, nil); err == nil || answer.Header.Get("etag") != `"v2"` {
		t.Fatalf("%+v %v", answer, err)
	}
	// A refusal keeps its headers: what a rate limit has left is said there.
	var provider *ProviderError
	if answer, err := (&HTTPClient{}).Exchange(t.Context(), server.URL+"/gone", "GET", nil, nil); !errors.As(err, &provider) || provider.HTTPStatus != 412 || answer.Header.Get("X-RateLimit-Remaining") != "0" {
		t.Fatalf("%+v %v", answer, err)
	}
}
func TestResponseSizeIsBounded(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		chunk := strings.Repeat("x", 1<<20)
		for range 33 {
			if _, err := w.Write([]byte(chunk)); err != nil {
				return
			}
		}
	}))
	defer server.Close()
	_, err := (&HTTPClient{}).Request(t.Context(), server.URL, "GET", nil, nil)
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
	_, err := (&HTTPClient{Transport: server.Client().Transport}).Request(t.Context(), server.URL, "GET", nil, nil)
	var refused *ProviderError
	if !errors.As(err, &refused) || refused.HTTPStatus != 400 || !strings.Contains(string(refused.Body), "valid IPv4 address") {
		t.Fatalf("refusal = %#v", err)
	}
}

// The upload a provider receives for plain names is exactly the documented
// multipart/form-data framing: each part's disposition, its PEM type, its
// bytes, in order.
func TestMultipartIsTheDocumentedFraming(t *testing.T) {
	parts := Multipart{
		{Field: "certificate", Filename: "certificate.pem", Content: []byte("leaf\n")},
		{Field: "certificate_key", Filename: "certificate_key.pem", Content: []byte("key\n")},
	}
	body, contentType, err := parts.Encode()
	if err != nil {
		t.Fatal(err)
	}
	mediaType, params, err := mime.ParseMediaType(contentType)
	if err != nil || mediaType != "multipart/form-data" || params["boundary"] == "" {
		t.Fatalf("content type %q", contentType)
	}
	boundary := params["boundary"]
	want := "--" + boundary + "\r\n" +
		"Content-Disposition: form-data; name=\"certificate\"; filename=\"certificate.pem\"\r\n" +
		"Content-Type: application/x-pem-file\r\n\r\n" +
		"leaf\n\r\n" +
		"--" + boundary + "\r\n" +
		"Content-Disposition: form-data; name=\"certificate_key\"; filename=\"certificate_key.pem\"\r\n" +
		"Content-Type: application/x-pem-file\r\n\r\n" +
		"key\n\r\n" +
		"--" + boundary + "--\r\n"
	if string(body) != want {
		t.Fatalf("body:\n%q\nwant:\n%q", body, want)
	}
}

// A name that holds a quote, a backslash or a line break stays inside its
// own header: the receiver reads back the names and bytes that were given,
// and no part the caller did not send.
func TestMultipartNamesCannotEndTheirHeader(t *testing.T) {
	hostile := []MultipartFile{
		{Field: `cert"; filename="other.pem`, Filename: `a"b\c.pem`, Content: []byte("one")},
		{Field: "key", Filename: "x.pem\"\r\nX-Injected: yes\r\n\r\ninjected", Content: []byte("two")},
	}
	var received []MultipartFile
	var types, injected []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reader, err := r.MultipartReader()
		if err != nil {
			t.Error(err)
			return
		}
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Error(err)
				return
			}
			content, _ := io.ReadAll(part)
			received = append(received, MultipartFile{Field: part.FormName(), Filename: part.FileName(), Content: content})
			types = append(types, part.Header.Get("Content-Type"))
			injected = append(injected, part.Header.Get("X-Injected"))
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	client := &HTTPClient{}
	if _, err := client.Request(t.Context(), server.URL, "POST", nil, Multipart(hostile)); err != nil {
		t.Fatal(err)
	}
	if len(received) != len(hostile) {
		t.Fatalf("received %d parts, sent %d", len(received), len(hostile))
	}
	for i, sent := range hostile {
		got := received[i]
		if got.Field != sent.Field || string(got.Content) != string(sent.Content) {
			t.Errorf("part %d: field %q content %q", i, got.Field, got.Content)
		}
		if types[i] != partContentType || injected[i] != "" {
			t.Errorf("part %d: content type %q, injected header %q", i, types[i], injected[i])
		}
	}
	if received[0].Filename != `a"b\c.pem` {
		t.Errorf("filename %q", received[0].Filename)
	}
	// A line break cannot be carried in a header: it arrives percent-encoded.
	if want := `x.pem"%0D%0AX-Injected: yes%0D%0A%0D%0Ainjected`; received[1].Filename != want {
		t.Errorf("filename %q, want %q", received[1].Filename, want)
	}
}

// A download link is followed once and only to https: a link that is not
// is refused before anything is read from it.
func TestDownloadFollowsOnlyAnHTTPSLink(t *testing.T) {
	served := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/logs" {
			http.Redirect(w, r, "/signed", http.StatusFound)
			return
		}
		served = true
		_, _ = w.Write([]byte("log"))
	}))
	defer server.Close()
	h := &HTTPClient{}
	_, err := h.Download(t.Context(), server.URL+"/logs", map[string]string{"Authorization": "Bearer example"}, 1<<10, func(io.Reader) error {
		t.Error("an http link was read")
		return nil
	})
	if provider, ok := errors.AsType[*ProviderError](err); !ok || provider.Failure != FailureClassAddress || served {
		t.Fatalf("err %v, served %v", err, served)
	}
}

// The host serving a download link gets only the User-Agent, even on the
// asking host's own name, where Go's client would send its Authorization,
// and the answer is the asking host's, whose headers say what a rate limit
// has left.
func TestADownloadLinkGetsNoHeaderButTheUserAgent(t *testing.T) {
	var got http.Header
	signed := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		_, _ = w.Write([]byte("log"))
	}))
	defer signed.Close()
	asking := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Ratelimit-Remaining", "41")
		http.Redirect(w, r, signed.URL+"/signed", http.StatusFound)
	}))
	defer asking.Close()
	h := &HTTPClient{Transport: signed.Client().Transport}
	var read string
	answer, err := h.Download(t.Context(), asking.URL+"/logs", map[string]string{"Authorization": "Bearer example", "User-Agent": "example-agent", "X-Example": "kept"}, 1<<10, func(body io.Reader) error {
		data, err := io.ReadAll(body)
		read = string(data)
		return err
	})
	if err != nil || read != "log" || answer.Header.Get("X-Ratelimit-Remaining") != "41" {
		t.Fatalf("read %q, answer %+v, err %v", read, answer, err)
	}
	for name := range got {
		if name != "User-Agent" && name != "Accept-Encoding" {
			t.Errorf("the link's host got %s", name)
		}
	}
}
