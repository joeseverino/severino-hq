package runtime

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
)

const MaxResponseBytes = 32 << 20

// maxRefusalBytes bounds the body kept from a non-2xx answer.
const maxRefusalBytes = 64 << 10

// HTTPClient has a request-local redirect policy. A read may follow only its
// own origin and never forwards caller credentials to the redirected request.
type HTTPClient struct {
	Transport http.RoundTripper
	Timeout   time.Duration
}

func NewHTTPClient(caFile string) (*HTTPClient, error) {
	roots, err := x509.SystemCertPool()
	if err != nil {
		return nil, &ProviderError{Message: "HQ's CA bundle could not be loaded"}
	}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil || !roots.AppendCertsFromPEM(data) {
			return nil, &ProviderError{Message: "HQ's CA bundle could not be loaded"}
		}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
	return &HTTPClient{Transport: transport, Timeout: DefaultRequestTimeout}, nil
}

// MultipartFile is one part of a multipart/form-data upload.
type MultipartFile struct {
	Field    string
	Filename string
	Content  []byte
}

// Multipart is a request payload sent as multipart/form-data, in part order.
type Multipart []MultipartFile

// partContentType is what each part declares: every upload here is PEM.
const partContentType = "application/x-pem-file"

// Encode writes the parts with mime/multipart, which quotes a field name or
// filename that could otherwise end its header, and returns the body and its
// content type.
func (m Multipart) Encode() ([]byte, string, error) {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for _, file := range m {
		header := textproto.MIMEHeader{}
		header.Set("Content-Disposition", multipart.FileContentDisposition(file.Field, file.Filename))
		header.Set("Content-Type", partContentType)
		part, err := writer.CreatePart(header)
		if err != nil {
			return nil, "", err
		}
		if _, err := part.Write(file.Content); err != nil {
			return nil, "", err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, "", err
	}
	return body.Bytes(), writer.FormDataContentType(), nil
}

// learnCredential learns the credential a request header carries: every
// token this process sends a vendor goes through here, minted or held, so the
// mask strikes it whatever reached it first.
func learnCredential(name, value string) {
	canonical := http.CanonicalHeaderKey(name)
	switch {
	case canonical == "Authorization", canonical == "Proxy-Authorization", canonical == "Cookie":
	case strings.Contains(canonical, "Key"), strings.Contains(canonical, "Token"), strings.Contains(canonical, "Secret"):
	default:
		return
	}
	mask.Learn(value)
	// The credential after its scheme: Bearer, Basic, Token.
	if _, credential, ok := strings.Cut(value, " "); ok {
		mask.Learn(credential)
	}
}

// requestFailure classifies a request that got no answer. The address is
// left out: it can carry a token in its query.
func requestFailure(err error) *ProviderError {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return &ProviderError{Message: "the request timed out", Failure: FailureClassNetwork}
	}
	return &ProviderError{Message: "the service is unreachable", Failure: FailureClassNetwork}
}

func origin(u *url.URL) string {
	port := u.Port()
	if port == "" {
		switch u.Scheme {
		case "https":
			port = "443"
		case "http":
			port = "80"
		}
	}
	return strings.ToLower(u.Scheme) + "://" + strings.ToLower(u.Hostname()) + ":" + port
}

// Answer is what a provider answered: its 2xx status and validated JSON body,
// undecoded, which each provider decodes once into its own type; an empty
// body is nil. Its headers (the ETag of the version read, what a rate limit
// has left) come with any answer, a refusal or a body that fails its checks
// included, and are nil only when nothing answered.
type Answer struct {
	Status int
	Header http.Header
	Body   json.RawMessage
}

// Request is Exchange's body alone.
func (h *HTTPClient) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	answer, err := h.Exchange(ctx, address, method, headers, payload)
	if err != nil {
		return nil, err
	}
	return answer.Body, nil
}

func (h *HTTPClient) Exchange(ctx context.Context, address, method string, headers map[string]string, payload any) (Answer, error) {
	var body []byte
	contentType := "application/json"
	if payload != nil {
		switch p := payload.(type) {
		case Multipart:
			var err error
			body, contentType, err = p.Encode()
			if err != nil {
				return Answer{}, &ProviderError{Message: "the request could not be encoded"}
			}
		case url.Values:
			body = []byte(p.Encode())
			contentType = "application/x-www-form-urlencoded"
		case string:
			body = []byte(p)
		case []byte:
			body = p
		default:
			var err error
			body, err = json.Marshal(payload)
			if err != nil {
				return Answer{}, &ProviderError{Message: "the request could not be encoded"}
			}
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, address, bytes.NewReader(body))
	if err != nil || request.URL.User != nil || request.URL.Hostname() == "" || (request.URL.Scheme != "http" && request.URL.Scheme != "https") {
		return Answer{}, &ProviderError{Message: "not a direct API address", Failure: FailureClassAddress}
	}
	request.Header.Set("Accept", "application/json")
	for name, value := range headers {
		request.Header.Set(name, value)
		learnCredential(name, value)
	}
	if payload != nil && request.Header.Get("Content-Type") == "" {
		request.Header.Set("Content-Type", contentType)
	}
	timeout := h.Timeout
	if timeout == 0 {
		timeout = DefaultRequestTimeout
	}
	if strings.HasPrefix(request.Header.Get("Content-Type"), "multipart/form-data") {
		timeout = MultipartTimeout
	}
	client := &http.Client{Transport: h.Transport, Timeout: timeout, CheckRedirect: func(next *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return &ProviderError{Message: "the service redirected too many times", Failure: FailureClassAddress}
		}
		if method != "GET" && method != "HEAD" {
			return &ProviderError{Message: "the service redirected a write request, which is not followed; use its direct API address", Failure: FailureClassAddress}
		}
		if origin(next.URL) != origin(request.URL) {
			return &ProviderError{Message: "the service redirected outside its API origin; use its direct API address", Failure: FailureClassAddress}
		}
		for name := range next.Header {
			if !strings.EqualFold(name, "Accept") && !strings.EqualFold(name, "Content-Type") {
				next.Header.Del(name)
			}
		}
		return nil
	}}
	response, err := client.Do(request)
	if err != nil {
		if provider, ok := errors.AsType[*ProviderError](err); ok {
			return Answer{}, provider
		}
		return Answer{}, requestFailure(err)
	}
	defer response.Body.Close()
	answer := Answer{Status: response.StatusCode, Header: response.Header}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		refusal := HTTPRefusal(response.StatusCode)
		refusal.Body, _ = io.ReadAll(io.LimitReader(response.Body, maxRefusalBytes))
		return answer, refusal
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, MaxResponseBytes+1))
	if err != nil {
		return Answer{}, &ProviderError{Message: "the service's answer could not be read", Failure: FailureClassNetwork}
	}
	if len(data) > MaxResponseBytes {
		return Answer{}, &ProviderError{Message: "the service answered with more than 32 MB"}
	}
	if len(data) == 0 {
		return answer, nil
	}
	responseContentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(responseContentType, "text/html") || strings.Contains(responseContentType, "application/xhtml+xml") || bytes.HasPrefix(bytes.TrimSpace(data), []byte("<")) {
		return answer, &ProviderError{Message: "address answered with a web page, not the API; use the service's direct API address", Failure: FailureClassAddress}
	}
	if !json.Valid(data) {
		return answer, &ProviderError{Message: "the service returned invalid JSON"}
	}
	answer.Body = data
	return answer, nil
}

// Download reads what address hands out as a signed link (a job's log): a
// GET of address answered with a redirect, whose target's body read is given,
// cut at limit bytes. The redirect is followed once, by hand, to an https
// address and with only the User-Agent of headers, so the credential that
// asked for the link never reaches the host that serves it, and that host
// redirects nowhere. The Answer is address's own, its headers what a rate
// limit has left.
func (h *HTTPClient) Download(ctx context.Context, address string, headers map[string]string, limit int64, read func(io.Reader) error) (Answer, error) {
	timeout := h.Timeout
	if timeout == 0 {
		timeout = DefaultRequestTimeout
	}
	client := &http.Client{Transport: h.Transport, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	get := func(at string, send map[string]string) (*http.Response, error) {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, at, http.NoBody)
		if err != nil || request.URL.User != nil || request.URL.Hostname() == "" || (request.URL.Scheme != "http" && request.URL.Scheme != "https") {
			return nil, &ProviderError{Message: "not a direct API address", Failure: FailureClassAddress}
		}
		for name, value := range send {
			request.Header.Set(name, value)
			learnCredential(name, value)
		}
		response, err := client.Do(request)
		if err != nil {
			return nil, requestFailure(err)
		}
		return response, nil
	}
	asked, err := get(address, headers)
	if err != nil {
		return Answer{}, err
	}
	_ = asked.Body.Close()
	answer := Answer{Status: asked.StatusCode, Header: asked.Header}
	link, err := asked.Location()
	if asked.StatusCode < 300 || asked.StatusCode >= 400 || err != nil {
		if asked.StatusCode >= 400 {
			return answer, HTTPRefusal(asked.StatusCode)
		}
		return answer, &ProviderError{Message: "the service handed out no link to download"}
	}
	if link.Scheme != "https" {
		return answer, &ProviderError{Message: "the service's download link is not https", Failure: FailureClassAddress}
	}
	served, err := get(link.String(), map[string]string{"User-Agent": headers["User-Agent"]})
	if err != nil {
		return answer, err
	}
	defer served.Body.Close()
	if served.StatusCode != http.StatusOK {
		return answer, HTTPRefusal(served.StatusCode)
	}
	if err := read(io.LimitReader(served.Body, limit)); err != nil {
		return answer, &ProviderError{Message: "the download could not be read", Failure: FailureClassNetwork, Err: err}
	}
	return answer, nil
}
