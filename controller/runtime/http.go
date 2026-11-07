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

// encode writes the parts with mime/multipart, which quotes a field name or
// filename that could otherwise end its header.
func (m Multipart) encode() ([]byte, string, error) {
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

// Request returns the provider's answer as validated JSON, undecoded: each
// provider decodes it once into its own type. An empty body is nil.
func (h *HTTPClient) Request(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, error) {
	data, _, err := h.do(ctx, address, method, headers, payload)
	if err != nil || len(data) == 0 {
		return nil, err
	}
	return data, nil
}

// Exchange is Request with the answer's headers: the ETag of the version read,
// what a rate limit has left. The headers come with any answer, a refusal or a
// body that fails its checks included, and are nil only when nothing answered.
func (h *HTTPClient) Exchange(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, http.Header, error) {
	data, header, err := h.do(ctx, address, method, headers, payload)
	if len(data) == 0 {
		data = nil
	}
	return data, header, err
}

func (h *HTTPClient) do(ctx context.Context, address, method string, headers map[string]string, payload any) (json.RawMessage, http.Header, error) {
	timeout := time.Duration(0)
	if _, ok := payload.(Multipart); ok {
		timeout = MultipartTimeout
	}
	return h.send(ctx, address, method, headers, payload, timeout)
}

func (h *HTTPClient) send(ctx context.Context, address, method string, headers map[string]string, payload any, timeoutOverride time.Duration) (json.RawMessage, http.Header, error) {
	var body []byte
	contentType := "application/json"
	if payload != nil {
		switch p := payload.(type) {
		case Multipart:
			var err error
			body, contentType, err = p.encode()
			if err != nil {
				return nil, nil, &ProviderError{Message: "the request could not be encoded"}
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
				return nil, nil, &ProviderError{Message: "the request could not be encoded"}
			}
		}
	}
	request, err := http.NewRequestWithContext(ctx, method, address, bytes.NewReader(body))
	if err != nil || request.URL.User != nil || request.URL.Hostname() == "" || (request.URL.Scheme != "http" && request.URL.Scheme != "https") {
		return nil, nil, &ProviderError{Message: "not a direct API address", Failure: FailureClassAddress}
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
	if timeoutOverride != 0 {
		timeout = timeoutOverride
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
			return nil, nil, provider
		}
		return nil, nil, requestFailure(err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		refusal := HTTPRefusal(response.StatusCode)
		refusal.Body, _ = io.ReadAll(io.LimitReader(response.Body, maxRefusalBytes))
		return nil, response.Header, refusal
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, MaxResponseBytes+1))
	if err != nil {
		return nil, nil, &ProviderError{Message: "the service's answer could not be read", Failure: FailureClassNetwork}
	}
	if len(data) > MaxResponseBytes {
		return nil, nil, &ProviderError{Message: "the service answered with more than 32 MB"}
	}
	if len(data) == 0 {
		return nil, response.Header, nil
	}
	responseContentType := strings.ToLower(response.Header.Get("Content-Type"))
	if strings.Contains(responseContentType, "text/html") || strings.Contains(responseContentType, "application/xhtml+xml") || bytes.HasPrefix(bytes.TrimSpace(data), []byte("<")) {
		return nil, response.Header, &ProviderError{Message: "address answered with a web page, not the API; use the service's direct API address", Failure: FailureClassAddress}
	}
	var result json.RawMessage
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&result); err != nil {
		return nil, response.Header, &ProviderError{Message: "the service returned invalid JSON"}
	}
	if err := decoder.Decode(new(json.RawMessage)); err != io.EOF {
		return nil, response.Header, &ProviderError{Message: "the service returned invalid JSON"}
	}
	return result, response.Header, nil
}
