package providers

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A GitHub App as a connection. The app's private key never reaches this
// process: it is rendered beside the SSH identities and openssl signs the JWT.
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
	act(r, runtime.ResourceKindGitHubDelivery, "reconcile", r.githubDeliveryReconcile)
}

func (r *Registry) githubConnection(ref string) (githubConnection, error) {
	prefix, err := r.Env.Prefix(runtime.ConnectionProviderGitHubApp, ref)
	if err != nil {
		return githubConnection{}, err
	}
	named, err := r.Env.Required(prefix, "CONNECTION_REF")
	if err != nil {
		return githubConnection{}, err
	}
	appID, err := r.Env.Required(prefix, "APP_ID")
	if err != nil {
		return githubConnection{}, err
	}
	appID = strings.TrimSpace(appID)
	if _, err := strconv.ParseUint(appID, 10, 64); err != nil {
		return githubConnection{}, &ProviderError{Message: "GitHub App app_id is not a number"}
	}
	return githubConnection{ref: named, appID: appID}, nil
}

// signingKey is the path a connection's key is rendered at, reachable only
// under the connection's own name.
func (r *Registry) signingKey(ref string, public bool) (string, error) {
	if ref == "" || strings.ContainsAny(ref, `/\`) || strings.HasPrefix(ref, ".") {
		return "", &ProviderError{Message: "invalid signing connection"}
	}
	if _, ok := r.Env.Prefixes()[ref]; !ok {
		return "", &ProviderError{Message: "signing connection " + ref, Err: runtime.ErrNoSuchConnection}
	}
	dir, err := r.Env.Required("HQ_CONTROLLER", "SSH_DIR")
	if err != nil {
		return "", err
	}
	name := ref + ".key"
	if public {
		name += ".pub"
	}
	return filepath.Join(dir, name), nil
}

func base64URL(data []byte) string { return base64.RawURLEncoding.EncodeToString(data) }

// jwt is a short-lived JWT naming the app, signed by openssl with the key the
// connection's name points at; this process sees only the signature.
func (r *Registry) githubJWT(ctx context.Context, c githubConnection) (string, error) {
	key, err := r.signingKey(c.ref, false)
	if err != nil {
		return "", err
	}
	now := r.Now()
	header, _ := json.Marshal(map[string]string{"alg": "RS256", "typ": "JWT"})
	claims, _ := json.Marshal(struct {
		Expires  int64  `json:"exp"`
		IssuedAt int64  `json:"iat"`
		Issuer   string `json:"iss"`
	}{now.Add(githubJWTLifetime).Unix(), now.Add(-githubJWTBackdate).Unix(), c.appID})
	input := base64URL(header) + "." + base64URL(claims)
	signature, err := r.commands().Run(ctx, []string{"openssl", "dgst", "-sha256", "-sign", key}, []byte(input), "sign for "+c.ref, c.ref, nil)
	if err != nil {
		return "", err
	}
	return input + "." + base64URL(signature), nil
}

func githubHeaders(token string) map[string]string {
	return map[string]string{
		"Accept":               "application/vnd.github+json",
		"Authorization":        "Bearer " + token,
		"X-GitHub-Api-Version": githubAPIVersion,
	}
}

// githubRequest is one request to GitHub's API under a bearer credential.
func (r *Registry) githubRequest(ctx context.Context, method, path, bearer string, payload any) (json.RawMessage, error) {
	return r.HTTP.Request(ctx, githubAPI+path, method, githubHeaders(bearer), payload)
}

// githubAsApp is one request authenticated as the app itself.
func (r *Registry) githubAsApp(ctx context.Context, c githubConnection, method, path string, payload any) (json.RawMessage, error) {
	jwt, err := r.githubJWT(ctx, c)
	if err != nil {
		return nil, err
	}
	return r.githubRequest(ctx, method, path, jwt, payload)
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
	raw, err := r.cached(ctx, installationKey(c, owner, repo), func() (json.RawMessage, error) {
		raw, err := r.githubAsApp(ctx, c, "GET", "/repos/"+url.PathEscape(owner)+"/"+url.PathEscape(repo)+"/installation", nil)
		if err != nil {
			return nil, err
		}
		installation, err := decodeAnswer[githubapi.Installation](raw, "GitHub installation")
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

// githubMint is an installation token for a request body naming its scope.
func (r *Registry) githubMint(ctx context.Context, c githubConnection, installation int, body githubapi.AppsCreateInstallationAccessTokenJSONBody) (string, error) {
	raw, err := r.githubAsApp(ctx, c, "POST", "/app/installations/"+strconv.Itoa(installation)+"/access_tokens", body)
	if err != nil {
		return "", err
	}
	token, err := decodeAnswer[githubapi.InstallationToken](raw, "GitHub installation token")
	if err != nil {
		return "", err
	}
	if token.Token == "" {
		return "", &ProviderError{Message: "GitHub did not issue an installation token"}
	}
	return token.Token, nil
}

// githubToken is a token for exactly these repositories and permissions,
// minted once per sweep for each distinct scope. Every repository shares one
// owner: a token spanning two installations is two accounts' authority.
func (r *Registry) githubToken(ctx context.Context, c githubConnection, repositories []string, permissions githubapi.AppPermissions) (string, error) {
	names := slices.Compact(slices.Sorted(slices.Values(repositories)))
	grant, _ := json.Marshal(permissions)
	if len(names) == 0 || string(grant) == "{}" {
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
	raw, err := r.cached(ctx, key, func() (json.RawMessage, error) {
		installation, err := r.githubInstallation(ctx, c, owner, repos[0])
		if err != nil {
			return nil, err
		}
		token, err := r.githubMint(ctx, c, installation, githubapi.AppsCreateInstallationAccessTokenJSONBody{
			Repositories: repos, Permissions: permissions,
		})
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

// githubCall is one API call under a token scoped to exactly its repositories
// and permissions.
func (r *Registry) githubCall(ctx context.Context, c githubConnection, method, path string, repositories []string, permissions githubapi.AppPermissions, payload any) (json.RawMessage, error) {
	token, err := r.githubToken(ctx, c, repositories, permissions)
	if err != nil {
		return nil, err
	}
	return r.githubRequest(ctx, method, path, token, payload)
}

// githubGet decodes one scoped GET into its GitHub type.
func githubGet[T any](ctx context.Context, r *Registry, c githubConnection, path string, repositories []string, permissions githubapi.AppPermissions) (T, error) {
	raw, err := r.githubCall(ctx, c, "GET", path, repositories, permissions, nil)
	if err != nil {
		var zero T
		return zero, err
	}
	route, _, _ := strings.Cut(path, "?")
	return decodeAnswer[T](raw, "GitHub "+route)
}

// githubInstallationRepositories is every repository the app's installations
// cover, as owner/name: choosing repositories on GitHub decides what HQ reads.
// Each installation is listed under a token that can read metadata only, and
// the installation found for each repository is kept for the sweep.
func (r *Registry) githubInstallationRepositories(ctx context.Context, c githubConnection) ([]string, error) {
	raw, err := r.cached(ctx, "github_app.repositories|"+c.ref, func() (json.RawMessage, error) {
		answer, err := r.githubAsApp(ctx, c, "GET", "/app/installations", nil)
		if err != nil {
			return nil, err
		}
		installations, err := decodeAnswer[[]githubapi.Installation](answer, "GitHub installations")
		if err != nil {
			return nil, err
		}
		found := []string{}
		for _, installation := range installations {
			if installation.ID == 0 {
				continue
			}
			token, err := r.githubMint(ctx, c, installation.ID, githubapi.AppsCreateInstallationAccessTokenJSONBody{
				Permissions: githubapi.AppPermissions{Metadata: githubapi.AppPermissionsMetadataRead},
			})
			if err != nil {
				return nil, err
			}
			listed, err := r.githubRequest(ctx, "GET", "/installation/repositories?per_page=100", token, nil)
			if err != nil {
				return nil, err
			}
			page, err := decodeAnswer[struct {
				Repositories []githubapi.Repository `json:"repositories"`
			}](listed, "GitHub installation repositories")
			if err != nil {
				return nil, err
			}
			for _, repository := range page.Repositories {
				owner, repo, err := githubRepositoryName(repository.FullName)
				if err != nil {
					continue
				}
				found = append(found, repository.FullName)
				id := installation.ID
				_, _ = r.cached(ctx, installationKey(c, owner, repo), func() (json.RawMessage, error) { return json.Marshal(id) })
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

// githubProbe is whether the key still signs for the app, and which accounts
// it is installed on.
func (r *Registry) githubProbe(ctx context.Context, ref string) (ProbeResult, error) {
	c, err := r.githubConnection(ref)
	if err != nil {
		return ProbeResult{}, err
	}
	raw, err := r.githubAsApp(ctx, c, "GET", "/app", nil)
	if err != nil {
		return ProbeResult{}, err
	}
	app, err := decodeAnswer[githubapi.Integration](raw, "GitHub App")
	if err != nil {
		return ProbeResult{}, err
	}
	if app.Slug == "" {
		return ProbeResult{}, &ProviderError{Message: "GitHub did not accept the app's signature", Failure: runtime.FailureClassCredential}
	}
	raw, err = r.githubAsApp(ctx, c, "GET", "/app/installations", nil)
	if err != nil {
		return ProbeResult{}, err
	}
	installations, err := decodeAnswer[[]githubapi.Installation](raw, "GitHub installations")
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
		return ProbeResult{}, &ProviderError{Message: "no signing key was rendered for " + c.ref, Err: err}
	}
	fingerprint, err := sshKeyFingerprint(string(public))
	if err != nil {
		return ProbeResult{}, err
	}
	return ProbeResult{Detail: "GitHub App " + app.Slug + ", key " + fingerprint, Reaches: accounts}, nil
}
