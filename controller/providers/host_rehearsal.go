package providers

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"io"
	"io/fs"
	"maps"
	"os"
	"slices"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What every page cost in the last release's rehearsal on a copy of
// production's data. Root keeps the walk's document for itself
// (host.Machine.RehearsalPagesPath) and the launcher gives the pass a copy
// once there is one; this reads that copy.

// hostRehearsalMaxBytes bounds the document: a page is one short line.
const hostRehearsalMaxBytes = 1 << 20

var (
	rehearsalRevision    = api.MustPattern("HostRehearsalRecord", "properties", "revision")
	rehearsalReason      = api.MustPattern("HostRehearsalRecord", "properties", "reason")
	rehearsalReasonLimit = api.MustLimit("HostRehearsalRecord", "properties", "reason", "maxLength")
	rehearsalPageLimit   = api.MustLimit("HostRehearsalRecord", "properties", "pages", "maxItems")
	rehearsedPage        = api.MustPattern("HostRehearsalRehearsedPage", "properties", "page")
	rehearsedPageLength  = api.MustLimit("HostRehearsalRehearsedPage", "properties", "page", "maxLength")
)

// hostRehearsal is the last rehearsal's one record, its pages by path. A
// page whose path the contract does not admit, or whose costs no walk could
// measure, is passed over, never a refusal of the rest.
func (r *Registry) hostRehearsal(context.Context) ([]any, error) {
	if r.Env.RehearsalPages == "" {
		return nil, &ProviderError{Message: "no release's rehearsal was given"}
	}
	file, err := os.Open(r.Env.RehearsalPages)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, &ProviderError{Message: "the launcher gave no rehearsal's pages"}
	}
	if err != nil {
		return nil, &ProviderError{Message: "read the rehearsal's pages", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, hostRehearsalMaxBytes+1))
	if err != nil {
		return nil, &ProviderError{Message: "read the rehearsal's pages", Err: err}
	}
	if len(data) > hostRehearsalMaxBytes {
		return nil, &ProviderError{Message: "the rehearsal's pages are larger than a walk writes"}
	}
	var walked host.WalkedPages
	if err := jsonv2.Unmarshal(data, &walked, jsonv2.RejectUnknownMembers(true)); err != nil {
		return nil, &ProviderError{Message: "the rehearsal's pages are not what the walk prints", Err: err}
	}
	if !rehearsalRevision.MatchString(walked.Revision) {
		return nil, &ProviderError{Message: "the rehearsal's pages name no release"}
	}
	record := runtime.HostRehearsalRecord{Revision: walked.Revision, Pages: []runtime.HostRehearsalRehearsedPage{}}
	if len(walked.Unread) <= rehearsalReasonLimit && rehearsalReason.MatchString(walked.Unread) {
		record.Reason = walked.Unread
	}
	for _, page := range slices.Sorted(maps.Keys(walked.Pages)) {
		cost := walked.Pages[page]
		if len(record.Pages) == rehearsalPageLimit || len(page) > rehearsedPageLength || !rehearsedPage.MatchString(page) ||
			cost.Status < 100 || cost.Status > 599 || min(cost.Queries, cost.RepeatedQueries, cost.Rows, cost.HTMLBytes, cost.RowHTMLBytes) < 0 {
			continue
		}
		record.Pages = append(record.Pages, runtime.HostRehearsalRehearsedPage{
			Page: page, Status: cost.Status, Queries: cost.Queries, RepeatedQueries: cost.RepeatedQueries,
			Rows: cost.Rows, HTMLBytes: cost.HTMLBytes, RowHTMLBytes: cost.RowHTMLBytes,
		})
	}
	return []any{record}, nil
}
