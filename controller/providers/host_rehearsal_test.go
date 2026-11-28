package providers

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

const rehearsedRevision = "0123456789abcdef0123456789abcdef01234567"

func rehearsalInventory(t *testing.T, body string) runtime.KindReport {
	t.Helper()
	path := filepath.Join(t.TempDir(), "rehearsal-pages.json")
	if err := os.WriteFile(path, []byte(body), 0o400); err != nil {
		t.Fatal(err)
	}
	kind := runtime.ResourceKindHostRehearsal
	declared := runtime.ControllerRegistry{Observations: map[string]string{string(kind): "host"}}
	controller := &Controller{Registry: New(runtime.Environment{RehearsalPages: path}, supplied(), &fakeHTTP{}), Declared: declared}
	inventory, err := controller.Inventory(t.Context(), []runtime.ResourceKind{kind}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	return inventory[string(kind)]
}

// The walk's document, as `manage.py page_costs` prints it, is one record of
// its release with each page by path; a page the contract does not admit is
// passed over.
func TestTheRehearsalsWalkIsOneRecordOfItsRelease(t *testing.T) {
	report := rehearsalInventory(t, `{"revision": "`+rehearsedRevision+`", "pages": {`+
		`"/infrastructure/": {"status": 200, "queries": 40, "repeated_queries": 12, "rows": 6, "html_bytes": 51200, "row_html_bytes": 3000},`+
		`"/": {"status": 302, "queries": 3, "repeated_queries": 0, "rows": 0, "html_bytes": 0, "row_html_bytes": 0},`+
		`"/a page": {"status": 200, "queries": 1, "repeated_queries": 0, "rows": 0, "html_bytes": 1, "row_html_bytes": 0}}}`)

	want := []any{runtime.HostRehearsalRecord{Revision: rehearsedRevision, Pages: []runtime.HostRehearsalRehearsedPage{
		{Page: "/", Status: 302, Queries: 3},
		{Page: "/infrastructure/", Status: 200, Queries: 40, RepeatedQueries: 12, Rows: 6, HTMLBytes: 51200, RowHTMLBytes: 3000},
	}}}
	if !report.OK || !reflect.DeepEqual(report.Records, want) {
		t.Errorf("%+v, want %+v", report, want)
	}
}

// A walk that read nothing says why; a document that names no release, or is
// not the walk's, is a failed read that leaves the last one standing.
func TestARehearsalNamesItsReleaseOrFails(t *testing.T) {
	unread := rehearsalInventory(t, `{"revision": "`+rehearsedRevision+`", "pages": {}, "unread": "no active operator to sign in as"}`)
	want := []any{runtime.HostRehearsalRecord{Revision: rehearsedRevision, Reason: "no active operator to sign in as", Pages: []runtime.HostRehearsalRehearsedPage{}}}
	if !unread.OK || !reflect.DeepEqual(unread.Records, want) {
		t.Errorf("%+v", unread)
	}
	for name, body := range map[string]string{
		"no revision":  `{"revision": "", "pages": {}}`,
		"not the walk": `{"revision": "` + rehearsedRevision + `", "pages": {}, "host": "example"}`,
	} {
		t.Run(name, func(t *testing.T) {
			if report := rehearsalInventory(t, body); report.OK || len(report.Records) != 0 {
				t.Errorf("%+v", report)
			}
		})
	}
}
