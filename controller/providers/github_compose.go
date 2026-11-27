package providers

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"path"
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
// app's Compose files on the default branch, and for each update's pull
// request every service's image before and after, as Docker Compose itself
// loads the app's files.

// githubApps is the directory each app's Compose files sit under, one
// directory per app.
const githubApps = "compose"

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
// Build is whether the service builds its image on its host, where Image,
// when written, is the name the build tags and no registry holds.
type GitHubComposeImage struct {
	Path    string `json:"path"`
	Service string `json:"service"`
	Image   string `json:"image"`
	Build   bool   `json:"build"`
}

// GitHubBuild is an app with a service that builds its image on its host,
// and each commit from the newest that changed its directory to the head,
// newest first: a host that applied one of them builds what a merge does.
type GitHubBuild struct {
	App     string   `json:"app"`
	Commits []string `json:"commits"`
}

// githubLayout is the apps part: the Compose files, the images they pin and
// the apps built on their host.
type githubLayout struct {
	compose []string
	images  []GitHubComposeImage
	builds  []GitHubBuild
	updater string
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
	layout := githubLayout{compose: compose, images: []GitHubComposeImage{}}
	built := []string{}
	for _, app := range sortedKeys(files) {
		// An app that does not load pins nothing HQ can say.
		services, err := g.composedServices(ctx, app, files[app], head)
		if err != nil {
			continue
		}
		for _, name := range sortedKeys(services) {
			service := services[name]
			layout.images = append(layout.images, GitHubComposeImage{Path: files[app][0], Service: name, Image: service.image, Build: service.build})
			if service.build && !slices.Contains(built, app) {
				built = append(built, app)
			}
		}
	}
	if layout.builds, err = g.builds(ctx, head, built); err != nil {
		return githubLayout{}, err
	}
	layout.updater, err = g.updater()
	return layout, err
}

// builds is each app of built with the commits from the newest that changed
// its directory to head: the head's history, one page of it, cut at that
// commit. The default branch takes squash merges alone, so its history is one
// line and those commits are exactly the revisions that build what the head
// builds. An app whose last change is past that page is left out, since HQ
// could not say which revisions build it.
func (g *githubRepository) builds(ctx context.Context, head string, built []string) ([]GitHubBuild, error) {
	found := []GitHubBuild{}
	if len(built) == 0 {
		return found, nil
	}
	history, err := json200(g.api.ReposListCommitsWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListCommitsParams{Sha: &head, PerPage: new(githubListPage)}))
	if err != nil {
		return nil, err
	}
	for _, app := range built {
		last, err := json200(g.api.ReposListCommitsWithResponse(ctx, g.owner, g.repo, &githubapi.ReposListCommitsParams{Sha: &head, Path: new(githubApps + "/" + app), PerPage: new(1)}))
		if err != nil {
			return nil, err
		}
		at := slices.IndexFunc(history, func(commit githubapi.Commit) bool { return len(last) > 0 && commit.Sha == last[0].Sha })
		if at < 0 {
			continue
		}
		build := GitHubBuild{App: app, Commits: []string{}}
		for _, commit := range history[:at+1] {
			build.Commits = append(build.Commits, commit.Sha)
		}
		found = append(found, build)
	}
	return found, nil
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

// appDirectory is the app whose directory, compose/<app>/, holds a path at
// any depth, or "" and false for a path outside every app's.
func appDirectory(file string) (string, bool) {
	app, rest, nested := strings.Cut(strings.TrimPrefix(file, githubApps+"/"), "/")
	if !strings.HasPrefix(file, githubApps+"/") || !nested || app == "" || rest == "" {
		return "", false
	}
	return app, true
}

// appChanges is each app whose directory an update's pull request changes,
// with every service whose image differs between its base and head: a change
// to an app's build inputs alone (its Dockerfile, its lock file) moves no
// image and is still that app's change. layout is the default branch's
// Compose files, which name the files an app keeps unchanged.
func (g *githubRepository) appChanges(ctx context.Context, pull GitHubOpenPullRequestRecord, layout []string) []GitHubAppChange {
	files := map[string][]string{}
	for _, file := range pull.Files {
		if app, ok := appDirectory(file.Path); ok && files[app] == nil {
			files[app] = []string{}
		}
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
		before, errBefore := g.composedServices(ctx, app, files[app], pull.Base)
		after, errAfter := g.composedServices(ctx, app, files[app], pull.Head)
		change := GitHubAppChange{App: app, Read: errBefore == nil && errAfter == nil, Images: []GitHubImageMove{}}
		for _, service := range sortedKeys(after) {
			if change.Read && after[service].image != "" && after[service].image != before[service].image {
				change.Images = append(change.Images, GitHubImageMove{Service: service, Before: before[service].image, After: after[service].image})
			}
		}
		found = append(found, change)
	}
	return found
}

// composedService is one service as an app's Compose files declare it: the
// image it names, "" for none, and whether it builds that image.
type composedService struct {
	image string
	build bool
}

// composedServices is each service that names an image or builds one in an
// app's Compose files at a commit, loaded as Docker Compose loads a
// directory: its first file of the default names, then its first override. A file absent at the commit is
// left out, and an app with none there has no services.
func (g *githubRepository) composedServices(ctx context.Context, app string, files []string, ref string) (map[string]composedService, error) {
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
	found := map[string]composedService{}
	if len(chosen) == 0 {
		return found, nil
	}
	model, err := loader.LoadModelWithContext(ctx, types.ConfigDetails{WorkingDir: "/" + app, ConfigFiles: chosen, Environment: types.Mapping{}}, composeOptions(app))
	if err != nil {
		return nil, err
	}
	services, _ := model["services"].(map[string]any)
	for name, service := range services {
		declared, _ := service.(map[string]any)
		written, named := declared["image"].(string)
		_, build := declared["build"]
		if !named && !build {
			continue
		}
		image := ""
		if named {
			if image, err = template.SubstituteWithOptions(written, composeUnset, template.WithoutLogging, template.WithReplacementFunction(composeResolved)); err != nil {
				return nil, err
			}
		}
		found[name] = composedService{image: image, build: build}
	}
	return found, nil
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
