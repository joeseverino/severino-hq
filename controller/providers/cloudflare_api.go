package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/providers/cfapi"
)

// Two credentials, each scoped to one surface. cloudflare_dns reads zones and
// reads and writes their DNS records, nothing else; cloudflare_api carries the
// account surface (analytics, zone settings, registration) and no DNS record.

const cloudflareAPIURL = "https://api.cloudflare.com/client/v4"

// Cloudflare's wording for a refusal of the credential itself rather than of
// one request. "Authentication error" alone is a missing permission.
var cloudflareCredentialRefusals = []string{"from location", "too many authentication failures", "invalid api token", "invalid access token", "expired"}

const cloudflarePermissionRefusal = "authentication error"

// cfEnvelope is the shape every Cloudflare REST answer shares. The spec repeats
// it per product family (iam_, zones_, d1_, ...), and success is checked before
// the operation's result type is known.
type cfEnvelope struct {
	Success    json.RawMessage `json:"success"`
	Errors     json.RawMessage `json:"errors"`
	Result     json.RawMessage `json:"result"`
	ResultInfo *struct {
		TotalPages json.RawMessage `json:"total_pages"`
		Cursor     json.RawMessage `json:"cursor"`
	} `json:"result_info"`
}

func (r *Registry) cloudflareURL(provider, ref string) (string, error) {
	prefix, err := r.Env.Prefix(provider, ref)
	if err != nil {
		return "", err
	}
	base := strings.TrimSpace(r.Env[prefix+"_URL"])
	if base == "" {
		base = cloudflareAPIURL
	}
	return strings.TrimRight(base, "/"), nil
}

func (r *Registry) cloudflareHeaders(provider, ref string) (map[string]string, error) {
	prefix, err := r.Env.Prefix(provider, ref)
	if err != nil {
		return nil, err
	}
	token, err := r.Env.Required(prefix, "API_TOKEN")
	if err != nil {
		return nil, err
	}
	return map[string]string{"Authorization": "Bearer " + token, "Accept": "application/json"}, nil
}

// cloudflareEnvelope makes one call and returns the whole envelope. A 200 with
// success false is a refusal too: a token missing one permission answers with no
// result, and an account that refused to answer must not read as empty.
func (r *Registry) cloudflareEnvelope(ctx context.Context, provider, ref, path, method string, payload any) (cfEnvelope, error) {
	prefix, err := r.Env.Prefix(provider, ref)
	if err != nil {
		return cfEnvelope{}, err
	}
	if err := r.cloudflareBreaker(prefix); err != nil {
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
		var refused *ProviderError
		if errors.As(err, &refused) && refused.HTTPStatus != 0 {
			detail := cloudflareErrors(refused.Body)
			return cfEnvelope{}, r.cloudflareRefused(prefix, "Cloudflare refused the request: "+detail, detail, refused.HTTPStatus, func() bool {
				return r.cloudflareVerified(ctx, provider, ref)
			})
		}
		return cfEnvelope{}, cloudflareTransportError(err, "Cloudflare request failed", "Cloudflare returned invalid JSON.")
	}
	var envelope cfEnvelope
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &envelope); err != nil {
			return cfEnvelope{}, &ProviderError{Message: "Cloudflare returned invalid JSON."}
		}
	}
	if !pyTruthy(envelope.Success) {
		detail := cloudflareErrors(raw)
		return cfEnvelope{}, r.cloudflareRefused(prefix, "Cloudflare refused the request: "+detail, detail, 0, nil)
	}
	return envelope, nil
}

// cloudflareTransportError words a failure that never reached Cloudflare the
// way the Python controller does: urllib names the network failure URLError.
func cloudflareTransportError(err error, failed, invalid string) error {
	var provider *ProviderError
	if errors.As(err, &provider) {
		switch {
		case provider.Failure == "network":
			return &ProviderError{Message: failed + ": URLError.", Failure: "network"}
		case provider.Message == "Provider returned invalid JSON.":
			return &ProviderError{Message: invalid}
		}
	}
	return err
}

// cloudflareBreaker refuses without a call when this sweep already saw the
// credential refused: every further call is refused too, and repeated failures
// lock the token out.
func (r *Registry) cloudflareBreaker(prefix string) error {
	r.snapshotMu.Lock()
	reason, refused := r.refusedCredentials[prefix]
	r.snapshotMu.Unlock()
	if !refused {
		return nil
	}
	return &ProviderError{Message: "Cloudflare refused the request: " + reason + " Not retried for the rest of this sweep.", Refusal: "credential", Failure: "credential", Reason: reason}
}

func (r *Registry) cloudflareRefused(prefix, message, detail string, status int, verified func() bool) error {
	refusal := cloudflareRefusal(detail, status, verified)
	if refusal == "credential" {
		r.snapshotMu.Lock()
		if r.refusedCredentials != nil {
			r.refusedCredentials[prefix] = detail
		}
		r.snapshotMu.Unlock()
		return &ProviderError{Message: message, Refusal: refusal, Failure: refusal, Reason: detail}
	}
	return &ProviderError{Message: message, Refusal: refusal, Failure: refusal}
}

// cloudflareRefusal names the refusal Cloudflare's text and status describe.
// Under 401 the words alone cannot tell a missing permission from a dead
// credential, so whether the credential still verifies decides.
func cloudflareRefusal(detail string, status int, verified func() bool) string {
	lowered := strings.ToLower(detail)
	for _, phrase := range cloudflareCredentialRefusals {
		if strings.Contains(lowered, phrase) {
			return "credential"
		}
	}
	if strings.Contains(lowered, cloudflarePermissionRefusal) && status != 401 {
		return "permission"
	}
	if status == 401 {
		if strings.Contains(lowered, cloudflarePermissionRefusal) && verified != nil && verified() {
			return "permission"
		}
		return "credential"
	}
	if status == 403 {
		return "permission"
	}
	return ""
}

// cloudflareVerification is /user/tokens/verify's result for one credential,
// once per sweep; empty when it does not verify.
func (r *Registry) cloudflareVerification(ctx context.Context, provider, ref string) cfapi.IamTokenVerifyResponseSingleSegment {
	var empty cfapi.IamTokenVerifyResponseSingleSegment
	prefix, err := r.Env.Prefix(provider, ref)
	if err != nil {
		return empty
	}
	raw, _ := r.cached(ctx, "cloudflare-verification:"+prefix, func() (json.RawMessage, error) {
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
			return json.RawMessage("{}"), nil
		}
		var envelope cfEnvelope
		if json.Unmarshal(answer, &envelope) != nil || !pyTruthy(envelope.Success) || !isJSONObject(envelope.Result) {
			return json.RawMessage("{}"), nil
		}
		return answer, nil
	})
	var verified cfapi.IamTokenVerifyResponseSingleSegment
	if json.Unmarshal(raw, &verified) != nil {
		return empty
	}
	return verified
}

func (r *Registry) cloudflareVerified(ctx context.Context, provider, ref string) bool {
	return r.cloudflareVerification(ctx, provider, ref).Result.Status == "active"
}

// cloudflareErrors joins the messages in an error envelope.
func cloudflareErrors(raw []byte) string {
	if len(raw) == 0 {
		raw = []byte("{}")
	}
	var parsed struct {
		Errors []struct {
			Message json.RawMessage `json:"message"`
		} `json:"errors"`
	}
	if !json.Valid(raw) {
		return "an unreadable error"
	}
	_ = json.Unmarshal(raw, &parsed)
	messages := []string{}
	for _, item := range parsed.Errors {
		if message := strings.TrimSpace(pyGetText(item.Message, "")); message != "" {
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
	envelope, err := r.cloudflareEnvelope(ctx, "cloudflare_dns", "", path, method, payload)
	return envelope.Result, err
}

// cloudflarePaged reads every page of a DNS-surface list. Cloudflare returns 100
// records at most; a tail silently missing would read as absent, and absent is
// what the reconciler acts on.
func (r *Registry) cloudflarePaged(ctx context.Context, path string) ([]json.RawMessage, error) {
	collected := []json.RawMessage{}
	for page := 1; ; page++ {
		if page > 50 {
			return nil, &ProviderError{Message: "Cloudflare list did not terminate."}
		}
		result, err := r.cloudflareRequest(ctx, fmt.Sprintf("%s%sper_page=100&page=%d", path, querySeparator(path), page), "GET", nil)
		if err != nil {
			return nil, err
		}
		batch := []json.RawMessage{}
		if pyTruthy(result) {
			if err := json.Unmarshal(result, &batch); err != nil {
				return nil, &ProviderError{Message: "Cloudflare returned an invalid list."}
			}
		}
		collected = append(collected, batch...)
		if len(batch) < 100 {
			return collected, nil
		}
	}
}

// Account readings go through cloudflare_api. Every list is read once per sweep;
// per-item requests only where no list carries the field.

func (r *Registry) cloudflareAPIRefs() []string { return r.refs("cloudflare_api") }

func (r *Registry) cloudflareAPIRequest(ctx context.Context, path, ref string) (cfEnvelope, error) {
	return r.cloudflareEnvelope(ctx, "cloudflare_api", ref, path, "GET", nil)
}

func (r *Registry) cloudflareAPIResult(ctx context.Context, path, ref string) (json.RawMessage, error) {
	envelope, err := r.cloudflareAPIRequest(ctx, path, ref)
	return envelope.Result, err
}

// cloudflareAPIList reads every page of an account list; non-object entries are
// dropped. total_pages decides when present: an endpoint may cap per_page below
// what was asked, so a short page is not proof of the last one.
func (r *Registry) cloudflareAPIList(ctx context.Context, path, ref string, perPage int) ([]json.RawMessage, error) {
	collected := []json.RawMessage{}
	for page := 1; page <= 50; page++ {
		envelope, err := r.cloudflareAPIRequest(ctx, fmt.Sprintf("%s%sper_page=%d&page=%d", path, querySeparator(path), perPage, page), ref)
		if err != nil {
			return nil, err
		}
		batch, err := cloudflareBatch(envelope)
		if err != nil {
			return nil, err
		}
		collected = append(collected, objectsOnly(batch)...)
		totalPages := 0
		if envelope.ResultInfo != nil {
			totalPages = rawInt(envelope.ResultInfo.TotalPages)
		}
		if (totalPages != 0 && page >= totalPages) || (totalPages == 0 && len(batch) < perPage) {
			return collected, nil
		}
	}
	return nil, &ProviderError{Message: "Cloudflare account list did not terminate."}
}

// cloudflareAPICursorList reads a cursor-paginated account list; an empty cursor ends it.
func (r *Registry) cloudflareAPICursorList(ctx context.Context, path, ref string, perPage int) ([]json.RawMessage, error) {
	collected := []json.RawMessage{}
	cursor := ""
	for range 200 {
		query := fmt.Sprintf("per_page=%d", perPage)
		if cursor != "" {
			query += "&cursor=" + pyQuote(cursor)
		}
		envelope, err := r.cloudflareAPIRequest(ctx, path+querySeparator(path)+query, ref)
		if err != nil {
			return nil, err
		}
		batch, err := cloudflareBatch(envelope)
		if err != nil {
			return nil, err
		}
		collected = append(collected, objectsOnly(batch)...)
		cursor = ""
		if envelope.ResultInfo != nil {
			cursor = pyOrText(envelope.ResultInfo.Cursor)
		}
		if cursor == "" {
			return collected, nil
		}
	}
	return nil, &ProviderError{Message: "Cloudflare account list did not terminate."}
}

// cloudflareBatch is a list page's result: missing is empty, anything but a list is invalid.
func cloudflareBatch(envelope cfEnvelope) ([]json.RawMessage, error) {
	if envelope.Result == nil {
		return []json.RawMessage{}, nil
	}
	batch := []json.RawMessage{}
	if err := json.Unmarshal(envelope.Result, &batch); err != nil || string(envelope.Result) == "null" {
		return nil, &ProviderError{Message: "Cloudflare account list returned an invalid result."}
	}
	return batch, nil
}

func (r *Registry) cloudflareAPIZones(ctx context.Context, ref string) ([]cfapi.ZonesZone, error) {
	raw, err := r.cached(ctx, "cloudflare-api-zones:"+ref, func() (json.RawMessage, error) {
		items, err := r.cloudflareAPIList(ctx, "/zones", ref, 50)
		if err != nil {
			return nil, err
		}
		return json.Marshal(items)
	})
	if err != nil {
		return nil, err
	}
	return decodeAs[[]cfapi.ZonesZone](raw, "Cloudflare returned an invalid zone list.")
}

func querySeparator(path string) string {
	if strings.Contains(path, "?") {
		return "&"
	}
	return "?"
}

func objectsOnly(items []json.RawMessage) []json.RawMessage {
	found := []json.RawMessage{}
	for _, item := range items {
		if isJSONObject(item) {
			found = append(found, item)
		}
	}
	return found
}

func isJSONObject(raw json.RawMessage) bool {
	trimmed := strings.TrimSpace(string(raw))
	return strings.HasPrefix(trimmed, "{")
}

// pyQuote is urllib.parse.quote(value, safe=""): every byte but A-Z a-z 0-9 _.-~ escaped.
func pyQuote(value string) string {
	var out strings.Builder
	for _, b := range []byte(value) {
		if b >= 'A' && b <= 'Z' || b >= 'a' && b <= 'z' || b >= '0' && b <= '9' || strings.IndexByte("_.-~", b) >= 0 {
			out.WriteByte(b)
		} else {
			fmt.Fprintf(&out, "%%%02X", b)
		}
	}
	return out.String()
}

// Python truth and text of a raw JSON value, as the Python controller reads
// loosely typed answers. An absent value is a zero-length raw.

func pyTruthy(raw json.RawMessage) bool {
	if len(raw) == 0 {
		return false
	}
	value, err := parsePy(raw)
	return err == nil && value.truthy()
}

// pyOrText is str(value or "").
func pyOrText(raw json.RawMessage) string {
	if !pyTruthy(raw) {
		return ""
	}
	return rawText(raw)
}

// pyGetText is str(d.get(key, fallback)): absent is the fallback, null is "None".
func pyGetText(raw json.RawMessage, fallback string) string {
	if len(raw) == 0 {
		return fallback
	}
	return rawText(raw)
}

// rawText is str() of a present JSON value.
func rawText(raw json.RawMessage) string {
	value, err := parsePy(raw)
	if err != nil {
		return ""
	}
	return value.str()
}

// rawInt is int(value or 0) for a number or numeric string.
func rawInt(raw json.RawMessage) int {
	if !pyTruthy(raw) {
		return 0
	}
	text := rawText(raw)
	if n, err := strconv.Atoi(strings.TrimSpace(text)); err == nil {
		return n
	}
	if f, err := strconv.ParseFloat(strings.TrimSpace(text), 64); err == nil {
		return int(f)
	}
	return 0
}
