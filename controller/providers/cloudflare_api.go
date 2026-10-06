package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/connections"
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
	var target T
	if err := json.Unmarshal(raw, &target); err != nil {
		return target, &ProviderError{Message: "cloudflare returned an invalid " + what, Err: err}
	}
	return target, nil
}

// cloudflareItems decodes each list entry into T.
func cloudflareItems[T any](items []json.RawMessage, what string) ([]T, error) {
	found := make([]T, 0, len(items))
	for _, item := range items {
		decoded, err := cloudflareDecode[T](item, what)
		if err != nil {
			return nil, err
		}
		found = append(found, decoded)
	}
	return found, nil
}

// present is whether a raw result carries a value: absent and null do not.
func present(raw json.RawMessage) bool {
	return len(raw) > 0 && string(raw) != "null"
}

// cloudflareCredential is a Cloudflare connection: the ref it is held under,
// and its address and API token.
func (r *Registry) cloudflareCredential(provider runtime.ConnectionProvider, ref string) (string, connections.APIToken, error) {
	connection, err := r.Supplied.For(provider, ref)
	if err != nil {
		return "", connections.APIToken{}, err
	}
	token, err := runtime.Need(connection.APIToken)
	return connection.Ref, token, err
}

func (r *Registry) cloudflareURL(provider runtime.ConnectionProvider, ref string) (string, error) {
	_, token, err := r.cloudflareCredential(provider, ref)
	if err != nil {
		return "", err
	}
	return strings.TrimRight(token.URL, "/"), nil
}

func (r *Registry) cloudflareHeaders(provider runtime.ConnectionProvider, ref string) (map[string]string, error) {
	_, token, err := r.cloudflareCredential(provider, ref)
	if err != nil {
		return nil, err
	}
	return map[string]string{"Authorization": "Bearer " + token.APIToken, "Accept": "application/json"}, nil
}

// cloudflareEnvelope makes one call and returns the whole envelope. A 200 with
// success false is a refusal too: a token missing one permission answers with no
// result, and an account that refused to answer must not read as empty.
func (r *Registry) cloudflareEnvelope(ctx context.Context, provider runtime.ConnectionProvider, ref, path, method string, payload any) (cfEnvelope, error) {
	held, _, err := r.cloudflareCredential(provider, ref)
	if err != nil {
		return cfEnvelope{}, err
	}
	if err := r.cloudflareBreaker(held); err != nil {
		return cfEnvelope{}, err
	}
	base, err := r.cloudflareURL(provider, ref)
	if err != nil {
		return cfEnvelope{}, err
	}
	headers, err := r.cloudflareHeaders(provider, ref)
	if err != nil {
		return cfEnvelope{}, err
	}
	raw, err := r.HTTP.Request(ctx, base+path, method, headers, payload)
	if err != nil {
		var answered *ProviderError
		if errors.As(err, &answered) && answered.HTTPStatus != 0 {
			return cfEnvelope{}, r.cloudflareRefused(held, "cloudflare refused the request", cloudflareErrors(answered.Body), answered.HTTPStatus, func() bool {
				return r.cloudflareVerified(ctx, provider, ref)
			})
		}
		return cfEnvelope{}, fmt.Errorf("cloudflare: %w", err)
	}
	if !present(raw) {
		return cfEnvelope{}, &ProviderError{Message: "cloudflare answered with no body"}
	}
	envelope, err := cloudflareDecode[cfEnvelope](raw, "answer")
	if err != nil {
		return cfEnvelope{}, err
	}
	if !envelope.Success {
		return cfEnvelope{}, r.cloudflareRefused(held, "cloudflare refused the request", cloudflareErrors(raw), 0, nil)
	}
	return envelope, nil
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
	return &ProviderError{Message: "cloudflare refused the credential earlier this sweep: " + reason, Failure: runtime.FailureClassCredential, Reason: reason}
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

// cloudflareVerification is /user/tokens/verify's result for one credential,
// once per sweep; empty when it does not verify.
func (r *Registry) cloudflareVerification(ctx context.Context, provider runtime.ConnectionProvider, ref string) cfapi.IamTokenVerifyResponseSingleSegment {
	var empty cfapi.IamTokenVerifyResponseSingleSegment
	held, _, err := r.cloudflareCredential(provider, ref)
	if err != nil {
		return empty
	}
	raw, err := r.cached(ctx, "cloudflare-verification:"+held, func() (json.RawMessage, error) {
		base, err := r.cloudflareURL(provider, ref)
		if err != nil {
			return nil, err
		}
		headers, err := r.cloudflareHeaders(provider, ref)
		if err != nil {
			return nil, err
		}
		answer, err := r.HTTP.Request(ctx, base+"/user/tokens/verify", "GET", headers, nil)
		if err != nil {
			// Kept as "does not verify" so the sweep asks once.
			return json.RawMessage("null"), nil
		}
		return answer, nil
	})
	if err != nil || !present(raw) {
		return empty
	}
	verified, err := cloudflareDecode[cfapi.IamTokenVerifyResponseSingleSegment](raw, "token verification")
	if err != nil || !verified.Success {
		return empty
	}
	return verified
}

func (r *Registry) cloudflareVerified(ctx context.Context, provider runtime.ConnectionProvider, ref string) bool {
	return r.cloudflareVerification(ctx, provider, ref).Result.Status == "active"
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

// cloudflareRequest is the zone-scoped DNS surface, unwrapped to its result.
func (r *Registry) cloudflareRequest(ctx context.Context, path, method string, payload any) (json.RawMessage, error) {
	envelope, err := r.cloudflareEnvelope(ctx, runtime.ConnectionProviderCloudflareDNS, "", path, method, payload)
	return envelope.Result, err
}

// Account readings go through cloudflare_api. Every list is read once per sweep;
// per-item requests only where no list carries the field.

func (r *Registry) cloudflareAPIRefs() []string {
	return r.refs(runtime.ConnectionProviderCloudflareAPI)
}

func (r *Registry) cloudflareAPIRequest(ctx context.Context, path, ref string) (cfEnvelope, error) {
	return r.cloudflareEnvelope(ctx, runtime.ConnectionProviderCloudflareAPI, ref, path, "GET", nil)
}

func (r *Registry) cloudflareAPIResult(ctx context.Context, path, ref string) (json.RawMessage, error) {
	envelope, err := r.cloudflareAPIRequest(ctx, path, ref)
	return envelope.Result, err
}

// cloudflareList is the one pagination loop for page-numbered lists, on either
// credential. A tail silently missing would read as absent, and absent is what
// the reconciler acts on, so every list is read to its last page: total_pages
// decides when the answer carries it (an endpoint may cap per_page below what
// was asked, so a short page is not proof of the last one), else a short page
// ends the list. A null or missing result is an empty page.
func (r *Registry) cloudflareList(ctx context.Context, provider runtime.ConnectionProvider, path, ref string, perPage int) ([]json.RawMessage, error) {
	collected := []json.RawMessage{}
	for page := 1; page <= cloudflareMaxPages; page++ {
		query := url.Values{"per_page": {strconv.Itoa(perPage)}, "page": {strconv.Itoa(page)}}
		envelope, err := r.cloudflareEnvelope(ctx, provider, ref, path+querySeparator(path)+query.Encode(), "GET", nil)
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
			return collected, nil
		}
	}
	return nil, &ProviderError{Message: fmt.Sprintf("cloudflare list %s ran past %d pages", path, cloudflareMaxPages)}
}

// cloudflareAPICursorList reads a cursor-paginated account list; an empty cursor ends it.
func (r *Registry) cloudflareAPICursorList(ctx context.Context, path, ref string, perPage int) ([]json.RawMessage, error) {
	collected := []json.RawMessage{}
	cursor := ""
	for range cloudflareMaxCursorPages {
		query := url.Values{"per_page": {strconv.Itoa(perPage)}}
		if cursor != "" {
			query.Set("cursor", cursor)
		}
		envelope, err := r.cloudflareAPIRequest(ctx, path+querySeparator(path)+query.Encode(), ref)
		if err != nil {
			return nil, err
		}
		batch, err := cloudflareBatch(envelope)
		if err != nil {
			return nil, err
		}
		collected = append(collected, batch...)
		cursor = ""
		if envelope.ResultInfo != nil {
			cursor = envelope.ResultInfo.Cursor
		}
		if cursor == "" {
			return collected, nil
		}
	}
	return nil, &ProviderError{Message: fmt.Sprintf("cloudflare list %s ran past %d pages", path, cloudflareMaxCursorPages)}
}

// cloudflareBatch is a list page's result: missing or null is empty, anything but a list is invalid.
func cloudflareBatch(envelope cfEnvelope) ([]json.RawMessage, error) {
	if !present(envelope.Result) {
		return nil, nil
	}
	return cloudflareDecode[[]json.RawMessage](envelope.Result, "list")
}

// cloudflareCachedList reads one list once per sweep.
func (r *Registry) cloudflareCachedList(ctx context.Context, key string, read func() ([]json.RawMessage, error)) ([]json.RawMessage, error) {
	raw, err := r.cached(ctx, key, func() (json.RawMessage, error) {
		items, err := read()
		if err != nil {
			return nil, err
		}
		return json.Marshal(items)
	})
	if err != nil {
		return nil, err
	}
	return cloudflareDecode[[]json.RawMessage](raw, "list")
}

func (r *Registry) cloudflareAPIZones(ctx context.Context, ref string) ([]cfapi.ZonesZone, error) {
	items, err := r.cloudflareCachedList(ctx, "cloudflare-api-zones:"+ref, func() ([]json.RawMessage, error) {
		return r.cloudflareList(ctx, runtime.ConnectionProviderCloudflareAPI, "/zones", ref, cloudflareAccountPerPage)
	})
	if err != nil {
		return nil, err
	}
	return cloudflareItems[cfapi.ZonesZone](items, "zone")
}

func querySeparator(path string) string {
	if strings.Contains(path, "?") {
		return "&"
	}
	return "?"
}
