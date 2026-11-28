package providers

import (
	"context"
	"crypto"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/connections"
	deployed "github.com/joeseverino/severino-hq/controller/host"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubFake is api.github.com and ghcr.io behind one httptest server: GETs
// answer by host, path and query; installation tokens are minted per request
// and remembered with their grant; every call is recorded.
type githubFake struct {
	t      *testing.T
	mu     sync.Mutex
	routes map[string]string // "host /path?query" -> JSON body
	status map[string]int    // "host /path?query" -> non-2xx status
	grants map[string]githubapi.AppsCreateInstallationAccessTokenJSONBody
	calls  []githubFakeCall
	// headers are sent with one route's answer, by "host /path?query".
	headers map[string]map[string]string
	// nodes is what the GraphQL change query answers for a repository, by
	// owner/name; one not named answers githubFakeNode.
	nodes map[string]string
	// remaining is what each answer under an installation token says the
	// installation has left; counted is how many answers GitHub counts
	// against it: every one but /rate_limit, a GraphQL query and a 304.
	remaining int
	counted   int
	// hour, when set, is GitHub's own count of the installation's hour: each
	// counted answer adds one, and a test adds what others spend.
	hour *githubFakeHour
}

// githubFakeHour is an installation's hour as GitHub counts it.
type githubFakeHour struct{ limit, used int }

// githubFakeNode is a repository as the change query answers it, as GitHub's
// GraphQL schema shapes it.
const githubFakeNode = `{"pushedAt":"2030-01-02T00:00:00Z","updatedAt":"2030-01-02T00:00:00Z","isArchived":false,` +
	`"defaultBranchRef":{"name":"main","target":{"oid":"aaaa000000000000000000000000000000000000","checkSuites":{"nodes":[{"status":"COMPLETED","conclusion":"SUCCESS","updatedAt":"2030-01-02T00:00:00Z"}]}}},` +
	`"pullRequests":{"totalCount":0,"nodes":[]},"releases":{"nodes":[]},"deployments":{"nodes":[]}}`

type githubFakeCall struct {
	method, host, path, auth string
	body                     map[string]any
}

const githubFakeAPI, githubFakeRegistry = "api.github.com", "ghcr.io"

// The listing routes, each followed by its page number.
const (
	githubInstallationsPage = "/app/installations?per_page=100&page="
	githubRepositoriesPage  = "/installation/repositories?per_page=100&page="
)

func (f *githubFake) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	host := req.Header.Get("X-Fake-Host")
	key := req.URL.Path
	if req.URL.RawQuery != "" {
		key += "?" + req.URL.RawQuery
	}
	data, _ := io.ReadAll(req.Body)
	var body map[string]any
	_ = json.Unmarshal(data, &body)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, githubFakeCall{req.Method, host, key, req.Header.Get("Authorization"), body})
	w.Header().Set("Content-Type", "application/json")
	if _, installation := f.grants[strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")]; installation && host == githubFakeAPI {
		resource := "core"
		switch req.URL.Path {
		case "/graphql":
			resource = "graphql"
		case "/rate_limit":
		default:
			f.counted++
			if f.hour != nil {
				f.hour.used++
			}
		}
		w.Header().Set("X-RateLimit-Resource", resource)
		w.Header().Set("X-RateLimit-Remaining", strconv.Itoa(f.remaining))
		w.Header().Set("X-RateLimit-Reset", strconv.Itoa(githubFakeReset))
	}
	headers, _ := routed(f.headers, host+" "+key)
	for name, value := range headers {
		w.Header().Set(name, value)
	}
	if code, _ := routed(f.status, host+" "+key); code != 0 {
		body, _ := routed(f.routes, host+" "+key)
		w.WriteHeader(code)
		_, _ = io.WriteString(w, orDefault(body, `{"message":"refused"}`))
		return
	}
	switch {
	case req.Method == http.MethodPost && strings.HasSuffix(key, "/access_tokens"):
		var grant githubapi.AppsCreateInstallationAccessTokenJSONBody
		_ = json.Unmarshal(data, &grant)
		token := "minted-" + strconv.Itoa(len(f.grants)+1)
		f.grants[token] = grant
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"token":"`+token+`","expires_at":"2030-01-01T00:00:00Z"}`)
		return
	case req.Method == http.MethodPost && key == "/graphql":
		variables, _ := body["variables"].(map[string]any)
		if _, pins := variables["workflows"]; pins {
			// The workflow files query, answered by its repository's /pins
			// route; refused 404, the branch holds neither directory.
			route := fmt.Sprintf("%s /repos/%s/%s/pins", host, variables["owner"], variables["name"])
			switch code := f.status[route]; code {
			case 0:
				_, _ = io.WriteString(w, f.routes[route])
			case http.StatusNotFound:
				_, _ = io.WriteString(w, `{"data":{"repository":{"workflows":null,"actions":null}}}`)
			default:
				w.WriteHeader(code)
			}
			return
		}
		if _, states := variables["pulls"]; states {
			// The pull request states query, answered by its repository's
			// /pull_states route; none, GitHub says nothing more of any.
			route := fmt.Sprintf("%s /repos/%s/%s/pull_states", host, variables["owner"], variables["name"])
			if code := f.status[route]; code != 0 {
				w.WriteHeader(code)
				return
			}
			_, _ = io.WriteString(w, orDefault(f.routes[route], `{"data":{"repository":{"pullRequests":{"nodes":[]}}}}`))
			return
		}
		_, _ = io.WriteString(w, f.changeQuery(body))
		return
	case req.Method != http.MethodGet:
		_, _ = io.WriteString(w, orDefault(f.routes[host+" "+req.Method+" "+key], `{"id":1}`))
		return
	case key == "/rate_limit" && f.routes[host+" "+key] == "" && f.hour != nil:
		core := fmt.Sprintf(`{"limit":%d,"remaining":%d,"reset":%d,"used":%d}`, f.hour.limit, f.hour.limit-f.hour.used, githubFakeReset, f.hour.used)
		_, _ = io.WriteString(w, `{"resources":{"core":`+core+`},"rate":`+core+`}`)
		return
	case key == "/rate_limit" && f.routes[host+" "+key] == "":
		_, _ = fmt.Fprintf(w, `{"resources":{"core":{"limit":5000,"remaining":%d,"reset":%d,"used":%d}},"rate":{"limit":5000,"remaining":%d,"reset":%d,"used":%d}}`,
			f.remaining, githubFakeReset, 5000-f.remaining, f.remaining, githubFakeReset, 5000-f.remaining)
		return
	}
	answer, ok := routed(f.routes, host+" "+key)
	if !ok {
		f.t.Errorf("unexpected GET %s %s", host, key)
		w.WriteHeader(http.StatusNotFound)
		return
	}
	_, _ = io.WriteString(w, answer)
}

// routed is a route's entry, its query named in any order.
func routed[V any](routes map[string]V, key string) (V, bool) {
	path, query, _ := strings.Cut(key, "?")
	asked, _ := url.ParseQuery(query)
	for name, found := range routes {
		at, named, _ := strings.Cut(name, "?")
		if listed, _ := url.ParseQuery(named); at == path && listed.Encode() == asked.Encode() {
			return found, true
		}
	}
	var zero V
	return zero, false
}

// githubFakeReset is when the fake's hour starts over: 18:06 UTC on the
// harness's day, twenty minutes after its clock.
const githubFakeReset = 1_900_001_200

// changeQuery answers a change query: each aliased repository's node, as the
// query's variables name them.
func (f *githubFake) changeQuery(body map[string]any) string {
	variables, _ := body["variables"].(map[string]any)
	owner, _ := variables["owner"].(string)
	data := map[string]json.RawMessage{}
	for name, value := range variables {
		index, ok := strings.CutPrefix(name, "n")
		if repo, named := value.(string); ok && named {
			node, held := f.nodes[owner+"/"+repo]
			if !held {
				node = githubFakeNode
			}
			data["r"+index] = json.RawMessage(node)
		}
	}
	encoded, _ := json.Marshal(map[string]any{"data": data})
	return string(encoded)
}

// grantOf is the grant a call's token was minted with; ok false for a JWT.
func (f *githubFake) grantOf(call githubFakeCall) (githubapi.AppsCreateInstallationAccessTokenJSONBody, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	grant, ok := f.grants[strings.TrimPrefix(call.auth, "Bearer ")]
	return grant, ok
}

func (f *githubFake) minted() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.grants)
}

// refuse answers one GitHub API route with a status from now on.
func (f *githubFake) refuse(host, key string, code int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.status[host+" "+key] = code
}

func (f *githubFake) answer(host, key, body string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.routes[host+" "+key] = body
}

func (f *githubFake) recorded() []githubFakeCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]githubFakeCall{}, f.calls...)
}

// writes is every call that is not a GET or a token mint.
func (f *githubFake) writes() []githubFakeCall {
	found := []githubFakeCall{}
	for _, call := range f.recorded() {
		if call.method != "GET" && !strings.HasSuffix(call.path, "/access_tokens") {
			found = append(found, call)
		}
	}
	return found
}

// toFake sends every request to the fake, naming the host it was for.
type toFake struct{ server *httptest.Server }

func (t toFake) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("X-Fake-Host", req.URL.Host)
	req.URL.Scheme, req.URL.Host = "http", strings.TrimPrefix(t.server.URL, "http://")
	return http.DefaultTransport.RoundTrip(req)
}

type githubHarness struct {
	*githubFake
	r   *Registry
	key *rsa.PrivateKey // the app's signing key, as the renderer writes it
}

func newGitHubHarness(t *testing.T, routes map[string]string, extra runtime.Environment) *githubHarness {
	t.Helper()
	fake := &githubFake{t: t, routes: map[string]string{
		githubFakeAPI + " " + githubInstallationsPage + "1": `[{"id":7,"account":{"login":"example"}}]`,
		githubFakeAPI + " " + githubRepositoriesPage + "1":  `{"total_count":1,"repositories":[{"full_name":"example/app"}]}`,
		githubFakeAPI + " /repos/example/app/installation":  `{"id":7}`,
		githubFakeAPI + " /repos/example/host/installation": `{"id":7}`,
		githubFakeAPI + " /repos/example/ext/installation":  `{"id":7}`,
	}, status: map[string]int{}, grants: map[string]githubapi.AppsCreateInstallationAccessTokenJSONBody{},
		headers: map[string]map[string]string{}, nodes: map[string]string{}, remaining: 4000}
	maps.Copy(fake.routes, routes)
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	env := extra
	env.SSHDir = t.TempDir()
	held := supplied(githubAppConnection("hq-app", "12345"))
	h := &githubHarness{githubFake: fake}
	h.r = New(env, held, &runtime.HTTPClient{Transport: toFake{server}})
	h.r.Now = func() time.Time { return time.Unix(1_900_000_000, 0) }
	h.r.Commands = &Commands{Env: env, Supplied: held, Exec: func(context.Context, []string, []byte, []string) ([]byte, []byte, int, error) {
		return nil, nil, 0, errors.New("a GitHub App connection starts no process")
	}, Remote: func(context.Context, string, string, []byte) ([]byte, []byte, int, error) {
		return nil, nil, 0, errors.New("a GitHub App connection makes no SSH call")
	}}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, _ := x509.MarshalPKCS8PrivateKey(key)
	if err := os.WriteFile(filepath.Join(env.SSHDir, "hq-app.key"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o400); err != nil {
		t.Fatal(err)
	}
	h.key = key
	return h
}

func TestGitHubAppJWTIsSignedWithTheConnectionsKey(t *testing.T) {
	h := newGitHubHarness(t, nil, runtime.Environment{})
	c, err := h.r.githubConnection("")
	if err != nil {
		t.Fatal(err)
	}
	jwt, err := h.r.githubJWT(t.Context(), c)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 {
		t.Fatalf("jwt %q", jwt)
	}
	var header, claims map[string]any
	headerJSON, _ := base64.RawURLEncoding.DecodeString(parts[0])
	claimsJSON, _ := base64.RawURLEncoding.DecodeString(parts[1])
	_ = json.Unmarshal(headerJSON, &header)
	_ = json.Unmarshal(claimsJSON, &claims)
	if header["alg"] != "RS256" || claims["iss"] != "12345" || claims["iat"] != float64(1_900_000_000-60) || claims["exp"] != float64(1_900_000_000+540) {
		t.Fatalf("header %v claims %v", header, claims)
	}
	// The signature verifies against the key the connection names: what
	// GitHub checks, rather than which program was asked to sign.
	signature, _ := base64.RawURLEncoding.DecodeString(parts[2])
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(&h.key.PublicKey, crypto.SHA256, digest[:], signature); err != nil {
		t.Fatalf("the JWT does not verify: %v", err)
	}
}

func TestGitHubAppJWTRefusesAKeyItCannotRead(t *testing.T) {
	h := newGitHubHarness(t, nil, runtime.Environment{})
	c, _ := h.r.githubConnection("")
	path := filepath.Join(h.r.Env.SSHDir, "hq-app.key")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("not a key"), 0o400); err != nil {
		t.Fatal(err)
	}
	if _, err := h.r.githubJWT(t.Context(), c); err == nil || strings.Contains(err.Error(), "not a key") {
		t.Fatalf("an unreadable key is refused without its contents: %v", err)
	}
}

func TestGitHubConnectionRefusals(t *testing.T) {
	for _, test := range []struct {
		name string
		env  runtime.Environment
		app  connections.Connection
	}{
		{"an app id that is not a number", runtime.Environment{}, githubAppConnection("hq-app", "12a")},
		{"no app id", runtime.Environment{}, githubAppConnection("hq-app", "")},
		{"a key name that leaves the key directory", runtime.Environment{SSHDir: "/keys"}, githubAppConnection("../hq-app", "1")},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := New(test.env, supplied(test.app), &fakeHTTP{})
			c, err := r.githubConnection("")
			if err == nil {
				_, err = r.githubJWT(t.Context(), c)
			}
			if err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestGitHubTokensAreScopedAndMintedOncePerSweep(t *testing.T) {
	h := newGitHubHarness(t, map[string]string{githubFakeAPI + " /repos/example/app": `{}`}, runtime.Environment{})
	c, _ := h.r.githubConnection("")
	ctx := t.Context()
	call := func(perms githubapi.AppPermissions) {
		t.Helper()
		if _, err := h.r.githubAs(ctx, c, []string{"example/app"}, perms).ReposGetWithResponse(ctx, "example", "app"); err != nil {
			t.Fatal(err)
		}
	}
	end := h.r.BeginSnapshot()
	call(githubChecksRead)
	call(githubChecksRead)
	call(githubPullsRead)
	end()
	if h.minted() != 2 {
		t.Fatalf("one token per scope within a sweep, got %d", h.minted())
	}
	h.mu.Lock()
	grants := maps.Clone(h.grants)
	h.mu.Unlock()
	for _, grant := range grants {
		if !slices.Equal(grant.Repositories, []string{"app"}) {
			t.Errorf("a token names exactly its repositories: %+v", grant)
		}
	}
	call(githubChecksRead)
	call(githubChecksRead)
	if h.minted() != 4 {
		t.Fatalf("outside a sweep every call mints, got %d", h.minted())
	}
	if _, err := h.r.githubToken(ctx, c, []string{"one/a", "two/b"}, githubActionsRead); err == nil || !strings.Contains(err.Error(), "one account") {
		t.Fatalf("a token spanning two accounts: %v", err)
	}
	if _, err := h.r.githubToken(ctx, c, []string{"one/a"}, githubapi.AppPermissions{}); err == nil {
		t.Fatal("a token naming no permission was minted")
	}
}

func TestGitHubProbe(t *testing.T) {
	public, _, _ := ed25519.GenerateKey(rand.Reader)
	key, _ := ssh.NewPublicKey(public)
	h := newGitHubHarness(t, map[string]string{githubFakeAPI + " /app": `{"slug":"hq-example"}`}, runtime.Environment{})
	if err := os.WriteFile(filepath.Join(h.r.Env.SSHDir, "hq-app.key.pub"), ssh.MarshalAuthorizedKey(key), 0o600); err != nil {
		t.Fatal(err)
	}
	result, err := h.r.githubProbe(t.Context(), "hq-app")
	if err != nil {
		t.Fatal(err)
	}
	fingerprint, _ := sshKeyFingerprint(string(ssh.MarshalAuthorizedKey(key)))
	if result.Detail != "GitHub App hq-example, key "+fingerprint || !slices.Equal(result.Reaches, []string{"example"}) || !strings.HasPrefix(fingerprint, "SHA256:") {
		t.Fatalf("%+v", result)
	}
	for _, call := range h.recorded() {
		if _, minted := h.grantOf(call); minted {
			t.Errorf("a probe asks as the app, never under an installation token: %+v", call)
		}
	}

	h.answer(githubFakeAPI, "/app", `{}`)
	if _, err := h.r.githubProbe(t.Context(), "hq-app"); func() runtime.FailureClass { f, _, _ := runtime.Classify(err); return f }() != runtime.FailureClassCredential {
		t.Fatalf("an app GitHub does not name is a refused credential: %v", err)
	}
}

// The app is registered with exactly what the controller's code asks for,
// a dependency update run's grant included, plus the pipeline's: Actions
// write for an admission starting the host's composition, and Deployments
// write for Deploy recording production's deployment, and the webhook's.
func TestTheAppHoldsExactlyWhatHQAsksFor(t *testing.T) {
	data, err := os.ReadFile("../../deploy/github-apps.json")
	if err != nil {
		t.Fatal(err)
	}
	var registered struct {
		Apps map[string]struct {
			Permissions map[string]string `json:"permissions"`
		} `json:"apps"`
	}
	if err := json.Unmarshal(data, &registered); err != nil {
		t.Fatal(err)
	}
	wanted := map[string]string{}
	for _, grant := range []githubapi.AppPermissions{githubRead, githubPullStatesRead, githubChecksWrite, githubPullsWrite, githubMergeWrite, githubRenovate, {Actions: githubapi.AppPermissionsActionsWrite}, {Deployments: githubapi.AppPermissionsDeploymentsWrite}} {
		encoded, _ := json.Marshal(grant)
		var names map[string]string
		_ = json.Unmarshal(encoded, &names)
		for name, level := range names {
			if level == "write" || wanted[name] == "" {
				wanted[name] = level
			}
		}
	}
	// No token asks for it, but GitHub sends the webhook's merge_group
	// events only to an App holding it (HQ's INBOUND requires).
	wanted["merge_queues"] = "read"
	if len(registered.Apps) != 1 {
		t.Fatalf("apps %v", registered.Apps)
	}
	for _, app := range registered.Apps {
		got, _ := json.Marshal(app.Permissions)
		want, _ := json.Marshal(wanted)
		if string(got) != string(want) {
			t.Fatalf("registered %s\nasked for %s", got, want)
		}
	}
}

// githubArtifactComment is the comment Renovate's run leaves on its pull
// request when a lock file would not update, as its branch worker writes it
// (lib/workers/repository/update/branch/index.ts), around a tool's stderr.
func githubArtifactComment(stderr string) string {
	return "### ⚠️ Artifact update problem\n\nRenovate failed to update an artifact related to this branch. You probably do not want to merge this PR as-is.\n\n" +
		"♻ Renovate will retry this branch, including artifacts, only when one of the following happens:\n\n - any of the package files in this branch needs updating, or \n" +
		" - the branch becomes conflicted, or\n - you click the rebase/retry checkbox if found above, or\n - you rename this PR's title to start with \"rebase!\" to trigger it manually\n\n" +
		"The artifact failure details are included below:\n\n##### File name: mise.toml\n\n```\n" + stderr + "\n```\n\n"
}

// githubRepositoryRoutes is one repository with a little of everything.
func githubRepositoryRoutes() map[string]string {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	workflow, _ := json.Marshal("jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n      - uses: \"actions/setup-go@0123456789abcdef0123456789abcdef01234567\"\n      - uses: ./.github/actions/local\n      - uses: docker://alpine:3\n  call:\n    uses: example/shared/.github/workflows/reuse.yml@main\n")
	composite, _ := json.Marshal("runs:\n  steps:\n    - uses: actions/checkout@v4\n")
	// The update's comments: a person quoting Renovate's heading, and the
	// artifact problem Renovate's run left, newer than one it edited since.
	comments, _ := json.Marshal([]map[string]any{
		{"user": map[string]string{"login": "hq-example[bot]"}, "body": githubArtifactComment("mise ERROR an older problem")},
		{"user": map[string]string{"login": "hq-example[bot]"}, "body": githubArtifactComment("\x1b[31mmise ERROR\x1b[0m cannot lock jdx/mise: not configured in this project")},
		{"user": map[string]string{"login": "octo"}, "body": githubArtifactComment("not Renovate's")},
	})
	return map[string]string{
		api("/issues/6/comments?per_page=100"): string(comments),
		api(""): `{"full_name":"example/app","private":true,"archived":false,"html_url":"https://github.com/example/app","default_branch":"main","pushed_at":"2030-01-02T00:00:00Z","visibility":"private",
			"security_and_analysis":{"secret_scanning":{"status":"enabled"},"advanced_security":{"status":"disabled"},"secret_scanning_push_protection":{"status":"paused"},"example_feature":{"status":"enabled"}}}`,
		api("/commits/main"): `{"sha":"aaaa000000000000000000000000000000000000","html_url":"https://github.com/example/app/commit/aaaa","author":{"login":"octo"},
			"commit":{"message":"Ship it\n\nbody","author":{"name":"Octo","date":"2030-01-01T00:00:00Z"},"committer":{"name":"Octo","date":"2030-01-02T00:00:00Z"}}}`,
		api("/actions/runs?per_page=50"): `{"workflow_runs":[
			{"id":3,"name":"CI renamed","workflow_id":11,"status":"waiting","conclusion":null,"event":"push","head_branch":"main","head_sha":"aaaa","html_url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"},
			{"id":2,"name":"CI","workflow_id":11,"status":"completed","conclusion":"success","event":"push","head_branch":"main","head_sha":"aaaa","created_at":"2030-01-02T00:00:00Z"},
			{"id":1,"name":"Gone","workflow_id":99,"status":"completed","conclusion":"success","event":"push","created_at":"2030-01-01T00:00:00Z"},
			{"id":4,"name":"Dependency graph","workflow_id":12,"status":"completed","event":"dynamic","created_at":"2030-01-04T00:00:00Z"}]}`,
		api("/actions/runs?branch=main&per_page=100"): `{"workflow_runs":[
			{"id":3,"name":"CI renamed","workflow_id":11,"status":"waiting","conclusion":null,"event":"push","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","created_at":"2030-01-03T00:00:00Z"}]}`,
		api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100"): `{"check_runs":[
			{"id":1,"name":"test","status":"completed","conclusion":"failure","started_at":"2030-01-01T00:00:00Z"},
			{"id":2,"name":"test","status":"completed","conclusion":"success","started_at":"2030-01-01T01:00:00Z"},
			{"id":3,"name":"lint","status":"in_progress","conclusion":null,"started_at":"2030-01-01T00:00:00Z"}]}`,
		api("/pulls?state=open&per_page=30"): `[{"number":6,"title":"Update example-lib to v2","html_url":"https://github.com/example/app/pull/6","user":{"login":"hq-example[bot]"},"draft":false,
				"created_at":"2030-01-03T00:00:00Z","updated_at":"2030-01-04T00:00:00Z","head":{"sha":"eeee","ref":"renovate/example-lib-2.x","repo":{"full_name":"example/app"}},"base":{"sha":"dddd","ref":"main"},"labels":[{"name":"dependencies"},{"name":"security"}]},
			{"number":5,"title":"Bump","html_url":"https://github.com/example/app/pull/5","user":{"login":"octo"},"draft":true,"created_at":"2030-01-01T00:00:00Z","updated_at":"2030-01-02T00:00:00Z","head":{"sha":"bbbb","ref":"bump"},"labels":[]}]`,
		api("/commits/eeee/check-runs?per_page=100"): `{"check_runs":[{"id":9,"name":"build","status":"completed","conclusion":"success"},{"id":10,"name":"test","status":"completed","conclusion":"failure"}]}`,
		api("/pulls/6"):         `{"number":6,"changed_files":4}`,
		githubFakeAPI + " /app": `{"slug":"hq-example"}`,
		api("/pulls/6/files?per_page=100"): `[{"sha":"c1","filename":"compose/example/compose.yml","status":"modified","additions":1,"deletions":1,"changes":2,"blob_url":"","raw_url":"","contents_url":"","patch":"@@ -1 +1 @@\\n-image: example/app:1\\n+image: example/app:2"},
			{"sha":"c2","filename":"README.md","status":"modified","additions":3,"deletions":0,"changes":3,"blob_url":"","raw_url":"","contents_url":"","patch":"@@ -1 +1,4 @@"},
			{"sha":"c3","filename":"compose/large/compose.yml","status":"added","additions":900,"deletions":0,"changes":900,"blob_url":"","raw_url":"","contents_url":"","patch":"@@ -0,0 +1 @@\\n+services: {}"},
			{"sha":"c4","filename":"compose/moved.yml","status":"renamed","additions":0,"deletions":0,"changes":0,"blob_url":"","raw_url":"","contents_url":"","previous_filename":"compose/old.yml"}]`,
		api("/actions/workflows?per_page=100"):     `{"workflows":[{"id":11,"name":"CI","state":"active"},{"id":12,"name":"graph","state":"active"}]}`,
		api("/actions/runs/3/pending_deployments"): `[{"environment":{"name":"production"}},{"environment":{}}]`,
		api("/releases?per_page=1"):                `[{"tag_name":"v1.2.0","html_url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}]`,
		api("/deployments?per_page=20"): `[{"id":21,"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z","payload":{"image":"ghcr.io/example/app/composition@sha256:aaaa"}},
			{"id":20,"environment":"production","sha":"9999","created_at":"2030-01-01T00:00:00Z","payload":""}]`,
		api("/deployments/21/statuses?per_page=1"):           `[{"state":"success","log_url":"https://github.com/example/app/actions/runs/3/job/77?pr=1","target_url":""}]`,
		api("/actions/jobs/77"):                              `{"id":77,"steps":[{"name":"Verify signature","conclusion":"success"},{"name":"Deploy","conclusion":"success"}]}`,
		api("/code-scanning/alerts?state=open&per_page=100"): `[{"rule":{"security_severity_level":"high","severity":"error"}},{"rule":{"severity":"warning"}}]`,
		api("/dependabot/alerts?state=open&per_page=100"): `[{"number":3,"html_url":"https://github.com/example/app/security/dependabot/3","dependency":{"package":{"ecosystem":"npm","name":"example-lib"},"manifest_path":"web/package.json"},
				"security_advisory":{"severity":"critical"},"security_vulnerability":{"package":{"ecosystem":"npm","name":"example-lib"},"first_patched_version":{"identifier":"2.0.1"}}},
			{"number":2,"html_url":"https://github.com/example/app/security/dependabot/2","dependency":{"manifest_path":"go.mod"},
				"security_advisory":{"severity":"high"},"security_vulnerability":{"package":{"ecosystem":"go","name":"example.com/unfixed"},"first_patched_version":null}}]`,
		api("/secret-scanning/alerts?state=open&per_page=100"): `[]`,
		api("/actions/artifacts?per_page=50"): `{"artifacts":[{"name":"late","expired":false,"expires_at":"2030-03-01T00:00:00Z","workflow_run":{"head_sha":"aaaa"}},
			{"name":"old","expired":true,"expires_at":"2029-01-01T00:00:00Z"},{"name":"soon","expired":false,"expires_at":"2030-02-01T00:00:00Z","workflow_run":{"head_sha":"aaaa"}}]}`,
		api("/rules/branches/main"): `[{"type":"pull_request","parameters":{"required_approving_review_count":1}},{"type":"pull_request","parameters":{"required_approving_review_count":2}},
			{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"test"},{"context":"lint"},{"context":"test"}]}},{"type":"non_fast_forward"},{"type":"deletion"}]`,
		api("/environments"): `{"environments":[{"name":"production","protection_rules":[{"id":1,"type":"required_reviewers","reviewers":[{"type":"User","reviewer":{"login":"octo"}},{"type":"Team","reviewer":{"name":"ops"}}]},
			{"id":2,"type":"wait_timer","wait_timer":5}],"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}},{"name":"preview"}]}`,
		api("/actions/runners"):                `{"runners":[{"name":"homelab","status":"online","busy":false,"labels":[{"name":"self-hosted"},{"name":"linux"}]}]}`,
		api("/actions/permissions"):            `{"enabled":true,"allowed_actions":"selected","sha_pinning_required":true}`,
		api("/actions/permissions/workflow"):   `{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}`,
		api("/vulnerability-alerts"):           ``,
		api("/collaborators?per_page=100"):     `[{"login":"octo","role_name":"admin"}]`,
		api("/keys?per_page=100"):              `[{"title":"deploy","read_only":true,"last_used":"2030-01-01T00:00:00Z","created_at":"2029-01-01T00:00:00Z"}]`,
		api("/actions/variables?per_page=100"): `{"variables":[{"name":"REGION","value":"never read"}]}`,
		api("/pins"): `{"data":{"repository":{
			"workflows":{"entries":[{"path":".github/workflows/ci.yml","object":{"text":` + string(workflow) + `}},{"path":".github/workflows/README.md","object":{"text":""}}]},
			"actions":{"entries":[{"object":{"entries":[{"path":".github/actions/local/action.yml","object":{"text":` + string(composite) + `}}]}}]}}}}`,
		githubFakeAPI + " /repos/actions/checkout/commits/v4": `{"sha":"cccc000000000000000000000000000000000000"}`,
		githubFakeAPI + " /repos/example/shared/commits/main": `{"sha":"dddd000000000000000000000000000000000000"}`,
	}
}

// readPass is one pass over every repository, as each of its kinds reports
// it: the repository's record, its workflow runs and its pull requests.
func readPass(t *testing.T, h *githubHarness) (GitHubRepositoryRecord, []GitHubWorkflowRunRecord, []GitHubOpenPullRequestRecord, []runtime.RefusedPart) {
	t.Helper()
	pass := h.r.githubRepositories(t.Context())
	if pass.err != nil || len(pass.repositories) != 1 {
		t.Fatalf("pass %+v", pass)
	}
	runs, pulls := []GitHubWorkflowRunRecord{}, []GitHubOpenPullRequestRecord{}
	for _, run := range pass.runs {
		runs = append(runs, run.(GitHubWorkflowRunRecord))
	}
	for _, pull := range pass.pulls {
		pulls = append(pulls, pull.(GitHubOpenPullRequestRecord))
	}
	return pass.repositories[0].(GitHubRepositoryRecord), runs, pulls, pass.refused
}

func readRepository(t *testing.T, h *githubHarness) (GitHubRepositoryRecord, []runtime.RefusedPart) {
	t.Helper()
	controller := NewController(h.r, runtime.ControllerRegistry{})
	report := controller.readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if !report.OK || len(report.Records) != 1 {
		t.Fatalf("report %+v", report)
	}
	return report.Records[0].(GitHubRepositoryRecord), report.RefusedParts
}

// A release live with a check after its deploy failed is a success on GitHub,
// told apart from one that passed by the description deploy/outcomes.json
// declares for it; another repository's own description is no outcome.
func TestADeploymentLiveWithACheckFailedSaysSo(t *testing.T) {
	for _, c := range []struct {
		state, description string
		want               bool
	}{
		{deployed.DeployLiveCheckFailed.State, deployed.DeployLiveCheckFailed.Description, true},
		{deployed.DeployLive.State, deployed.DeployLive.Description, false},
		{"success", "Deployment finished successfully.", false},
	} {
		routes := githubRepositoryRoutes()
		status, _ := json.Marshal([]map[string]string{{"state": c.state, "description": c.description, "log_url": "", "target_url": ""}})
		routes[githubFakeAPI+" /repos/example/app/deployments/21/statuses?per_page=1"] = string(status)
		record, _ := readRepository(t, newGitHubHarness(t, routes, runtime.Environment{}))
		if len(record.Deployments) != 1 || record.Deployments[0].CheckFailed != c.want {
			t.Errorf("%q: read %+v, want check_failed %v", c.description, record.Deployments, c.want)
		}
	}
}

// The security features forwarded are the ones the contract declares, each
// status one of its two words: the routes' feature GitHub might add is left
// out, and a status word it might add is none.
func TestGitHubRepositoryReading(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	// The default branch's run, and a pull request's on another branch.
	routes[api("/actions/runs?per_page=50")] = `{"workflow_runs":[
		{"id":6,"name":"CI","workflow_id":11,"check_suite_id":60,"status":"completed","conclusion":"failure","event":"pull_request","head_branch":"bump","head_sha":"bbbb","html_url":"https://github.com/example/app/actions/runs/6","created_at":"2030-01-05T00:00:00Z"},
		{"id":3,"name":"CI renamed","workflow_id":11,"status":"waiting","conclusion":null,"event":"push","head_branch":"main","head_sha":"aaaa","html_url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"},
		{"id":1,"name":"Gone","workflow_id":99,"status":"completed","conclusion":"success","event":"push","head_branch":"old","created_at":"2030-01-01T00:00:00Z"},
		{"id":4,"name":"Dependency graph","workflow_id":12,"status":"completed","event":"dynamic","head_branch":"bump","created_at":"2030-01-04T00:00:00Z"}]}`
	routes[api("/actions/runs?branch=main&per_page=100")] = `{"workflow_runs":[
		{"id":3,"name":"CI renamed","workflow_id":11,"check_suite_id":30,"status":"waiting","conclusion":null,"event":"push","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","html_url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"}]}`
	routes[api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100")] = `{"check_runs":[
		{"id":1,"name":"test","status":"completed","conclusion":"failure","started_at":"2030-01-01T00:00:00Z","check_suite":{"id":30}},
		{"id":2,"name":"test","status":"completed","conclusion":"success","started_at":"2030-01-01T01:00:00Z","check_suite":{"id":30}},
		{"id":3,"name":"lint","status":"in_progress","conclusion":null,"started_at":"2030-01-01T00:00:00Z","check_suite":{"id":30}}]}`
	routes[api("/check-suites/60/check-runs?per_page=100")] = `{"check_runs":[{"id":61,"name":"test","status":"completed","conclusion":"failure","check_suite":{"id":60}}]}`
	// As GitHub's GraphQL answers it: an Actions job, a job of GitHub's own
	// dynamic run, a rerun, and a commit status.
	routes[api("/pull_states")] = `{"data":{"repository":{"pullRequests":{"nodes":[
		{"number":6,"mergeStateStatus":"BLOCKED","reviewDecision":"REVIEW_REQUIRED","statusCheckRollup":{"contexts":{"nodes":[
			{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS","startedAt":"2030-01-03T00:00:00Z","completedAt":"2030-01-03T00:04:00Z","url":"https://github.com/example/app/runs/41","checkSuite":{"workflowRun":{"event":"pull_request"}}},
			{"__typename":"CheckRun","name":"test","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2030-01-03T00:00:00Z","checkSuite":{"workflowRun":{"event":"pull_request"}}},
			{"__typename":"CheckRun","name":"update-graph","status":"COMPLETED","conclusion":"FAILURE","startedAt":"2030-01-03T00:00:00Z","checkSuite":{"workflowRun":{"event":"dynamic"}}},
			{"__typename":"CheckRun","name":"test","status":"IN_PROGRESS","conclusion":null,"startedAt":"2030-01-03T01:00:00Z","checkSuite":{"workflowRun":{"event":"pull_request"}}},
			{"__typename":"StatusContext","context":"example/scan","state":"ERROR","createdAt":"2030-01-03T00:02:00Z","targetUrl":"https://scan.example.com/42"}]}}},
		{"number":5,"mergeStateStatus":"DRAFT","reviewDecision":null,"statusCheckRollup":null}]}}}}`
	h := newGitHubHarness(t, routes, runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/secret-scanning/alerts?state=open&per_page=100", 403)
	record, runs, pulls, refused := readPass(t, h)

	got, _ := json.Marshal(record)
	var view map[string]any
	_ = json.Unmarshal(got, &view)
	for _, test := range []struct {
		name string
		got  any
		want string
	}{
		{"head", record.Head, `{"sha":"aaaa000000000000000000000000000000000000","message":"Ship it","author":"octo","date":"2030-01-02T00:00:00Z","url":"https://github.com/example/app/commit/aaaa"}`},
		{"a check stands as its last run did", record.Checks, `{"state":"pending","total":2,"failing":[],"running":1,"names":["lint","test"]}`},
		{"the checks the newest pull request runs", record.PullRequestChecks, `["build","test"]`},
		{"each open pull request, with its checks and files read only for an update's", pulls, `[{"connection_ref":"hq-app","repository":"example/app","number":6,"title":"Update example-lib to v2","url":"https://github.com/example/app/pull/6","author":"hq-example[bot]","draft":false,"created_at":"2030-01-03T00:00:00Z","updated_at":"2030-01-04T00:00:00Z","head":"eeee","base":"dddd","branch":"renovate/example-lib-2.x","base_branch":"main","labels":["dependencies","security"],"head_repository":"example/app","checks":{"state":"failure","total":2,"failing":["test"],"running":0,"names":["build","test"]},` +
			`"files":[{"path":"compose/example/compose.yml","status":"modified","additions":1,"deletions":1,"app":"example"},{"path":"README.md","status":"modified","additions":3,"deletions":0,"app":""},` +
			`{"path":"compose/large/compose.yml","status":"added","additions":900,"deletions":0,"app":"large"},{"path":"compose/moved.yml","status":"renamed","additions":0,"deletions":0,"app":""}],"changed_files":4,"apps":null,` +
			`"artifact_problem":["mise ERROR cannot lock jdx/mise: not configured in this project"],` +
			`"mergeable_state":"blocked","review_state":"review_required","check_runs":[{"name":"build","status":"completed","conclusion":"success","started_at":"2030-01-03T00:00:00Z","completed_at":"2030-01-03T00:04:00Z","url":"https://github.com/example/app/runs/41"},` +
			`{"name":"test","status":"in_progress","conclusion":"","started_at":"2030-01-03T01:00:00Z"},{"name":"example/scan","status":"completed","conclusion":"failure","started_at":"2030-01-03T00:02:00Z","url":"https://scan.example.com/42"}],"carried":false},` +
			`{"connection_ref":"hq-app","repository":"example/app","number":5,"title":"Bump","url":"https://github.com/example/app/pull/5","author":"octo","draft":true,"created_at":"2030-01-01T00:00:00Z","updated_at":"2030-01-02T00:00:00Z","head":"bbbb","base":"","branch":"bump","base_branch":"","labels":[],"head_repository":"","checks":null,"files":null,"changed_files":0,"apps":null,"mergeable_state":"draft","review_state":"","check_runs":[],"carried":false}]`},
		{"each open Dependabot alert, its package from the dependency or the vulnerability", record.DependabotAlerts, `[{"number":3,"package":"example-lib","ecosystem":"npm","manifest_path":"web/package.json","severity":"critical","first_patched_version":"2.0.1","url":"https://github.com/example/app/security/dependabot/3"},` +
			`{"number":2,"package":"example.com/unfixed","ecosystem":"go","manifest_path":"go.mod","severity":"high","first_patched_version":null,"url":"https://github.com/example/app/security/dependabot/2"}]`},
		{"each workflow's newest run on each branch, under its name now, with its jobs", runs, `[{"connection_ref":"hq-app","repository":"example/app","id":3,"name":"CI","status":"waiting","conclusion":"","event":"push","branch":"main","sha":"aaaa000000000000000000000000000000000000","url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z",` +
			`"jobs":[{"name":"test","status":"completed","conclusion":"success"},{"name":"lint","status":"in_progress","conclusion":""}],"carried":false},` +
			`{"connection_ref":"hq-app","repository":"example/app","id":6,"name":"CI","status":"completed","conclusion":"failure","event":"pull_request","branch":"bump","sha":"bbbb","url":"https://github.com/example/app/actions/runs/6","created_at":"2030-01-05T00:00:00Z","jobs":[{"name":"test","status":"completed","conclusion":"failure"}],"carried":false}]`},
		{"waiting", record.Waiting[0].Environments, `["production"]`},
		{"release", record.Release, `{"tag":"v1.2.0","url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}`},
		{"the newest deployment to each environment, with what its job verified", record.Deployments, `[{"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z","state":"success","url":"https://github.com/example/app/actions/runs/3/job/77?pr=1","verified":[{"name":"Verify signature","conclusion":"success"}],"check_failed":false,"image":"ghcr.io/example/app/composition@sha256:aaaa"}]`},
		{"alerts by severity; a refused kind is absent, not zero", record.Alerts, `{"code_scanning":{"high":1,"warning":1},"dependabot":{"critical":1,"high":1}}`},
		{"live artifacts, soonest first", record.Artifacts, `[{"name":"soon","expires_at":"2030-02-01T00:00:00Z","sha":"aaaa"},{"name":"late","expires_at":"2030-03-01T00:00:00Z","sha":"aaaa"}]`},
		{"rules", record.Rules, `{"pull_request":true,"reviews":2,"required_checks":["lint","test"],"blocks_force_push":true,"blocks_deletion":true}`},
		{"environments", record.Environments, `[{"name":"production","reviewers":["octo","ops"],"wait_minutes":5,"branches":"protected"},{"name":"preview","reviewers":[],"wait_minutes":0,"branches":"any"}]`},
		{"runners", record.Runners, `[{"name":"homelab","status":"online","busy":false,"labels":["linux","self-hosted"]}]`},
		{"access", record.Access, `{"visibility":"private","collaborators":[{"login":"octo","role":"admin"}],"deploy_keys":[{"title":"deploy","read_only":true,"last_used":"2030-01-01T00:00:00Z","created_at":"2029-01-01T00:00:00Z"}],"allowed_actions":"selected","pinning_required":true,"token":"read","token_approves_reviews":false,"vulnerability_alerts":true,"security":{"advanced_security":"disabled","secret_scanning":"enabled","secret_scanning_push_protection":null}}`},
		{"variable names, never values", record.Variables, `["REGION"]`},
		{"every unpinned uses line with the commit its ref names", record.Pins, `[{"path":".github/workflows/ci.yml","uses":"actions/checkout@v4","action":"actions/checkout","ref":"v4","sha":"cccc000000000000000000000000000000000000"},{"path":".github/workflows/ci.yml","uses":"example/shared/.github/workflows/reuse.yml@main","action":"example/shared/.github/workflows/reuse.yml","ref":"main","sha":"dddd000000000000000000000000000000000000"},{"path":".github/actions/local/action.yml","uses":"actions/checkout@v4","action":"actions/checkout","ref":"v4","sha":"cccc000000000000000000000000000000000000"}]`},
		{"a workflow called from another repository is named", record.CalledWorkflows, `["example/shared/.github/workflows/reuse.yml@main"]`},
		{"images only for the composition's own repository", record.Images, `[]`},
	} {
		t.Run(test.name, func(t *testing.T) {
			encoded, _ := json.Marshal(test.got)
			if string(encoded) != test.want {
				t.Fatalf("got  %s\nwant %s", encoded, test.want)
			}
		})
	}
	if record.ConnectionRef != "hq-app" || record.Repository != "example/app" || !record.Private || record.Archived || record.DefaultBranch != "main" {
		t.Errorf("record %+v", record)
	}
	// A 403 under a token that holds every read permission is a feature the
	// repository does not offer, never a permission to grant.
	if len(refused) != 1 || refused[0].Part != runtime.PartLeakedCredentials || refused[0].Scope != "example/app" || refused[0].Refusal == runtime.FailureClassPermission {
		t.Fatalf("refused %+v", refused)
	}
	resolved := 0
	for _, call := range h.recorded() {
		grant, minted := h.grantOf(call)
		if strings.HasPrefix(call.path, "/repos/actions/checkout/commits/") {
			resolved++
		}
		if !minted || call.method != "GET" || strings.HasPrefix(call.path, "/app") || strings.HasSuffix(call.path, "/installation") || strings.HasPrefix(call.path, "/installation/repositories") || call.path == "/rate_limit" {
			continue
		}
		encoded, _ := json.Marshal(grant.Permissions)
		if want, _ := json.Marshal(githubRead); string(encoded) != string(want) || !slices.Equal(grant.Repositories, []string{"app"}) {
			t.Errorf("%s under %s for %v", call.path, encoded, grant.Repositories)
		}
	}
	if resolved != 1 {
		t.Errorf("a tag is resolved once per repository, resolved %d times", resolved)
	}
}

// A repository's runs and pull requests are read in its pass and carried with
// it: unchanged, each kind sends the one record that names it. Refused, the
// pull request states are refused for that kind alone, and its pull requests
// still read.
func TestRunsAndPullRequestsAreReadWithTheirRepository(t *testing.T) {
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/pull_states", http.StatusForbidden)
	controller := NewController(h.r, runtime.ControllerRegistry{})
	kinds := []runtime.ResourceKind{runtime.ResourceKindGitHubRepository, runtime.ResourceKindGitHubWorkflowRun, runtime.ResourceKindGitHubOpenPullRequest}
	reports := map[runtime.ResourceKind]runtime.KindReport{}
	for _, kind := range kinds {
		reports[kind] = controller.readKind(t.Context(), h.r.readers[string(kind)])
	}
	pulls := reports[runtime.ResourceKindGitHubOpenPullRequest]
	if refused := pulls.RefusedParts; len(refused) != 1 || refused[0].Part != runtime.PartPullStates || refused[0].Scope != "example/app" || len(pulls.Records) != 2 {
		t.Fatalf("pull requests %+v", pulls)
	}
	for _, refused := range reports[runtime.ResourceKindGitHubRepository].RefusedParts {
		if refused.Part == runtime.PartPullStates {
			t.Errorf("the repository's reading holds the pull requests' refusal: %+v", refused)
		}
	}
	if pull := pulls.Records[0].(GitHubOpenPullRequestRecord); pull.MergeableState != "" || len(pull.CheckRuns) != 0 {
		t.Errorf("a refused part read %+v", pull)
	}

	held := heldAfter(t, reports[runtime.ResourceKindGitHubRepository])
	NewController(h.r, runtime.ControllerRegistry{GithubRepositories: runtime.GitHubRepositoriesPlan{Held: held}})
	for _, kind := range kinds {
		if report := controller.readKind(t.Context(), h.r.readers[string(kind)]); !slices.Equal(carried(report), []string{"example/app"}) || len(report.Records) != 1 {
			t.Errorf("%s sent %+v", kind, report.Records)
		}
	}
}

// A check run in the suite of one of GitHub's own dynamic runs (the
// dependency graph's) fails no commit: no workflow of the repository owns it.
func TestGitHubsOwnRunsFailNoCommit(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	routes[api("/actions/runs?per_page=50")] = `{"workflow_runs":[
		{"id":2,"name":"CI","workflow_id":11,"check_suite_id":20,"status":"completed","conclusion":"success","event":"push","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","created_at":"2030-01-02T00:00:00Z"},
		{"id":4,"name":"Graph Update: uv in /packages/example","workflow_id":12,"check_suite_id":40,"status":"completed","conclusion":"failure","event":"dynamic","head_branch":"main","head_sha":"aaaa000000000000000000000000000000000000","created_at":"2030-01-04T00:00:00Z"},
		{"id":5,"name":"Graph Update: uv in /packages/example","workflow_id":12,"check_suite_id":50,"status":"completed","conclusion":"failure","event":"dynamic","head_branch":"renovate/example-lib-2.x","head_sha":"eeee","created_at":"2030-01-04T00:00:00Z"}]}`
	routes[api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100")] = `{"check_runs":[
		{"id":1,"name":"test","status":"completed","conclusion":"success","started_at":"2030-01-02T00:00:00Z","check_suite":{"id":20}},
		{"id":2,"name":"update-uv-graph","status":"completed","conclusion":"failure","started_at":"2030-01-04T00:00:00Z","check_suite":{"id":40}}]}`
	routes[api("/commits/eeee/check-runs?per_page=100")] = `{"check_runs":[
		{"id":9,"name":"build","status":"completed","conclusion":"success","check_suite":{"id":90}},
		{"id":10,"name":"update-uv-graph","status":"completed","conclusion":"failure","check_suite":{"id":50}}]}`
	record, _, pulls, _ := readPass(t, newGitHubHarness(t, routes, runtime.Environment{}))

	if record.Checks.State != "success" || slices.Contains(record.Checks.Names, "update-uv-graph") {
		t.Errorf("head commit checks %+v", record.Checks)
	}
	pull := pulls[0].Checks
	if pull == nil || pull.State != "success" || slices.Contains(record.PullRequestChecks, "update-uv-graph") {
		t.Errorf("pull request checks %+v, names %v", pull, record.PullRequestChecks)
	}
}

// A workflow that does not run on every push fails the default branch from
// its newest run there, at an older commit, whatever passed on the head since,
// until a run of it passes: a rerun included, and names the step it failed at. A pull request's run from a
// branch of the same name is not the branch's, and a weekly run behind a full
// page of the branch's runs is read from its workflow.
func TestABranchFailsUntilEachWorkflowsNewestRunPasses(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	const head, older = "aaaa000000000000000000000000000000000000", "9999000000000000000000000000000000000000"
	scorecard := func(conclusion string) string {
		return `{"id":45,"name":"Scorecard supply-chain security","workflow_id":13,"check_suite_id":70,"status":"completed","conclusion":"` + conclusion +
			`","event":"schedule","head_branch":"main","head_sha":"` + older + `","html_url":"https://github.com/example/app/actions/runs/45","created_at":"2030-01-01T00:00:00Z"}`
	}
	ci := `{"id":50,"name":"CI","workflow_id":11,"check_suite_id":80,"status":"completed","conclusion":"success","event":"push","head_branch":"main","head_sha":"` + head + `","created_at":"2030-01-03T00:00:00Z"}`
	forked := `{"id":51,"name":"Scorecard","workflow_id":13,"status":"completed","conclusion":"success","event":"pull_request","head_branch":"main","head_sha":"bbbb","created_at":"2030-01-04T00:00:00Z"}`
	for _, test := range []struct {
		name, conclusion string
		full             bool
		state            string
		failing          []string
		runs             []string
	}{
		{"a scheduled run that failed on an older commit fails the branch", "failure", false, "failure", []string{"Publish"}, []string{"https://github.com/example/app/actions/runs/45"}},
		{"read from its workflow behind a full page", "failure", true, "failure", []string{"Publish"}, []string{"https://github.com/example/app/actions/runs/45"}},
		{"a rerun that passed clears it", "success", false, "success", []string{}, []string{}},
	} {
		t.Run(test.name, func(t *testing.T) {
			routes := githubRepositoryRoutes()
			routes[api("/actions/workflows?per_page=100")] = `{"workflows":[{"id":11,"name":"CI","state":"active"},{"id":13,"name":"Scorecard","state":"active"}]}`
			routes[api("/commits/"+head+"/check-runs?per_page=100")] = `{"check_runs":[{"id":1,"name":"test","status":"completed","conclusion":"success","check_suite":{"id":80}}]}`
			routes[api("/check-suites/70/check-runs?per_page=100")] = `{"check_runs":[
				{"id":71,"name":"Analysis","status":"completed","conclusion":"success","check_suite":{"id":70}},
				{"id":72,"name":"Publish","status":"completed","conclusion":"failure","check_suite":{"id":70}}]}`
			routes[api("/actions/jobs/72")] = `{"id":72,"steps":[
				{"name":"Set up job","status":"completed","conclusion":"success","number":1},
				{"name":"Publish results","status":"completed","conclusion":"failure","number":2}]}`
			page := []string{forked, ci}
			if test.full {
				for len(page) < githubListPage {
					page = append(page, ci)
				}
				routes[api("/actions/workflows/13/runs?branch=main&per_page=50")] = `{"workflow_runs":[` + forked + `,` + scorecard(test.conclusion) + `]}`
			} else {
				page = append(page, scorecard(test.conclusion))
			}
			routes[api("/actions/runs?branch=main&per_page=100")] = `{"workflow_runs":[` + strings.Join(page, ",") + `]}`
			h := newGitHubHarness(t, routes, runtime.Environment{})
			// A log GitHub no longer keeps leaves why unsaid, never the step.
			h.refuse(githubFakeAPI, "/repos/example/app/actions/jobs/72/steps/2/logs", http.StatusGone)
			record, runs, _, _ := readPass(t, h)

			urls := []string{}
			for _, run := range runs {
				if run.Branch == "main" && githubFailed[run.Conclusion] {
					urls = append(urls, run.URL)
				}
			}
			if record.Checks.State != test.state || !slices.Equal(record.Checks.Failing, test.failing) || !slices.Equal(urls, test.runs) {
				t.Errorf("checks %+v, failing runs %v", record.Checks, urls)
			}
			// Each failing job of a workflow run names the step it failed at.
			for _, job := range test.failing {
				if failure := record.Checks.Failures[job]; failure.Step != "Publish results" || failure.Error != "" {
					t.Errorf("%s failed as %+v", job, failure)
				}
			}
		})
	}
}

func TestGitHubRepositoryParts(t *testing.T) {
	for _, test := range []struct {
		name    string
		status  map[string]int
		part    runtime.ReadingPartName
		check   func(GitHubRepositoryRecord) bool
		failure runtime.FailureClass
	}{
		{"workflows it may not read are that part refused, not no pins",
			map[string]int{"/pins": 500}, runtime.PartWorkflowPins,
			func(r GitHubRepositoryRecord) bool { return r.Pins == nil && r.CalledWorkflows == nil }, runtime.FailureClassUnclassified},
		{"a repository without workflows has nothing to pin",
			map[string]int{"/pins": 404}, "",
			func(r GitHubRepositoryRecord) bool { return r.Pins != nil && len(r.Pins) == 0 }, ""},
		{"rules a plan does not offer", map[string]int{"/rules/branches/main": 403}, runtime.PartBranchRules,
			func(r GitHubRepositoryRecord) bool { return r.Rules == nil }, runtime.FailureClassUnclassified},
		{"environments refused read as none", map[string]int{"/environments": 403}, runtime.PartEnvironments,
			func(r GitHubRepositoryRecord) bool { return r.Environments != nil && len(r.Environments) == 0 }, runtime.FailureClassUnclassified},
		{"vulnerability alerts answering 404 are off, not refused", map[string]int{"/vulnerability-alerts": 404}, "",
			func(r GitHubRepositoryRecord) bool { return r.Access != nil && !r.Access.VulnerabilityAlerts }, ""},
		{"a vulnerability alerts answer that is neither is access refused", map[string]int{"/vulnerability-alerts": 500}, runtime.PartAccess,
			func(r GitHubRepositoryRecord) bool { return r.Access == nil }, runtime.FailureClassUnclassified},
		{"access refused whole", map[string]int{"/keys?per_page=100": 404}, runtime.PartAccess,
			func(r GitHubRepositoryRecord) bool { return r.Access == nil }, runtime.FailureClassUnclassified},
		{"Dependabot alerts refused are no rows and no count, not none", map[string]int{"/dependabot/alerts?state=open&per_page=100": 403}, runtime.PartDependabot,
			func(r GitHubRepositoryRecord) bool {
				_, counted := r.Alerts["dependabot"]
				return r.DependabotAlerts == nil && !counted
			}, runtime.FailureClassUnclassified},
		{"a refused credential keeps its class", map[string]int{"/actions/variables?per_page=100": 401}, runtime.PartVariables,
			func(r GitHubRepositoryRecord) bool { return r.Variables == nil }, runtime.FailureClassCredential},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
			for path, code := range test.status {
				h.refuse(githubFakeAPI, "/repos/example/app"+path, code)
			}
			record, refused := readRepository(t, h)
			if !test.check(record) {
				t.Errorf("record %+v", record)
			}
			if test.part == "" {
				if len(refused) != 0 {
					t.Errorf("refused %+v", refused)
				}
				return
			}
			if len(refused) != 1 || refused[0].Part != test.part || refused[0].Refusal != test.failure {
				t.Errorf("refused %+v", refused)
			}
		})
	}
	h := newGitHubHarness(t, githubRepositoryRoutes(), runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/commits/main", 502)
	report := NewController(h.r, runtime.ControllerRegistry{}).readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if report.OK {
		t.Fatal("a repository whose head cannot be read fails the reading, never reads as empty")
	}
}

// withSecondRepository is the routes with example/other installed beside
// example/app, read from the same routes under its own name.
func withSecondRepository(routes map[string]string) map[string]string {
	for key, body := range githubRepositoryRoutes() {
		if rest, ok := strings.CutPrefix(key, githubFakeAPI+" /repos/example/app"); ok {
			routes[githubFakeAPI+" /repos/example/other"+rest] = strings.ReplaceAll(body, "example/app", "example/other")
		}
	}
	routes[githubFakeAPI+" "+githubRepositoriesPage+"1"] = `{"total_count":2,"repositories":[{"full_name":"example/app"},{"full_name":"example/other"}]}`
	routes[githubFakeAPI+" /repos/example/other/installation"] = `{"id":7}`
	return routes
}

func TestARepositoryThatCannotBeReadIsRefusedAlone(t *testing.T) {
	h := newGitHubHarness(t, withSecondRepository(githubRepositoryRoutes()), runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/other/commits/main", 502)
	report := NewController(h.r, runtime.ControllerRegistry{}).readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if !report.OK || len(report.Records) != 1 || report.Records[0].(GitHubRepositoryRecord).Repository != "example/app" {
		t.Fatalf("the repository that reads is reported: %+v", report)
	}
	whole := []runtime.RefusedPart{}
	for _, refused := range report.RefusedParts {
		if refused.Part == runtime.PartWhole {
			whole = append(whole, refused)
		}
	}
	if len(whole) != 1 || whole[0].Scope != "example/other" || whole[0].ConnectionRef != "hq-app" {
		t.Fatalf("the repository that does not is refused by name: %+v", report.RefusedParts)
	}
}

// Every installation and every repository it covers is read, however many
// pages GitHub lists them on.
func TestEveryInstalledRepositoryIsListed(t *testing.T) {
	full := make([]string, githubListPage)
	for i := range full {
		full[i] = `{"id":` + strconv.Itoa(100+i) + `,"account":{"login":"example"}}`
	}
	repositories := make([]string, githubListPage)
	for i := range repositories {
		repositories[i] = `{"full_name":"example/repo-` + strconv.Itoa(i) + `"}`
	}
	h := newGitHubHarness(t, map[string]string{
		githubFakeAPI + " " + githubInstallationsPage + "1": "[" + strings.Join(full[:githubListPage-1], ",") + `,{"id":7,"account":{"login":"example"}}]`,
		githubFakeAPI + " " + githubInstallationsPage + "2": `[{"id":8,"account":{"login":"example"}}]`,
		githubFakeAPI + " " + githubRepositoriesPage + "1":  `{"total_count":101,"repositories":[` + strings.Join(repositories, ",") + `]}`,
		githubFakeAPI + " " + githubRepositoriesPage + "2":  `{"total_count":101,"repositories":[{"full_name":"example/last"}]}`,
	}, runtime.Environment{})
	c, _ := h.r.githubConnection("")
	names, err := h.r.githubInstallationRepositories(t.Context(), c)
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != githubListPage+1 || !slices.Contains(names, "example/last") || !slices.Contains(names, "example/repo-0") {
		t.Fatalf("listed %d: %v", len(names), names)
	}
	installations := 0
	for _, call := range h.recorded() {
		if strings.HasSuffix(call.path, "/access_tokens") {
			installations++
		}
	}
	if installations != githubListPage+1 {
		t.Fatalf("every installation on every page is listed, minted for %d", installations)
	}
}

func TestAListThatNeverEndsIsRefused(t *testing.T) {
	page := make([]string, githubListPage)
	for i := range page {
		page[i] = "1"
	}
	read := 0
	_, err := githubPaged(func(int) ([]string, error) { read++; return page, nil })
	if err == nil || read != githubPageLimit {
		t.Fatalf("read %d pages: %v", read, err)
	}
}

// The mint takes everything it needs as arguments: the token it asks for
// names exactly its scope, under a JWT the app's key signed.
func TestMintGitHubTokenNamesItsScope(t *testing.T) {
	h := newGitHubHarness(t, nil, runtime.Environment{})
	app := GitHubApp{ID: "12345", Key: h.key}
	minted, err := h.r.mintGitHubToken(t.Context(), app, 7, []string{"app"}, githubChecksRead)
	token := minted.Token
	if err != nil || token == "" || minted.ExpiresAt != "2030-01-01T00:00:00Z" {
		t.Fatalf("token %+v: %v", minted, err)
	}
	calls := h.recorded()
	if len(calls) != 1 || calls[0].method != "POST" || calls[0].path != "/app/installations/7/access_tokens" {
		t.Fatalf("calls %+v", calls)
	}
	grant, _ := h.grantOf(githubFakeCall{auth: "Bearer " + token})
	got, _ := json.Marshal(grant.Permissions)
	if want, _ := json.Marshal(githubChecksRead); !slices.Equal(grant.Repositories, []string{"app"}) || string(got) != string(want) {
		t.Fatalf("grant %+v", grant)
	}
	parts := strings.Split(strings.TrimPrefix(calls[0].auth, "Bearer "), ".")
	signature, _ := base64.RawURLEncoding.DecodeString(parts[len(parts)-1])
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if len(parts) != 3 || rsa.VerifyPKCS1v15(&h.key.PublicKey, crypto.SHA256, digest[:], signature) != nil {
		t.Fatal("the mint is asked under a JWT the app's key signed")
	}
	if _, err := h.r.mintGitHubToken(t.Context(), app, 7, []string{"app"}, githubapi.AppPermissions{}); err == nil || len(h.recorded()) != 1 {
		t.Fatal("a token naming no permission, which GitHub would grant every one of, was asked for")
	}
}

func TestGitHubImagesOfTheComposition(t *testing.T) {
	routes := githubRepositoryRoutes()
	routes[githubFakeRegistry+" /token?scope=repository%3Aexample%2Fapp%3Apull&service=ghcr.io"] = `{"token":"pull-app"}`
	routes[githubFakeRegistry+" /v2/example/app/tags/list?n=1000"] = `{"tags":["v1","v2","sha256-abc.sig","sha256-def.att","sha256-012.sig","sha256-345","sha256-abc"]}`
	h := newGitHubHarness(t, routes, runtime.Environment{SourceRepository: "example/app", Image: "ghcr.io/example/app-composed:v9@sha256:0000"})
	h.refuse(githubFakeRegistry, "/token?scope=repository%3Aexample%2Fapp-composed%3Apull&service=ghcr.io", 403)
	record, refused := readRepository(t, h)
	if encoded, _ := json.Marshal(record.Images); string(encoded) != `[{"name":"example/app","tags":2,"signed":["012","345","abc"]}]` {
		t.Fatalf("images %s", encoded)
	}
	if len(refused) != 1 || refused[0].Part != runtime.PartImages || refused[0].Scope != "example/app:example/app-composed" {
		t.Fatalf("an image the app may not read is refused alone: %+v", refused)
	}
	for _, call := range h.recorded() {
		if call.host == githubFakeRegistry && strings.HasPrefix(call.path, "/token") {
			user, _ := base64.StdEncoding.DecodeString(strings.TrimPrefix(call.auth, "Basic "))
			if !strings.HasPrefix(string(user), "x-access-token:minted-") {
				t.Errorf("the registry takes the app's read token: %q", user)
			}
		}
	}
}

// deliveryRoutes is a host and one extension whose latest admission is admitted.
func deliveryRoutes(admitted, pipeline string) map[string]string {
	return map[string]string{
		githubFakeAPI + " /repos/example/ext/actions/workflows/admit.yml/runs?branch=main&per_page=20&status=success": `{"workflow_runs":[{"id":50,"head_sha":"` + admitted + `","created_at":"2030-01-04T23:50:00Z","updated_at":"2030-01-05T00:00:00Z"}]}`,
		githubFakeAPI + " /repos/example/host/actions/workflows/compose.yml/runs?branch=main&per_page=20":             pipeline,
		githubFakeAPI + " /repos/example/host/actions/workflows/deploy.yml/runs?branch=main&per_page=20":              `{"workflow_runs":[]}`,
	}
}

const running = "1111111111111111111111111111111111111111"
const admitted = "2222222222222222222222222222222222222222"

func deliveryHarness(t *testing.T, routes map[string]string) *githubHarness {
	h := newGitHubHarness(t, routes, runtime.Environment{SourceRepository: "example/host", Image: "ghcr.io/example/host:v1"})
	h.r.Extensions = []runtime.AdmittedExtension{{Plugin: "ext", SourceRepository: "example/ext", SourceWorkflow: ".github/workflows/admit.yml", SourceCommit: running}}
	return h
}

func checkRunsRoute(sha, body string) (string, string) {
	return githubFakeAPI + " /repos/example/ext/commits/" + sha + "/check-runs?app_id=12345&check_name=Severino+HQ+%C2%B7+Production&filter=latest", body
}

var deliverySpec = Object{"repository": "example/host", "workflow": ".github/workflows/compose.yml", "branch": "main", "production": string(runtime.GitHubDeliveryProductionCurrent)}

func TestGitHubDelivery(t *testing.T) {
	for _, test := range []struct {
		name       string
		admitted   string
		pipeline   string
		checks     map[string]string // sha -> check-runs answer
		extra      map[string]string
		reason     string
		stage      string
		writes     []string // method path of each write, in order
		checkState string   // the status the check is written with
	}{
		{"current production writes nothing", running, `{"workflow_runs":[]}`,
			map[string]string{running: `{"check_runs":[{"id":8,"conclusion":"success"}]}`}, nil,
			"Current", "live", nil, ""},
		{"an admission with no composition yet is reported and nothing is started", admitted, `{"workflow_runs":[{"id":60,"name":"Compose","status":"completed","conclusion":"success","created_at":"2030-01-04T00:00:00Z"}]}`,
			map[string]string{admitted: `{"check_runs":[]}`}, nil,
			"Delivering", "No deploy has started", []string{"POST /repos/example/ext/check-runs"}, "queued"},
		{"a running composition is reported, not restarted", admitted, `{"workflow_runs":[{"id":61,"name":"Compose","status":"in_progress","created_at":"2030-01-05T00:01:00Z","html_url":"https://github.com/example/host/actions/runs/61"}]}`,
			map[string]string{admitted: `{"check_runs":[{"id":9,"conclusion":null}]}`}, nil,
			"Delivering", "Compose run 61 is running", []string{"PATCH /repos/example/ext/check-runs/9"}, "in_progress"},
		{"a composition waiting for approval", admitted, `{"workflow_runs":[{"id":62,"name":"Deploy","status":"waiting","created_at":"2030-01-05T00:01:00Z"}]}`,
			map[string]string{admitted: `{"check_runs":[]}`}, nil,
			"Delivering", "Deploy run 62 is waiting for approval", []string{"POST /repos/example/ext/check-runs"}, "in_progress"},
		// A deploy that passed before production is read running it is still on its way, never failed.
		{"a deploy that finished before production is read is still deploying", admitted, `{"workflow_runs":[{"id":63,"name":"Deploy","status":"completed","conclusion":"success","created_at":"2030-01-05T00:01:00Z"}]}`,
			map[string]string{admitted: `{"check_runs":[{"id":9,"status":"in_progress","conclusion":null}]}`}, nil,
			"Delivering", "Deploy run 63 finished", []string{"PATCH /repos/example/ext/check-runs/9"}, "in_progress"},
		{"a failed composition is degraded and never retried", admitted, `{"workflow_runs":[{"id":63,"name":"Compose","status":"completed","conclusion":"failure","created_at":"2030-01-05T00:01:00Z"}]}`,
			map[string]string{admitted: `{"check_runs":[]}`}, nil,
			"NotDelivered", "Compose run 63 failed", []string{"POST /repos/example/ext/check-runs"}, "completed"},
		{"a live commit is confirmed and announced once", running, `{"workflow_runs":[]}`,
			map[string]string{running: `{"check_runs":[{"id":10,"conclusion":"failure"}]}`},
			map[string]string{
				githubFakeAPI + " /repos/example/ext/commits/" + running + "/pulls":  `[{"number":4,"merged_at":null},{"number":5,"merged_at":"2030-01-01T00:00:00Z"}]`,
				githubFakeAPI + " /repos/example/ext/issues/5/comments?per_page=100": `[{"body":"thanks"}]`,
			},
			"Delivering", "live", []string{"PATCH /repos/example/ext/check-runs/10", "POST /repos/example/ext/issues/5/comments"}, "completed"},
		{"an announced commit is not announced again", running, `{"workflow_runs":[]}`,
			map[string]string{running: `{"check_runs":[{"id":10,"status":"in_progress","conclusion":null}]}`},
			map[string]string{
				githubFakeAPI + " /repos/example/ext/commits/" + running + "/pulls":  `[{"number":5,"merged_at":"2030-01-01T00:00:00Z"}]`,
				githubFakeAPI + " /repos/example/ext/issues/5/comments?per_page=100": `[{"body":"<!-- severino-hq-delivery:` + running + ` -->\nLive"}]`,
			},
			"Delivering", "live", []string{"PATCH /repos/example/ext/check-runs/10"}, "completed"},
	} {
		t.Run(test.name, func(t *testing.T) {
			routes := deliveryRoutes(test.admitted, test.pipeline)
			for sha, body := range test.checks {
				key, value := checkRunsRoute(sha, body)
				routes[key] = value
			}
			maps.Copy(routes, test.extra)
			h := deliveryHarness(t, routes)

			plan, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDelivery, "reconcile", deliverySpec, nil, false)
			if err != nil || len(h.writes()) != 0 {
				t.Fatalf("a plan writes nothing: %v %v", err, h.writes())
			}
			result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDelivery, "reconcile", deliverySpec, nil, true)
			if err != nil {
				t.Fatal(err)
			}
			if result.Conditions[0].Reason != test.reason || plan.Conditions[0].Reason != test.reason {
				t.Errorf("conditions %+v", result.Conditions)
			}
			status := result.Status.(GitHubDeliveryRecord)
			if status.Extensions[0].Stage != test.stage {
				t.Errorf("stage %q", status.Extensions[0].Stage)
			}
			if result.Changed != (len(test.writes) > 0) {
				t.Errorf("changed %v", result.Changed)
			}
			writes := []string{}
			for _, call := range h.writes() {
				writes = append(writes, call.method+" "+call.path)
				if strings.Contains(call.path, "/dispatches") {
					t.Errorf("HQ never starts a workflow: %s", call.path)
				}
				grant, _ := h.grantOf(call)
				if grant.Permissions.Actions != "" || !slices.Equal(grant.Repositories, []string{"ext"}) {
					t.Errorf("each write's token carries its own permission alone: %+v", grant)
				}
			}
			if !slices.Equal(writes, test.writes) {
				t.Fatalf("writes %v, want %v", writes, test.writes)
			}
			if len(test.writes) > 0 && test.checkState != "" {
				if body := h.writes()[0].body; body["status"] != test.checkState {
					t.Errorf("check body %v", body)
				}
			}
		})
	}
}

// A run from before the admission never carries it.
func TestGitHubDeliveryRunBeforeTheAdmissionDoesNotCarryIt(t *testing.T) {
	routes := deliveryRoutes(admitted, `{"workflow_runs":[{"id":59,"name":"Compose","status":"completed","conclusion":"success","created_at":"2030-01-04T23:59:59Z"},{"id":64,"name":"Compose","status":"queued","created_at":"2030-01-05T00:00:00Z"}]}`)
	key, value := checkRunsRoute(admitted, `{"check_runs":[]}`)
	routes[key] = value
	h := deliveryHarness(t, routes)
	records, err := h.r.githubDeliveryInventory(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	record := records[0].(GitHubDeliveryRecord)
	want := "ext: 2222222 is approved, production still runs 1111111. Compose run 64 is running."
	if record.Production != want || record.Extensions[0].Admitted != admitted || record.Workflow != githubCompose {
		t.Fatalf("%+v", record)
	}
	if report := checkReport(extensionDelivery{admitted: admitted, running: running, run: &githubapi.WorkflowRun{ID: 64, Status: "queued"}}, GitHubDeliverySpec{Repository: "example/host", Workflow: githubCompose}, ""); report.Status != "queued" || report.Title != "Deploy queued" || report.DetailsURL != "https://github.com/example/host/actions/workflows/compose.yml" {
		t.Fatalf("report %+v", report)
	}
}

// The latest admission is the one created last, wherever GitHub lists it: an
// older admission listed first, or beside the one production runs, is never
// read as the latest.
func TestGitHubDeliveryLatestAdmissionIsTheNewestListed(t *testing.T) {
	const older = "3333333333333333333333333333333333333333"
	admissions := githubFakeAPI + " /repos/example/ext/actions/workflows/admit.yml/runs?branch=main&per_page=20&status=success"
	run := func(id int, sha, created string) string {
		return `{"id":` + strconv.Itoa(id) + `,"head_sha":"` + sha + `","created_at":"` + created + `","updated_at":"` + created + `"}`
	}
	newest := run(52, running, "2030-01-05T00:00:00Z")
	old := run(40, older, "2029-12-01T00:00:00Z")
	sameMoment := run(51, older, "2030-01-05T00:00:00Z")
	for name, listed := range map[string]string{
		"an older admission listed first":         old + "," + newest,
		"an older admission listed after":         newest + "," + old,
		"an older admission of the same moment":   sameMoment + "," + newest,
		"older admissions on both sides":          old + "," + newest + "," + run(41, older, "2029-12-02T00:00:00Z"),
		"a moment that does not read loses by id": run(39, older, "") + "," + newest,
	} {
		t.Run(name, func(t *testing.T) {
			routes := deliveryRoutes(running, `{"workflow_runs":[]}`)
			routes[admissions] = `{"workflow_runs":[` + listed + `]}`
			key, value := checkRunsRoute(running, `{"check_runs":[{"id":8,"conclusion":"success"}]}`)
			routes[key] = value
			h := deliveryHarness(t, routes)
			result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDelivery, "reconcile", deliverySpec, nil, true)
			if err != nil {
				t.Fatal(err)
			}
			status := result.Status.(GitHubDeliveryRecord)
			if status.Extensions[0].Admitted != running || status.Extensions[0].Stage != "live" || result.Conditions[0].Reason != "Current" {
				t.Fatalf("production runs the newest admission: %+v %+v", status, result.Conditions)
			}
			if result.Message != "Up to date." || len(h.writes()) != 0 {
				t.Fatalf("nothing is reported for a current production: %q %v", result.Message, h.writes())
			}
		})
	}
}

// A result says in its own line whether anything is waiting on the owner.
func TestGitHubDeliveryResultNeverReadsAsDoneWhileNotDeployed(t *testing.T) {
	routes := deliveryRoutes(admitted, `{"workflow_runs":[{"id":63,"name":"Deploy","status":"completed","conclusion":"failure","created_at":"2030-01-05T00:01:00Z","html_url":"https://github.com/example/host/actions/runs/63"}]}`)
	key, value := checkRunsRoute(admitted, `{"check_runs":[]}`)
	routes[key] = value
	h := deliveryHarness(t, routes)
	result, err := h.r.runAction(t.Context(), runtime.ResourceKindGitHubDelivery, "reconcile", deliverySpec, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	want := "ext: 2222222 is approved, production still runs 1111111. Deploy run 63 failed. Re-run it on GitHub."
	if result.Message != "Not deployed." || result.Conditions[0].Message != want {
		t.Fatalf("%q %q", result.Message, result.Conditions[0].Message)
	}
	if status := result.Status.(GitHubDeliveryRecord); status.Extensions[0].RunURL != "https://github.com/example/host/actions/runs/63" || status.Extensions[0].StageState != stageFailed {
		t.Fatalf("the run to re-run is named: %+v", status.Extensions[0])
	}
}

// The extensions delivery follows are the ones HQ's registry names.
func TestTheRegistryNamesTheComposedExtensions(t *testing.T) {
	declared := runtime.ControllerRegistry{Extensions: []runtime.AdmittedExtension{{Plugin: "ext", SourceRepository: "example/ext"}}}
	env, err := runtime.ReadEnvironment([]string{"SEVERINO_HQ_SOURCE_REPOSITORY= example/host ", "HQ_CONTROLLER_IMAGE=ghcr.io/example/host:v1"})
	if err != nil {
		t.Fatal(err)
	}
	r := New(env, supplied(), &fakeHTTP{})
	composed := NewController(r, declared).composition()
	if composed.Repository != "example/host" || composed.Image != "ghcr.io/example/host:v1" || len(composed.Extensions) != 1 || composed.Extensions[0].Plugin != "ext" {
		t.Fatalf("%+v", composed)
	}
}

func TestGitHubDeliveryWithoutASourceRepositoryReadsNothing(t *testing.T) {
	h := newGitHubHarness(t, nil, runtime.Environment{})
	records, err := h.r.githubDeliveryInventory(t.Context())
	if err != nil || len(records) != 0 || len(h.recorded()) != 0 {
		t.Fatalf("%v %v %v", records, err, h.recorded())
	}
}

// The admission opens the check the controller moves on, under the same name,
// and the Deploy runs it reads wait for approval on the production
// environment, which is what a run's "waiting" status is.
func TestDeliveryNamesAgreeWithThePipeline(t *testing.T) {
	read := func(path string) string {
		t.Helper()
		data, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Fatal(err)
		}
		return string(data)
	}
	if !strings.Contains(read(".github/actions/admit-plugin/action.yml"), githubCheckName) {
		t.Errorf("the admission does not name the check %q", githubCheckName)
	}
	if !strings.Contains(read(githubDeploy), "environment:\n      name: production\n") {
		t.Errorf("%s has no job that waits on the production environment", githubDeploy)
	}
}

// A part of a repository the token already reached is refused as a feature the
// repository leaves off when GitHub answers forbidden or not found; any other
// answer keeps its own reason.
func TestRepositoryPartRefusalSaysWhatTheStatusMeans(t *testing.T) {
	for _, test := range []struct {
		status int
		off    bool
	}{{403, true}, {404, true}, {500, false}, {401, false}} {
		t.Run(strconv.Itoa(test.status), func(t *testing.T) {
			refused := asRepositoryRefusal(runtime.HTTPRefusal(test.status))
			if off := strings.HasPrefix(refused.Error(), "not available on this repository"); off != test.off {
				t.Fatalf("%d refused as %q", test.status, refused)
			}
		})
	}
}

// githubLeftOff is each part GitHub answers as a feature the repository's plan
// lacks or its settings leave off, as it answers for a private repository on a
// free plan: the path, the status and GitHub's message.
var githubLeftOff = []struct {
	part    runtime.ReadingPartName
	path    string
	status  int
	message string
}{
	{runtime.PartCodeScanning, "/code-scanning/alerts?state=open&per_page=100", http.StatusForbidden,
		"Code scanning is not enabled for this repository. Please enable code scanning in the repository settings."},
	{runtime.PartBranchRules, "/rules/branches/main", http.StatusForbidden,
		"Upgrade to GitHub Pro or make this repository public to enable this feature."},
	{runtime.PartLeakedCredentials, "/secret-scanning/alerts?state=open&per_page=100", http.StatusNotFound,
		"Secret scanning is disabled on this repository."},
}

func leaveOff(t *testing.T, routes map[string]string) *githubHarness {
	t.Helper()
	for _, off := range githubLeftOff {
		body, _ := json.Marshal(map[string]string{"message": off.message, "documentation_url": "https://docs.github.com/rest", "status": strconv.Itoa(off.status)})
		routes[githubFakeAPI+" /repos/example/app"+off.path] = string(body)
	}
	h := newGitHubHarness(t, routes, runtime.Environment{})
	for _, off := range githubLeftOff {
		h.refuse(githubFakeAPI, "/repos/example/app"+off.path, off.status)
	}
	return h
}

// A part GitHub leaves off is refused in GitHub's own words, so a plan without
// the feature or a setting that leaves it off is never read as a permission
// HQ's App lacks.
func TestARepositoryPartLeftOffSaysWhyInGitHubsWords(t *testing.T) {
	_, refused := readRepository(t, leaveOff(t, githubRepositoryRoutes()))
	said := map[runtime.ReadingPartName]runtime.RefusedPart{}
	for _, part := range refused {
		said[part.Part] = part
	}
	for _, off := range githubLeftOff {
		if got := said[off.part]; got.Reason != off.message || got.Refusal == runtime.FailureClassPermission || got.Scope != "example/app" {
			t.Errorf("%s refused %+v, want GitHub's %q", off.part, got, off.message)
		}
	}
	if len(refused) != len(githubLeftOff) {
		t.Errorf("refused %+v", refused)
	}
}

// An archived repository is read for what it is, and none of its parts are:
// GitHub scans it no more and HQ measures nothing of it, so nothing it leaves
// off is refused.
func TestAnArchivedRepositoryReadsNoParts(t *testing.T) {
	routes := githubRepositoryRoutes()
	at := githubFakeAPI + " /repos/example/app"
	routes[at] = strings.Replace(routes[at], `"archived":false`, `"archived":true`, 1)
	h := leaveOff(t, routes)
	record, refused := readRepository(t, h)
	if !record.Archived || record.Head.SHA == "" || len(refused) != 0 {
		t.Fatalf("record %+v refused %+v", record, refused)
	}
	for _, call := range h.recorded() {
		for _, off := range githubLeftOff {
			if path, _, _ := strings.Cut(off.path, "?"); call.path == "/repos/example/app"+path {
				t.Errorf("an archived repository's %s was read", off.part)
			}
		}
	}
}
