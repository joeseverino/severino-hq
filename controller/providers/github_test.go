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
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
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
}

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
	if host == githubFakeAPI && req.URL.Path != "/graphql" {
		recordVendorCall("github", req.Method, req.URL.Path)
	}
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
	if code := f.status[host+" "+key]; code != 0 {
		w.WriteHeader(code)
		io.WriteString(w, `{"message":"refused"}`)
		return
	}
	switch {
	case req.Method == "POST" && strings.HasSuffix(key, "/access_tokens"):
		var grant githubapi.AppsCreateInstallationAccessTokenJSONBody
		_ = json.Unmarshal(data, &grant)
		token := "minted-" + strconv.Itoa(len(f.grants)+1)
		f.grants[token] = grant
		io.WriteString(w, `{"token":"`+token+`","expires_at":"2030-01-01T00:00:00Z"}`)
		return
	case req.Method != "GET":
		io.WriteString(w, `{"id":1}`)
		return
	}
	answer, ok := f.routes[host+" "+key]
	if !ok {
		f.t.Errorf("unexpected GET %s %s", host, key)
		w.WriteHeader(http.StatusNotFound)
		return
	}
	io.WriteString(w, answer)
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
	}, status: map[string]int{}, grants: map[string]githubapi.AppsCreateInstallationAccessTokenJSONBody{}}
	maps.Copy(fake.routes, routes)
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	env := extra
	env.SSHDir = t.TempDir()
	held := supplied(githubAppConnection("hq-app", "12345", "synthetic"))
	h := &githubHarness{githubFake: fake}
	h.r = New(env, held, &runtime.HTTPClient{Transport: toFake{server}})
	h.r.Now = func() time.Time { return time.Unix(1_900_000_000, 0) }
	h.r.Commands = &Commands{Env: env, Supplied: held, Exec: func(context.Context, []string, []byte, []string) ([]byte, []byte, int, error) {
		return nil, nil, 0, errors.New("a GitHub App connection starts no process")
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
		{"an app id that is not a number", runtime.Environment{}, githubAppConnection("hq-app", "12a", "synthetic")},
		{"no app id", runtime.Environment{}, githubAppConnection("hq-app", "", "synthetic")},
		{"a key name that leaves the key directory", runtime.Environment{SSHDir: "/keys"}, githubAppConnection("../hq-app", "1", "synthetic")},
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
		if _, err := h.r.githubCall(ctx, c, "GET", "/repos/example/app", []string{"example/app"}, perms, nil); err != nil {
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
// write for Deploy recording production's deployment.
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
	for _, grant := range []githubapi.AppPermissions{githubRead, githubChecksWrite, githubPullsWrite, githubRenovate, {Actions: githubapi.AppPermissionsActionsWrite}, {Deployments: githubapi.AppPermissionsDeploymentsWrite}} {
		encoded, _ := json.Marshal(grant)
		var names map[string]string
		_ = json.Unmarshal(encoded, &names)
		for name, level := range names {
			if level == "write" || wanted[name] == "" {
				wanted[name] = level
			}
		}
	}
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

// githubRepositoryRoutes is one repository with a little of everything.
func githubRepositoryRoutes() map[string]string {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	workflow := base64.StdEncoding.EncodeToString([]byte("jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n      - uses: \"actions/setup-go@0123456789abcdef0123456789abcdef01234567\"\n      - uses: ./.github/actions/local\n      - uses: docker://alpine:3\n  call:\n    uses: example/shared/.github/workflows/reuse.yml@main\n"))
	composite := base64.StdEncoding.EncodeToString([]byte("runs:\n  steps:\n    - uses: actions/checkout@v4\n"))
	return map[string]string{
		api(""): `{"full_name":"example/app","private":true,"archived":true,"html_url":"https://github.com/example/app","default_branch":"main","pushed_at":"2030-01-02T00:00:00Z","visibility":"private",
			"security_and_analysis":{"secret_scanning":{"status":"enabled"},"advanced_security":{"status":"disabled"}}}`,
		api("/commits/main"): `{"sha":"aaaa000000000000000000000000000000000000","html_url":"https://github.com/example/app/commit/aaaa","author":{"login":"octo"},
			"commit":{"message":"Ship it\n\nbody","author":{"name":"Octo","date":"2030-01-01T00:00:00Z"},"committer":{"name":"Octo","date":"2030-01-02T00:00:00Z"}}}`,
		api("/actions/runs?per_page=50"): `{"workflow_runs":[
			{"id":3,"name":"CI renamed","workflow_id":11,"status":"waiting","conclusion":null,"event":"push","head_branch":"main","head_sha":"aaaa","html_url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"},
			{"id":2,"name":"CI","workflow_id":11,"status":"completed","conclusion":"success","event":"push","head_branch":"main","head_sha":"aaaa","created_at":"2030-01-02T00:00:00Z"},
			{"id":1,"name":"Gone","workflow_id":99,"status":"completed","conclusion":"success","event":"push","created_at":"2030-01-01T00:00:00Z"},
			{"id":4,"name":"Dependency graph","workflow_id":12,"status":"completed","event":"dynamic","created_at":"2030-01-04T00:00:00Z"}]}`,
		api("/commits/aaaa000000000000000000000000000000000000/check-runs?per_page=100"): `{"check_runs":[
			{"id":1,"name":"test","status":"completed","conclusion":"failure","started_at":"2030-01-01T00:00:00Z"},
			{"id":2,"name":"test","status":"completed","conclusion":"success","started_at":"2030-01-01T01:00:00Z"},
			{"id":3,"name":"lint","status":"in_progress","conclusion":null,"started_at":"2030-01-01T00:00:00Z"}]}`,
		api("/pulls?state=open&per_page=30"): `[{"number":6,"title":"Update example-lib to v2","html_url":"https://github.com/example/app/pull/6","user":{"login":"renovate[bot]"},"draft":false,
				"created_at":"2030-01-03T00:00:00Z","updated_at":"2030-01-04T00:00:00Z","head":{"sha":"eeee","ref":"renovate/example-lib-2.x"},"labels":[{"name":"dependencies"},{"name":"security"}]},
			{"number":5,"title":"Bump","html_url":"https://github.com/example/app/pull/5","user":{"login":"octo"},"draft":true,"created_at":"2030-01-01T00:00:00Z","updated_at":"2030-01-02T00:00:00Z","head":{"sha":"bbbb","ref":"bump"},"labels":[]}]`,
		api("/commits/eeee/check-runs?per_page=100"): `{"check_runs":[{"id":9,"name":"build","status":"completed","conclusion":"success"},{"id":10,"name":"test","status":"completed","conclusion":"failure"}]}`,
		api("/actions/workflows?per_page=100"):       `{"workflows":[{"id":11,"name":"CI","state":"active"},{"id":12,"name":"graph","state":"active"}]}`,
		api("/actions/runs/3/pending_deployments"):   `[{"environment":{"name":"production"}},{"environment":{}}]`,
		api("/releases?per_page=1"):                  `[{"tag_name":"v1.2.0","html_url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}]`,
		api("/deployments?per_page=20"): `[{"id":21,"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z"},
			{"id":20,"environment":"production","sha":"9999","created_at":"2030-01-01T00:00:00Z"}]`,
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
		api("/actions/runners"):                                    `{"runners":[{"name":"homelab","status":"online","busy":false,"labels":[{"name":"self-hosted"},{"name":"linux"}]}]}`,
		api("/actions/permissions"):                                `{"enabled":true,"allowed_actions":"selected","sha_pinning_required":true}`,
		api("/actions/permissions/workflow"):                       `{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}`,
		api("/vulnerability-alerts"):                               ``,
		api("/collaborators?per_page=100"):                         `[{"login":"octo","role_name":"admin"}]`,
		api("/keys?per_page=100"):                                  `[{"title":"deploy","read_only":true,"last_used":"2030-01-01T00:00:00Z","created_at":"2029-01-01T00:00:00Z"}]`,
		api("/actions/variables?per_page=100"):                     `{"variables":[{"name":"REGION","value":"never read"}]}`,
		api("/contents/.github/workflows?ref=main"):                `[{"name":"ci.yml","path":".github/workflows/ci.yml","type":"file"},{"name":"README.md","path":".github/workflows/README.md","type":"file"}]`,
		api("/contents/.github/actions?ref=main"):                  `[{"name":"local","path":".github/actions/local","type":"dir"}]`,
		api("/contents/.github/actions/local?ref=main"):            `[{"name":"action.yml","path":".github/actions/local/action.yml","type":"file"}]`,
		api("/contents/.github/workflows/ci.yml?ref=main"):         `{"type":"file","path":".github/workflows/ci.yml","content":"` + workflow[:20] + `\n` + workflow[20:] + `"}`,
		api("/contents/.github/actions/local/action.yml?ref=main"): `{"type":"file","path":".github/actions/local/action.yml","content":"` + composite + `"}`,
		githubFakeAPI + " /repos/actions/checkout/commits/v4":      `{"sha":"cccc000000000000000000000000000000000000"}`,
		githubFakeAPI + " /repos/example/shared/commits/main":      `{"sha":"dddd000000000000000000000000000000000000"}`,
	}
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

func TestGitHubRepositoryReading(t *testing.T) {
	routes := githubRepositoryRoutes()
	h := newGitHubHarness(t, routes, runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/secret-scanning/alerts?state=open&per_page=100", 403)
	record, refused := readRepository(t, h)

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
		{"each open pull request, with its checks read only for an update's", record.PullRequests, `[{"number":6,"title":"Update example-lib to v2","url":"https://github.com/example/app/pull/6","author":"renovate[bot]","draft":false,"created_at":"2030-01-03T00:00:00Z","updated_at":"2030-01-04T00:00:00Z","head":"eeee","branch":"renovate/example-lib-2.x","labels":["dependencies","security"],"checks":{"state":"failure","total":2,"failing":["test"],"running":0,"names":["build","test"]}},` +
			`{"number":5,"title":"Bump","url":"https://github.com/example/app/pull/5","author":"octo","draft":true,"created_at":"2030-01-01T00:00:00Z","updated_at":"2030-01-02T00:00:00Z","head":"bbbb","branch":"bump","labels":[],"checks":null}]`},
		{"each open Dependabot alert, its package from the dependency or the vulnerability", record.DependabotAlerts, `[{"number":3,"package":"example-lib","ecosystem":"npm","manifest_path":"web/package.json","severity":"critical","first_patched_version":"2.0.1","url":"https://github.com/example/app/security/dependabot/3"},` +
			`{"number":2,"package":"example.com/unfixed","ecosystem":"go","manifest_path":"go.mod","severity":"high","first_patched_version":null,"url":"https://github.com/example/app/security/dependabot/2"}]`},
		{"each workflow that exists now once, under its name now", record.Runs, `[{"id":3,"name":"CI","status":"waiting","conclusion":"","event":"push","branch":"main","sha":"aaaa","url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"}]`},
		{"waiting", record.Waiting[0].Environments, `["production"]`},
		{"release", record.Release, `{"tag":"v1.2.0","url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}`},
		{"the newest deployment to each environment, with what its job verified", record.Deployments, `[{"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z","state":"success","url":"https://github.com/example/app/actions/runs/3/job/77?pr=1","verified":[{"name":"Verify signature","conclusion":"success"}]}]`},
		{"alerts by severity; a refused kind is absent, not zero", record.Alerts, `{"code_scanning":{"high":1,"warning":1},"dependabot":{"critical":1,"high":1}}`},
		{"live artifacts, soonest first", record.Artifacts, `[{"name":"soon","expires_at":"2030-02-01T00:00:00Z","sha":"aaaa"},{"name":"late","expires_at":"2030-03-01T00:00:00Z","sha":"aaaa"}]`},
		{"rules", record.Rules, `{"pull_request":true,"reviews":2,"required_checks":["lint","test"],"blocks_force_push":true,"blocks_deletion":true}`},
		{"environments", record.Environments, `[{"name":"production","reviewers":["octo","ops"],"wait_minutes":5,"branches":"protected"},{"name":"preview","reviewers":[],"wait_minutes":0,"branches":"any"}]`},
		{"runners", record.Runners, `[{"name":"homelab","status":"online","busy":false,"labels":["linux","self-hosted"]}]`},
		{"access", record.Access, `{"visibility":"private","collaborators":[{"login":"octo","role":"admin"}],"deploy_keys":[{"title":"deploy","read_only":true,"last_used":"2030-01-01T00:00:00Z","created_at":"2029-01-01T00:00:00Z"}],"allowed_actions":"selected","pinning_required":true,"token":"read","token_approves_reviews":false,"vulnerability_alerts":true,"security":{"advanced_security":"disabled","secret_scanning":"enabled"}}`},
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
	if record.ConnectionRef != "hq-app" || record.Repository != "example/app" || !record.Private || !record.Archived || record.DefaultBranch != "main" {
		t.Errorf("record %+v", record)
	}
	// A 403 under a token that holds every read permission is a feature the
	// repository does not offer, never a permission to grant.
	if len(refused) != 1 || refused[0].Part != runtime.PartLeakedCredentials || refused[0].Scope != "example/app" || refused[0].Refusal == runtime.FailureClassPermission || !strings.HasPrefix(refused[0].Reason, "not available on this repository") {
		t.Fatalf("refused %+v", refused)
	}
	resolved := 0
	for _, call := range h.recorded() {
		grant, minted := h.grantOf(call)
		if strings.HasPrefix(call.path, "/repos/actions/checkout/commits/") {
			resolved++
		}
		if !minted || call.method != "GET" || strings.HasPrefix(call.path, "/app") || strings.HasSuffix(call.path, "/installation") || strings.HasPrefix(call.path, githubRepositoriesPage) {
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

func TestGitHubRepositoryParts(t *testing.T) {
	for _, test := range []struct {
		name    string
		status  map[string]int
		part    runtime.ReadingPartName
		check   func(GitHubRepositoryRecord) bool
		failure runtime.FailureClass
	}{
		{"workflows it may not read are that part refused, not no pins",
			map[string]int{"/contents/.github/workflows?ref=main": 500}, runtime.PartWorkflowPins,
			func(r GitHubRepositoryRecord) bool { return r.Pins == nil && r.CalledWorkflows == nil }, runtime.FailureClassUnclassified},
		{"a repository without workflows has nothing to pin",
			map[string]int{"/contents/.github/workflows?ref=main": 404, "/contents/.github/actions?ref=main": 404}, "",
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
	_, err := githubPaged(func(string) ([]string, error) { read++; return page, nil })
	if err == nil || read != githubPageLimit {
		t.Fatalf("read %d pages: %v", read, err)
	}
}

// The mint takes everything it needs as arguments: the token it asks for
// names exactly its scope, under a JWT the app's key signed.
func TestMintGitHubTokenNamesItsScope(t *testing.T) {
	h := newGitHubHarness(t, nil, runtime.Environment{})
	app := GitHubApp{ID: "12345", Key: h.key}
	minted, err := MintGitHubToken(t.Context(), h.r.HTTP, h.r.Now(), app, 7, []string{"app"}, githubChecksRead)
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
	if _, err := MintGitHubToken(t.Context(), h.r.HTTP, h.r.Now(), app, 7, []string{"app"}, githubapi.AppPermissions{}); err == nil || len(h.recorded()) != 1 {
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
func deliveryRoutes(admitted string, pipeline string) map[string]string {
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

			plan, err := h.r.runAction(runtime.ResourceKindGitHubDelivery, "reconcile", t.Context(), deliverySpec, nil, false)
			if err != nil || len(h.writes()) != 0 {
				t.Fatalf("a plan writes nothing: %v %v", err, h.writes())
			}
			result, err := h.r.runAction(runtime.ResourceKindGitHubDelivery, "reconcile", t.Context(), deliverySpec, nil, true)
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
			result, err := h.r.runAction(runtime.ResourceKindGitHubDelivery, "reconcile", t.Context(), deliverySpec, nil, true)
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
	routes := deliveryRoutes(admitted, `{"workflow_runs":[{"id":63,"name":"Deploy","status":"completed","conclusion":"success","created_at":"2030-01-05T00:01:00Z","html_url":"https://github.com/example/host/actions/runs/63"}]}`)
	key, value := checkRunsRoute(admitted, `{"check_runs":[]}`)
	routes[key] = value
	h := deliveryHarness(t, routes)
	result, err := h.r.runAction(runtime.ResourceKindGitHubDelivery, "reconcile", t.Context(), deliverySpec, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	want := "ext: 2222222 is approved, production still runs 1111111. Deploy run 63 finished without deploying it. Re-run it on GitHub."
	if result.Message != "Not deployed." || result.Conditions[0].Message != want {
		t.Fatalf("%q %q", result.Message, result.Conditions[0].Message)
	}
	if status := result.Status.(GitHubDeliveryRecord); status.Extensions[0].RunURL != "https://github.com/example/host/actions/runs/63" {
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
