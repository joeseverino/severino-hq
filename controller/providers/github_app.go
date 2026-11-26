package providers

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/internal/mask"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A GitHub App as a connection. The app's private key is rendered beside the
// SSH identities, root's and readable by this account alone, and read only to
// sign the JWT.
// Every installation token is minted for exactly the repositories and
// permissions a call names, and reused only by calls naming the same ones
// within the same sweep.

const (
	// githubAPI is the server GitHub's REST description names.
	githubAPI        = githubapi.ServerURL
	githubAPIVersion = "2022-11-28"
	// GitHub refuses a JWT that expires more than ten minutes out, or one
	// issued in the future by a skewed clock; backdating absorbs the skew.
	githubJWTBackdate = 60 * time.Second
	githubJWTLifetime = 540 * time.Second
)

// githubConnection is one GitHub App connection: the ref its signing key is
// rendered under and the app it signs for.
type githubConnection struct {
	ref   string
	appID string
}

func (r *Registry) admitGitHub() {
	r.probe(runtime.ConnectionProviderGitHubApp, r.githubProbe)
	r.reader(runtime.ResourceKindGitHubRepository, r.githubRepositories)
	r.reader(runtime.ResourceKindGitHubDelivery, r.githubDeliveryInventory)
	r.reader(runtime.ResourceKindGitHubWebhook, r.githubWebhook)
	r.tally(runtime.ResourceKindGitHubAllowance, r.githubAllowances)
	act(r, runtime.ResourceKindGitHubDelivery, "reconcile", r.githubDeliveryReconcile)
	act(r, runtime.ResourceKindGitHubPullRequest, "merge", r.githubMerge)
	act(r, runtime.ResourceKindGitHubDashboardBox, "reconcile", r.githubDashboardBox)
}

func (r *Registry) githubConnection(ref string) (githubConnection, error) {
	connection, err := r.Supplied.For(runtime.ConnectionProviderGitHubApp, ref)
	if err != nil {
		return githubConnection{}, err
	}
	app, err := runtime.Need(connection.GitHubApp)
	if err != nil {
		return githubConnection{}, err
	}
	if _, err := strconv.ParseUint(app.AppID, 10, 64); err != nil {
		return githubConnection{}, &ProviderError{Message: "GitHub App app_id is not a number"}
	}
	return githubConnection{ref: connection.Ref, appID: app.AppID}, nil
}

// signingKey is the path a connection's key is rendered at, reachable only
// under the connection's own name.
func (r *Registry) signingKey(ref string, public bool) (string, error) {
	if ref == "" || strings.ContainsAny(ref, `/\`) || strings.HasPrefix(ref, ".") {
		return "", &ProviderError{Message: "invalid signing connection"}
	}
	if _, ok := r.Supplied.Get(ref); !ok {
		return "", &ProviderError{Message: "signing connection " + ref, Err: runtime.ErrNoSuchConnection}
	}
	dir := r.Env.SSHDir
	if dir == "" {
		return "", &ProviderError{Err: runtime.ErrSettingMissing}
	}
	name := ref + ".key"
	if public {
		name += ".pub"
	}
	return filepath.Join(dir, name), nil
}

func base64URL(data []byte) string { return base64.RawURLEncoding.EncodeToString(data) }

// GitHubApp is what signs as a GitHub App: its id and its private key.
type GitHubApp struct {
	ID  string
	Key *rsa.PrivateKey
}

// jwt is a short-lived JWT naming the app, signed RS256 with its key.
func (app GitHubApp) jwt(now time.Time) (string, error) {
	header, _ := json.Marshal(map[string]string{"alg": "RS256", "typ": "JWT"})
	claims, _ := json.Marshal(struct {
		Expires  int64  `json:"exp"`
		IssuedAt int64  `json:"iat"`
		Issuer   string `json:"iss"`
	}{now.Add(githubJWTLifetime).Unix(), now.Add(-githubJWTBackdate).Unix(), app.ID})
	input := base64URL(header) + "." + base64URL(claims)
	digest := sha256.Sum256([]byte(input))
	signature, err := rsa.SignPKCS1v15(rand.Reader, app.Key, crypto.SHA256, digest[:])
	if err != nil {
		return "", &ProviderError{Message: "the JWT for GitHub App " + app.ID + " could not be signed"}
	}
	return input + "." + base64URL(signature), nil
}

// githubNoGrant is a permission set naming nothing, as the token request encodes it.
const githubNoGrant = "{}"

// mintGitHubToken is an installation token for exactly these repositories
// (names within the installation's account) and permissions, with the moment
// GitHub says it expires. No repositories is every repository the installation
// covers; no permissions is refused, since GitHub would grant all of the
// installation's.
func (r *Registry) mintGitHubToken(ctx context.Context, app GitHubApp, installation int, repositories []string, permissions githubapi.AppPermissions) (githubapi.InstallationToken, error) {
	if grant, _ := json.Marshal(permissions); string(grant) == githubNoGrant {
		return githubapi.InstallationToken{}, &ProviderError{Message: "a GitHub token names its permissions"}
	}
	jwt, err := app.jwt(r.Now())
	body := githubapi.AppsCreateInstallationAccessTokenJSONRequestBody{Repositories: repositories, Permissions: permissions}
	token, err := json201(r.githubClient(jwt, false, err).AppsCreateInstallationAccessTokenWithResponse(ctx, strconv.Itoa(installation), body))
	if err != nil {
		return githubapi.InstallationToken{}, err
	}
	if token.Token == "" {
		return githubapi.InstallationToken{}, &ProviderError{Message: "GitHub did not issue an installation token"}
	}
	mask.Learn(token.Token)
	return token, nil
}

// githubApp is the app a connection signs as, its key read from where the
// connection's name points.
func (r *Registry) githubApp(c githubConnection) (GitHubApp, error) {
	path, err := r.signingKey(c.ref, false)
	if err != nil {
		return GitHubApp{}, err
	}
	key, err := readSigningKey(path)
	if err != nil {
		return GitHubApp{}, &ProviderError{Message: "the signing key for " + c.ref + " cannot be read"}
	}
	return GitHubApp{ID: c.appID, Key: key}, nil
}

// githubJWT is a short-lived JWT naming the connection's app.
func (r *Registry) githubJWT(_ context.Context, c githubConnection) (string, error) {
	app, err := r.githubApp(c)
	if err != nil {
		return "", err
	}
	return app.jwt(r.Now())
}

// readSigningKey is the RSA key a PKCS#8 PEM file holds, as the secrets
// renderer writes it.
func readSigningKey(path string) (*rsa.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil || block.Type != "PRIVATE KEY" {
		return nil, errors.New("not a PKCS#8 PEM key")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("not an RSA key")
	}
	return key, nil
}

// githubHeaders is what every request to GitHub's API sends, under a bearer
// when it has one.
func githubHeaders(bearer string) map[string]string {
	headers := map[string]string{
		"Accept":               "application/vnd.github+json",
		"X-GitHub-Api-Version": githubAPIVersion,
		"User-Agent":           githubUserAgent,
	}
	if bearer != "" {
		headers["Authorization"] = "Bearer " + bearer
	}
	return headers
}

// githubClient is GitHub's generated client under one bearer: an
// installation token, the app's JWT, or none for an anonymous read. Every
// call fails with err when there is one. Under a token that spends, each call
// passes the fuse of the installation it spends.
func (r *Registry) githubClient(bearer string, spends bool, err error) *githubapi.ClientWithResponses {
	fuse := ""
	if spends {
		fuse = bearer
	}
	doer := vendorDoer{r: r, err: err, headers: githubHeaders(bearer), exchange: r.githubExchange(fuse)}
	return &githubapi.ClientWithResponses{ClientInterface: &githubapi.Client{Server: githubAPI + "/", Client: doer}}
}

// githubAsApp is the client authenticated as the app itself, on the app's
// own allowance rather than an installation's.
func (r *Registry) githubAsApp(ctx context.Context, c githubConnection) *githubapi.ClientWithResponses {
	jwt, err := r.githubJWT(ctx, c)
	return r.githubClient(jwt, false, err)
}

// githubAs is the client under a token scoped to exactly these repositories
// and permissions.
func (r *Registry) githubAs(ctx context.Context, c githubConnection, repositories []string, permissions githubapi.AppPermissions) *githubapi.ClientWithResponses {
	token, err := r.githubToken(ctx, c, repositories, permissions)
	return r.githubClient(token, true, err)
}

// githubRepositoryName is (owner, repository) from owner/repository.
func githubRepositoryName(name string) (string, string, error) {
	owner, repo, _ := strings.Cut(strings.TrimSpace(name), "/")
	if owner == "" || repo == "" || strings.Contains(repo, "/") {
		return "", "", &ProviderError{Message: fmt.Sprintf("%q is not an owner/repository name", name)}
	}
	return owner, repo, nil
}

func installationKey(c githubConnection, owner, repo string) string {
	return "github_app.installation|" + c.ref + "|" + owner + "|" + repo
}

// githubInstallation is the installation covering one repository, asked of
// GitHub once per sweep.
func (r *Registry) githubInstallation(ctx context.Context, c githubConnection, owner, repo string) (int, error) {
	raw, err := cached(ctx, r, installationKey(c, owner, repo), func() (json.RawMessage, error) {
		installation, err := json200(r.githubAsApp(ctx, c).AppsGetRepoInstallationWithResponse(ctx, owner, repo))
		if err != nil {
			return nil, err
		}
		if installation.ID == 0 {
			return nil, &ProviderError{Message: "the GitHub App is not installed on " + owner + "/" + repo}
		}
		return json.Marshal(installation.ID)
	})
	if err != nil {
		return 0, err
	}
	var id int
	err = json.Unmarshal(raw, &id)
	return id, err
}

// githubMint is an installation token the connection's app mints for its scope.
func (r *Registry) githubMint(ctx context.Context, c githubConnection, installation int, repositories []string, permissions githubapi.AppPermissions) (string, error) {
	app, err := r.githubApp(c)
	if err != nil {
		return "", err
	}
	token, err := r.mintGitHubToken(ctx, app, installation, repositories, permissions)
	if err == nil {
		r.githubSpend().minted(token.Token, c.ref, installation)
	}
	return token.Token, err
}

// githubToken is a token for exactly these repositories and permissions,
// minted once per sweep for each distinct scope. Every repository shares one
// owner: a token spanning two installations is two accounts' authority.
func (r *Registry) githubToken(ctx context.Context, c githubConnection, repositories []string, permissions githubapi.AppPermissions) (string, error) {
	names := slices.Compact(slices.Sorted(slices.Values(repositories)))
	grant, _ := json.Marshal(permissions)
	if len(names) == 0 || string(grant) == githubNoGrant {
		return "", &ProviderError{Message: "a GitHub token names its repositories and its permissions"}
	}
	owners := map[string]bool{}
	repos := make([]string, len(names))
	for i, name := range names {
		owner, repo, err := githubRepositoryName(name)
		if err != nil {
			return "", err
		}
		owners[owner], repos[i] = true, repo
	}
	if len(owners) != 1 {
		return "", &ProviderError{Message: "one GitHub token covers one account's repositories"}
	}
	owner, _, _ := githubRepositoryName(names[0])
	key := "github_app.token|" + c.ref + "|" + strings.Join(names, ",") + "|" + string(grant)
	raw, err := cached(ctx, r, key, func() (json.RawMessage, error) {
		installation, err := r.githubInstallation(ctx, c, owner, repos[0])
		if err != nil {
			return nil, err
		}
		token, err := r.githubMint(ctx, c, installation, repos, permissions)
		if err != nil {
			return nil, err
		}
		return json.Marshal(token)
	})
	if err != nil {
		return "", err
	}
	var token string
	err = json.Unmarshal(raw, &token)
	return token, err
}

// githubInstallationRepositories is every repository the app's installations
// cover, as owner/name: choosing repositories on GitHub decides what HQ reads.
// Each installation is listed under a token that can read metadata only, and
// the installation found for each repository is kept for the sweep.
func (r *Registry) githubInstallationRepositories(ctx context.Context, c githubConnection) ([]string, error) {
	raw, err := cached(ctx, r, "github_app.repositories|"+c.ref, func() (json.RawMessage, error) {
		installations, err := r.githubInstallations(ctx, c)
		if err != nil {
			return nil, err
		}
		found := []string{}
		for _, installation := range installations {
			if installation.ID == 0 {
				continue
			}
			if account, err := installation.Account.AsSimpleUser(); err == nil {
				r.githubSpend().named(installation.ID, account.Login)
			}
			token, err := r.githubMint(ctx, c, installation.ID, nil, githubapi.AppPermissions{Metadata: githubapi.AppPermissionsMetadataRead})
			if err != nil {
				return nil, err
			}
			client := r.githubClient(token, true, nil)
			repositories, err := githubPaged(func(page int) ([]string, error) {
				listed, err := json200(client.AppsListReposAccessibleToInstallationWithResponse(ctx, &githubapi.AppsListReposAccessibleToInstallationParams{PerPage: new(githubListPage), Page: new(page)}))
				names := make([]string, len(listed.Repositories))
				for i, repository := range listed.Repositories {
					names[i] = repository.FullName
				}
				return names, err
			})
			if err != nil {
				return nil, err
			}
			for _, name := range repositories {
				owner, repo, err := githubRepositoryName(name)
				if err != nil {
					continue
				}
				found = append(found, name)
				id := installation.ID
				_, _ = cached(ctx, r, installationKey(c, owner, repo), func() (json.RawMessage, error) { return json.Marshal(id) })
			}
		}
		return json.Marshal(slices.Compact(slices.Sorted(slices.Values(found))))
	})
	if err != nil {
		return nil, err
	}
	var names []string
	err = json.Unmarshal(raw, &names)
	return names, err
}

// githubInstallations is every installation of the connection's app.
func (r *Registry) githubInstallations(ctx context.Context, c githubConnection) ([]githubapi.Installation, error) {
	client := r.githubAsApp(ctx, c)
	return githubPaged(func(page int) ([]githubapi.Installation, error) {
		return json200(client.AppsListInstallationsWithResponse(ctx, &githubapi.AppsListInstallationsParams{PerPage: new(githubListPage), Page: new(page)}))
	})
}

// githubPageLimit bounds a paged list: a list that never comes back short is
// refused rather than followed.
const githubPageLimit = 100

// githubPaged is every item of a paged GitHub list, read a full page at a time
// until a page comes back short. read takes the page's number and asks for
// githubListPage items.
func githubPaged[T any](read func(page int) ([]T, error)) ([]T, error) {
	found := []T{}
	for page := 1; page <= githubPageLimit; page++ {
		items, err := read(page)
		if err != nil {
			return nil, err
		}
		found = append(found, items...)
		if len(items) < githubListPage {
			return found, nil
		}
	}
	return nil, &ProviderError{Message: fmt.Sprintf("GitHub listed more than %d pages", githubPageLimit)}
}

// sshKeyFingerprint is a public key's fingerprint as GitHub lists an app's
// keys: SHA256 of the DER public key, base64.
func sshKeyFingerprint(authorized string) (string, error) {
	key, _, _, _, err := ssh.ParseAuthorizedKey([]byte(strings.TrimSpace(authorized)))
	if err != nil {
		return "", &ProviderError{Message: "GitHub App public key could not be read", Err: err}
	}
	public, ok := key.(ssh.CryptoPublicKey)
	if !ok {
		return "", &ProviderError{Message: "GitHub App public key could not be read"}
	}
	der, err := x509.MarshalPKIXPublicKey(public.CryptoPublicKey())
	if err != nil {
		return "", &ProviderError{Message: "GitHub App public key could not be read", Err: err}
	}
	digest := sha256.Sum256(der)
	return "SHA256:" + base64.StdEncoding.EncodeToString(digest[:]), nil
}

// githubBot is the login GitHub gives the app's bot user, whose name its
// installation tokens act under: its slug with "[bot]" appended, as
// Renovate's GitHub platform documents its username.
func (r *Registry) githubBot(ctx context.Context, c githubConnection) (string, error) {
	app, err := json200(r.githubAsApp(ctx, c).AppsGetAuthenticatedWithResponse(ctx))
	if err != nil {
		return "", err
	}
	if app.Slug == "" {
		return "", &ProviderError{Message: "GitHub named no slug for the app"}
	}
	return app.Slug + "[bot]", nil
}

// githubProbe is whether the key still signs for the app, and which accounts
// it is installed on.
func (r *Registry) githubProbe(ctx context.Context, ref string) (ProbeResult, error) {
	c, err := r.githubConnection(ref)
	if err != nil {
		return ProbeResult{}, err
	}
	app, err := json200(r.githubAsApp(ctx, c).AppsGetAuthenticatedWithResponse(ctx))
	if err != nil {
		return ProbeResult{}, err
	}
	if app.Slug == "" {
		return ProbeResult{}, &ProviderError{Message: "GitHub did not accept the app's signature", Failure: runtime.FailureClassCredential}
	}
	installations, err := r.githubInstallations(ctx, c)
	if err != nil {
		return ProbeResult{}, err
	}
	accounts := []string{}
	for _, installation := range installations {
		if account, err := installation.Account.AsSimpleUser(); err == nil && account.Login != "" {
			accounts = append(accounts, account.Login)
		}
	}
	slices.Sort(accounts)
	path, err := r.signingKey(c.ref, true)
	if err != nil {
		return ProbeResult{}, err
	}
	public, err := os.ReadFile(path)
	if err != nil {
		return ProbeResult{}, &ProviderError{Message: "no signing key was written for " + c.ref, Err: err}
	}
	fingerprint, err := sshKeyFingerprint(string(public))
	if err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: "GitHub App " + app.Slug + ", key " + fingerprint, Reaches: accounts}, nil
}
