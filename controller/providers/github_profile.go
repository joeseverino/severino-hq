package providers

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// A person's public GitHub profile and the repositories they star. Whose
// profile is HQ's to say: the accounts in the registry's plan are the ones a
// sign-in names.
//
// Everything read is public, so the read needs no authority. Where a GitHub
// App is connected its calls are made under a token that can read one covered
// repository's metadata and nothing else, so they count against the
// installation's allowance. Where none is, they are anonymous, and GitHub
// allows an address 60 of those an hour. One account costs the profile, the
// stars and the picture, then a release and an advisory call for each watched
// repository, and the contract states how many are watched. So the kind keeps
// a slower clock than the sweep, which HQ holds, and a read that the hour's
// allowance cannot cover is refused before its first call.

const (
	// githubProfileConcurrency is how many watched repositories are asked about at once.
	githubProfileConcurrency = 8
	// githubStarsPage is how many stars are counted; the newest are watched.
	githubStarsPage = 100
	// githubAdvisoriesPage is how many published advisories a repository lists.
	githubAdvisoriesPage = 5
	// githubAvatarHost is the only host a picture is fetched from.
	githubAvatarHost = "avatars.githubusercontent.com"
	// githubAvatarSize is the picture's width in pixels.
	githubAvatarSize = 96
	// githubStarJSON lists stars with the moment each repository was starred.
	githubStarJSON = "application/vnd.github.star+json"
	// githubUserAgent is the contact every keyless read sends.
	githubUserAgent = nwsUserAgent
)

var (
	githubWatchedLimit = api.MustLimit("GitHubProfileRecord", "properties", "watched", "maxItems")
	githubAvatarLimit  = api.MustLimit("GitHubProfileRecord", "properties", "avatar", "maxLength")
	githubLogin        = api.MustPattern("GitHubProfilePlan", "properties", "accounts", "items")
)

// errCarried is a reader's answer when its kind was not due: nothing was asked
// of the provider, and HQ keeps what it holds.
var errCarried = errors.New("not due; the last reading stands")

// githubProfileCost is the calls one account's read makes at most.
func githubProfileCost() int { return 3 + 2*githubWatchedLimit }

// GitHubProfileRecord is one account as GitHub shows it to anyone.
type GitHubProfileRecord struct {
	Login       string          `json:"login"`
	Name        string          `json:"name"`
	Bio         string          `json:"bio"`
	URL         string          `json:"url"`
	Followers   int             `json:"followers"`
	Following   int             `json:"following"`
	PublicRepos int             `json:"public_repos"`
	CreatedAt   string          `json:"created_at"`
	Company     string          `json:"company"`
	Location    string          `json:"location"`
	Website     string          `json:"website"`
	Social      string          `json:"social"`
	PublicGists int             `json:"public_gists"`
	Hireable    bool            `json:"hireable"`
	Avatar      string          `json:"avatar"`
	Starred     int             `json:"starred"`
	Watched     []GitHubWatched `json:"watched"`
}

// GitHubWatched is one starred repository with its latest release and advisories.
type GitHubWatched struct {
	Name        string           `json:"name"`
	URL         string           `json:"url"`
	Description string           `json:"description"`
	Language    string           `json:"language"`
	Stars       int              `json:"stars"`
	StarredAt   string           `json:"starred_at"`
	Release     *GitHubRelease   `json:"release"`
	Advisories  []GitHubAdvisory `json:"advisories"`
}

type GitHubAdvisory struct {
	ID          string `json:"id"`
	Severity    string `json:"severity"`
	Summary     string `json:"summary"`
	URL         string `json:"url"`
	PublishedAt string `json:"published_at"`
}

func (r *Registry) admitGitHubProfile() {
	r.reader(runtime.ResourceKindGitHubProfile, r.githubProfiles)
	// Read with or without a connection: the accounts HQ names are the source.
	r.readsOnlyHeld(runtime.ResourceKindGitHubProfile, func() bool { return len(r.Profiles.Accounts) > 0 })
}

// githubPublicBearer is the token public reads are made under, or "" where no
// GitHub App is connected. It grants metadata of one covered repository: the
// least a token can hold, since it is there for the allowance and what is
// read is public. An app that is connected and cannot mint fails the read.
func (r *Registry) githubPublicBearer(ctx context.Context) (string, error) {
	// No connection of this provider is no app; one that is named and
	// incomplete is an error, as it is to every other GitHub read.
	if len(r.Supplied.Refs(runtime.ConnectionProviderGitHubApp)) == 0 {
		return "", nil
	}
	c, err := r.githubConnection("")
	if err != nil {
		return "", err
	}
	covered, err := r.githubInstallationRepositories(ctx, c)
	if err != nil || len(covered) == 0 {
		return "", err
	}
	return r.githubToken(ctx, c, covered[:1], githubapi.AppPermissions{Metadata: githubapi.AppPermissionsMetadataRead})
}

func githubPublicHeaders(accept, bearer string) map[string]string {
	headers := map[string]string{"Accept": accept, "X-GitHub-Api-Version": githubAPIVersion, "User-Agent": githubUserAgent}
	if bearer != "" {
		headers["Authorization"] = "Bearer " + bearer
	}
	return headers
}

// githubPublic is one GET of public data from GitHub's API, decoded into T. A
// missing resource is the zero value and ok false.
func githubPublic[T any](ctx context.Context, r *Registry, bearer, path, accept, what string) (T, bool, error) {
	raw, err := r.HTTP.Request(ctx, githubAPI+path, "GET", githubPublicHeaders(accept, bearer), nil)
	if httpStatus(err) == http.StatusNotFound {
		var zero T
		return zero, false, nil
	}
	if err != nil {
		var zero T
		return zero, false, fmt.Errorf("%s: %w", what, err)
	}
	found, err := decodeAnswer[T](raw, what)
	return found, err == nil, err
}

// githubProfiles reads every account the plan names, when the plan says the
// reading is due and the hour's allowance covers all of it.
func (r *Registry) githubProfiles(ctx context.Context) ([]any, error) {
	plan := r.Profiles
	if !plan.Due || len(plan.Accounts) == 0 {
		return nil, errCarried
	}
	for _, login := range plan.Accounts {
		if !githubLogin.MatchString(login) {
			return nil, &ProviderError{Message: fmt.Sprintf("%q is not a GitHub login", login)}
		}
	}
	bearer, err := r.githubPublicBearer(ctx)
	if err != nil {
		return nil, err
	}
	limit, _, err := githubPublic[githubapi.RateLimitOverview](ctx, r, bearer, "/rate_limit", "application/vnd.github+json", "GitHub rate limit")
	if err != nil {
		return nil, err
	}
	if need, left := githubProfileCost()*len(plan.Accounts), limit.Resources.Core.Remaining; left < need {
		resets := time.Unix(int64(limit.Resources.Core.Reset), 0).UTC().Format("15:04 UTC")
		whom := "this address %d more anonymous calls"
		if bearer != "" {
			whom = "the app %d more calls"
		}
		refuse(ctx, runtime.PartWhole, "", "", &ProviderError{Message: fmt.Sprintf(
			"GitHub allows "+whom+" until %s and the read needs %d; the last reading stands", left, resets, need)})
		return nil, errCarried
	}
	records := make([]any, len(plan.Accounts))
	for i, login := range plan.Accounts {
		record, err := r.githubProfile(ctx, bearer, login)
		if err != nil {
			return nil, err
		}
		records[i] = record
	}
	return records, nil
}

func (r *Registry) githubProfile(ctx context.Context, bearer, login string) (GitHubProfileRecord, error) {
	const accept = "application/vnd.github+json"
	account := "/users/" + url.PathEscape(login)
	user, ok, err := githubPublic[githubapi.PublicUser](ctx, r, bearer, account, accept, "GitHub profile")
	if err != nil {
		return GitHubProfileRecord{}, err
	}
	if !ok {
		return GitHubProfileRecord{}, &ProviderError{Message: "GitHub has no account " + login}
	}
	stars, _, err := githubPublic[[]githubapi.StarredRepository](ctx, r, bearer, account+"/starred?per_page="+strconv.Itoa(githubStarsPage), githubStarJSON, "GitHub stars")
	if err != nil {
		return GitHubProfileRecord{}, err
	}
	newest := stars[:min(len(stars), githubWatchedLimit)]
	watched := make([]GitHubWatched, len(newest))
	// No repository's calls depend on another's, so the read waits for the
	// slowest answer and not for the sum of them.
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(githubProfileConcurrency)
	for i, star := range newest {
		group.Go(func() error {
			found, err := r.githubWatched(groupCtx, bearer, star)
			watched[i] = found
			return err
		})
	}
	if err := group.Wait(); err != nil {
		return GitHubProfileRecord{}, err
	}
	avatar, err := r.githubAvatar(ctx, user.AvatarURL)
	if err != nil {
		refuse(ctx, runtime.PartAvatar, "", login, err)
	}
	return GitHubProfileRecord{
		Login: orDefault(user.Login, login), Name: user.Name, Bio: user.Bio, URL: user.HTMLURL,
		Followers: user.Followers, Following: user.Following, PublicRepos: user.PublicRepos,
		CreatedAt: user.CreatedAt, Company: user.Company, Location: user.Location, Website: user.Blog,
		Social: user.TwitterUsername, PublicGists: user.PublicGists, Hireable: user.Hireable,
		Avatar: avatar, Starred: len(stars), Watched: watched,
	}, nil
}

func (r *Registry) githubWatched(ctx context.Context, bearer string, star githubapi.StarredRepository) (GitHubWatched, error) {
	const accept = "application/vnd.github+json"
	name := star.Repo.FullName
	watched := GitHubWatched{
		Name: name, URL: star.Repo.HTMLURL, Description: star.Repo.Description, Language: star.Repo.Language,
		Stars: star.Repo.StargazersCount, StarredAt: star.StarredAt, Advisories: []GitHubAdvisory{},
	}
	if _, _, err := githubRepositoryName(name); err != nil {
		return watched, err
	}
	release, ok, err := githubPublic[githubapi.Release](ctx, r, bearer, "/repos/"+name+"/releases/latest", accept, "latest release of "+name)
	if err != nil {
		return watched, err
	}
	if ok && release.TagName != "" {
		watched.Release = &GitHubRelease{Tag: release.TagName, URL: release.HTMLURL, PublishedAt: release.PublishedAt}
	}
	advisories, _, err := githubPublic[[]githubapi.RepositoryAdvisory](ctx, r, bearer,
		"/repos/"+name+"/security-advisories?state=published&sort=published&direction=desc&per_page="+strconv.Itoa(githubAdvisoriesPage),
		accept, "advisories of "+name)
	if err != nil {
		return watched, err
	}
	for _, advisory := range advisories {
		watched.Advisories = append(watched.Advisories, GitHubAdvisory{
			ID: orDefault(advisory.CveID, advisory.GhsaID), Severity: string(advisory.Severity), Summary: advisory.Summary,
			URL: advisory.HTMLURL, PublishedAt: advisory.PublishedAt,
		})
	}
	return watched, nil
}

// githubAvatar is the account's picture as a data: URI, "" for an account
// that names none. Only GitHub's picture host is asked.
func (r *Registry) githubAvatar(ctx context.Context, address string) (string, error) {
	if address == "" {
		return "", nil
	}
	parsed, err := url.Parse(address)
	if err != nil || parsed.Scheme != "https" || parsed.Host != githubAvatarHost {
		return "", &ProviderError{Message: "the picture is not on GitHub's picture host", Failure: runtime.FailureClassAddress}
	}
	query := parsed.Query()
	query.Set("s", strconv.Itoa(githubAvatarSize))
	parsed.RawQuery = query.Encode()
	picture := r.Picture
	if picture == nil {
		picture = fetchPicture
	}
	// What base64 makes of the bytes must fit beside the prefix.
	kind, data, err := picture(ctx, parsed.String(), githubAvatarLimit/4*3-64)
	if err != nil {
		return "", err
	}
	return "data:" + kind + ";base64," + base64.StdEncoding.EncodeToString(data), nil
}

// fetchPicture GETs an image of at most limit bytes. What it is comes from its
// own bytes, never from the response's word for it, and no redirect is followed.
func fetchPicture(ctx context.Context, address string, limit int) (string, []byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return "", nil, &ProviderError{Message: "the picture's address is not valid", Failure: runtime.FailureClassAddress}
	}
	request.Header.Set("User-Agent", githubUserAgent)
	client := &http.Client{Timeout: runtime.DefaultRequestTimeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	response, err := client.Do(request)
	if err != nil {
		return "", nil, &ProviderError{Message: "the picture could not be fetched", Failure: runtime.FailureClassNetwork, Err: err}
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", nil, runtime.HTTPRefusal(response.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, int64(limit)+1))
	if err != nil {
		return "", nil, &ProviderError{Message: "the picture could not be read", Failure: runtime.FailureClassNetwork, Err: err}
	}
	if len(data) > limit {
		return "", nil, &ProviderError{Message: "the picture is larger than HQ keeps"}
	}
	kind := http.DetectContentType(data)
	if !strings.HasPrefix(kind, "image/") {
		return "", nil, &ProviderError{Message: "the picture is not an image"}
	}
	return kind, data, nil
}
