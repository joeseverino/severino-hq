package providers

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"path"
	"regexp"
	"slices"
	"strings"

	"github.com/compose-spec/compose-go/v2/cli"
	"github.com/compose-spec/compose-go/v2/loader"
	"github.com/compose-spec/compose-go/v2/template"
	"github.com/compose-spec/compose-go/v2/types"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/providers/githubapi"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// What a repository a project opts in to updates holds of its apps: each
// app's Compose files on the default branch, the Dependency Dashboard
// Renovate keeps open, and for each update's pull request every service's
// image before and after, as Docker Compose itself loads the app's files.

const (
	// githubApps is the directory each app's Compose files sit under, one
	// directory per app.
	githubApps = "compose"
	// githubDashboard is the title Renovate gives the issue that lists every
	// update it holds back or has not opened yet.
	githubDashboard = "Dependency Dashboard"
)

var githubComposeKept = api.MustLimit("GitHubRepositoryRecord", "properties", "compose", "maxItems")

// GitHubAppChange is one app an update's pull request changes the Compose
// files of. Read is whether its files at base and head both loaded and each
// image resolved without the host's environment; an app read false moves
// nothing HQ can say.
type GitHubAppChange struct {
	App    string            `json:"app"`
	Read   bool              `json:"read"`
	Images []GitHubImageMove `json:"images"`
}

// GitHubImageMove is one service whose image the pull request changes:
// Before is "" for a service it adds.
type GitHubImageMove struct {
	Service string `json:"service"`
	Before  string `json:"before"`
	After   string `json:"after"`
}

// optedIn is whether a project opts the repository in to dependency updates,
// as HQ's update plan names it.
func (g *githubRepository) optedIn() bool {
	return slices.ContainsFunc(g.r.Updates.Owners, func(owner runtime.DependencyUpdatesOwner) bool {
		return slices.ContainsFunc(owner.Repositories, func(name string) bool { return strings.EqualFold(name, g.name) })
	})
}

// GitHubComposeImage is one service's image in an app's Compose files at the
// default branch's head: what a merge pins. Path is the app's first file.
type GitHubComposeImage struct {
	Path    string `json:"path"`
	Service string `json:"service"`
	Image   string `json:"image"`
}

// GitHubHeldBranch is a branch the Dependency Dashboard lists with a box to
// tick and no pull request: Marker is the box's word, and Apps each app whose
// image its line names.
type GitHubHeldBranch struct {
	Branch string   `json:"branch"`
	Marker string   `json:"marker"`
	Apps   []string `json:"apps"`
}

// githubHeldLine is a Dashboard line whose box, ticked, has Renovate open the
// branch's pull request: the four kinds it lists with none open, as
// dependency-dashboard.ts writes them (getCheckbox, getListItem).
var githubHeldLine = regexp.MustCompile(`(?m)^ - \[ \] <!-- (unpend|approve|unschedule|unlimit)-branch=(\S+) -->(.*)$`)

// githubLayout is the apps part: the Compose files, the images they pin and
// the Dashboard with the branches it holds.
type githubLayout struct {
	compose   []string
	images    []GitHubComposeImage
	dashboard string
	held      []GitHubHeldBranch
}

func (g *githubRepository) apps(ctx context.Context, branch, head string) (githubLayout, error) {
	compose, err := g.composeFiles(ctx, branch)
	if err != nil {
		return githubLayout{}, err
	}
	files := map[string][]string{}
	for _, file := range compose {
		app, _ := composeApp(file)
		files[app] = append(files[app], file)
	}
	layout := githubLayout{compose: compose, images: []GitHubComposeImage{}, held: []GitHubHeldBranch{}}
	named := map[string][]string{}
	for _, app := range sortedKeys(files) {
		// An app that does not load pins nothing HQ can say.
		images, err := g.composeImages(ctx, app, files[app], head)
		if err != nil {
			continue
		}
		for _, service := range sortedKeys(images) {
			layout.images = append(layout.images, GitHubComposeImage{Path: files[app][0], Service: service, Image: images[service]})
			named[app] = append(named[app], imageName(images[service]))
		}
	}
	var body string
	layout.dashboard, body, err = g.dashboard(ctx)
	for _, line := range githubHeldLine.FindAllStringSubmatch(body, -1) {
		held := GitHubHeldBranch{Branch: line[2], Marker: line[1], Apps: []string{}}
		for _, app := range sortedKeys(named) {
			if slices.ContainsFunc(named[app], func(name string) bool { return namesImage(line[3], name) }) {
				held.Apps = append(held.Apps, app)
			}
		}
		layout.held = append(layout.held, held)
	}
	return layout, err
}

// imageName is an image reference without its tag or digest, as Renovate
// names the package in a Dashboard line.
func imageName(image string) string {
	image, _, _ = strings.Cut(image, "@")
	if colon := strings.LastIndex(image, ":"); colon > strings.LastIndex(image, "/") {
		image = image[:colon]
	}
	return image
}

// namesImage is whether a Dashboard line names an image whole, never as part
// of a longer name.
func namesImage(line, name string) bool {
	return regexp.MustCompile("(^|[\\s`(])" + regexp.QuoteMeta(name) + "($|[\\s`),])").MatchString(line)
}

// composeApp is the app a path's Compose file belongs to: a file directly in
// compose/<app>/ under a name Docker Compose finds in a directory.
func composeApp(file string) (string, bool) {
	dir, name := path.Split(file)
	parent, app := path.Split(strings.TrimSuffix(dir, "/"))
	found := parent == githubApps+"/" && app != "" &&
		(slices.Contains(cli.DefaultFileNames, name) || slices.Contains(cli.DefaultOverrideFileNames, name))
	return app, found
}

// composeFiles is every app's Compose files on a branch, sorted: the app
// directory's tree alone, so a large repository costs two small answers.
func (g *githubRepository) composeFiles(ctx context.Context, branch string) ([]string, error) {
	root, err := json200(g.api.GitGetTreeWithResponse(ctx, g.owner, g.repo, branch, nil))
	if err != nil {
		return nil, err
	}
	found, dir := []string{}, ""
	for _, entry := range root.Tree {
		if entry.Path == githubApps && entry.Type == "tree" {
			dir = entry.Sha
		}
	}
	if dir == "" {
		return found, nil
	}
	apps, err := json200(g.api.GitGetTreeWithResponse(ctx, g.owner, g.repo, dir, &githubapi.GitGetTreeParams{Recursive: new("1")}))
	if err != nil {
		return nil, err
	}
	for _, entry := range apps.Tree {
		file := githubApps + "/" + entry.Path
		if _, ok := composeApp(file); ok && entry.Type == "blob" {
			found = append(found, file)
		}
	}
	slices.Sort(found)
	return found[:min(len(found), githubComposeKept)], nil
}

// dashboard is the address and body of the open Dependency Dashboard, or "":
// Renovate edits it every run, so it is among the issues updated last.
func (g *githubRepository) dashboard(ctx context.Context) (string, string, error) {
	issues, err := json200(g.api.IssuesListForRepoWithResponse(ctx, g.owner, g.repo, &githubapi.IssuesListForRepoParams{
		State: new(githubapi.IssuesListForRepoParamsStateOpen), Sort: new(githubapi.IssuesListForRepoParamsSortUpdated), PerPage: new(githubListPage),
	}))
	for _, issue := range issues {
		if issue.PullRequest.URL == "" && issue.Title == githubDashboard {
			return issue.HTMLURL, issue.Body, err
		}
	}
	return "", "", err
}

// appChanges is each app whose Compose files an update's pull request
// changes, with every service whose image differs between its base and head.
// layout is the default branch's Compose files, which name the files an app
// keeps unchanged.
func (g *githubRepository) appChanges(ctx context.Context, pull GitHubPull, layout []string) []GitHubAppChange {
	files := map[string][]string{}
	for _, file := range pull.Files {
		if app, ok := composeApp(file.Path); ok {
			files[app] = append(files[app], file.Path)
		}
	}
	for _, file := range layout {
		if app, ok := composeApp(file); ok && files[app] != nil && !slices.Contains(files[app], file) {
			files[app] = append(files[app], file)
		}
	}
	found := []GitHubAppChange{}
	for _, app := range sortedKeys(files) {
		before, errBefore := g.composeImages(ctx, app, files[app], pull.Base)
		after, errAfter := g.composeImages(ctx, app, files[app], pull.Head)
		change := GitHubAppChange{App: app, Read: errBefore == nil && errAfter == nil, Images: []GitHubImageMove{}}
		for _, service := range sortedKeys(after) {
			if change.Read && after[service] != "" && after[service] != before[service] {
				change.Images = append(change.Images, GitHubImageMove{Service: service, Before: before[service], After: after[service]})
			}
		}
		found = append(found, change)
	}
	return found
}

// composeImages is each service's image in an app's Compose files at a
// commit, loaded as Docker Compose loads a directory: its first file of the
// default names, then its first override. A file absent at the commit is
// left out, and an app with none there has no services.
func (g *githubRepository) composeImages(ctx context.Context, app string, files []string, ref string) (map[string]string, error) {
	present := map[string]types.ConfigFile{}
	for _, file := range files {
		answer, err := json200(g.api.ReposGetContentWithResponse(ctx, g.owner, g.repo, file, &githubapi.ReposGetContentParams{Ref: &ref}))
		if answered, _ := errors.AsType[*ProviderError](err); answered != nil && answered.HTTPStatus == http.StatusNotFound {
			continue
		}
		if err != nil {
			return nil, err
		}
		content, err := base64.StdEncoding.DecodeString(strings.ReplaceAll(answer.Content, "\n", ""))
		if err != nil || answer.Encoding != "base64" {
			return nil, errors.Join(errors.New(file+" is not a file GitHub sent whole"), err)
		}
		// The name stays the same at every commit: the loader warns once per name.
		present[path.Base(file)] = types.ConfigFile{Filename: file, Content: content}
	}
	chosen := []types.ConfigFile{}
	for _, names := range [][]string{cli.DefaultFileNames, cli.DefaultOverrideFileNames} {
		if at := slices.IndexFunc(names, func(name string) bool { _, ok := present[name]; return ok }); at >= 0 {
			chosen = append(chosen, present[names[at]])
		}
	}
	images := map[string]string{}
	if len(chosen) == 0 {
		return images, nil
	}
	model, err := loader.LoadModelWithContext(ctx, types.ConfigDetails{WorkingDir: "/" + app, ConfigFiles: chosen, Environment: types.Mapping{}}, composeOptions(app))
	if err != nil {
		return nil, err
	}
	services, _ := model["services"].(map[string]any)
	for name, service := range services {
		declared, _ := service.(map[string]any)
		written, ok := declared["image"].(string)
		if !ok {
			continue
		}
		if images[name], err = template.SubstituteWithOptions(written, composeUnset, template.WithoutLogging, template.WithReplacementFunction(composeResolved)); err != nil {
			return nil, err
		}
	}
	return images, nil
}

// composeOptions loads a repository's files merged as Compose merges them,
// and nothing beyond them: no include, extends or env_file reaches the
// controller's disk. Nothing is interpolated in the load, so only an image
// is, by composeResolved; every service counts, whatever its profile.
func composeOptions(app string) func(*loader.Options) {
	return func(o *loader.Options) {
		o.SetProjectName(loader.NormalizeProjectName(app), true)
		o.SkipInclude, o.SkipExtends, o.SkipInterpolation, o.SkipNormalization = true, true, true, true
		o.SkipConsistencyCheck, o.SkipDefaultValues, o.ResolvePaths = true, true, false
	}
}

// composeUnset is the environment an image is read in: none, since the
// variables a host sets are not in the repository.
func composeUnset(string) (string, bool) { return "", false }

// composeResolved interpolates one variable of an image as Compose does, and
// refuses one only the host's environment could fill: an image with a blank
// in its place would be a guess.
func composeResolved(substring string, mapping template.Mapping, cfg *template.Config) (string, error) {
	value, applied, err := template.DefaultReplacementAppliedFunc(substring, mapping, cfg)
	if err == nil && !applied {
		err = errors.New(substring + " is set only where the app runs")
	}
	return value, err
}
