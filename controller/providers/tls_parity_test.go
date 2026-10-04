package providers

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"os"
	"time"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// runTLSParity answers one TLS fixture for the differential harness. Every
// network, TLS and command call is answered from the fixture.
func runTLSParity(data []byte) {
	var input struct {
		Surface       string                     `json:"surface"`
		Spec          Object                     `json:"spec"`
		Observed      Object                     `json:"observed"`
		Apply         bool                       `json:"apply"`
		Env           map[string]string          `json:"env"`
		Routes        map[string]json.RawMessage `json:"routes"`
		Answers       map[string]json.RawMessage `json:"answers"`
		Statuses      map[string]int             `json:"statuses"`
		WriteStatuses map[string]int             `json:"write_statuses"`
		TLS           []map[string]fakeServe     `json:"tls"`
		Certs         map[string]string          `json:"certs"`
		Commands      map[string]fakeOutcome     `json:"commands"`
		Lineage       map[string]string          `json:"lineage"`
		Verification  *runtime.Verification      `json:"verification"`
		Ref           string                     `json:"ref"`
		Data          string                     `json:"data"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&input); err != nil {
		fmt.Fprintln(os.Stderr, "invalid fixture")
		os.Exit(2)
	}
	h := &fakeHTTP{routes: map[string]any{"/api/tokens": Object{"token": "synthetic"}}, fail: map[string]error{}, answers: map[string]any{}, writeFail: map[string]error{}}
	for path, value := range input.Routes {
		h.routes[path] = value
	}
	for path, value := range input.Answers {
		h.answers[path] = value
	}
	for path, code := range input.Statuses {
		h.fail[path] = runtime.HTTPRefusal(code)
	}
	for path, code := range input.WriteStatuses {
		h.writeFail[path] = runtime.HTTPRefusal(code)
	}
	certs := map[string][]byte{}
	for name, text := range input.Certs {
		if block, _ := pem.Decode([]byte(text)); block != nil {
			certs[name] = block.Bytes
		}
	}
	r := New(runtime.Environment(input.Env), h)
	r.TLS = &fakeDialer{http: h, phases: input.TLS, certs: certs}
	fake := &fakeCommander{http: h, outcomes: input.Commands, lineage: input.Lineage}
	r.Commands = &Commands{Env: r.Env, Exec: fake.exec}
	r.Now = func() time.Time { return time.Date(2026, 1, 2, 12, 0, 0, 0, time.UTC) }
	clock := time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)
	r.Monotonic = func() time.Time { return clock }
	r.Sleep = func(_ context.Context, d time.Duration) error { clock = clock.Add(d); return nil }
	cleanup := r.BeginSnapshot()
	defer cleanup()
	ledger := &refusals{}
	ctx := runtime.WithVerification(context.WithValue(context.Background(), refusalKey{}, ledger), input.Verification)

	var value any
	var err error
	switch input.Surface {
	case "reconcile":
		value, err = r.runAction(runtime.ResourceKindTLSCertificate, "reconcile", ctx, input.Spec, input.Observed, input.Apply)
	case "renew":
		value, err = r.runAction(runtime.ResourceKindTLSCertificate, "renew", ctx, input.Spec, input.Observed, input.Apply)
	case "uploaded_reconcile":
		value, err = r.runAction(runtime.ResourceKindTLSUploadedCertificate, "reconcile", ctx, input.Spec, input.Observed, input.Apply)
	case "uploaded_delete":
		value, err = r.runAction(runtime.ResourceKindTLSUploadedCertificate, "delete", ctx, input.Spec, input.Observed, input.Apply)
	case "sign":
		var signature []byte
		signature, err = r.Sign(ctx, input.Ref, []byte(input.Data))
		value = Object{"signature": base64.StdEncoding.EncodeToString(signature)}
	case "signing_public_key":
		var key string
		key, err = r.SigningPublicKey(input.Ref)
		value = Object{"public_key": key}
	case "onepassword_probe":
		value, err = r.probes["onepassword"](ctx, input.Ref)
	default:
		fmt.Fprintln(os.Stderr, "unknown fixture surface")
		os.Exit(2)
	}
	requests := []Object{}
	for _, request := range h.requests {
		payload := request.payload
		if files, ok := payload.(runtime.Multipart); ok {
			parts := []Object{}
			for _, part := range files {
				parts = append(parts, Object{"field": part.Field, "filename": part.Filename, "content": string(part.Content)})
			}
			payload = Object{"multipart": parts}
		}
		requests = append(requests, Object{"path": request.path, "method": request.method, "payload": payload})
	}
	output := Object{"result": value, "error": "", "requests": requests, "refused_parts": []Object{}}
	if ledger.entries != nil {
		output["refused_parts"] = ledger.entries
	}
	if err != nil {
		output["result"] = nil
		output["error"] = err.Error()
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		os.Exit(2)
	}
	os.Exit(0)
}
