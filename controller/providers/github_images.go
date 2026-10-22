package providers

import (
	"context"
	"encoding/base64"
	"net/url"
	"slices"
	"strconv"
	"strings"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubRegistry is the container registry an app token reads packages from;
// GitHub's package API does not take an installation token. It is fixed here
// and never taken from an image reference: the app's token is sent to it.
const githubRegistry = "ghcr.io"

type GitHubImage struct {
	Name   string   `json:"name"`
	Tags   int      `json:"tags"`
	Signed []string `json:"signed"`
}

// registryToken and registryTags are the OCI distribution API's token and
// tag-list answers.
type registryToken struct {
	Token string `json:"token"`
}

type registryTags struct {
	Tags []string `json:"tags"`
}

// images is the container images this controller's own composition names for
// the repository: the host's image and the composition it runs, read from the
// registry, which takes the app's token where GitHub's package API does not.
// Each image is refused alone.
func (g *githubRepository) images(ctx context.Context) ([]GitHubImage, error) {
	composed := g.r.composition()
	found := []GitHubImage{}
	if composed.Repository != g.name {
		return found, nil
	}
	names := map[string]bool{strings.ToLower(g.name): true}
	if rest, ok := strings.CutPrefix(composed.Image, githubRegistry+"/"); ok {
		rest, _, _ = strings.Cut(rest, "@")
		rest, _, _ = strings.Cut(rest, ":")
		names[strings.ToLower(rest)] = true
	}
	token, err := g.r.githubToken(ctx, g.c, []string{g.name}, githubRead)
	if err != nil {
		return nil, err
	}
	for _, image := range sortedKeys(names) {
		scope := g.name + ":" + image
		read, err := g.image(ctx, image, token)
		if err != nil {
			refuse(ctx, runtime.PartImages, g.c.ref, scope, asRepositoryRefusal(err))
			continue
		}
		found = append(found, read)
	}
	return found, nil
}

// image is an image's tags, and which digests carry a cosign signature, which
// the tag list alone says. Cosign 2 tags its signature sha256-<digest>.sig.
// Cosign 3 attaches a referrer, and on a registry without the referrers API
// (GHCR) the referrers index is tagged sha256-<digest>. Either is the
// signature's presence, not its verification, which the deploy does.
func (g *githubRepository) image(ctx context.Context, image, token string) (GitHubImage, error) {
	basic := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + token))
	query := url.Values{"scope": {"repository:" + image + ":pull"}, "service": {githubRegistry}}
	raw, err := g.r.HTTP.Request(ctx, "https://"+githubRegistry+"/token?"+query.Encode(), "GET", map[string]string{"Authorization": "Basic " + basic}, nil)
	if err != nil {
		return GitHubImage{}, err
	}
	pull, err := decodeAnswer[registryToken](raw, "registry token")
	if err != nil {
		return GitHubImage{}, err
	}
	raw, err = g.r.HTTP.Request(ctx, "https://"+githubRegistry+"/v2/"+image+"/tags/list?n="+strconv.Itoa(githubTagsPage), "GET", map[string]string{"Authorization": "Bearer " + pull.Token}, nil)
	if err != nil {
		return GitHubImage{}, err
	}
	listed, err := decodeAnswer[registryTags](raw, "registry tags")
	if err != nil {
		return GitHubImage{}, err
	}
	record := GitHubImage{Name: image, Signed: []string{}}
	for _, tag := range listed.Tags {
		if !strings.HasPrefix(tag, "sha256-") {
			record.Tags++
			continue
		}
		digest := strings.TrimPrefix(tag, "sha256-")
		if signature, ok := strings.CutSuffix(digest, ".sig"); ok {
			record.Signed = append(record.Signed, signature)
		} else if !strings.Contains(digest, ".") {
			record.Signed = append(record.Signed, digest)
		}
	}
	slices.Sort(record.Signed)
	record.Signed = slices.Compact(record.Signed)
	return record, nil
}
