package providers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

const profileAdvisories = "/security-advisories?state=published&sort=published&direction=desc&per_page=5"

// profileRoutes is GitHub as an anonymous reader sees one account that stars
// two repositories, with allowance left for remaining calls.
func profileRoutes(remaining int) map[string]string {
	api := func(path string) string { return githubFakeAPI + " " + path }
	return map[string]string{
		api("/rate_limit"):                              fmt.Sprintf(`{"resources":{"core":{"limit":60,"remaining":%d,"reset":1900003600},"search":{"remaining":10,"reset":1}}}`, remaining),
		api("/users/example"):                           `{"login":"example","name":"Example Person","bio":null,"html_url":"https://github.com/example","avatar_url":"https://avatars.githubusercontent.com/u/1?v=4","followers":3,"following":4,"public_repos":5,"public_gists":1,"created_at":"2015-01-02T03:04:05Z","company":null,"location":"Somewhere","blog":"https://example.com","twitter_username":null,"hireable":null}`,
		api("/users/example/starred?per_page=100"):      `[{"starred_at":"2026-09-01T00:00:00Z","repo":{"full_name":"example/tool","html_url":"https://github.com/example/tool","description":"A tool","language":"Go","stargazers_count":9}},{"starred_at":"2026-08-01T00:00:00Z","repo":{"full_name":"example/quiet","html_url":"https://github.com/example/quiet","description":null,"language":null,"stargazers_count":1}}]`,
		api("/repos/example/tool/releases/latest"):      `{"tag_name":"v1.2.0","html_url":"https://github.com/example/tool/releases/tag/v1.2.0","published_at":"2026-09-10T00:00:00Z"}`,
		api("/repos/example/tool" + profileAdvisories):  `[{"ghsa_id":"GHSA-xxxx-yyyy-zzzz","cve_id":null,"html_url":"https://github.com/example/tool/security/advisories/GHSA-xxxx-yyyy-zzzz","summary":"A flaw","severity":"high","published_at":"2026-09-11T00:00:00Z"}]`,
		api("/repos/example/quiet" + profileAdvisories): `[]`,
	}
}

func profileHarness(t *testing.T, remaining int, plan runtime.GitHubProfilePlan) (*githubHarness, *Controller) {
	t.Helper()
	h := newGitHubHarness(t, profileRoutes(remaining), nil)
	// A repository with no release answers 404, which is an answer.
	h.refuse(githubFakeAPI, "/repos/example/quiet/releases/latest", 404)
	h.r.Picture = func(_ context.Context, address string, limit int) (string, []byte, error) {
		if address != "https://avatars.githubusercontent.com/u/1?s=96&v=4" || limit <= 0 {
			t.Errorf("picture asked at %s with limit %d", address, limit)
		}
		return "image/png", []byte("picture"), nil
	}
	return h, NewController(h.r, runtime.ControllerRegistry{GithubProfiles: plan})
}

func readProfiles(t *testing.T, c *Controller) runtime.KindReport {
	t.Helper()
	return c.readKind(t.Context(), c.readers[string(runtime.ResourceKindGitHubProfile)])
}

func TestGitHubProfileIsReadAnonymouslyWhenDue(t *testing.T) {
	h, c := profileHarness(t, 60, runtime.GitHubProfilePlan{Accounts: []string{"example"}, Due: true})
	report := readProfiles(t, c)
	if !report.OK || report.Carried || len(report.Records) != 1 || len(report.RefusedParts) != 0 {
		t.Fatalf("report %+v", report)
	}
	encoded, _ := json.Marshal(report.Records[0])
	want := `{"login":"example","name":"Example Person","bio":"","url":"https://github.com/example","followers":3,"following":4,"public_repos":5,` +
		`"created_at":"2015-01-02T03:04:05Z","company":"","location":"Somewhere","website":"https://example.com","social":"","public_gists":1,"hireable":false,` +
		`"avatar":"data:image/png;base64,cGljdHVyZQ==","starred":2,"watched":[` +
		`{"name":"example/tool","url":"https://github.com/example/tool","description":"A tool","language":"Go","stars":9,"starred_at":"2026-09-01T00:00:00Z",` +
		`"release":{"tag":"v1.2.0","url":"https://github.com/example/tool/releases/tag/v1.2.0","published_at":"2026-09-10T00:00:00Z"},` +
		`"advisories":[{"id":"GHSA-xxxx-yyyy-zzzz","severity":"high","summary":"A flaw","url":"https://github.com/example/tool/security/advisories/GHSA-xxxx-yyyy-zzzz","published_at":"2026-09-11T00:00:00Z"}]},` +
		`{"name":"example/quiet","url":"https://github.com/example/quiet","description":"","language":"","stars":1,"starred_at":"2026-08-01T00:00:00Z","release":null,"advisories":[]}]}`
	if string(encoded) != want {
		t.Errorf("record\n got %s\nwant %s", encoded, want)
	}
	for _, call := range h.recorded() {
		if call.auth != "" {
			t.Errorf("%s %s carried a credential", call.method, call.path)
		}
	}
	// The allowance is asked for, then at most the stated cost is spent.
	if spent := len(h.recorded()) - 1; spent > githubProfileCost()-1 {
		t.Errorf("%d calls for one account, over the stated cost", spent)
	}
}

func TestGitHubProfileIsCarriedUntilDue(t *testing.T) {
	for name, plan := range map[string]runtime.GitHubProfilePlan{
		"not due":     {Accounts: []string{"example"}},
		"no accounts": {Due: true},
	} {
		t.Run(name, func(t *testing.T) {
			h, c := profileHarness(t, 60, plan)
			report := readProfiles(t, c)
			if !report.OK || !report.Carried || len(report.Records) != 0 || len(report.RefusedParts) != 0 {
				t.Fatalf("report %+v", report)
			}
			if calls := h.recorded(); len(calls) != 0 {
				t.Errorf("GitHub was asked: %+v", calls)
			}
		})
	}
}

func TestGitHubProfileRefusesAReadTheAllowanceCannotCover(t *testing.T) {
	h, c := profileHarness(t, githubProfileCost()-1, runtime.GitHubProfilePlan{Accounts: []string{"example"}, Due: true})
	report := readProfiles(t, c)
	if !report.OK || !report.Carried || len(report.Records) != 0 {
		t.Fatalf("a read over the allowance keeps the last reading: %+v", report)
	}
	if len(report.RefusedParts) != 1 || report.RefusedParts[0].Part != runtime.PartWhole ||
		!strings.Contains(report.RefusedParts[0].Reason, "until 18:46 UTC") {
		t.Fatalf("refused %+v", report.RefusedParts)
	}
	if calls := h.recorded(); len(calls) != 1 || calls[0].path != "/rate_limit" {
		t.Errorf("only the allowance is asked for: %+v", calls)
	}
}

func TestGitHubProfileFailsWholeAndKeepsThePictureApart(t *testing.T) {
	h, c := profileHarness(t, 60, runtime.GitHubProfilePlan{Accounts: []string{"example"}, Due: true})
	h.r.Picture = func(context.Context, string, int) (string, []byte, error) {
		return "", nil, &ProviderError{Message: "the picture could not be fetched", Failure: runtime.FailureClassNetwork}
	}
	report := readProfiles(t, c)
	if !report.OK || len(report.Records) != 1 || report.Records[0].(GitHubProfileRecord).Avatar != "" {
		t.Fatalf("report %+v", report)
	}
	if len(report.RefusedParts) != 1 || report.RefusedParts[0].Part != runtime.PartAvatar || report.RefusedParts[0].Scope != "example" {
		t.Fatalf("refused %+v", report.RefusedParts)
	}

	h, c = profileHarness(t, 60, runtime.GitHubProfilePlan{Accounts: []string{"example"}, Due: true})
	h.refuse(githubFakeAPI, "/repos/example/tool/releases/latest", 502)
	if report := readProfiles(t, c); report.OK || report.Carried {
		t.Fatalf("a repository that cannot be read fails the reading, so the last one stands: %+v", report)
	}

	_, c = profileHarness(t, 60, runtime.GitHubProfilePlan{Accounts: []string{"not a login"}, Due: true})
	if report := readProfiles(t, c); report.OK {
		t.Fatalf("a name that is not a login is never sent to GitHub: %+v", report)
	}
}

func TestGitHubAvatarIsFetchedOnlyFromGitHubsPictureHost(t *testing.T) {
	r := New(runtime.Environment{}, &fakeHTTP{})
	r.Picture = func(context.Context, string, int) (string, []byte, error) {
		t.Fatal("fetched")
		return "", nil, nil
	}
	for _, address := range []string{"http://avatars.githubusercontent.com/u/1", "https://example.com/u/1", "https://avatars.githubusercontent.com.example.com/u/1", "://"} {
		if _, err := r.githubAvatar(t.Context(), address); err == nil {
			t.Errorf("%s was accepted", address)
		}
	}
	if found, err := r.githubAvatar(t.Context(), ""); err != nil || found != "" {
		t.Errorf("an account with no picture: %q %v", found, err)
	}
}

func TestAProfileIsSweptOnlyWhereHQNamesAnAccount(t *testing.T) {
	r := New(runtime.Environment{}, &fakeHTTP{})
	kind := string(runtime.ResourceKindGitHubProfile)
	if NewController(r, runtime.ControllerRegistry{}).hasSource(kind, nil) {
		t.Error("no account, and the kind claims a source")
	}
	if !NewController(r, runtime.ControllerRegistry{GithubProfiles: runtime.GitHubProfilePlan{Accounts: []string{"example"}}}).hasSource(kind, nil) {
		t.Error("an account HQ names is the source")
	}
}

func TestFetchPictureKeepsOnlyASmallImageFromTheAddressAsked(t *testing.T) {
	png := "\x89PNG\r\n\x1a\n" + strings.Repeat("x", 40)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		switch req.URL.Path {
		case "/picture":
			// What the response calls itself is not what is believed.
			w.Header().Set("Content-Type", "text/plain")
			io.WriteString(w, png)
		case "/page":
			w.Header().Set("Content-Type", "image/png")
			io.WriteString(w, "<html><body>not a picture</body></html>")
		case "/elsewhere":
			http.Redirect(w, req, "/picture", http.StatusFound)
		}
	}))
	t.Cleanup(server.Close)
	kind, data, err := fetchPicture(t.Context(), server.URL+"/picture", len(png))
	if err != nil || kind != "image/png" || string(data) != png {
		t.Fatalf("%q %d bytes %v", kind, len(data), err)
	}
	for name, attempt := range map[string]struct {
		path  string
		limit int
	}{"too large": {"/picture", len(png) - 1}, "not an image": {"/page", 1000}, "a redirect": {"/elsewhere", 1000}} {
		if _, _, err := fetchPicture(t.Context(), server.URL+attempt.path, attempt.limit); err == nil {
			t.Errorf("%s was kept", name)
		}
	}
}
