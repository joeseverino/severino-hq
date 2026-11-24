package providers

import (
	"bytes"
	"encoding/json"
	"io"
	"maps"
	"net/http"
)

// vendorDoer is the HttpRequestDoer every generated vendor client sends
// through: it hands each request to the registry's Transport, so egress,
// redirect and budget rules hold for a generated call as for any other.
// Exchange answers only a 2xx and returns any other answer as its error, so
// what the doer returns is a 200 whose body, when it has one, is JSON
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
	headers := maps.Clone(d.headers)
	for name := range req.Header {
		headers[name] = req.Header.Get(name)
	}
	var data json.RawMessage
	var header http.Header
	var err error
	if d.cache != "" && req.Method == http.MethodGet {
		data, err = d.r.cached(req.Context(), d.cache+" "+req.URL.String(), func() (json.RawMessage, error) {
			data, _, err := d.r.HTTP.Exchange(req.Context(), req.URL.String(), req.Method, headers, nil)
			return data, err
		})
	} else {
		data, header, err = d.r.HTTP.Exchange(req.Context(), req.URL.String(), req.Method, headers, payload)
	}
	if err != nil {
		return nil, err
	}
	header = header.Clone()
	if header == nil {
		header = http.Header{}
	}
	if len(data) > 0 {
		header.Set("Content-Type", "application/json")
	}
	return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: header, Body: io.NopCloser(bytes.NewReader(data)), Request: req}, nil
}

// json200 is a generated reply's 200 body, the zero value for an empty one.
func json200[T any](reply interface{ GetJSON200() *T }, err error) (T, error) {
	if err != nil {
		var zero T
		return zero, err
	}
	return deref(reply.GetJSON200()), nil
}
