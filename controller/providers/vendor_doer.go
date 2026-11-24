package providers

import (
	"bytes"
	"io"
	"maps"
	"net/http"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// vendorDoer is the HttpRequestDoer every generated vendor client sends
// through: it hands each request to the registry's Transport, so egress,
// redirect and budget rules hold for a generated call as for any other.
// Exchange answers only a 2xx and returns any other answer as its error, so
// what the doer returns is a success whose body, when it has one, is JSON
// Exchange already validated.
type vendorDoer struct {
	r *Registry
	// err is why the connection cannot be reached, which every request returns.
	err error
	// headers is the connection's credential, sent with every request.
	headers map[string]string
	// cache scopes the sweep cache a GET is read once through; empty reads fresh.
	cache string
}

func (d vendorDoer) Do(req *http.Request) (*http.Response, error) {
	if d.err != nil {
		return nil, d.err
	}
	var payload any
	if req.Body != nil {
		body, err := io.ReadAll(req.Body)
		if err != nil {
			return nil, err
		}
		payload = body
	}
	headers := map[string]string{}
	maps.Copy(headers, d.headers)
	for name := range req.Header {
		headers[name] = req.Header.Get(name)
	}
	exchange := func() (runtime.Answer, error) {
		return d.r.HTTP.Exchange(req.Context(), req.URL.String(), req.Method, headers, payload)
	}
	var answer runtime.Answer
	var err error
	if d.cache != "" && req.Method == http.MethodGet {
		answer, err = cached(req.Context(), d.r, d.cache+" "+req.URL.String(), exchange)
	} else {
		answer, err = exchange()
	}
	if err != nil {
		return nil, err
	}
	header := answer.Header.Clone()
	if header == nil {
		header = http.Header{}
	}
	if len(answer.Body) > 0 {
		header.Set("Content-Type", "application/json")
	}
	return &http.Response{StatusCode: answer.Status, Status: http.StatusText(answer.Status), Header: header, Body: io.NopCloser(bytes.NewReader(answer.Body)), Request: req}, nil
}

// json200 is a generated reply's 200 body, the zero value for an empty one.
func json200[T any](reply interface{ GetJSON200() *T }, err error) (T, error) {
	if err != nil {
		var zero T
		return zero, err
	}
	return deref(reply.GetJSON200()), nil
}

// json201 is a generated reply's 201 body, the zero value for an empty one.
func json201[T any](reply interface{ GetJSON201() *T }, err error) (T, error) {
	if err != nil {
		var zero T
		return zero, err
	}
	return deref(reply.GetJSON201()), nil
}
