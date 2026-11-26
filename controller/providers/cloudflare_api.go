package providers

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Two credentials, each scoped to one surface. cloudflare_dns reads zones and
// reads and writes their DNS records, nothing else; cloudflare_api carries the
// account surface (analytics, zone settings, registration) and no DNS record.

// List paging. The caps bound a loop a misbehaving endpoint could keep going.
const (
	cloudflarePerPage        = 100 // default and maximum page size
	cloudflareAccountPerPage = 50  // account lists
	cloudflarePagesPerPage   = 10  // Pages projects, which answer at most 10
	cloudflareMaxPages       = 50
	cloudflareMaxCursorPages = 200
)

// Cloudflare's wording for a refusal of the credential itself rather than of
// one request. "Authentication error" alone is a missing permission.
var cloudflareCredentialRefusals = []string{"from location", "too many authentication failures", "invalid api token", "invalid access token", "expired"}

const cloudflarePermissionRefusal = "authentication error"

// cfMessage is one entry of an envelope's errors.
type cfMessage struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// cfEnvelope is the shape every Cloudflare REST answer shares. The spec repeats
// it per product family (iam_, zones_, d1_, ...), and success is checked before
// the operation's result type is known.
type cfEnvelope struct {
	Success    bool            `json:"success"`
	Errors     []cfMessage     `json:"errors"`
	Result     json.RawMessage `json:"result"`
	ResultInfo *struct {
		TotalPages int    `json:"total_pages"`
		Cursor     string `json:"cursor"`
	} `json:"result_info"`
}

// cloudflareDecode decodes one answer into T; an answer of another shape is an
// error that names what was being read.
func cloudflareDecode[T any](raw json.RawMessage, what string) (T, error) {
	return decodeAs[T](raw, "cloudflare returned an invalid "+what)
}

// cloudflareItems decodes each list entry into T.
func cloudflareItems[T any](items []json.RawMessage, what string) ([]T, error) {
	return recordsOf(items, func(item json.RawMessage) (T, error) { return cloudflareDecode[T](item, what) })
}

// present is whether a raw result carries a value: absent and null do not.
func present(raw json.RawMessage) bool {
	return len(raw) > 0 && string(raw) != "null"
}

// cloudflareDoer is a credential's ref, its API address and a doer sending
// with its token, with no rule of Cloudflare's around the exchange.
func (r *Registry) cloudflareDoer(provider runtime.ConnectionProvider, ref string) (string, string, vendorDoer) {
	connection, err := r.Supplied.For(provider, ref)
	token, missing := runtime.Need(connection.APIToken)
	return connection.Ref, strings.TrimRight(token.URL, "/") + "/", vendorDoer{r: r, err: cmp.Or(err, missing), headers: map[string]string{"Authorization": "Bearer " + token.APIToken.Reveal(), "Accept": "application/json"}}
}

// cloudflare is one credential's client. Its exchange refuses without a call
// once the sweep saw the credential refused, classifies a refusal, and reads a
// 200 with success false as one too: a token missing one permission answers
// with no result, and an account that refused to answer must not read as
// empty. A sweep's client reads each GET once.
func (r *Registry) cloudflare(provider runtime.ConnectionProvider, ref string, sweep bool) *cfapi.ClientWithResponses {
	held, base, doer := r.cloudflareDoer(provider, ref)
	if sweep {
		doer.cache = "cloudflare:" + string(provider) + ":" + held
	}
	doer.exchange = func(ctx context.Context, url, method string, headers map[string]string, payload any) (runtime.Answer, error) {
		if err := r.cloudflareBreaker(held); err != nil {
			return runtime.Answer{}, err
		}
		answer, err := r.HTTP.Exchange(ctx, url, method, headers, payload)
		var answered *ProviderError
		switch {
		case errors.As(err, &answered) && answered.HTTPStatus != 0:
			return answer, r.cloudflareRefused(held, "cloudflare refused the request", cloudflareErrors(answered.Body), answered.HTTPStatus, func() bool {
				return r.cloudflareVerified(ctx, provider, ref)
			})
		case err != nil:
			return answer, fmt.Errorf("cloudflare: %w", err)
		case !present(answer.Body):
			return answer, &ProviderError{Message: "cloudflare answered with no body"}
		}
		envelope, err := cloudflareDecode[cfEnvelope](answer.Body, "answer")
		if err != nil {
			return answer, err
		}
		if !envelope.Success {
			return answer, r.cloudflareRefused(held, "cloudflare refused the request", cloudflareErrors(answer.Body), 0, nil)
		}
		return answer, nil
	}
	return &cfapi.ClientWithResponses{ClientInterface: &cfapi.API{Server: base, Client: doer}}
}

// cloudflareRead is one call's envelope, which the client's exchange checked.
func cloudflareRead(reply vendorReply, err error) (cfEnvelope, error) {
	body, err := replyBody(reply, err)
	if err != nil {
		return cfEnvelope{}, err
	}
	return cloudflareDecode[cfEnvelope](body, "answer")
}

// cloudflareResult is one call's envelope result.
func cloudflareResult(reply vendorReply, err error) (json.RawMessage, error) {
	envelope, err := cloudflareRead(reply, err)
	return envelope.Result, err
}

// cloudflareBreaker refuses without a call when this sweep already saw the
// credential refused: every further call is refused too, and repeated failures
// lock the token out.
func (r *Registry) cloudflareBreaker(held string) error {
	r.snapshotMu.Lock()
	reason, refused := r.refusedCredentials[held]
	r.snapshotMu.Unlock()
	if !refused {
		return nil
	}
	return &ProviderError{Message: "cloudflare refused the credential earlier in this read: " + reason, Failure: runtime.FailureClassCredential, Reason: reason}
}

// cloudflareRefused is the error for a refusal, recording a refused credential
// so the rest of the sweep does not call with it again.
func (r *Registry) cloudflareRefused(held, message, detail string, status int, verified func() bool) error {
	failure := cloudflareRefusal(detail, status, verified)
	refused := &ProviderError{Message: message + ": " + detail, Failure: failure, HTTPStatus: status}
	if failure == runtime.FailureClassCredential {
		refused.Reason = detail
		r.snapshotMu.Lock()
		if r.refusedCredentials != nil {
			r.refusedCredentials[held] = detail
		}
		r.snapshotMu.Unlock()
	}
	return refused
}

// cloudflareRefusal classifies a refusal from Cloudflare's words and status.
// Under 401 the words alone cannot tell a missing permission from a dead
// credential, so whether the credential still verifies decides.
func cloudflareRefusal(detail string, status int, verified func() bool) runtime.FailureClass {
	lowered := strings.ToLower(detail)
	for _, phrase := range cloudflareCredentialRefusals {
		if strings.Contains(lowered, phrase) {
			return runtime.FailureClassCredential
		}
	}
	permissionWords := strings.Contains(lowered, cloudflarePermissionRefusal)
	switch {
	case status == 401 && permissionWords && verified != nil && verified():
		return runtime.FailureClassPermission
	case status == 401:
		return runtime.FailureClassCredential
	case permissionWords, status == 403:
		return runtime.FailureClassPermission
	}
	return runtime.FailureClassUnclassified
}

// cloudflareVerified is whether /user/tokens/verify answers active for one
// credential, asked once per sweep with no rule of Cloudflare's around it,
// since those rules ask it.
func (r *Registry) cloudflareVerified(ctx context.Context, provider runtime.ConnectionProvider, ref string) bool {
	held, base, doer := r.cloudflareDoer(provider, ref)
	verified, _ := cached(ctx, r, "cloudflare-verification:"+held, func() (cfapi.IamTokenVerifyResponseSingleSegment, error) {
		// Any failure is kept as "does not verify", so the sweep asks once.
		var verified cfapi.IamTokenVerifyResponseSingleSegment
		if body, err := replyBody((&cfapi.ClientWithResponses{ClientInterface: &cfapi.API{Server: base, Client: doer}}).UserAPITokensVerifyTokenWithResponse(ctx)); err == nil && present(body) {
			verified, _ = cloudflareDecode[cfapi.IamTokenVerifyResponseSingleSegment](body, "token verification")
		}
		return verified, nil
	})
	return bool(verified.Success) && verified.Result.Status == "active"
}

// cloudflareErrors joins the messages in an error envelope.
func cloudflareErrors(raw []byte) string {
	if !present(raw) {
		return "no reason given"
	}
	var parsed struct {
		Errors []cfMessage `json:"errors"`
	}
	if json.Unmarshal(raw, &parsed) != nil {
		return "an unreadable error"
	}
	messages := []string{}
	for _, item := range parsed.Errors {
		if message := strings.TrimSpace(item.Message); message != "" {
			messages = append(messages, message)
		}
	}
	if len(messages) == 0 {
		return "no reason given"
	}
	return strings.Join(messages, "; ")
}

// Account readings go through cloudflare_api. Every list is read once per sweep;
// per-item requests only where no list carries the field.

func (r *Registry) cloudflareAPIRefs() []string {
	return r.refs(runtime.ConnectionProviderCloudflareAPI)
}

// cloudflarePages is the one pagination loop for page-numbered lists, each
// entry decoded into T. A tail silently missing would read as absent, and
// absent is what the reconciler acts on, so every list is read to its last
// page: total_pages decides when the answer carries it (an endpoint may cap
// per_page below what was asked, so a short page is not proof of the last
// one), else a short page ends the list. A null or missing result is an empty
// page.
func cloudflarePages[T any](what string, perPage int, read func(page int) (vendorReply, error)) ([]T, error) {
	collected := []json.RawMessage{}
	for page := 1; page <= cloudflareMaxPages; page++ {
		envelope, err := cloudflareRead(read(page))
		if err != nil {
			return nil, err
		}
		batch, err := cloudflareBatch(envelope)
		if err != nil {
			return nil, err
		}
		collected = append(collected, batch...)
		totalPages := 0
		if envelope.ResultInfo != nil {
			totalPages = envelope.ResultInfo.TotalPages
		}
		if (totalPages != 0 && page >= totalPages) || (totalPages == 0 && len(batch) < perPage) {
			return cloudflareItems[T](collected, what)
		}
	}
	return nil, &ProviderError{Message: fmt.Sprintf("cloudflare list of %ss ran past %d pages", what, cloudflareMaxPages)}
}

// cloudflareCursorPages reads a cursor-paginated list; an empty cursor ends it.
func cloudflareCursorPages[T any](what string, read func(cursor *string) (vendorReply, error)) ([]T, error) {
	collected := []json.RawMessage{}
	var cursor *string
	for range cloudflareMaxCursorPages {
		envelope, err := cloudflareRead(read(cursor))
		if err != nil {
			return nil, err
		}
		batch, err := cloudflareBatch(envelope)
		if err != nil {
			return nil, err
		}
		collected = append(collected, batch...)
		if envelope.ResultInfo == nil || envelope.ResultInfo.Cursor == "" {
			return cloudflareItems[T](collected, what)
		}
		cursor = &envelope.ResultInfo.Cursor
	}
	return nil, &ProviderError{Message: fmt.Sprintf("cloudflare list of %ss ran past %d pages", what, cloudflareMaxCursorPages)}
}

// cloudflareBatch is a list page's result: missing or null is empty, anything but a list is invalid.
func cloudflareBatch(envelope cfEnvelope) ([]json.RawMessage, error) {
	if !present(envelope.Result) {
		return nil, nil
	}
	return cloudflareDecode[[]json.RawMessage](envelope.Result, "list")
}

// cloudflareAPIZones is every zone the account credential ref can see.
func (r *Registry) cloudflareAPIZones(ctx context.Context, ref string) ([]cfapi.ZonesZone, error) {
	return r.cloudflareZoneList(ctx, runtime.ConnectionProviderCloudflareAPI, ref, cloudflareAccountPerPage)
}

// cloudflareZoneList is every zone one Cloudflare credential can see, read
// once per snapshot for that credential.
func (r *Registry) cloudflareZoneList(ctx context.Context, provider runtime.ConnectionProvider, ref string, perPage int) ([]cfapi.ZonesZone, error) {
	client := r.cloudflare(provider, ref, true)
	return cloudflarePages[cfapi.ZonesZone]("zone", perPage, func(page int) (vendorReply, error) {
		return client.ZonesGetWithResponse(ctx, &cfapi.ZonesGetParams{Page: new(float32(page)), PerPage: new(float32(perPage))})
	})
}
