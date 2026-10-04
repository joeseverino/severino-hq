package providers

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
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
	r        *Registry
	signedMu sync.Mutex
	signed   [][]string // argv of every openssl call, then its input
}

func newGitHubHarness(t *testing.T, routes map[string]string, extra runtime.Environment) *githubHarness {
	t.Helper()
	fake := &githubFake{t: t, routes: map[string]string{
		githubFakeAPI + " /app/installations":                      `[{"id":7,"account":{"login":"example"}}]`,
		githubFakeAPI + " /installation/repositories?per_page=100": `{"total_count":1,"repositories":[{"full_name":"example/app"}]}`,
		githubFakeAPI + " /repos/example/app/installation":         `{"id":7}`,
		githubFakeAPI + " /repos/example/host/installation":        `{"id":7}`,
		githubFakeAPI + " /repos/example/ext/installation":         `{"id":7}`,
	}, status: map[string]int{}, grants: map[string]githubapi.AppsCreateInstallationAccessTokenJSONBody{}}
	maps.Copy(fake.routes, routes)
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	env := runtime.Environment{"GITHUB_APP_CONNECTION_REF": "hq-app", "GITHUB_APP_APP_ID": "12345", "HQ_CONTROLLER_SSH_DIR": t.TempDir()}
	maps.Copy(env, extra)
	h := &githubHarness{githubFake: fake}
	h.r = New(env, &runtime.HTTPClient{Transport: toFake{server}})
	h.r.Now = func() time.Time { return time.Unix(1_900_000_000, 0) }
	h.r.Commands = &Commands{Env: env, Exec: func(_ context.Context, argv []string, stdin []byte, _ []string) ([]byte, []byte, int, error) {
		if argv[0] != "openssl" {
			return nil, nil, 0, errors.New("unexpected command")
		}
		h.signedMu.Lock()
		h.signed = append(h.signed, append(append([]string{}, argv...), string(stdin)))
		h.signedMu.Unlock()
		return []byte("signature"), nil, 0, nil
	}}
	return h
}

func TestGitHubAppJWTIsSignedByOpenSSLWithTheConnectionsKey(t *testing.T) {
	h := newGitHubHarness(t, nil, nil)
	c, err := h.r.githubConnection("")
	if err != nil {
		t.Fatal(err)
	}
	jwt, err := h.r.githubJWT(t.Context(), c)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 || parts[2] != base64URL([]byte("signature")) {
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
	h.signedMu.Lock()
	argv := h.signed[0]
	h.signedMu.Unlock()
	want := []string{"openssl", "dgst", "-sha256", "-sign", filepath.Join(h.r.Env["HQ_CONTROLLER_SSH_DIR"], "hq-app.key")}
	if !slices.Equal(argv[:5], want) || argv[5] != parts[0]+"."+parts[1] {
		t.Fatalf("signed %v", argv)
	}
}

func TestGitHubConnectionRefusals(t *testing.T) {
	for _, test := range []struct {
		name string
		env  runtime.Environment
	}{
		{"an app id that is not a number", runtime.Environment{"GITHUB_APP_CONNECTION_REF": "hq-app", "GITHUB_APP_APP_ID": "12a"}},
		{"no app id", runtime.Environment{"GITHUB_APP_CONNECTION_REF": "hq-app"}},
		{"a key name that leaves the key directory", runtime.Environment{"GITHUB_APP_CONNECTION_REF": "../hq-app", "GITHUB_APP_APP_ID": "1", "HQ_CONTROLLER_SSH_DIR": "/keys"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := New(test.env, &fakeHTTP{})
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
	h := newGitHubHarness(t, map[string]string{githubFakeAPI + " /repos/example/app": `{}`}, nil)
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
	h := newGitHubHarness(t, map[string]string{githubFakeAPI + " /app": `{"slug":"hq-example"}`}, nil)
	if err := os.WriteFile(filepath.Join(h.r.Env["HQ_CONTROLLER_SSH_DIR"], "hq-app.key.pub"), ssh.MarshalAuthorizedKey(key), 0o600); err != nil {
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
// plus Actions write for an admission starting the host's composition.
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
	for _, grant := range []githubapi.AppPermissions{githubRead, githubChecksWrite, githubPullsWrite, {Actions: githubapi.AppPermissionsActionsWrite}} {
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
		api(""): `{"full_name":"example/app","private":true,"html_url":"https://github.com/example/app","default_branch":"main","pushed_at":"2030-01-02T00:00:00Z","visibility":"private",
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
		api("/pulls?state=open&per_page=30"):         `[{"number":5,"title":"Bump","html_url":"https://github.com/example/app/pull/5","user":{"login":"bot"},"draft":false,"updated_at":"2030-01-02T00:00:00Z","head":{"sha":"bbbb"}}]`,
		api("/commits/bbbb/check-runs?per_page=100"): `{"check_runs":[{"id":9,"name":"build","status":"completed","conclusion":"success"}]}`,
		api("/actions/workflows?per_page=100"):       `{"workflows":[{"id":11,"name":"CI","state":"active"},{"id":12,"name":"graph","state":"active"}]}`,
		api("/actions/runs/3/pending_deployments"):   `[{"environment":{"name":"production"}},{"environment":{}}]`,
		api("/releases?per_page=1"):                  `[{"tag_name":"v1.2.0","html_url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}]`,
		api("/deployments?per_page=20"): `[{"id":21,"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z"},
			{"id":20,"environment":"production","sha":"9999","created_at":"2030-01-01T00:00:00Z"}]`,
		api("/deployments/21/statuses?per_page=1"):             `[{"state":"success","log_url":"https://github.com/example/app/actions/runs/3/job/77?pr=1","target_url":""}]`,
		api("/actions/jobs/77"):                                `{"id":77,"steps":[{"name":"Verify signature","conclusion":"success"},{"name":"Deploy","conclusion":"success"}]}`,
		api("/code-scanning/alerts?state=open&per_page=100"):   `[{"rule":{"security_severity_level":"high","severity":"error"}},{"rule":{"severity":"warning"}}]`,
		api("/dependabot/alerts?state=open&per_page=100"):      `[{"security_advisory":{"severity":"critical"}}]`,
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
		api("/automated-security-fixes"):                           `{"enabled":true,"paused":false}`,
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
	h := newGitHubHarness(t, routes, nil)
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
		{"pull request checks", record.PullRequestChecks, `["build"]`},
		{"each workflow that exists now once, under its name now", record.Runs, `[{"id":3,"name":"CI","status":"waiting","conclusion":"","event":"push","branch":"main","sha":"aaaa","url":"https://github.com/example/app/actions/runs/3","created_at":"2030-01-03T00:00:00Z"}]`},
		{"waiting", record.Waiting[0].Environments, `["production"]`},
		{"release", record.Release, `{"tag":"v1.2.0","url":"https://github.com/example/app/releases/v1.2.0","published_at":"2030-01-01T00:00:00Z"}`},
		{"the newest deployment to each environment, with what its job verified", record.Deployments, `[{"environment":"production","sha":"aaaa","created_at":"2030-01-02T00:00:00Z","state":"success","url":"https://github.com/example/app/actions/runs/3/job/77?pr=1","verified":[{"name":"Verify signature","conclusion":"success"}]}]`},
		{"alerts by severity; a refused kind is absent, not zero", record.Alerts, `{"code_scanning":{"high":1,"warning":1},"dependabot":{"critical":1}}`},
		{"live artifacts, soonest first", record.Artifacts, `[{"name":"soon","expires_at":"2030-02-01T00:00:00Z","sha":"aaaa"},{"name":"late","expires_at":"2030-03-01T00:00:00Z","sha":"aaaa"}]`},
		{"rules", record.Rules, `{"pull_request":true,"reviews":2,"required_checks":["lint","test"],"blocks_force_push":true,"blocks_deletion":true}`},
		{"environments", record.Environments, `[{"name":"production","reviewers":["octo","ops"],"wait_minutes":5,"branches":"protected"},{"name":"preview","reviewers":[],"wait_minutes":0,"branches":"any"}]`},
		{"runners", record.Runners, `[{"name":"homelab","status":"online","busy":false,"labels":["linux","self-hosted"]}]`},
		{"access", record.Access, `{"visibility":"private","collaborators":[{"login":"octo","role":"admin"}],"deploy_keys":[{"title":"deploy","read_only":true,"last_used":"2030-01-01T00:00:00Z","created_at":"2029-01-01T00:00:00Z"}],"allowed_actions":"selected","pinning_required":true,"token":"read","token_approves_reviews":false,"security_fixes":true,"security":{"advanced_security":"disabled","secret_scanning":"enabled"}}`},
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
	if record.ConnectionRef != "hq-app" || record.Repository != "example/app" || !record.Private || record.DefaultBranch != "main" {
		t.Errorf("record %+v", record)
	}
	// A 403 under a token that holds every read permission is a feature the
	// repository does not offer, never a permission to grant.
	if len(refused) != 1 || refused[0].Part != runtime.PartLeakedCredentials || refused[0].Scope != "example/app" || refused[0].Refusal == runtime.FailureClassPermission || !strings.HasPrefix(refused[0].Reason, "Not offered") {
		t.Fatalf("refused %+v", refused)
	}
	resolved := 0
	for _, call := range h.recorded() {
		grant, minted := h.grantOf(call)
		if strings.HasPrefix(call.path, "/repos/actions/checkout/commits/") {
			resolved++
		}
		if !minted || call.method != "GET" || strings.HasPrefix(call.path, "/app") || strings.HasSuffix(call.path, "/installation") || call.path == "/installation/repositories?per_page=100" {
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
		{"access refused whole", map[string]int{"/keys?per_page=100": 404}, runtime.PartAccess,
			func(r GitHubRepositoryRecord) bool { return r.Access == nil }, runtime.FailureClassUnclassified},
		{"a refused credential keeps its class", map[string]int{"/actions/variables?per_page=100": 401}, runtime.PartVariables,
			func(r GitHubRepositoryRecord) bool { return r.Variables == nil }, runtime.FailureClassCredential},
	} {
		t.Run(test.name, func(t *testing.T) {
			h := newGitHubHarness(t, githubRepositoryRoutes(), nil)
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
	h := newGitHubHarness(t, githubRepositoryRoutes(), nil)
	h.refuse(githubFakeAPI, "/repos/example/app/commits/main", 502)
	report := NewController(h.r, runtime.ControllerRegistry{}).readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if report.OK {
		t.Fatal("a repository whose head cannot be read fails the reading, never reads as empty")
	}
}

func TestGitHubImagesOfTheComposition(t *testing.T) {
	routes := githubRepositoryRoutes()
	routes[githubFakeRegistry+" /token?scope=repository%3Aexample%2Fapp%3Apull&service=ghcr.io"] = `{"token":"pull-app"}`
	routes[githubFakeRegistry+" /v2/example/app/tags/list?n=1000"] = `{"tags":["v1","v2","sha256-abc.sig","sha256-def.att","sha256-012.sig"]}`
	h := newGitHubHarness(t, routes, runtime.Environment{"SEVERINO_HQ_SOURCE_REPOSITORY": "example/app", "HQ_CONTROLLER_IMAGE": "ghcr.io/example/app-composed:v9@sha256:0000"})
	h.refuse(githubFakeRegistry, "/token?scope=repository%3Aexample%2Fapp-composed%3Apull&service=ghcr.io", 403)
	record, refused := readRepository(t, h)
	if encoded, _ := json.Marshal(record.Images); string(encoded) != `[{"name":"example/app","tags":2,"signed":["012","abc"]}]` {
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
		githubFakeAPI + " /repos/example/ext/actions/workflows/admit.yml/runs?branch=main&per_page=1&status=success": `{"workflow_runs":[{"id":50,"head_sha":"` + admitted + `","updated_at":"2030-01-05T00:00:00Z"}]}`,
		githubFakeAPI + " /repos/example/host/actions/workflows/compose.yml/runs?branch=main&per_page=20":            pipeline,
		githubFakeAPI + " /repos/example/host/actions/workflows/deploy.yml/runs?branch=main&per_page=20":             `{"workflow_runs":[]}`,
	}
}

const running = "1111111111111111111111111111111111111111"
const admitted = "2222222222222222222222222222222222222222"

func deliveryHarness(t *testing.T, routes map[string]string) *githubHarness {
	h := newGitHubHarness(t, routes, runtime.Environment{"SEVERINO_HQ_SOURCE_REPOSITORY": "example/host", "HQ_CONTROLLER_IMAGE": "ghcr.io/example/host:v1"})
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
			"Delivering", "no composition has started", []string{"POST /repos/example/ext/check-runs"}, "queued"},
		{"a running composition is reported, not restarted", admitted, `{"workflow_runs":[{"id":61,"name":"Compose","status":"in_progress","created_at":"2030-01-05T00:01:00Z","html_url":"https://github.com/example/host/actions/runs/61"}]}`,
			map[string]string{admitted: `{"check_runs":[{"id":9,"conclusion":null}]}`}, nil,
			"Delivering", "Compose run 61 is running", []string{"PATCH /repos/example/ext/check-runs/9"}, "in_progress"},
		{"a composition waiting for approval", admitted, `{"workflow_runs":[{"id":62,"name":"Deploy","status":"waiting","created_at":"2030-01-05T00:01:00Z"}]}`,
			map[string]string{admitted: `{"check_runs":[]}`}, nil,
			"Delivering", "Deploy run 62 is waiting for deploy approval", []string{"POST /repos/example/ext/check-runs"}, "in_progress"},
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

// Regression: a run from before the admission never carries it.
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
	want := "ext 2222222 is admitted and production runs 1111111: Compose run 64 is running"
	if record.Production != want || record.Extensions[0].Admitted != admitted || record.Workflow != githubCompose {
		t.Fatalf("%+v", record)
	}
	if report := checkReport(extensionDelivery{admitted: admitted, running: running, run: &githubapi.WorkflowRun{ID: 64, Status: "queued"}}, GitHubDeliverySpec{Repository: "example/host", Workflow: githubCompose}, ""); report.Status != "queued" || report.Title != "Composition queued" || report.DetailsURL != "https://github.com/example/host/actions/workflows/compose.yml" {
		t.Fatalf("report %+v", report)
	}
}

// The extensions delivery follows are the ones HQ's registry names.
func TestTheRegistryNamesTheComposedExtensions(t *testing.T) {
	declared := runtime.ControllerRegistry{Extensions: []runtime.AdmittedExtension{{Plugin: "ext", SourceRepository: "example/ext"}}}
	r := New(runtime.Environment{"SEVERINO_HQ_SOURCE_REPOSITORY": " example/host ", "HQ_CONTROLLER_IMAGE": "ghcr.io/example/host:v1"}, &fakeHTTP{})
	composed := NewController(r, declared).composition()
	if composed.Repository != "example/host" || composed.Image != "ghcr.io/example/host:v1" || len(composed.Extensions) != 1 || composed.Extensions[0].Plugin != "ext" {
		t.Fatalf("%+v", composed)
	}
}

func TestGitHubDeliveryWithoutASourceRepositoryReadsNothing(t *testing.T) {
	h := newGitHubHarness(t, nil, nil)
	records, err := h.r.githubDeliveryInventory(t.Context())
	if err != nil || len(records) != 0 || len(h.recorded()) != 0 {
		t.Fatalf("%v %v %v", records, err, h.recorded())
	}
}

// The workflows and scripts that report delivery name the check and the
// comment marker the controller writes.
func TestDeliveryNamesAgreeWithThePipeline(t *testing.T) {
	read := func(path string) string {
		t.Helper()
		data, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Fatal(err)
		}
		return string(data)
	}
	for _, path := range []string{".github/workflows/deploy.yml", ".github/actions/admit-plugin/action.yml", "scripts/hq-report.sh"} {
		if !strings.Contains(read(path), githubCheckName) {
			t.Errorf("%s does not name the check %q", path, githubCheckName)
		}
	}
	marker, _, _ := strings.Cut(githubDeliveryMark, "%s")
	if !strings.Contains(read("scripts/hq-report.sh"), `marker="`+marker+"${COMMIT") {
		t.Errorf("hq-report.sh does not write the marker %q", marker)
	}
}
