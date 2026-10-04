package runtime

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
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
		return nil, &ProviderError{Message: "Controller CA bundle could not be loaded."}
	}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil || !roots.AppendCertsFromPEM(data) {
			return nil, &ProviderError{Message: "Controller CA bundle could not be loaded."}
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

func (m Multipart) encode() ([]byte, string) {
	token := make([]byte, 16)
	_, _ = rand.Read(token)
	boundary := "----severino-hq-" + hex.EncodeToString(token)
	var body bytes.Buffer
	for _, part := range m {
		body.WriteString("--" + boundary + "\r\n")
		body.WriteString(`Content-Disposition: form-data; name="` + part.Field + `"; filename="` + part.Filename + "\"\r\n")
		body.WriteString("Content-Type: application/x-pem-file\r\n\r\n")
		body.Write(part.Content)
		body.WriteString("\r\n")
	}
	body.WriteString("--" + boundary + "--\r\n")
	return body.Bytes(), "multipart/form-data; boundary=" + boundary
}

// requestFailure names a failed request the way the Python controller does:
// the transport error's class.
func requestFailure(err error) *ProviderError {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return &ProviderError{Message: "Provider request failed: TimeoutError.", Failure: FailureClassNetwork}
	}
	return &ProviderError{Message: "Provider request failed: URLError.", Failure: FailureClassNetwork}
}

// AsMultipartFailure rewrites a failure the way the Python multipart path does:
// its own sentence, and no failure classification.
func AsMultipartFailure(err *ProviderError) *ProviderError {
	kind := "URLError"
	if err.HTTPStatus != 0 {
		kind = "HTTPError"
	} else if strings.Contains(err.Message, "TimeoutError") {
		kind = "TimeoutError"
	} else if err.Failure != FailureClassNetwork {
		return err
	}
	return &ProviderError{Message: "Provider multipart request failed: " + kind + ".", HTTPStatus: err.HTTPStatus}
}

func origin(u *url.URL) string {
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else if u.Scheme == "http" {
			port = "80"
		}
	}
	return strings.ToLower(u.Scheme) + "://" + strings.ToLower(u.Hostname()) + ":" + port
}

// Request returns the provider's answer as validated JSON, undecoded: each
// provider decodes it once into its own type. An empty body is nil.
func (h *HTTPClient) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	data, _, err := h.do(ctx, address, method, headers, payload)
	if err != nil || len(data) == 0 {
		return nil, err
	}
	return data, nil
}

// RequestHeader GETs address and returns its validated JSON with one response
// header from the same answer, such as the ETag of the version read. The header
// is kept even when the body fails its checks.
func (h *HTTPClient) RequestHeader(ctx context.Context, address string, headers map[string]string, name string) (json.RawMessage, string, error) {
	data, header, err := h.do(ctx, address, "GET", headers, nil)
	if len(data) == 0 {
		data = nil
	}
	return data, header.Get(name), err
}

func (h *HTTPClient) do(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, http.Header, error) {
	if files, ok := payload.(Multipart); ok {
		data, header, err := h.send(ctx, address, method, headers, files, MultipartTimeout)
		var provider *ProviderError
		if errors.As(err, &provider) {
			return nil, nil, AsMultipartFailure(provider)
		}
		return data, header, err
	}
	return h.send(ctx, address, method, headers, payload, 0)
}

func (h *HTTPClient) send(ctx context.Context, address, method string, headers map[string]string, payload any, timeoutOverride time.Duration) (json.RawMessage, http.Header, error) {
	var body []byte
	contentType := "application/json"
	if payload != nil {
		switch p := payload.(type) {
		case Multipart:
			body, contentType = p.encode()
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
				return nil, nil, &ProviderError{Message: "Provider request could not be encoded."}
			}
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, address, bytes.NewReader(body))
	if err != nil || request.URL.User != nil || request.URL.Hostname() == "" || (request.URL.Scheme != "http" && request.URL.Scheme != "https") {
		return nil, nil, &ProviderError{Message: "Use the provider's direct API address.", Failure: FailureClassAddress}
	}
	request.Header.Set("Accept", "application/json")
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	if payload != nil && request.Header.Get("Content-Type") == "" {
		request.Header.Set("Content-Type", contentType)
	}
	timeout := h.Timeout
	if timeout == 0 {
		timeout = DefaultRequestTimeout
	}
	if timeoutOverride != 0 {
		timeout = timeoutOverride
	}
	client := &http.Client{Transport: h.Transport, Timeout: timeout, CheckRedirect: func(next *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return &ProviderError{Message: "Provider redirected too many times.", Failure: FailureClassAddress}
		}
		if method != "GET" && method != "HEAD" {
			return &ProviderError{Message: "The address redirected a write request, which is not followed. Use the provider's direct API address.", Failure: FailureClassAddress}
		}
		if origin(next.URL) != origin(request.URL) {
			return &ProviderError{Message: "The address redirected outside the API origin. Use the provider's direct API address.", Failure: FailureClassAddress}
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
		var provider *ProviderError
		if errors.As(err, &provider) {
			return nil, nil, provider
		}
		return nil, nil, requestFailure(err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		refusal := HTTPRefusal(response.StatusCode)
		refusal.Body, _ = io.ReadAll(io.LimitReader(response.Body, maxRefusalBytes))
		return nil, nil, refusal
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, MaxResponseBytes+1))
	if err != nil {
		return nil, nil, &ProviderError{Message: "Provider response could not be read.", Failure: FailureClassNetwork}
	}
	if len(data) > MaxResponseBytes {
		return nil, nil, &ProviderError{Message: "The provider answered with more than 32 MB."}
	}
	if len(data) == 0 {
		return nil, response.Header, nil
	}
	responseContentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(responseContentType, "text/html") || strings.Contains(responseContentType, "application/xhtml+xml") || bytes.HasPrefix(bytes.TrimSpace(data), []byte("<")) {
		return nil, response.Header, &ProviderError{Message: "The address answered with a web page, not the API. Use the provider's direct API address.", Failure: FailureClassAddress}
	}
	var result json.RawMessage
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&result); err != nil {
		return nil, response.Header, &ProviderError{Message: "Provider returned invalid JSON."}
	}
	if err := decoder.Decode(new(json.RawMessage)); err != io.EOF {
		return nil, response.Header, &ProviderError{Message: "Provider returned invalid JSON."}
	}
	return result, response.Header, nil
}
